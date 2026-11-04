"""Decode pump.fun Anchor events out of Solana ``Program data:`` log lines.

The hot path is:

    WebSocket frame -> JSON -> base64 -> struct unpack -> dataclass

No RPC call is made. The trade event carries the post-trade bonding-curve
reserves, so price, market cap and curve progress are exact chain values rather
than estimates from a price feed.

Layouts (after the 8-byte Anchor discriminator):

TradeEvent (121 fixed bytes, extended fee fields parsed when present)::

    mint(32) | sol_amount(u64) | token_amount(u64) | is_buy(u8) | user(32)
    | timestamp(i64) | virtual_sol(u64) | virtual_token(u64)
    | real_sol(u64) | real_token(u64)
    | fee_recipient(32) | fee_bps(u64) | fee(u64)
    | creator(32) | creator_fee_bps(u64) | creator_fee(u64) | ...

CreateEvent::

    name(string) | symbol(string) | uri(string)
    | mint(32) | bonding_curve(32) | user(32)
    | creator(32) | timestamp(i64) | virtual_token(u64) | virtual_sol(u64)
    | real_token(u64) | total_supply(u64) | ...

CompleteEvent::

    user(32) | mint(32) | bonding_curve(32) | timestamp(i64)

Anchor strings are a little-endian ``u32`` length followed by UTF-8 bytes.
Anything that does not parse cleanly is dropped, never guessed.
"""

from __future__ import annotations

import base64
import json
import re
import struct
import time
from typing import Any

from .base58 import b58decode, b58encode
from .constants import (
    DISC_COMPLETE,
    DISC_CREATE,
    DISC_TRADE,
    LAMPORTS_PER_SOL,
    PUMP_PROGRAM,
    TOKEN_SCALE,
)
from .idl import IdlRegistry, default_registry
from .models import CompleteEvent, CreateEvent, DecodedBatch, RawEvent, TradeEvent

_PROGRAM_DATA = "Program data:"
_INVOKE = re.compile(r"^Program (\S+) invoke \[(\d+)\]")
_EXIT = re.compile(r"^Program (\S+) (?:success|failed)")
EVENT_IX_TAG = bytes.fromhex("e445a52e51cb9a1d")


class Reader:
    """A bounds-checked little-endian reader."""

    __slots__ = ("data", "offset")

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.offset = 0

    def remaining(self) -> int:
        return len(self.data) - self.offset

    def take(self, size: int) -> bytes:
        end = self.offset + size
        if size < 0 or end > len(self.data):
            raise ValueError("payload ended early")
        chunk = self.data[self.offset : end]
        self.offset = end
        return chunk

    def u8(self) -> int:
        return self.take(1)[0]

    def u32(self) -> int:
        return int(struct.unpack("<I", self.take(4))[0])

    def u64(self) -> int:
        return int(struct.unpack("<Q", self.take(8))[0])

    def i64(self) -> int:
        return int(struct.unpack("<q", self.take(8))[0])

    def pubkey(self) -> str:
        return b58encode(self.take(32))

    def string(self, *, max_length: int = 1024) -> str:
        length = self.u32()
        if length > max_length:
            raise ValueError("string is too long")
        return self.take(length).decode("utf-8", "replace")


def decode_program_data(line: str) -> bytes | None:
    """Return the raw bytes of a ``Program data:`` log line, or ``None``."""
    if not line.startswith(_PROGRAM_DATA):
        return None
    try:
        payload = line.split(_PROGRAM_DATA, 1)[1].strip()
        return base64.b64decode(payload, validate=False)
    except Exception:
        return None


