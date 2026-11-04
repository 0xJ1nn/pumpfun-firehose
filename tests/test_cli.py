from __future__ import annotations

from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from pumpfire.cli import _matches_event, _read_watchlist, app
from pumpfire.models import CreateEvent, TradeEvent
from tests.helpers import trade_payload

runner = CliRunner()


def test_read_watchlist(tmp_path: Path) -> None:
    path = tmp_path / "mints.txt"
    path.write_text(
        "# watch these\n"
        "MintOne\n"
        "\n"
        "  MintTwo  \n"
        "# MintThree\n",
        encoding="utf-8",
    )
    assert _read_watchlist(path) == {"MintOne", "MintTwo"}


def test_read_watchlist_missing(tmp_path: Path) -> None:
    with pytest.raises(typer.BadParameter):
        _read_watchlist(tmp_path / "missing.txt")


def _trade(mint: str = "A", *, sol: float = 1.0, is_buy: bool = True) -> TradeEvent:
    return TradeEvent(
        signature="sig",
        mint=mint,
        user="W",
        is_buy=is_buy,
        sol=sol,
        tokens=1.0,
        tokens_raw=1_000_000,
        price_sol=1e-7,
        timestamp=1,
    )


def _create(mint: str = "A") -> CreateEvent:
    return CreateEvent(
        signature="sig",
        mint=mint,
        name="N",
        symbol="S",
        uri="",
        creator="C",
        bonding_curve="B",
        timestamp=1,
    )


def test_matches_event_filters_all_event_kinds_by_mint() -> None:
    mints = {"A"}
    assert _matches_event(_create("A"), mints=mints, min_sol=0, buys_only=False, sells_only=False)
    assert not _matches_event(_create("B"), mints=mints, min_sol=0, buys_only=False, sells_only=False)
    assert _matches_event(_trade("A"), mints=mints, min_sol=0, buys_only=False, sells_only=False)
    assert not _matches_event(_trade("B"), mints=mints, min_sol=0, buys_only=False, sells_only=False)


def test_matches_event_trade_filters() -> None:
    mints: set[str] = set()
    assert not _matches_event(_trade(sol=1.0), mints=mints, min_sol=2.0, buys_only=False, sells_only=False)
    assert _matches_event(_trade(sol=2.0), mints=mints, min_sol=2.0, buys_only=False, sells_only=False)
    assert not _matches_event(
        _trade(is_buy=False), mints=mints, min_sol=0, buys_only=True, sells_only=False
    )
    assert not _matches_event(
        _trade(is_buy=True), mints=mints, min_sol=0, buys_only=False, sells_only=True
    )


def test_stream_help_has_new_options() -> None:
    result = runner.invoke(app, ["stream", "--help"])
    assert result.exit_code == 0
    for option in ("--mint", "--watchlist", "--out", "--metadata"):
        assert option in result.output


def test_api_help_has_no_ui() -> None:
    result = runner.invoke(app, ["api", "--help"])
    assert result.exit_code == 0
    assert "--no-ui" in result.output


def test_out_requires_json() -> None:
    result = runner.invoke(app, ["stream", "--out", "trades.jsonl"])
    assert result.exit_code != 0
    assert "--out requires --json" in result.output


def test_decode_command() -> None:
    result = runner.invoke(app, ["decode", "--log", trade_payload(), "--compact"])
    assert result.exit_code == 0
    assert '"kind":"trade"' in result.output
    assert '"mint":"' in result.output
