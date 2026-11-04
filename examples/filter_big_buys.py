"""Alert on buys of 2 SOL or more on coins younger than ten minutes.

Core install only:  pip install -e .
Run:                python examples/filter_big_buys.py
"""

from __future__ import annotations

import asyncio
import time

from pumpfire import CoinBook, Firehose


async def main() -> None:
    book = CoinBook()

    async def on_event(event) -> None:
        if event.kind != "trade" or not event.is_buy or event.sol < 2.0:
            return
        coin = book.get(event.mint)
        if coin is None or coin.age_s(time.time()) > 600:
            return
        symbol = coin.symbol or coin.name or event.mint[:6]
        print(
            f"🚨 {symbol} buy {event.sol:.3f} SOL "
            f"@ {event.price_sol:.3e} · curve {coin.curve_progress_pct():.1f}%"
        )

    firehose = Firehose(book, on_event=on_event)
    await firehose.start()
    try:
        await asyncio.sleep(3600)
    finally:
        await firehose.stop()


if __name__ == "__main__":
    asyncio.run(main())