def decode_trade(body: bytes, *, signature: str, slot: int | None, seen_ts: float) -> TradeEvent | None:
    """Decode one TradeEvent body (discriminator already removed)."""
    if len(body) < 121:
        return None
    try:
        reader = Reader(body)
        mint = reader.pubkey()
        sol_lamports = reader.u64()
        token_raw = reader.u64()
        is_buy = bool(reader.u8())
        user = reader.pubkey()
        timestamp = reader.i64()
        virtual_sol_raw = reader.u64()
        virtual_token_raw = reader.u64()
        real_sol_raw = reader.u64()
        real_token_raw = reader.u64()
    except (ValueError, struct.error):
        return None
    if not mint or not user or token_raw <= 0:
        return None

    price_sol: float | None
    if virtual_sol_raw > 0 and virtual_token_raw > 0:
        price_sol = (virtual_sol_raw / LAMPORTS_PER_SOL) / (virtual_token_raw / TOKEN_SCALE)
    else:
        price_sol = (sol_lamports / LAMPORTS_PER_SOL) / (token_raw / TOKEN_SCALE)

    fee_sol: float | None = None
    creator: str | None = None
    creator_fee_sol: float | None = None
    # Extended TradeEvent: fee recipient, fee bps, fee, creator, creator fee.
    if len(body) >= 250:
        try:
            ext = Reader(body)
            ext.take(121)               # core fields
            ext.take(32)                # fee_recipient
            ext.u64()                   # fee_basis_points
            fee_lamports = ext.u64()    # protocol fee
            creator = ext.pubkey()
            ext.u64()                   # creator_fee_basis_points
            creator_fee_lamports = ext.u64()
            fee_sol = fee_lamports / LAMPORTS_PER_SOL
            creator_fee_sol = creator_fee_lamports / LAMPORTS_PER_SOL
        except (ValueError, struct.error):
            fee_sol = None
            creator = None
            creator_fee_sol = None

    if timestamp <= 0:
        timestamp = int(seen_ts)
    return TradeEvent(
        signature=signature,
        mint=mint,
        user=user,
        is_buy=is_buy,
        sol=sol_lamports / LAMPORTS_PER_SOL,
        tokens=token_raw / TOKEN_SCALE,
        tokens_raw=token_raw,
        price_sol=price_sol,
        timestamp=timestamp,
        slot=slot,
        seen_ts=seen_ts,
        virtual_sol=virtual_sol_raw / LAMPORTS_PER_SOL if virtual_sol_raw else None,
        virtual_token=virtual_token_raw / TOKEN_SCALE if virtual_token_raw else None,
        real_sol=real_sol_raw / LAMPORTS_PER_SOL if real_sol_raw else None,
        real_token=real_token_raw / TOKEN_SCALE if real_token_raw else None,
        fee_sol=fee_sol,
        creator=creator,
        creator_fee_sol=creator_fee_sol,
    )


def decode_create(body: bytes, *, signature: str, slot: int | None, seen_ts: float) -> CreateEvent | None:
    """Decode one CreateEvent body (discriminator already removed)."""
    try:
        reader = Reader(body)
        name = reader.string()
        symbol = reader.string()
        uri = reader.string()
        mint = reader.pubkey()
        bonding_curve = reader.pubkey()
        user = reader.pubkey()
    except (ValueError, struct.error):
        return None
    if not mint:
        return None

    creator = user
    timestamp = int(seen_ts)
    virtual_sol: float | None = None
    virtual_token: float | None = None
    real_token: float | None = None
    total_supply: float | None = None

    # createV2 appends the launch state. Parse it when present.
    if reader.remaining() >= 72:
        try:
            ext_creator = reader.pubkey()
            ext_timestamp = reader.i64()
            virtual_token_raw = reader.u64()
            virtual_sol_raw = reader.u64()
            real_token_raw = reader.u64()
            total_supply_raw = reader.u64()
            creator = ext_creator or creator
            timestamp = ext_timestamp if ext_timestamp > 0 else timestamp
            virtual_token = virtual_token_raw / TOKEN_SCALE if virtual_token_raw else None
            virtual_sol = virtual_sol_raw / LAMPORTS_PER_SOL if virtual_sol_raw else None
            real_token = real_token_raw / TOKEN_SCALE if real_token_raw else None
            total_supply = total_supply_raw / TOKEN_SCALE if total_supply_raw else None
            # A handful of create events carry values that do not describe a
            # fresh curve. Do not let a bad launch state poison the first price;
            # the first trade event will set the real reserves a moment later.
            if virtual_sol is not None and not (1.0 <= virtual_sol <= 1_000_000.0):
                virtual_sol = None
            if virtual_token is not None and not (1_000_000.0 <= virtual_token <= 1e12):
                virtual_token = None
            if real_token is not None and not (0.0 <= real_token <= 1e12):
                real_token = None
            if total_supply is not None and not (1_000.0 <= total_supply <= 1e12):
                total_supply = None
        except (ValueError, struct.error):
            pass

    return CreateEvent(
        signature=signature,
        mint=mint,
        name=name,
        symbol=symbol,
        uri=uri,
        creator=creator,
        bonding_curve=bonding_curve,
        timestamp=timestamp,
        slot=slot,
        seen_ts=seen_ts,
        virtual_sol=virtual_sol,
        virtual_token=virtual_token,
        real_token=real_token,
        total_supply=total_supply,
    )


