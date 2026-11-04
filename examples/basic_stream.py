"""Print every decoded pump.fun event for 60 seconds.

Core install only:  pip install -e .
Run:                python examples/basic_stream.py
"""

from __future__ import annotations

import asyncio

from pumpfire import CoinBook, Firehose


async def main() -> None:
    book = CoinBook()

    async def on_event(event) -> None:
        if event.kind == "trade":
            coin = book.get(event.mint)
            symbol = (coin.symbol or coin.name) if coin else event.mint[:6]
            side = "BUY " if event.is_buy else "SELL"
            print(f"{side} {symbol:<12} {event.sol:>8.3f} SOL  {event.price_sol:.3e} SOL")

    firehose = Firehose(book, on_event=on_event)
    await firehose.start()
    try:
        await asyncio.sleep(60)
    finally:
        await firehose.stop()
    print(book.stats_dict())


if __name__ == "__main__":
    asyncio.run(main())
