from __future__ import annotations

import json
from pathlib import Path

import pytest

from pumpfire import CoinBook, Firehose
from pumpfire.backfill import decode_record, load_fixture, replay_fixture
from pumpfire.models import CreateEvent, TradeEvent

FIXTURE = Path(__file__).parent / "fixtures" / "synthetic_notifications.json"


def test_fixture_is_marked_synthetic_not_real_traffic() -> None:
    records = json.loads(FIXTURE.read_text(encoding="utf-8"))
    marker = records[0]["_fixture"]

    assert marker["kind"] == "synthetic"
    assert "not real traffic" in marker["note"].lower()


def test_load_fixture_separates_provenance_from_records() -> None:
    meta, records = load_fixture(FIXTURE)

    assert meta["kind"] == "synthetic"
    assert len(records) == 2
    assert all("_fixture" not in record for record in records)


def test_fixture_replays_through_the_shared_decode_path(mint_str: str) -> None:
    book = CoinBook()

    stats = replay_fixture(FIXTURE, book)

    assert stats.records == 2
    assert stats.transactions == 2
    assert stats.creates == 1
    assert stats.trades == 1
    assert stats.errors == 0

    coin = book.get(mint_str)
    assert coin is not None
    assert coin.symbol == "TEST"
    assert isinstance(book.creates[0], CreateEvent)
    trade = book.trades[0]
    assert isinstance(trade, TradeEvent)
    assert trade.signature == "5SYNTHETICtrade00000000000000000000000000000"
    assert trade.mint == mint_str
    assert trade.is_buy is True
    assert trade.sol > 0
    assert book.stats.trades == 1


def test_replaying_a_synthetic_fixture_warns_about_provenance() -> None:
    book = CoinBook()

    with pytest.warns(UserWarning, match="synthetic"):
        stats = replay_fixture(FIXTURE, book)

    assert stats.records == 2


def test_fixture_decode_matches_the_live_ingest_path() -> None:
    _, records = load_fixture(FIXTURE)
    replayed = decode_record(records[1])

    firehose = Firehose(CoinBook())
    firehose._handle_raw(json.dumps(records[1]))

    live_trade = firehose.book.trades[0]
    replay_trade = replayed.trades[0]
    assert live_trade.signature == replay_trade.signature
    assert live_trade.mint == replay_trade.mint
    assert live_trade.user == replay_trade.user
    assert live_trade.sol == replay_trade.sol
    assert live_trade.tokens_raw == replay_trade.tokens_raw
    assert live_trade.is_buy == replay_trade.is_buy
    assert live_trade.timestamp == replay_trade.timestamp
