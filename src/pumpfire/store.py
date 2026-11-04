"""Optional SQLite persistence.

The live API reads from memory. This module is for people who want to keep the
tape: it batches writes on a background task so a disk hiccup cannot stall the
WebSocket reader.
"""

from __future__ import annotations

import asyncio
import csv
import json
import sqlite3
import sys
import time
from contextlib import ExitStack, closing
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO

from .models import Coin, CompleteEvent, CreateEvent, TradeEvent

if TYPE_CHECKING:
    import aiosqlite

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  signature TEXT NOT NULL,
  mint TEXT NOT NULL,
  user TEXT NOT NULL,
  is_buy INTEGER NOT NULL,
  sol REAL NOT NULL,
  tokens REAL NOT NULL,
  price_sol REAL,
  timestamp INTEGER NOT NULL,
  slot INTEGER,
  seen_ts REAL NOT NULL,
  virtual_sol REAL,
  virtual_token REAL,
  real_sol REAL,
  real_token REAL,
  fee_sol REAL,
  creator TEXT,
  creator_fee_sol REAL
);
CREATE INDEX IF NOT EXISTS ix_trades_mint_ts ON trades(mint, timestamp);
CREATE INDEX IF NOT EXISTS ix_trades_ts ON trades(timestamp);

CREATE TABLE IF NOT EXISTS creates (
  mint TEXT PRIMARY KEY,
  signature TEXT,
  name TEXT,
  symbol TEXT,
  uri TEXT,
  creator TEXT,
  bonding_curve TEXT,
  timestamp INTEGER,
  slot INTEGER,
  seen_ts REAL,
  virtual_sol REAL,
  virtual_token REAL,
  real_token REAL,
  total_supply REAL
);

CREATE TABLE IF NOT EXISTS completes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  signature TEXT,
  mint TEXT,
  user TEXT,
  bonding_curve TEXT,
  timestamp INTEGER,
  slot INTEGER,
  seen_ts REAL,
  UNIQUE(signature, mint)
);

