"""The pump.fun program firehose.

One ``logsSubscribe`` call that mentions the pump.fun program receives every
trade and every launch on the bonding curve. Most notifications carry the
Anchor event inline, so a trade is decoded from the notification itself: no
transaction fetch, no API key, no quota.

The reader and the user callback are separate tasks joined by a bounded queue,
so a slow callback drops the oldest event and increments a counter instead of
stalling the socket.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from .constants import (
    DEFAULT_STALE_AFTER_S,
    PUBLIC_WS,
    PUMP_AMM_PROGRAM,
    PUMP_PROGRAM,
)
from .decoder import decode_notification, notification_value
from .models import CoinBook, CompleteEvent, CreateEvent, DecodedBatch, RawEvent, TradeEvent

if TYPE_CHECKING:
    from .metadata import MetadataFetcher

LOGGER = logging.getLogger("pumpfire.firehose")

Event = TradeEvent | CreateEvent | CompleteEvent | RawEvent
EventCallback = Callable[[Event], Awaitable[None] | None]


class Firehose:
    """A supervised, reconnecting subscription to the pump.fun program logs."""

    def __init__(
        self,
        book: CoinBook,
        *,
        endpoints: list[str] | tuple[str, ...] | None = None,
        program: str | None = None,
        programs: list[str] | tuple[str, ...] | None = None,
        commitment: str = "processed",
        stale_after_s: float = DEFAULT_STALE_AFTER_S,
        dedupe_size: int = 100_000,
        max_queue: int = 20_000,
        metadata: MetadataFetcher | None = None,
        on_event: EventCallback | None = None,
        connect: Callable[..., Any] | None = None,
    ) -> None:
        self.book = book
        self.endpoints = list(endpoints or PUBLIC_WS)
        if programs is not None:
            self.programs = list(programs)
        elif program is not None:
            self.programs = [program]
        else:
            self.programs = [PUMP_PROGRAM, PUMP_AMM_PROGRAM]
        self.program = self.programs[0]
        self.commitment = commitment
        self.stale_after_s = stale_after_s
        self.dedupe_size = dedupe_size
        self.max_queue = max_queue
        self.metadata = metadata
        self._on_event = on_event
        self._connect = connect
        self._seen: OrderedDict[str, float] = OrderedDict()
        self._queue: asyncio.Queue[Event] | None = None
        self._tasks: list[asyncio.Task[Any]] = []
        self._stop = asyncio.Event()
        self.connected = False
        self.endpoint: str | None = None
        self.last_error: str | None = None
        self.unknown_events = 0
        self.program_data_lines = 0
        self.malformed = 0
        self.dropped = 0

    # ── lifecycle ───────────────────────────────────────────────────────
    async def __aenter__(self) -> Firehose:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    async def start(self) -> Firehose:
        if self.metadata is not None:
            await self.metadata.start()
        if self._on_event is not None:
            self._queue = asyncio.Queue(maxsize=self.max_queue)
            self._tasks.append(asyncio.create_task(self._dispatch_loop(), name="pumpfire-dispatch"))
        self._tasks.append(asyncio.create_task(self.run(), name="pumpfire-reader"))
        await asyncio.sleep(0)
        return self

    def add_listener(self, callback: EventCallback) -> None:
        """Attach an event callback, chaining with any existing listener.

        Call before :meth:`start` when possible. Calling it after start also
        works as long as an event loop is running.
        """
        if self._on_event is None:
            self._on_event = callback
        else:
            existing = self._on_event

            async def combined(event: Event) -> None:
                await _maybe_await(existing, event)
                await _maybe_await(callback, event)

            self._on_event = combined
        if self._queue is None and self._tasks:
            self._queue = asyncio.Queue(maxsize=self.max_queue)
            self._tasks.append(asyncio.create_task(self._dispatch_loop(), name="pumpfire-dispatch"))

    async def stop(self) -> None:
        self._stop.set()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        if self.metadata is not None:
            await self.metadata.stop()
        self.connected = False

    # ── connection ──────────────────────────────────────────────────────
    def _websockets_connect(self) -> Callable[..., Any]:
        if self._connect is not None:
            return self._connect
        try:
            import websockets
        except ImportError as exc:  # pragma: no cover - declared dependency
            raise RuntimeError("pumpfire needs the websockets package") from exc
        return websockets.connect

    async def _stream(self, url: str) -> None:
        connect = self._websockets_connect()
        async with connect(
            url,
            open_timeout=12,
            ping_interval=20,
            ping_timeout=20,
            max_size=2**23,
        ) as ws:
            # The public endpoint accepts one address per `mentions` filter.
            # Send one subscription request per program on the same socket.
            for index, program in enumerate(self.programs, start=1):
                request = {
                    "jsonrpc": "2.0",
                    "id": index,
                    "method": "logsSubscribe",
                    "params": [
                        {"mentions": [program]},
                        {"commitment": self.commitment},
                    ],
                }
                await ws.send(json.dumps(request))
            self.endpoint = url
            self.book.stats.endpoint = url
            self.connected = True
            LOGGER.info("subscribed to %s via %s", ", ".join(self.programs), url)
            while not self._stop.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=self.stale_after_s)
                except asyncio.TimeoutError as exc:
                    raise ConnectionError(f"no message for {self.stale_after_s}s") from exc
                self._handle_raw(raw)

    def _handle_raw(self, raw: str | bytes) -> None:
        try:
            message = json.loads(raw)
        except Exception:
            self.malformed += 1
            self.book.stats.errors += 1
            return
        if not isinstance(message, dict):
            self.malformed += 1
            return
        if "params" not in message:
            error = message.get("error")
            if error:
                raise RuntimeError(json.dumps(error)[:200])
            return
        value = notification_value(message)
        if value is None:
            return
        signature = value.get("signature")
        if not signature:
            return
        now = time.time()
        self.book.stats.messages += 1
        self.book.stats.message_rate.add(now=now)
        self.book.stats.last_message_ts = now

        if signature in self._seen:
            self.book.stats.duplicate_signatures += 1
            return
        self._remember(signature, now)

        batch = decode_notification(value, seen_ts=now)
        self._apply(batch, value)

    def _remember(self, signature: str, now: float) -> None:
        self._seen[signature] = now
        if len(self._seen) > self.dedupe_size:
            # Drop the oldest quarter in one pass; cheaper than a per-event pop.
            drop = max(1, self.dedupe_size // 4)
            for _ in range(drop):
                if not self._seen:
                    break
                self._seen.popitem(last=False)

    def _apply(self, batch: DecodedBatch, value: dict[str, Any]) -> None:
        logs = value.get("logs") or []
        program_data = sum(1 for line in logs if line.startswith("Program data:"))
        self.program_data_lines += program_data
        if not batch.events:
            if program_data:
                self.unknown_events += 1
            return
        self.book.stats.notifications += 1
        self.book.stats.decoded += len(batch.events)
        for creation in batch.creates:
            self.book.apply_create(creation)
            if self.metadata is not None and creation.uri:
                self.metadata.enqueue(creation.mint, creation.uri)
            self._emit(creation)
        for trade in batch.trades:
            self.book.apply_trade(trade)
            self._emit(trade)
        for complete in batch.completes:
            self.book.apply_complete(complete)
            self._emit(complete)
        for raw_event in batch.raw_events:
            self.book.apply_raw(raw_event)
            self._emit(raw_event)

    def _emit(self, event: Event) -> None:
        if self._queue is None:
            return
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
            self.dropped += 1
            self.book.stats.dropped += 1

    async def _dispatch_loop(self) -> None:
        """Sequential consumer so a mint's buy/sell order is preserved."""
        while not self._stop.is_set():
            queue = self._queue
            if queue is None:
                await asyncio.sleep(0.05)
                continue
            try:
                event = await asyncio.wait_for(queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            try:
                await _maybe_await(self._on_event, event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # a bad callback must not kill the stream
                self.book.stats.errors += 1
                LOGGER.warning("event callback failed: %s", exc)

    # ── supervision ─────────────────────────────────────────────────────
    async def run(self) -> None:
        """Subscribe and reconnect forever, rotating endpoints with backoff."""
        index = 0
        backoff = 2.0
        while not self._stop.is_set():
            url = self.endpoints[index % len(self.endpoints)]
            try:
                await self._stream(url)
                backoff = 2.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.connected = False
                self.book.stats.reconnects += 1
                self.last_error = repr(exc)[:200]
                LOGGER.warning("reconnecting after %s: %s", url, exc)
                index += 1
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    # ── observability ───────────────────────────────────────────────────
    def last_message_age(self) -> float | None:
        last = self.book.stats.last_message_ts
        return None if not last else round(time.time() - last, 1)

    def is_stale(self) -> bool:
        age = self.last_message_age()
        return age is None or age > self.stale_after_s

    def health(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "endpoint": self.endpoint,
            "program": self.program,
            "programs": self.programs,
            "commitment": self.commitment,
            "last_message_age_s": self.last_message_age(),
            "stale": self.is_stale(),
            "stale_after_s": self.stale_after_s,
            "reconnects": self.book.stats.reconnects,
            "dropped": self.dropped,
            "queue_depth": self._queue.qsize() if self._queue is not None else 0,
            "last_error": self.last_error,
            "unknown_events": self.unknown_events,
            "program_data_lines": self.program_data_lines,
            "malformed": self.malformed,
            "stats": self.book.stats_dict(),
        }


async def _maybe_await(callback: EventCallback | None, item: Event) -> None:
    if callback is None:
        return
    result = callback(item)
    if inspect.isawaitable(result):
        await result
