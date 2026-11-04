"""A deterministic demo feed for the dashboard and for screenshots.

No network, no keys, no surprises. It seeds a handful of plausible coins and
then generates trades, launches, and graduations at a steady rate.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import random
import time
from typing import Any

from .constants import INITIAL_REAL_TOKEN, INITIAL_VIRTUAL_SOL, INITIAL_VIRTUAL_TOKEN, TOKEN_SCALE
from .models import CoinBook, CompleteEvent, CreateEvent, TradeEvent

_SEEDS: list[dict[str, Any]] = [
    {
        "name": "dogwifhat",
        "symbol": "WIF",
        "description": "The dog with the hat. A very serious financial instrument.",
        "twitter": "dogwifcoin",
        "website": "https://dogwifhat.io",
        "age": 420,
    },
    {
        "name": "Book of Meme",
        "symbol": "BOME",
        "description": "A meme book, permanently on-chain.",
        "twitter": "bookofmeme",
        "website": "https://bookofmeme.io",
        "age": 310,
    },
    {
        "name": "Pump Cat",
        "symbol": "PCAT",
        "description": "Cat. Chart. Chaos.",
        "twitter": "pumpcat",
        "website": None,
        "age": 190,
    },
    {
        "name": "Solana Summer",
        "symbol": "SUMMER",
        "description": "It is always summer on Solana.",
        "twitter": "solanasummer",
        "website": "https://solanasummer.xyz",
        "age": 95,
    },
    {
        "name": "Just a Chill Guy",
        "symbol": "CHILL",
        "description": "Unbothered. Moisturised. In his lane.",
        "twitter": "chillguy",
        "website": None,
        "age": 60,
    },
    {
        "name": "Quantum Toad",
        "symbol": "QTOAD",
        "description": "Both up and down until you look.",
        "twitter": "quantumtoad",
        "website": None,
        "age": 28,
    },
    {
        "name": "Breakfast Coin",
        "symbol": "EGG",
        "description": "The most important coin of the day.",
        "twitter": "breakfastcoin",
        "website": None,
        "age": 12,
    },
]

_LAUNCH_NAMES = [
    ("Moon Squirrel", "MSQRL"),
    ("Gas Fee Enjoyer", "GAS"),
    ("Liquid Cat", "LCAT"),
    ("Zero Sleep", "ZZZ"),
    ("Tiny Bank", "TINY"),
    ("Exit Liquidity", "EXIT"),
    ("Good Dog", "GDOG"),
    ("Onchain Coffee", "BEAN"),
]

_SIZES = [0.01, 0.02, 0.05, 0.1, 0.2, 0.35, 0.5, 1.0, 2.0, 4.0, 8.0]
_WEIGHTS = [20, 18, 16, 14, 10, 8, 6, 4, 2, 1.5, 0.5]


class DemoFeed:
    """Generate a realistic-looking pump.fun tape without a socket."""

    def __init__(self, book: CoinBook, *, tick_s: float = 0.08, on_event: Any = None) -> None:
        self.book = book
        self.tick_s = tick_s
        self.on_event = on_event
        self._random = random.Random(7)
        self._stop = asyncio.Event()
        self._seq = 0
        self._wallet_seq = 0
        self._last_launch = 0.0
        self._started = False

    async def start(self) -> None:
        self.book.set_sol_usd(200.0, source="demo")
        now = time.time()
        for seed in _SEEDS:
            age = float(seed["age"])
            created = now - age
            mint = _mint_for(seed["symbol"])
            creation = CreateEvent(
                signature=f"demo-create-{seed['symbol'].lower()}",
                mint=mint,
                name=seed["name"],
                symbol=seed["symbol"],
                uri="",
                creator=_wallet(),
                bonding_curve=_mint_for(seed["symbol"] + "-curve"),
                timestamp=int(created),
                seen_ts=created,
                virtual_sol=INITIAL_VIRTUAL_SOL,
                virtual_token=INITIAL_VIRTUAL_TOKEN,
                real_token=INITIAL_REAL_TOKEN,
                total_supply=1_000_000_000.0,
            )
            self.book.apply_create(creation)
            self._emit(creation)
            self.book.set_metadata(
                mint,
                {
                    "image": None,
                    "description": seed["description"],
                    "twitter": (
                        "https://x.com/" + seed["twitter"] if seed["twitter"] else None
                    ),
                    "telegram": None,
                    "website": seed["website"],
                    "created_on": None,
                },
            )
            # Give each coin some history so the dashboard has movement.
            coin = self.book.get(mint)
            if coin is None:
                continue
            price = coin.price_sol
            for step in range(self._random.randint(12, 40)):
                ts = created + (age * (step + 1) / 45.0)
                is_buy = self._random.random() < 0.58
                sol = self._random.choice(_SIZES) * 0.35
                tokens = sol / price if price > 0 else 1.0
                if is_buy:
                    coin.virtual_sol += sol
                    coin.virtual_token -= tokens
                    coin.real_sol += sol
                    coin.real_token -= tokens
                else:
                    coin.virtual_sol = max(1.0, coin.virtual_sol - sol)
                    coin.virtual_token += tokens
                    coin.real_sol = max(0.0, coin.real_sol - sol)
                    coin.real_token += tokens
                price = coin.virtual_sol / coin.virtual_token if coin.virtual_token > 0 else price
                self._seq += 1
                trade = TradeEvent(
                    signature=f"demo-{self._seq}",
                    mint=mint,
                    user=_wallet(),
                    is_buy=is_buy,
                    sol=sol,
                    tokens=tokens,
                    tokens_raw=int(tokens * TOKEN_SCALE),
                    price_sol=price,
                    timestamp=int(ts),
                    seen_ts=ts,
                    virtual_sol=coin.virtual_sol,
                    virtual_token=coin.virtual_token,
                    real_sol=coin.real_sol,
                    real_token=coin.real_token,
                )
                self.book.apply_trade(trade)
                self._emit(trade)
        self._started = True

    async def run(self) -> None:
        if not self._started:
            await self.start()
        while not self._stop.is_set():
            await asyncio.sleep(self.tick_s)
            self._tick()
            now = time.time()
            if now - self._last_launch > 18.0:
                self._last_launch = now
                self._launch(now)

    def _tick(self) -> None:
        coins = [coin for coin in self.book.coins.values() if not coin.graduated]
        if not coins:
            return
        coin = self._random.choice(coins)
        price = coin.price_sol or (INITIAL_VIRTUAL_SOL / INITIAL_VIRTUAL_TOKEN)
        is_buy = self._random.random() < 0.56
        sol = self._random.choices(_SIZES, weights=_WEIGHTS, k=1)[0]
        tokens = sol / price if price > 0 else 1.0
        if is_buy:
            coin.virtual_sol += sol
            coin.virtual_token = max(1.0, coin.virtual_token - tokens)
            coin.real_sol += sol
            coin.real_token = max(0.0, coin.real_token - tokens)
        else:
            coin.virtual_sol = max(1.0, coin.virtual_sol - sol)
            coin.virtual_token += tokens
            coin.real_sol = max(0.0, coin.real_sol - sol)
            coin.real_token += tokens
        price = coin.virtual_sol / coin.virtual_token if coin.virtual_token > 0 else price
        now = time.time()
        self._seq += 1
        trade = TradeEvent(
            signature=f"demo-{self._seq}",
            mint=coin.mint,
            user=_wallet(),
            is_buy=is_buy,
            sol=sol,
            tokens=tokens,
            tokens_raw=int(tokens * TOKEN_SCALE),
            price_sol=price,
            timestamp=int(now),
            seen_ts=now,
            virtual_sol=coin.virtual_sol,
            virtual_token=coin.virtual_token,
            real_sol=coin.real_sol,
            real_token=coin.real_token,
        )
        self.book.apply_trade(trade)
        self._emit(trade)
        if coin.real_token <= 0 or coin.curve_progress_pct() >= 100.0:
            coin.graduated = True
            coin.graduated_ts = now
            complete = CompleteEvent(
                signature=f"demo-complete-{coin.mint[-6:]}",
                mint=coin.mint,
                user=trade.user,
                bonding_curve=coin.bonding_curve,
                timestamp=int(now),
                seen_ts=now,
            )
            self.book.apply_complete(complete)
            self._emit(complete)

    def _launch(self, now: float) -> None:
        name, symbol = self._random.choice(_LAUNCH_NAMES)
        suffix = str(self._random.randint(1, 99))
        mint = _mint_for(symbol + suffix)
        creation = CreateEvent(
            signature=f"demo-create-{self._seq}",
            mint=mint,
            name=name,
            symbol=symbol + suffix,
            uri="",
            creator=_wallet(),
            bonding_curve=_mint_for(symbol + suffix + "-curve"),
            timestamp=int(now),
            seen_ts=now,
            virtual_sol=INITIAL_VIRTUAL_SOL,
            virtual_token=INITIAL_VIRTUAL_TOKEN,
            real_token=INITIAL_REAL_TOKEN,
            total_supply=1_000_000_000.0,
        )
        self.book.apply_create(creation)
        self._emit(creation)
        self.book.set_metadata(
            mint,
            {
                "image": None,
                "description": "Freshly launched. The chart has not decided yet.",
                "twitter": None,
                "telegram": None,
                "website": None,
                "created_on": None,
            },
        )

    def _emit(self, event: Any) -> None:
        if self.on_event is None:
            return
        result = self.on_event(event)
        if inspect.isawaitable(result):
            with contextlib.suppress(RuntimeError):
                asyncio.ensure_future(result)

    async def stop(self) -> None:
        self._stop.set()


_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_WALLET_RNG = random.Random(42)


def _wallet() -> str:
    # Deterministic but plausible-looking base58 wallet for the demo.
    return "".join(_WALLET_RNG.choice(_ALPHABET) for _ in range(44))


def _mint_for(seed: str) -> str:
    rng = random.Random(seed)
    return "".join(rng.choice(_ALPHABET) for _ in range(44))
