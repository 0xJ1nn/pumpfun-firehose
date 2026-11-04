from __future__ import annotations

import subprocess
import sys

BLOCK_OPTIONAL = """
import builtins
import sys

blocked = {"fastapi", "uvicorn", "httpx", "aiosqlite", "typer", "rich"}
real_import = builtins.__import__


def fake_import(name, *args, **kwargs):
    if name.split(".")[0] in blocked:
        raise ImportError("blocked optional dependency: " + name)
    return real_import(name, *args, **kwargs)


builtins.__import__ = fake_import
import pumpfire
from pumpfire import CoinBook, Firehose, TradeEvent
import pumpfire.store
from pumpfire.store import export_table, table_counts

print("core-ok")
"""


def _run(source: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", source],
        capture_output=True,
        text=True,
    )


def test_core_import_does_not_need_optional_dependencies() -> None:
    result = _run(BLOCK_OPTIONAL)
    assert result.returncode == 0, result.stderr
    assert "core-ok" in result.stdout


def test_star_import_stays_core_only() -> None:
    source = BLOCK_OPTIONAL + "\nexec('from pumpfire import *')\nprint('star-ok')\n"
    result = _run(source)
    assert result.returncode == 0, result.stderr
    assert "star-ok" in result.stdout


def test_optional_helpers_are_available_when_installed() -> None:
    from pumpfire import MetadataFetcher, SolPrice, SQLiteStore

    assert MetadataFetcher.__name__ == "MetadataFetcher"
    assert SolPrice.__name__ == "SolPrice"
    assert SQLiteStore.__name__ == "SQLiteStore"


def test_unknown_attribute_raises() -> None:
    import pumpfire

    name = "not_a_real_thing"
    try:
        getattr(pumpfire, name)
    except AttributeError:
        pass
    else:  # pragma: no cover
        raise AssertionError("unknown attribute should raise AttributeError")
