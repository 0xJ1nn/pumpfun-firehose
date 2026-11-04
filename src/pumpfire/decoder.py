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
import struct
import time
from typing import Any

from .base58 import b58encode
from .constants import (
    DISC_COMPLETE,
    DISC_CREATE,
    DISC_TRADE,
    LAMPORTS_PER_SOL,
    TOKEN_SCALE,
)
from .models import CompleteEvent, CreateEvent, DecodedBatch, TradeEvent

_PROGRAM_DATA = "Program data:"


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


def decode_logs(
    logs: list[str],
    *,
    signature: str = "",
    slot: int | None = None,
    seen_ts: float | None = None,
) -> DecodedBatch:
    """Decode every pump.fun event in a transaction's log list."""
    now = time.time() if seen_ts is None else seen_ts
    batch = DecodedBatch(signature=signature, slot=slot)
    for line in logs or ():
        raw = decode_program_data(line)
        if raw is None or len(raw) < 8:
            continue
        discriminator = raw[:8]
        body = raw[8:]
        if discriminator == DISC_TRADE:
            event = decode_trade(body, signature=signature, slot=slot, seen_ts=now)
            if event is not None:
                batch.trades.append(event)
        elif discriminator == DISC_CREATE:
            creation = decode_create(body, signature=signature, slot=slot, seen_ts=now)
            if creation is not None:
                batch.creates.append(creation)
        elif discriminator == DISC_COMPLETE:
            complete = decode_complete(body, signature=signature, slot=slot, seen_ts=now)
            if complete is not None:
                batch.completes.append(complete)
    return batch


def decode_notification(
    value: dict[str, Any], *, seen_ts: float | None = None
) -> DecodedBatch:
    """Decode the ``params.result.value`` object of a logsSubscribe message."""
    signature = str(value.get("signature") or "")
    slot = value.get("slot")
    if value.get("err"):
        return DecodedBatch(signature=signature, slot=slot)
    logs = value.get("logs") or []
    return decode_logs(logs, signature=signature, slot=slot, seen_ts=seen_ts)


def decode_log_line(line: str) -> DecodedBatch:
    """Decode one raw ``Program data:`` line (useful for debugging)."""
    return decode_logs([line])


def notification_value(message: dict[str, Any]) -> dict[str, Any] | None:
    """Extract ``params.result.value`` from a JSON-RPC notification."""
    if "params" not in message:
        return None
    result = (message.get("params") or {}).get("result") or {}
    value = result.get("value")
    return value if isinstance(value, dict) else None
