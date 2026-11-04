"""PumpFire — every pump.fun trade, decoded straight from the chain.

The core package has one dependency: ``websockets``. The dashboard, HTTP API,
metadata fetcher, SOL price feed, and SQLite sink are optional extras and are
imported lazily, so a library user never pays for a frontend they do not use.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

__version__ = "0.3.0"

from .decoder import decode_log_line, decode_logs, decode_notification
from .firehose import Firehose
from .idl import IdlRegistry
from .models import (
    Coin,
    CoinBook,
    CompleteEvent,
    CreateEvent,
    DecodedBatch,
    RawEvent,
    TradeEvent,
)

if TYPE_CHECKING:  # optional helpers, available only with the matching extra
    from .metadata import MetadataFetcher as MetadataFetcher
    from .solprice import SolPrice as SolPrice
    from .store import SQLiteStore as SQLiteStore

_LAZY = {
    "MetadataFetcher": (".metadata", "MetadataFetcher", "api"),
    "SolPrice": (".solprice", "SolPrice", "api"),
    "SQLiteStore": (".store", "SQLiteStore", "api"),
}

__all__ = [
    "Coin",
    "CoinBook",
    "CompleteEvent",
    "CreateEvent",
    "DecodedBatch",
    "Firehose",
    "IdlRegistry",
    "RawEvent",
    "TradeEvent",
    "decode_log_line",
    "decode_logs",
    "decode_notification",
    "__version__",
]


def __getattr__(name: str) -> Any:
    """Import optional helpers only when they are actually requested."""
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    module_name, attribute, extra = target
    try:
        module = import_module(module_name, __name__)
    except ImportError as exc:
        raise ModuleNotFoundError(
            f"pumpfire.{name} needs optional dependencies. "
            f"Install with: pip install 'pumpfun-firehose[{extra}]'"
        ) from exc
    return getattr(module, attribute)