def decode_complete(
    body: bytes, *, signature: str, slot: int | None, seen_ts: float
) -> CompleteEvent | None:
    """Decode one CompleteEvent body (discriminator already removed)."""
    if len(body) < 104:
        return None
    try:
        reader = Reader(body)
        user = reader.pubkey()
        mint = reader.pubkey()
        bonding_curve = reader.pubkey()
        timestamp = reader.i64()
    except (ValueError, struct.error):
        return None
    if not mint:
        return None
    return CompleteEvent(
        signature=signature,
        mint=mint,
        user=user,
        bonding_curve=bonding_curve,
        timestamp=timestamp if timestamp > 0 else int(seen_ts),
        slot=slot,
        seen_ts=seen_ts,
    )


def decode_payload(
    program: str | None,
    payload: bytes,
    *,
    signature: str,
    slot: int | None,
    seen_ts: float,
    registry: IdlRegistry,
) -> TradeEvent | CreateEvent | CompleteEvent | RawEvent | None:
    """Decode one event payload from a known program."""
    if len(payload) < 8:
        return None
    discriminator = payload[:8]
    body = payload[8:]
    if program in (None, PUMP_PROGRAM):
        if discriminator == DISC_TRADE:
            return decode_trade(body, signature=signature, slot=slot, seen_ts=seen_ts)
        if discriminator == DISC_CREATE:
            return decode_create(body, signature=signature, slot=slot, seen_ts=seen_ts)
        if discriminator == DISC_COMPLETE:
            return decode_complete(body, signature=signature, slot=slot, seen_ts=seen_ts)
    if program is not None and program not in registry.by_address:
        return None
    decoded = registry.decode(program, payload)
    if decoded is None:
        return None
    return RawEvent(
        program=decoded.program,
        name=decoded.name,
        signature=signature,
        slot=slot,
        seen_ts=seen_ts,
        fields=decoded.fields,
        extra_bytes=decoded.extra_bytes,
        discriminator=decoded.discriminator,
    )


def append_event(batch: DecodedBatch, event: Any) -> None:
    if isinstance(event, TradeEvent):
        batch.trades.append(event)
    elif isinstance(event, CreateEvent):
        batch.creates.append(event)
    elif isinstance(event, CompleteEvent):
        batch.completes.append(event)
    elif isinstance(event, RawEvent):
        batch.raw_events.append(event)


def decode_logs(
    logs: list[str],
    *,
    signature: str = "",
    slot: int | None = None,
    seen_ts: float | None = None,
    registry: IdlRegistry | None = None,
) -> DecodedBatch:
    """Decode every pump.fun / PumpSwap event in a transaction's log list.

    The log stack is tracked so events with the same Anchor discriminator in
    different programs (some event names are shared) are attributed correctly.
    Events that are not one of the three book events are returned as
    :class:`RawEvent` records via the bundled IDLs.
    """
    now = time.time() if seen_ts is None else seen_ts
    batch = DecodedBatch(signature=signature, slot=slot)
    idls = registry or default_registry()
    stack: list[str] = []
    for line in logs or ():
        invoke = _INVOKE.match(line)
        if invoke:
            stack.append(invoke.group(1))
            continue
        exit_match = _EXIT.match(line)
        if exit_match:
            program_id = exit_match.group(1)
            while stack:
                if stack.pop() == program_id:
                    break
            continue
        raw = decode_program_data(line)
        if raw is None or len(raw) < 8:
            continue
        program = stack[-1] if stack else None
        event = decode_payload(
            program,
            raw,
            signature=signature,
            slot=slot,
            seen_ts=now,
            registry=idls,
        )
        if event is not None:
            append_event(batch, event)
    return batch


