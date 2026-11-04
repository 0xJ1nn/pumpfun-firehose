"""Historical replay and RPC backfill.

Two ways to process the past:

* :func:`replay_records` takes saved ``logsSubscribe`` notifications or
  ``getTransaction`` responses and feeds them through the same decoder as the
  live firehose.
* :func:`backfill_events` walks ``getSignaturesForAddress`` +
  ``getTransaction`` for one mint, bonding curve, or pool, then does the same.

Both paths apply events to a :class:`~pumpfire.models.CoinBook`, so the output
looks exactly like a live session.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .decoder import decode_logs, decode_notification, decode_transaction
from .idl import IdlRegistry, default_registry
from .models import CoinBook, DecodedBatch

EventCallback = Callable[[Any], None]


@dataclass
class BackfillStats:
    records: int = 0
    transactions: int = 0
    trades: int = 0
    creates: int = 0
    completes: int = 0
    raw_events: int = 0
    errors: int = 0
    skipped: int = 0

    def add(self, batch: DecodedBatch) -> None:
        self.trades += len(batch.trades)
        self.creates += len(batch.creates)
        self.completes += len(batch.completes)
        self.raw_events += len(batch.raw_events)

    def as_dict(self) -> dict[str, int]:
        return {
            "records": self.records,
            "transactions": self.transactions,
            "trades": self.trades,
            "creates": self.creates,
            "completes": self.completes,
            "raw_events": self.raw_events,
            "errors": self.errors,
            "skipped": self.skipped,
        }


class RpcError(RuntimeError):
    """The RPC returned an error or a request failed permanently."""


class RpcClient:
    """A small, polite JSON-RPC client for backfill."""

    def __init__(
        self,
        url: str = "https://api.mainnet-beta.solana.com",
        *,
        timeout: float = 20.0,
        rps: float = 2.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.url = url
        self.rps = max(0.1, rps)
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._owned = client is None
        self._last_call = 0.0

    async def close(self) -> None:
        if self._owned:
            await self._client.aclose()

    async def _throttle(self) -> None:
        interval = 1.0 / self.rps
        wait = interval - (time.monotonic() - self._last_call)
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_call = time.monotonic()

    async def call(self, method: str, params: list[Any]) -> Any:
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        last_error: Exception | None = None
        for attempt in range(3):
            await self._throttle()
            try:
                response = await self._client.post(
                    self.url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                )
                if response.status_code == 429:
                    await asyncio.sleep(2.0 * (attempt + 1))
                    continue
                response.raise_for_status()
                data = response.json()
                if data.get("error"):
                    raise RpcError(str(data["error"])[:300])
                return data.get("result")
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
                await asyncio.sleep(1.5 * (attempt + 1))
        raise RpcError(f"RPC call failed: {method}: {last_error}")

    async def signatures(self, address: str, limit: int = 100) -> list[dict[str, Any]]:
        result = await self.call(
            "getSignaturesForAddress",
            [address, {"limit": max(1, min(limit, 1000))}],
        )
        return result or []

    async def transaction(self, signature: str) -> dict[str, Any] | None:
        result = await self.call(
            "getTransaction",
            [
                signature,
                {
                    "encoding": "json",
                    "maxSupportedTransactionVersion": 0,
                    "commitment": "confirmed",
                },
            ],
        )
        return result if isinstance(result, dict) else None


def read_records(path: str | Path) -> list[dict[str, Any]]:
    """Read a JSON array, a single JSON object, or JSONL."""
    text = Path(path).read_text(encoding="utf-8").strip()
    if not text:
        return []
    if text.startswith("["):
        data = json.loads(text)
        if not isinstance(data, list):
            raise ValueError("JSON array expected")
        return [item for item in data if isinstance(item, dict)]
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return [json.loads(line) for line in text.splitlines() if line.strip()]
        return [data] if isinstance(data, dict) else []
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def decode_record(
    record: dict[str, Any],
    *,
    registry: IdlRegistry | None = None,
) -> DecodedBatch:
    """Decode one saved notification or transaction."""
    if "meta" in record and "transaction" in record:
        return decode_transaction(record, registry=registry)
    if "params" in record:
        value = ((record.get("params") or {}).get("result") or {}).get("value") or {}
        return decode_notification(
            value,
            seen_ts=record.get("seen_ts"),
            registry=registry,
        )
    return decode_logs(
        record.get("logs") or [],
        signature=str(record.get("signature") or ""),
        slot=record.get("slot"),
        seen_ts=record.get("seen_ts") or record.get("blockTime"),
        registry=registry,
    )


def apply_batch(book: CoinBook, batch: DecodedBatch) -> None:
    for creation in batch.creates:
        book.apply_create(creation)
    for trade in batch.trades:
        book.apply_trade(trade)
    for complete in batch.completes:
        book.apply_complete(complete)
    for raw_event in batch.raw_events:
        book.apply_raw(raw_event)


def _emit(batch: DecodedBatch, on_event: EventCallback | None) -> None:
    if on_event is None:
        return
    for event in batch.events:
        on_event(event)


def replay_records(
    records: Iterable[dict[str, Any]],
    book: CoinBook,
    *,
    on_event: EventCallback | None = None,
    registry: IdlRegistry | None = None,
) -> BackfillStats:
    """Replay saved records through the decoder and a live book."""
    idls = registry or default_registry()
    stats = BackfillStats()
    for record in records:
        stats.records += 1
        try:
            batch = decode_record(record, registry=idls)
        except Exception:
            stats.errors += 1
            continue
        if not batch.events:
            stats.skipped += 1
            continue
        stats.transactions += 1
        stats.add(batch)
        apply_batch(book, batch)
        _emit(batch, on_event)
    return stats


async def backfill_events(
    client: RpcClient,
    *,
    address: str | None = None,
    signatures: list[str] | None = None,
    limit: int = 100,
    book: CoinBook | None = None,
    on_event: EventCallback | None = None,
    registry: IdlRegistry | None = None,
) -> BackfillStats:
    """Fetch historical transactions and replay them into a book."""
    if not signatures and not address:
        raise ValueError("address or signatures is required")
    idls = registry or default_registry()
    live = book or CoinBook()
    stats = BackfillStats()
    items: list[Any]
    if signatures:
        items = list(signatures)
    else:
        assert address is not None
        items = await client.signatures(address, limit)
    stats.records = len(items)
    for item in items:
        if isinstance(item, str):
            signature = item
            failed = False
        else:
            signature = str(item.get("signature") or "")
            failed = bool(item.get("err"))
        if not signature:
            stats.skipped += 1
            continue
        if failed:
            stats.skipped += 1
            continue
        try:
            result = await client.transaction(signature)
        except Exception:
            stats.errors += 1
            continue
        if result is None:
            stats.skipped += 1
            continue
        try:
            batch = decode_transaction(result, registry=idls)
        except Exception:
            stats.errors += 1
            continue
        if not batch.events:
            stats.skipped += 1
            continue
        stats.transactions += 1
        stats.add(batch)
        apply_batch(live, batch)
        _emit(batch, on_event)
    return stats
