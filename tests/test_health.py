from __future__ import annotations

import asyncio
import base64
import json
import time

import pytest

from pumpfire import CoinBook, Firehose
from pumpfire.constants import PUMP_PROGRAM
from pumpfire.health import EventQueue, StalenessGate, StaleStream
from tests.helpers import trade_payload


def notification(signature: str, logs: list[str], *, slot: int = 1) -> str:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "method": "logsNotification",
            "params": {
                "result": {
                    "context": {"slot": slot},
                    "value": {"signature": signature, "slot": slot, "err": None, "logs": logs},
                },
                "subscription": 1,
            },
        }
    )


def trade_frame(signature: str, *, slot: int = 1) -> str:
    return notification(
        signature,
        [
            f"Program {PUMP_PROGRAM} invoke [1]",
            trade_payload(),
            f"Program {PUMP_PROGRAM} success",
        ],
        slot=slot,
    )


def unknown_event_frame(signature: str, *, slot: int = 2) -> str:
    payload = base64.b64encode(bytes.fromhex("0011223344556677") + b"\x00" * 64).decode()
    return notification(
        signature,
        [
            f"Program {PUMP_PROGRAM} invoke [1]",
            "Program data: " + payload,
            f"Program {PUMP_PROGRAM} success",
        ],
        slot=slot,
    )


def attached_firehose(max_queue: int = 8) -> Firehose:
    """A firehose with its dispatch queue attached but no socket opened."""
    firehose = Firehose(CoinBook(), on_event=lambda event: None, max_queue=max_queue)
    firehose._queue = EventQueue(max_queue)
    return firehose


# ── the gate refuses while stale ────────────────────────────────────────
def test_gate_refuses_while_last_event_is_older_than_threshold() -> None:
    gate = StalenessGate(stale_after_s=45.0)
    gate.observe(1_000.0)

    decision = gate.check(now=1_000.0 + 46.0)

    assert decision.live is False
    assert decision.stale is True
    assert decision.reason == "stale"
    assert decision.age_s == 46.0
    assert gate.allows(now=1_046.0) is False


def test_gate_allows_at_the_threshold() -> None:
    gate = StalenessGate(stale_after_s=45.0)
    gate.observe(1_000.0)

    decision = gate.check(now=1_045.0)

    assert decision.live is True
    assert decision.stale is False
    assert decision.reason == "fresh"


def test_gate_without_any_event_is_stale() -> None:
    gate = StalenessGate(stale_after_s=45.0)

    decision = gate.check(now=1_000.0)

    assert decision.live is False
    assert decision.stale is True
    assert decision.age_s is None
    assert decision.reason == "no-event"
    assert gate.is_stale(now=1_000.0) is True


def test_require_live_refuses_a_stale_entry() -> None:
    gate = StalenessGate(stale_after_s=10.0)
    gate.observe(1_000.0)

    with pytest.raises(StaleStream):
        gate.require_live(now=1_011.0)


def test_firehose_reports_stale_and_not_live() -> None:
    firehose = Firehose(CoinBook(), stale_after_s=30.0)
    firehose.connected = True
    firehose.gate.observe(time.monotonic() - 120.0)

    health = firehose.health()

    assert health.stale is True
    assert health.live is False
    assert health.last_event_age_s is not None
    assert health.last_event_age_s >= 119.0
    assert health.as_dict()["stale"] is True

    firehose.gate.observe()
    refreshed = firehose.health()
    assert refreshed.stale is False
    assert refreshed.live is True


# ── the health object ───────────────────────────────────────────────────
def test_health_exposes_age_reconnects_dropped_and_ratio() -> None:
    firehose = attached_firehose(max_queue=1)
    firehose.book.stats.reconnects = 4
    firehose._handle_raw(trade_frame("sig-decoded", slot=1))
    firehose._handle_raw(trade_frame("sig-decoded", slot=1))  # duplicate
    firehose._handle_raw("{not json")
    firehose._handle_raw(unknown_event_frame("sig-unknown", slot=2))
    firehose._handle_raw(trade_frame("sig-dropped", slot=3))

    health = firehose.health()
    payload = health.as_dict()

    assert payload["last_event_age_s"] is not None
    assert payload["reconnects"] == 4
    assert payload["dropped"] == 1
    assert payload["decoded"] == 2
    assert payload["rejected"] == 2
    assert payload["decoded_ratio"] == 0.5
    assert health.queue.depth <= 1
    assert health.queue.dropped == 1
    assert payload["queue_depth"] <= 1
    assert payload["stats"]["duplicate_signatures"] == 1