def decode_notification(
    value: dict[str, Any],
    *,
    seen_ts: float | None = None,
    registry: IdlRegistry | None = None,
) -> DecodedBatch:
    """Decode the ``params.result.value`` object of a logsSubscribe message."""
    signature = str(value.get("signature") or "")
    slot = value.get("slot")
    if value.get("err"):
        return DecodedBatch(signature=signature, slot=slot)
    logs = value.get("logs") or []
    return decode_logs(
        logs,
        signature=signature,
        slot=slot,
        seen_ts=seen_ts,
        registry=registry,
    )


def _event_key(event: Any) -> tuple[Any, ...] | None:
    if isinstance(event, RawEvent):
        return (
            "raw",
            event.name,
            event.discriminator,
            json.dumps(event.fields, sort_keys=True, default=str),
        )
    if isinstance(event, TradeEvent):
        return (
            "trade",
            event.mint,
            event.user,
            event.is_buy,
            event.sol,
            event.tokens_raw,
            event.timestamp,
        )
    if isinstance(event, CreateEvent):
        return ("create", event.mint, event.name, event.symbol, event.uri, event.timestamp)
    if isinstance(event, CompleteEvent):
        return ("complete", event.mint, event.timestamp)
    return None


def _account_keys(result: dict[str, Any]) -> list[str]:
    transaction = result.get("transaction") or {}
    message = transaction.get("message") or {}
    keys = [
        item.get("pubkey") if isinstance(item, dict) else item
        for item in message.get("accountKeys") or []
    ]
    loaded = (result.get("meta") or {}).get("loadedAddresses") or {}
    keys.extend(loaded.get("writable") or [])
    keys.extend(loaded.get("readonly") or [])
    return [key for key in keys if isinstance(key, str)]


def decode_transaction(
    result: dict[str, Any],
    *,
    registry: IdlRegistry | None = None,
) -> DecodedBatch:
    """Decode a ``getTransaction`` result, including ``emit_cpi!`` events.

    Log events are decoded first. Inner instructions that carry the Anchor
    ``emit_cpi!`` tag are decoded too, so the backfill does not silently miss
    events that never appear in ``meta.logMessages``.
    """
    meta = result.get("meta") or {}
    transaction = result.get("transaction") or {}
    signatures = transaction.get("signatures") or []
    signature = signatures[0] if signatures else ""
    slot = result.get("slot")
    block_time = result.get("blockTime")
    now = float(block_time) if block_time else time.time()
    idls = registry or default_registry()
    batch = decode_logs(
        meta.get("logMessages") or [],
        signature=signature,
        slot=slot,
        seen_ts=now,
        registry=idls,
    )
    seen = {key for event in batch.events if (key := _event_key(event)) is not None}
    keys = _account_keys(result)
    for group in meta.get("innerInstructions") or []:
        for instruction in group.get("instructions") or []:
            index = instruction.get("programIdIndex")
            data = instruction.get("data")
            if not isinstance(index, int) or not isinstance(data, str):
                continue
            if index < 0 or index >= len(keys):
                continue
            try:
                raw = b58decode(data)
            except ValueError:
                continue
            if raw[:8] != EVENT_IX_TAG:
                continue
            event = decode_payload(
                keys[index],
                raw[8:],
                signature=signature,
                slot=slot,
                seen_ts=now,
                registry=idls,
            )
            key = _event_key(event) if event is not None else None
            if event is None or key in seen:
                continue
            if key is not None:
                seen.add(key)
            append_event(batch, event)
    return batch


def decode_log_line(
    line: str,
    *,
    program: str | None = None,
    registry: IdlRegistry | None = None,
) -> DecodedBatch:
    """Decode one raw ``Program data:`` line (useful for debugging).

    Pass ``program="pump_amm"`` when decoding a line from the AMM, because a
    few event names share a discriminator across the two programs.
    """
    idls = registry or default_registry()
    logs = [line]
    if program:
        address = idls.address_for(program)
        if address:
            logs = [f"Program {address} invoke [1]", line, f"Program {address} success"]
    return decode_logs(logs, registry=idls)


def notification_value(message: dict[str, Any]) -> dict[str, Any] | None:
    """Extract ``params.result.value`` from a JSON-RPC notification."""
    if "params" not in message:
        return None
    result = (message.get("params") or {}).get("result") or {}
    value = result.get("value")
    return value if isinstance(value, dict) else None
