from __future__ import annotations

import time

from fastapi.testclient import TestClient

from pumpfire.api import create_app
from pumpfire.models import CoinBook, CompleteEvent, CreateEvent, TradeEvent


def make_book() -> CoinBook:
    book = CoinBook(sol_usd=200.0)
    now = time.time()
    book.apply_create(
        CreateEvent(
            signature="create",
            mint="MINT1",
            name="Test Coin",
            symbol="TEST",
            uri="ipfs://x",
            creator="CREATOR",
            bonding_curve="CURVE",
            timestamp=int(now) - 60,
            seen_ts=now - 60,
            virtual_sol=30.0,
            virtual_token=1_073_000_000.0,
            real_token=793_100_000.0,
            total_supply=1_000_000_000.0,
        )
    )
    book.apply_trade(
        TradeEvent(
            signature="trade1",
            mint="MINT1",
            user="WALLET1",
            is_buy=True,
            sol=2.0,
            tokens=10_000_000.0,
            tokens_raw=10_000_000_000_000,
            price_sol=2e-7,
            timestamp=int(now),
            seen_ts=now,
            virtual_sol=32.0,
            virtual_token=1_063_000_000.0,
            real_sol=2.0,
            real_token=783_100_000.0,
        )
    )
    book.apply_complete(
        CompleteEvent(
            signature="complete",
            mint="MINT1",
            user="WALLET1",
            bonding_curve="CURVE",
            timestamp=int(now),
            seen_ts=now,
        )
    )
    return book


def client() -> TestClient:
    return TestClient(create_app(make_book()))


def test_health() -> None:
    response = client().get("/health")
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_stats() -> None:
    response = client().get("/v1/stats")
    assert response.status_code == 200
    payload = response.json()
    assert payload["stats"]["coins"] == 1
    assert payload["sol_price"]["usd"] == 200.0


def test_coins() -> None:
    response = client().get("/v1/coins")
    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    coin = payload["coins"][0]
    assert coin["mint"] == "MINT1"
    assert coin["symbol"] == "TEST"
    assert coin["graduated"] is True


def test_coin_detail_and_trades() -> None:
    response = client().get("/v1/coins/MINT1?trades=10")
    assert response.status_code == 200
    payload = response.json()
    assert payload["mint"] == "MINT1"
    assert len(payload["trades"]) == 1
    assert payload["trades"][0]["side"] == "buy"


def test_coin_not_found() -> None:
    assert client().get("/v1/coins/NOPE").status_code == 404
    assert client().get("/v1/coins/NOPE/trades").status_code == 404


def test_tape_filters() -> None:
    response = client().get("/v1/trades?side=buy&min_sol=1")
    assert response.status_code == 200
    assert response.json()["count"] == 1
    assert client().get("/v1/trades?side=sell").json()["count"] == 0
    assert client().get("/v1/trades?min_sol=99").json()["count"] == 0


def test_creates_and_completes() -> None:
    assert client().get("/v1/creates").json()["count"] == 1
    assert client().get("/v1/completes").json()["count"] == 1


def test_dashboard_html() -> None:
    response = client().get("/")
    assert response.status_code == 200
    assert "PUMP" in response.text
    assert "pump.fun" in response.text


def test_websocket_snapshot() -> None:
    with client().websocket_connect("/ws") as ws:
        payload = ws.receive_json()
    assert payload["type"] == "snapshot"
    assert payload["coins"][0]["mint"] == "MINT1"