CREATE TABLE IF NOT EXISTS coins (
  mint TEXT PRIMARY KEY,
  name TEXT,
  symbol TEXT,
  uri TEXT,
  creator TEXT,
  bonding_curve TEXT,
  created_ts REAL,
  first_seen_ts REAL,
  last_ts REAL,
  price_sol REAL,
  market_cap_sol REAL,
  volume_sol REAL,
  buys INTEGER,
  sells INTEGER,
  trade_count INTEGER,
  unique_buyers INTEGER,
  unique_sellers INTEGER,
  graduated INTEGER,
  graduated_ts REAL,
  curve_progress_pct REAL,
  metadata_json TEXT
);
"""

TABLES = {"trades", "creates", "completes", "coins"}


class SQLiteStore:
    """A batched, non-blocking SQLite sink."""

    def __init__(self, path: str | Path, *, batch_size: int = 500, flush_s: float = 0.5) -> None:
        self.path = str(path)
        self.batch_size = batch_size
        self.flush_s = flush_s
        self.queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue(maxsize=100_000)
        self.dropped = 0
        self.written = 0
        self.errors = 0
        self._db: aiosqlite.Connection | None = None
        self._task: asyncio.Task[Any] | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        try:
            import aiosqlite
        except ImportError as exc:  # pragma: no cover - exercised by the optional-import test
            raise RuntimeError(
                "SQLite persistence needs optional dependencies. "
                "Install with: pip install 'pumpfun-firehose[api]'"
            ) from exc
        self._db = await aiosqlite.connect(self.path)
        await self._db.executescript(SCHEMA)
        await self._db.commit()
        self._task = asyncio.create_task(self._writer(), name="pumpfire-sqlite")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            await self._task
            self._task = None
        if self._db is not None:
            await self._db.close()
            self._db = None

    # ── enqueue ─────────────────────────────────────────────────────────
    def enqueue_trade(self, event: TradeEvent) -> None:
        self._put(("trade", event))

    def enqueue_create(self, event: CreateEvent) -> None:
        self._put(("create", event))

    def enqueue_complete(self, event: CompleteEvent) -> None:
        self._put(("complete", event))

    def enqueue_coin(self, coin: Coin) -> None:
        self._put(("coin", coin))

    def _put(self, item: tuple[str, Any]) -> None:
        try:
            self.queue.put_nowait(item)
        except asyncio.QueueFull:
            self.dropped += 1

    # ── writer ──────────────────────────────────────────────────────────
    async def _writer(self) -> None:
        while not self._stop.is_set():
            batch = await self._drain(self.flush_s)
            if batch:
                await self._flush(batch)
        while not self.queue.empty():
            batch = await self._drain(0.0)
            if not batch:
                break
            await self._flush(batch)

    async def _drain(self, timeout: float) -> list[tuple[str, Any]]:
        batch: list[tuple[str, Any]] = []
        if timeout > 0 and self.queue.empty():
            try:
                batch.append(await asyncio.wait_for(self.queue.get(), timeout=timeout))
            except asyncio.TimeoutError:
                return batch
        while len(batch) < self.batch_size:
            try:
                batch.append(self.queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return batch

    async def _flush(self, batch: list[tuple[str, Any]]) -> None:
        if self._db is None:
            return
        trades: list[tuple[Any, ...]] = []
        creates: list[tuple[Any, ...]] = []
        completes: list[tuple[Any, ...]] = []
        coins: list[tuple[Any, ...]] = []
        for kind, event in batch:
            if kind == "trade":
                trades.append(_trade_row(event))
            elif kind == "create":
                creates.append(_create_row(event))
            elif kind == "complete":
                completes.append(_complete_row(event))
            elif kind == "coin":
                coins.append(_coin_row(event))
        try:
            if trades:
                await self._db.executemany(
                    "INSERT INTO trades (signature, mint, user, is_buy, sol, tokens, price_sol,"
                    " timestamp, slot, seen_ts, virtual_sol, virtual_token, real_sol, real_token,"
                    " fee_sol, creator, creator_fee_sol)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    trades,
                )
            if creates:
                await self._db.executemany(
                    "INSERT OR REPLACE INTO creates (mint, signature, name, symbol, uri, creator,"
                    " bonding_curve, timestamp, slot, seen_ts, virtual_sol, virtual_token,"
                    " real_token, total_supply)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    creates,
                )
            if completes:
                await self._db.executemany(
                    "INSERT OR IGNORE INTO completes (signature, mint, user, bonding_curve,"
                    " timestamp, slot, seen_ts) VALUES (?,?,?,?,?,?,?)",
                    completes,
                )
            if coins:
                await self._db.executemany(
                    "INSERT INTO coins (mint, name, symbol, uri, creator, bonding_curve,"
                    " created_ts, first_seen_ts, last_ts, price_sol, market_cap_sol, volume_sol,"
                    " buys, sells, trade_count, unique_buyers, unique_sellers, graduated,"
                    " graduated_ts, curve_progress_pct, metadata_json)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(mint) DO UPDATE SET"
                    " name=excluded.name, symbol=excluded.symbol, uri=excluded.uri,"
                    " creator=excluded.creator, bonding_curve=excluded.bonding_curve,"
                    " created_ts=excluded.created_ts, first_seen_ts=excluded.first_seen_ts,"
                    " last_ts=excluded.last_ts, price_sol=excluded.price_sol,"
                    " market_cap_sol=excluded.market_cap_sol, volume_sol=excluded.volume_sol,"
                    " buys=excluded.buys, sells=excluded.sells,"
                    " trade_count=excluded.trade_count, unique_buyers=excluded.unique_buyers,"
                    " unique_sellers=excluded.unique_sellers, graduated=excluded.graduated,"
                    " graduated_ts=excluded.graduated_ts,"
                    " curve_progress_pct=excluded.curve_progress_pct,"
                    " metadata_json=excluded.metadata_json",
                    coins,
                )
            await self._db.commit()
            self.written += len(batch)
        except Exception:
            self.errors += 1

    def health(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "queue_depth": self.queue.qsize(),
            "written": self.written,
            "dropped": self.dropped,
            "errors": self.errors,
        }


def _trade_row(event: TradeEvent) -> tuple[Any, ...]:
    return (
        event.signature,
        event.mint,
        event.user,
        int(event.is_buy),
        event.sol,
        event.tokens,
        event.price_sol,
        event.timestamp,
        event.slot,
        event.seen_ts,
        event.virtual_sol,
        event.virtual_token,
        event.real_sol,
        event.real_token,
        event.fee_sol,
        event.creator,
        event.creator_fee_sol,
    )


def _create_row(event: CreateEvent) -> tuple[Any, ...]:
    return (
        event.mint,
        event.signature,
        event.name,
        event.symbol,
        event.uri,
        event.creator,
        event.bonding_curve,
        event.timestamp,
        event.slot,
        event.seen_ts,
        event.virtual_sol,
        event.virtual_token,
        event.real_token,
        event.total_supply,
    )


def _complete_row(event: CompleteEvent) -> tuple[Any, ...]:
    return (
        event.signature,
        event.mint,
        event.user,
        event.bonding_curve,
        event.timestamp,
        event.slot,
        event.seen_ts,
    )


def _coin_row(coin: Coin) -> tuple[Any, ...]:
    return (
        coin.mint,
        coin.name,
        coin.symbol,
        coin.uri,
        coin.creator,
        coin.bonding_curve,
        coin.created_ts,
        coin.first_seen_ts,
        coin.last_ts,
        coin.price_sol,
        coin.market_cap_sol,
        coin.volume_sol,
        coin.buys,
        coin.sells,
        coin.trade_count,
        len(coin.unique_buyers),
        len(coin.unique_sellers),
        int(coin.graduated),
        coin.graduated_ts,
        coin.curve_progress_pct(),
        json.dumps(coin.metadata, separators=(",", ":")),
    )


def export_table(
    db_path: str | Path,
    table: str,
    fmt: str = "csv",
    out: str | Path = "-",
) -> int:
    """Export one table to CSV or JSONL. Returns the number of rows written."""
    if table not in TABLES:
        raise ValueError(f"table must be one of: {', '.join(sorted(TABLES))}")
    if fmt not in {"csv", "jsonl"}:
        raise ValueError("format must be csv or jsonl")
    with closing(sqlite3.connect(str(db_path))) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(f"SELECT * FROM {table}").fetchall()
        with ExitStack() as stack:
            handle: TextIO
            if str(out) == "-":
                handle = sys.stdout
            else:
                handle = stack.enter_context(open(out, "w", newline="", encoding="utf-8"))
            if fmt == "csv":
                writer = csv.writer(handle)
                keys = list(rows[0].keys()) if rows else []
                if keys:
                    writer.writerow(keys)
                for row in rows:
                    writer.writerow([row[key] for key in keys])
            else:
                for row in rows:
                    handle.write(json.dumps(dict(row), separators=(",", ":")) + "\n")
    return len(rows)


def table_counts(db_path: str | Path) -> dict[str, int]:
    connection = sqlite3.connect(str(db_path))
    try:
        out = {}
        for table in sorted(TABLES):
            try:
                out[table] = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except sqlite3.Error:
                out[table] = 0
        return out
    finally:
        connection.close()


def now_ts() -> float:
    return time.time()
