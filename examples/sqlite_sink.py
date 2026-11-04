"""Persist the firehose to SQLite.

Install:  pip install -e ".[api]"
Run:      python examples/sqlite_sink.py

Then inspect it with:

    sqlite3 pumpfire.db "select count(*) from trades"
    sqlite3 pumpfire.db "select symbol, market_cap_sol from coins order by market_cap_sol desc limit 10"
"""

from __future__ import annotations

import asyncio

from pumpfire import CoinBook, Firehose, SQLiteStore


async def main() -> None:
    book = CoinBook()
    store = SQLiteStore("pumpfire.db")
    await store.start()

    async def on_event(event) -> None:
        if event.kind == "trade":
            store.enqueue_trade(event)
        elif event.kind == "create":
            store.enqueue_create(event)
        elif event.kind == "complete":
            store.enqueue_complete(event)

    firehose = Firehose(book, on_event=on_event)
    await firehose.start()
    try:
        await asyncio.sleep(3600)
    finally:
        await firehose.stop()
        await store.stop()


if __name__ == "__main__":
    asyncio.run(main())
