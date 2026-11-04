from __future__ import annotations

import time

from pumpfire.constants import INITIAL_REAL_TOKEN, INITIAL_VIRTUAL_SOL, INITIAL_VIRTUAL_TOKEN
from pumpfire.models import CoinBook, CompleteEvent, CreateEvent, TradeEvent


def trade(
    mint: str = "M1",
    *,
    user: str = "W1",
    is_buy: bool = True,
    sol: float = 1.0,
    price: float = 1e-7,
    ts: float | None = None,
    virtual_sol: float | None = None,
    virtual_token: float | None = None,
    real_sol: float | None = None,
    real_token: float | None = None,
) -> TradeEvent:
    return TradeEvent(
        signature=f"sig-{time.time_ns()}",
        mint=mint,
        user=user,
        is_buy=is_buy,
        sol=sol,
        tokens=sol / price if price else 0.0,
        tokens_raw=int((sol / price) * 1e6) if price else 0,
        price_sol=price,
        timestamp=int(ts or time.time()),
        seen_ts=ts or time.time(),
        virtual_sol=virtual_sol,
        virtual_token=virtual_token,
        real_sol=real_sol,
        real_token=real_token,
    )


def creation(mint: str = "M1", ts: float | None = None) -> CreateEvent:
    return CreateEvent(
        signature="create-sig",
        mint=mint,
        name="Test Coin",
        symbol="TEST",
        uri="ipfs://test",
        creator="CREATOR",
        bonding_curve="CURVE",
        timestamp=int(ts or time.time()),
        seen_ts=ts or time.time(),
        virtual_sol=INITIAL_VIRTUAL_SOL,
        virtual_token=INITIAL_VIRTUAL_TOKEN,
        real_token=INITIAL_REAL_TOKEN,
        total_supply=1_000_000_000.0,
    )


def test_create_sets_launch_state() -> None:
    book = CoinBook(sol_usd=200.0)
    coin = book.apply_create(creation())
    assert coin.name == "Test Coin"
    assert coin.symbol == "TEST"
    assert coin.price_sol > 0
    assert coin.market_cap_sol > 0
    assert coin.curve_progress_pct() == 0.0
    assert coin.volume_sol == 0.0
    assert book.stats.creates == 1


def test_trade_aggregation() -> None:
    book = CoinBook(sol_usd=200.0)
    book.apply_create(creation())
    now = time.time()
    book.apply_trade(trade(user="W1", is_buy=True, sol=2.0, price=2e-7, ts=now))
    book.apply_trade(trade(user="W2", is_buy=True, sol=3.0, price=3e-7, ts=now))
    book.apply_trade(trade(user="W1", is_buy=False, sol=1.0, price=2.5e-7, ts=now))
    coin = book.get("M1")
    assert coin is not None
    assert coin.buys == 2
    assert coin.sells == 1
    assert coin.volume_sol == 6.0
    assert coin.buy_sol == 5.0
    assert coin.sell_sol == 1.0
    assert len(coin.unique_buyers) == 2
    assert len(coin.unique_sellers) == 1
    assert coin.trade_count == 3
    assert coin.price_sol == 2.5e-7
    assert coin.market_cap_sol == 2.5e-7 * 1_000_000_000.0


def test_reserves_drive_price_and_curve() -> None:
    book = CoinBook()
    book.apply_create(creation())
    coin = book.apply_trade(
        trade(
            price=1e-7,
            virtual_sol=60.0,
            virtual_token=800_000_000.0,
            real_sol=30.0,
            real_token=600_000_000.0,
        )
    )
    assert abs(coin.price_sol - 60.0 / 800_000_000.0) < 1e-18
    assert abs(coin.curve_progress_pct() - (1 - 600_000_000.0 / 793_100_000.0) * 100) < 0.01


def test_windows() -> None:
    book = CoinBook()
    now = 1_700_000_000.0
    book.apply_create(creation(ts=now - 600))
    book.apply_trade(trade(sol=1.0, price=1e-7, ts=now - 300))
    book.apply_trade(trade(sol=2.0, price=2e-7, ts=now - 30))
    coin = book.get("M1")
    assert coin is not None
    assert abs(coin.volume_window(60, now) - 2.0) < 1e-9
    assert abs(coin.volume_window(600, now) - 3.0) < 1e-9
    assert abs(coin.change_window(60, now) - 100.0) < 1e-6
    buys, sells = coin.flow_window(60, now)
    assert buys == 1 and sells == 0


def test_eviction_is_lru() -> None:
    book = CoinBook(max_coins=2)
    book.apply_trade(trade("A", ts=1.0))
    book.apply_trade(trade("B", ts=2.0))
    book.apply_trade(trade("A", ts=3.0))
    book.apply_trade(trade("C", ts=4.0))
    assert set(book.coins) == {"A", "C"}


def test_top_sorting_and_search() -> None:
    book = CoinBook(sol_usd=100.0)
    book.apply_create(creation("M1"))
    book.apply_create(creation("M2"))
    book.get("M1").symbol = "AAA"
    book.get("M2").symbol = "BBB"
    book.get("M1").market_cap_sol = 100.0
    book.get("M2").market_cap_sol = 200.0
    assert [c.mint for c in book.top(2, sort="mcap")] == ["M2", "M1"]
    assert [c.mint for c in book.top(2, query="aaa")] == ["M1"]
    assert [c.mint for c in book.top(2, sort="volume")] == ["M1", "M2"]


def test_complete_marks_graduated() -> None:
    book = CoinBook()
    book.apply_create(creation())
    book.apply_complete(
        CompleteEvent(
            signature="done",
            mint="M1",
            user="W",
            bonding_curve="C",
            timestamp=1_700_000_000,
            seen_ts=1_700_000_000,
        )
    )
    assert book.get("M1").graduated is True
    assert book.stats.completes == 1
    assert book.graduated_count() == 1


def test_snapshot_shape() -> None:
    book = CoinBook(sol_usd=150.0)
    book.apply_create(creation())
    book.apply_trade(trade(sol=1.0))
    snapshot = book.snapshot(coins=10, trades=10, creates=10)
    assert snapshot["type"] == "snapshot"
    assert snapshot["stats"]["coins"] == 1
    assert snapshot["coins"][0]["mint"] == "M1"
    assert snapshot["trades"][0]["sol"] == 1.0
    assert snapshot["creates"][0]["symbol"] == "TEST"
    assert snapshot["sol_price"]["usd"] == 150.0


def test_metadata_is_attached() -> None:
    book = CoinBook()
    book.apply_create(creation())
    book.set_metadata("M1", {"image": "https://example.com/a.png", "description": "hi"})
    assert book.get("M1").metadata["description"] == "hi"
