from __future__ import annotations

import json
import sqlite3
import time

import pytest

from pumpfire.models import Coin, CompleteEvent, CreateEvent, TradeEvent
from pumpfire.store import SQLiteStore, export_table, table_counts


def make_events() -> tuple[TradeEvent, CreateEvent, CompleteEvent, Coin]:
    now = time.time()
    trade = TradeEvent(
        signature="sig",
        mint="MINT",
        user="WALLET",
        is_buy=True,
        sol=1.5,
        tokens=1000.0,
        tokens_raw=1_000_000_000,
        price_sol=1e-7,
        timestamp=int(now),
        seen_ts=now,
        virtual_sol=31.5,
        virtual_token=1_072_000_000.0,
        real_sol=1.5,
        real_token=793_000_000.0,
    )
    creation = CreateEvent(
        signature="create",
        mint="MINT",
        name="Test",
        symbol="TST",
        uri="ipfs://x",
        creator="CREATOR",
        bonding_curve="CURVE",
        timestamp=int(now),
        seen_ts=now,
    )
    complete = CompleteEvent(
        signature="done",
        mint="MINT",
        user="WALLET",
        bonding_curve="CURVE",
        timestamp=int(now),
        seen_ts=now,
    )
    coin = Coin(mint="MINT", name="Test", symbol="TST", created_ts=now)
    coin.price_sol = 1e-7
    coin.market_cap_sol = 100.0
    coin.volume_sol = 1.5
    coin.buys = 1
    coin.trade_count = 1
    return trade, creation, complete, coin


@pytest.mark.asyncio
async def test_store_round_trip(tmp_path) -> None:
    db = tmp_path / "test.db"
    store = SQLiteStore(db)
    await store.start()
    trade, creation, complete, coin = make_events()
    store.enqueue_create(creation)
    store.enqueue_trade(trade)
    store.enqueue_complete(complete)
    store.enqueue_coin(coin)
    await store.stop()
    assert store.errors == 0
    counts = table_counts(db)
    assert counts["trades"] == 1
    assert counts["creates"] == 1
    assert counts["completes"] == 1
    assert counts["coins"] == 1
    connection = sqlite3.connect(db)
    try:
        row = connection.execute("SELECT mint, is_buy, sol FROM trades").fetchone()
        assert row == ("MINT", 1, 1.5)
    finally:
        connection.close()


def test_export_csv_and_jsonl(tmp_path) -> None:
    db = tmp_path / "test.db"
    connection = sqlite3.connect(db)
    connection.executescript(
        "CREATE TABLE trades (id INTEGER PRIMARY KEY, mint TEXT, sol REAL);"
        "INSERT INTO trades (mint, sol) VALUES ('M', 1.5);"
    )
    connection.commit()
    connection.close()
    csv_path = tmp_path / "trades.csv"
    assert export_table(db, "trades", "csv", csv_path) == 1
    assert "mint,sol" in csv_path.read_text()
    jsonl_path = tmp_path / "trades.jsonl"
    assert export_table(db, "trades", "jsonl", jsonl_path) == 1
    record = json.loads(jsonl_path.read_text().strip())
    assert record["mint"] == "M"


def test_export_rejects_unknown_table(tmp_path) -> None:
    db = tmp_path / "test.db"
    sqlite3.connect(db).close()
    with pytest.raises(ValueError):
        export_table(db, "nope", "csv", tmp_path / "x.csv")
    with pytest.raises(ValueError):
        export_table(db, "trades", "xml", tmp_path / "x.xml")
