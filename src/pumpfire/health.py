"""Stream liveness: a staleness gate, a lossy queue, and a health snapshot.

The WebSocket reconnects by nature, so a gap is a first-class state rather than
an error. Three small pieces make that explicit:

* :class:`StalenessGate` turns "when did we last see an event" into a decision a
  consumer can enforce with :meth:`StalenessGate.require_live`.
* :class:`EventQueue` bounds the reader-to-consumer handoff, drops the oldest
  event under pressure, and keeps the drop count. Its census is synchronous, so
  observing the queue never blocks the reader.
* :class:`StreamHealth` is the single snapshot a dashboard or a trading entry
  reads: last-event age, reconnect count, drop count, and the decoded/rejected
  ratio.

Nothing here touches the network, and the module has no dependencies outside the
standard library.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

T = TypeVar("T")

# Reasons a gate can report.
FRESH = "fresh"
STALE = "stale"
NO_EVENT = "no-event"


class StaleStream(RuntimeError):
    """Raised when live data is required but the last event is too old."""

    def __init__(self, decision: GateDecision) -> None:
        self.decision = decision
        super().__init__(
            f"stream is not live ({decision.reason}); "
            f"last event age {decision.age_s}s, threshold {decision.stale_after_s}s"
        )


@dataclass(frozen=True, slots=True)
class GateDecision:
    """The outcome of one gate check."""

    live: bool
    stale: bool
    age_s: float | None
    reason: str
    stale_after_s: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "live": self.live,
            "stale": self.stale,
            "age_s": self.age_s,
            "reason": self.reason,
            "stale_after_s": self.stale_after_s,
        }


class StalenessGate:
    """Decide whether the stream is fresh enough to trust.

    An event at or under the threshold is live; anything older, or no event at
    all, is stale and therefore untradeable.
    """

    __slots__ = ("stale_after_s", "last_event_ts")

    def __init__(self, stale_after_s: float) -> None:
        if stale_after_s <= 0:
            raise ValueError("stale_after_s must be positive")
        self.stale_after_s = float(stale_after_s)
        self.last_event_ts: float | None = None

    def observe(self, now: float | None = None) -> None:
        """Record an event (or a message that proves the socket is alive)."""
        self.last_event_ts = time.time() if now is None else now

    def age(self, now: float | None = None) -> float | None:
        if self.last_event_ts is None:
            return None
        now = time.time() if now is None else now
        return round(max(0.0, now - self.last_event_ts), 3)

    def is_stale(self, now: float | None = None) -> bool:
        age = self.age(now)
        return True if age is None else age > self.stale_after_s

    def is_live(self, now: float | None = None) -> bool:
        return not self.is_stale(now)

    def check(self, now: float | None = None) -> GateDecision:
        age = self.age(now)
        if age is None:
            return GateDecision(False, True, None, NO_EVENT, self.stale_after_s)
        stale = age > self.stale_after_s
        return GateDecision(not stale, stale, age, STALE if stale else FRESH, self.stale_after_s)

    def allows(self, now: float | None = None) -> bool:
        return self.check(now).live

    def require_live(self, now: float | None = None) -> GateDecision:
        """Return the decision, or raise :class:`StaleStream` when not live."""
        decision = self.check(now)
        if not decision.live:
            raise StaleStream(decision)
        return decision


@dataclass(frozen=True, slots=True)
class QueueCensus:
    """A synchronous snapshot of a lossy queue."""

    depth: int
    maxsize: int
    enqueued: int
    dropped: int

    def as_dict(self) -> dict[str, int]:
        return {
            "depth": self.depth,
            "maxsize": self.maxsize,
            "enqueued": self.enqueued,
            "dropped": self.dropped,
        }


class EventQueue(Generic[T]):
    """A bounded queue that drops the oldest item instead of blocking.

    ``put_nowait`` never waits: when the queue is full the oldest item is
    discarded first, so the reader keeps up with the socket at the cost of the
    stale end of the backlog. Every discard is counted.
    """

    __slots__ = ("_queue", "maxsize", "enqueued", "dropped")

    def __init__(self, maxsize: int) -> None:
        if maxsize < 1:
            raise ValueError("maxsize must be at least 1")
        self.maxsize = int(maxsize)
        self._queue: asyncio.Queue[T] = asyncio.Queue(maxsize=self.maxsize)
        self.enqueued = 0
        self.dropped = 0

    def put_nowait(self, item: T) -> bool:
        """Enqueue without waiting. Returns ``True`` when an older item was dropped."""
        dropped = False
        if self._queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
                self.dropped += 1
                dropped = True
        self._queue.put_nowait(item)
        self.enqueued += 1
        return dropped

    async def put(self, item: T) -> bool:
        """Async spelling of :meth:`put_nowait`; still never blocks."""
        return self.put_nowait(item)

    async def get(self) -> T:
        return await self._queue.get()

    def get_nowait(self) -> T:
        return self._queue.get_nowait()

    def empty(self) -> bool:
        return self._queue.empty()

    def qsize(self) -> int:
        return self._queue.qsize()

    def census(self) -> QueueCensus:
        """Read depth and counters without awaiting anything."""
        return QueueCensus(
            depth=self._queue.qsize(),
            maxsize=self.maxsize,
            enqueued=self.enqueued,
            dropped=self.dropped,
        )


@dataclass(slots=True)
class StreamHealth:
    """One consistent view of the stream for monitoring and entry gating."""

    connected: bool
    endpoint: str | None
    program: str
    programs: tuple[str, ...]
    commitment: str
    stale_after_s: float
    last_event_ts: float | None
    last_event_age_s: float | None
    stale: bool
    reconnects: int
    dropped: int
    decoded: int
    rejected: int
    queue: QueueCensus
    last_error: str | None = None
    unknown_events: int = 0
    program_data_lines: int = 0
    malformed: int = 0
    stats: dict[str, Any] = field(default_factory=dict)

    @property
    def live(self) -> bool:
        """Connected and fed by a recent event."""
        return self.connected and not self.stale

    @property
    def decoded_ratio(self) -> float | None:
        """Share of notifications that decoded, over decoded plus rejected."""
        total = self.decoded + self.rejected
        return round(self.decoded / total, 4) if total else None

    @property
    def rejected_ratio(self) -> float | None:
        """Share of notifications that were rejected, over decoded plus rejected."""
        total = self.decoded + self.rejected
        return round(self.rejected / total, 4) if total else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "live": self.live,
            "endpoint": self.endpoint,
            "program": self.program,
            "programs": list(self.programs),
            "commitment": self.commitment,
            "last_event_ts": self.last_event_ts,
            "last_event_age_s": self.last_event_age_s,
            "last_message_age_s": self.last_event_age_s,
            "stale": self.stale,
            "stale_after_s": self.stale_after_s,
            "reconnects": self.reconnects,
            "dropped": self.dropped,
            "decoded": self.decoded,
            "rejected": self.rejected,
            "decoded_ratio": self.decoded_ratio,
            "rejected_ratio": self.rejected_ratio,
            "queue_depth": self.queue.depth,
            "queue": self.queue.as_dict(),
            "last_error": self.last_error,
            "unknown_events": self.unknown_events,
            "program_data_lines": self.program_data_lines,
            "malformed": self.malformed,
            "stats": self.stats,
        }