def test_health_reports_clean_state_before_any_event() -> None:
    firehose = Firehose(CoinBook())

    health = firehose.health()

    assert health.decoded == 0
    assert health.rejected == 0
    assert health.decoded_ratio is None
    assert health.last_event_age_s is None
    assert health.queue.depth == 0
    assert health.stale is True
    assert health.live is False


def test_no_event_refusal_message_has_no_none_age() -> None:
    gate = StalenessGate(stale_after_s=10.0)

    with pytest.raises(StaleStream) as excinfo:
        gate.require_live(now=1_000.0)

    message = str(excinfo.value)
    assert "None" not in message
    assert "no event observed yet" in message


# ── age follows the monotonic clock, not the wall clock ─────────────────
class FakeClock:
    """A clock whose wall and monotonic readings the test controls."""

    def __init__(
        self,
        *,
        wall: float = 0.0,
        mono: float = 0.0,
        wall_step: float = 0.0,
    ) -> None:
        self.wall = wall
        self.mono = mono
        self.wall_step = wall_step
        self.mono_reads = 0

    def time(self) -> float:
        value = self.wall
        self.wall += self.wall_step
        return value

    def monotonic(self) -> float:
        self.mono_reads += 1
        return self.mono


def test_backward_wall_clock_step_cannot_mask_a_stale_stream(monkeypatch) -> None:
    clock = FakeClock(wall=1_000_000.0, mono=10_000.0)
    monkeypatch.setattr("pumpfire.health.time", clock)
    gate = StalenessGate(stale_after_s=30.0)
    gate.observe()
    assert gate.require_live().live

    clock.mono += 16 * 60
    clock.wall -= 16 * 60

    with pytest.raises(StaleStream) as excinfo:
        gate.require_live()
    assert excinfo.value.decision.age_s == 960.0


def test_health_snapshot_derives_age_and_stale_from_one_check(monkeypatch) -> None:
    clock = FakeClock(wall=5.0, mono=5.0, wall_step=495.0)
    monkeypatch.setattr("pumpfire.health.time", clock)
    firehose = Firehose(CoinBook(), stale_after_s=10.0)
    firehose.connected = True
    firehose.gate.observe(now=0.0)

    snapshot = firehose.health()

    assert snapshot.last_event_age_s == 5.0
    assert snapshot.stale is False
    assert snapshot.live is True
    assert clock.mono_reads == 1


# ── counting a dropped connection ───────────────────────────────────────
class ScriptedSocket:
    """A fake WebSocket that serves a script then fails."""

    def __init__(self, frames: list[str], error: BaseException, on_open=None) -> None:
        self.frames = list(frames)
        self.error = error
        self.on_open = on_open
        self.sent: list[str] = []

    async def __aenter__(self) -> ScriptedSocket:
        if self.on_open is not None:
            self.on_open()
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def send(self, payload: str) -> None:
        self.sent.append(payload)

    async def recv(self) -> str:
        if self.frames:
            return self.frames.pop(0)
        raise self.error


async def _no_wait(_delay: float) -> None:
    return None


async def test_reconnect_counts_once_and_keeps_the_drop_census(monkeypatch) -> None:
    firehose = attached_firehose(max_queue=1)
    sessions = [
        ScriptedSocket(
            [trade_frame("sig-a"), trade_frame("sig-b"), trade_frame("sig-c")],
            ConnectionError("closed"),
        ),
        ScriptedSocket([], ConnectionError("closed"), on_open=firehose._stop.set),
    ]

    def connect(url: str, **settings: object) -> ScriptedSocket:
        return sessions.pop(0)

    firehose._connect = connect
    monkeypatch.setattr("pumpfire.firehose.asyncio.sleep", _no_wait)

    await asyncio.wait_for(firehose.run(), timeout=1.0)

    health = firehose.health()
    assert health.reconnects == 1
    assert health.connect_failures == 0
    assert health.dropped == 2
    assert health.queue.dropped == 2
    assert health.queue.enqueued == 3
    assert firehose.decoded == 3


async def test_first_failed_connect_is_a_connect_failure_not_a_reconnect(monkeypatch) -> None:
    firehose = Firehose(CoinBook(), stale_after_s=30.0)
    attempts: list[str] = []

    def connect(url: str, **settings: object) -> ScriptedSocket:
        attempts.append(url)
        if len(attempts) == 2:
            firehose._stop.set()
        raise ConnectionRefusedError("refused")

    firehose._connect = connect
    monkeypatch.setattr("pumpfire.firehose.asyncio.sleep", _no_wait)

    await asyncio.wait_for(firehose.run(), timeout=1.0)

    health = firehose.health()
    assert len(attempts) == 2
    assert health.reconnects == 0
    assert health.connect_failures == 2
    assert firehose.book.stats.reconnects == 0
