"""PumpFire — every pump.fun trade, decoded straight from the chain."""

from __future__ import annotations

__version__ = "0.1.0"

from .decoder import decode_log_line, decode_logs, decode_notification
from .firehose import Firehose
from .metadata import MetadataFetcher
from .models import Coin, CoinBook, CompleteEvent, CreateEvent, DecodedBatch, TradeEvent
from .solprice import SolPrice
from .store import SQLiteStore

__all__ = [
    "Coin",
    "CoinBook",
    "CompleteEvent",
    "CreateEvent",
    "DecodedBatch",
    "Firehose",
    "MetadataFetcher",
    "SQLiteStore",
    "SolPrice",
    "TradeEvent",
    "decode_log_line",
    "decode_logs",
    "decode_notification",
    "__version__",
]
