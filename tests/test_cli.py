from __future__ import annotations

from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from pumpfire.cli import _read_watchlist, app
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
