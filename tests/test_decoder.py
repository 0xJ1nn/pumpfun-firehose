from __future__ import annotations

import base64
import struct

from pumpfire.base58 import b58decode, b58encode
from pumpfire.constants import DISC_CREATE, DISC_TRADE
from pumpfire.decoder import (
    decode_log_line,
    decode_logs,
    decode_notification,
    decode_program_data,
)
from tests.helpers import (
    CREATOR,
    MINT,
    USER,
    complete_payload,
    create_payload,
    trade_payload,
)


def test_base58_round_trip() -> None:
    assert b58decode(b58encode(MINT)) == MINT
    assert b58encode(b"\x00\x00\x01") == "112"


def test_trade_core_fields() -> None:
    batch = decode_log_line(trade_payload())
    assert len(batch.trades) == 1
    trade = batch.trades[0]
    assert trade.mint == b58encode(MINT)
    assert trade.user == b58encode(USER)
    assert trade.is_buy is True
    assert trade.sol == 1.5
    assert trade.tokens == 10_000.0
    assert trade.timestamp == 1_700_000_000
    assert trade.virtual_sol == 45.0
    assert trade.virtual_token == 900_000_000.0
    assert trade.real_sol == 15.0
    assert trade.real_token == 700_000_000.0
    # 45 SOL / 900M tokens
    assert trade.price_sol is not None
    assert abs(trade.price_sol - (45.0 / 900_000_000.0)) < 1e-18


def test_trade_extended_fee_fields() -> None:
    batch = decode_log_line(trade_payload(extended=True))
    trade = batch.trades[0]
    assert trade.fee_sol == 0.0015
    assert trade.creator == b58encode(CREATOR)
    assert trade.creator_fee_sol == 0.00075


def test_trade_sell_and_fallback_price() -> None:
    batch = decode_log_line(
        trade_payload(
            is_buy=False,
            sol_lamports=2_000_000_000,
            token_raw=8_000_000_000,
            virtual_sol=0,
            virtual_token=0,
        )
    )
    trade = batch.trades[0]
    assert trade.is_buy is False
    assert trade.sol == 2.0
    assert trade.price_sol == 2.0 / 8_000.0


def test_create_legacy() -> None:
    batch = decode_log_line(create_payload())
    assert len(batch.creates) == 1
    creation = batch.creates[0]
    assert creation.name == "Test Coin"
    assert creation.symbol == "TEST"
    assert creation.uri == "ipfs://bafy-test"
    assert creation.mint == b58encode(MINT)
    assert creation.creator == b58encode(USER)
    assert creation.bonding_curve == b58encode(b"\x04" * 32)
    assert creation.virtual_sol is None


def test_create_extended() -> None:
    batch = decode_log_line(create_payload(extended=True))
    creation = batch.creates[0]
    assert creation.creator == b58encode(CREATOR)
    assert creation.timestamp == 1_700_000_000
    assert creation.virtual_sol == 30.0
    assert creation.virtual_token == 1_073_000_000.0
    assert creation.real_token == 793_100_000.0
    assert creation.total_supply == 1_000_000_000.0


def test_create_rejects_absurd_launch_state() -> None:
    batch = decode_log_line(create_payload(extended=True, virtual_sol=10_000_000))
    creation = batch.creates[0]
    assert creation.virtual_sol is None
    assert creation.virtual_token == 1_073_000_000.0


def test_complete() -> None:
    batch = decode_log_line(complete_payload())
    assert len(batch.completes) == 1
    complete = batch.completes[0]
    assert complete.mint == b58encode(MINT)
    assert complete.user == b58encode(USER)
    assert complete.timestamp == 1_700_000_100


def test_multiple_events_in_one_transaction() -> None:
    batch = decode_logs([create_payload(), trade_payload(), complete_payload()])
    assert len(batch.creates) == 1
    assert len(batch.trades) == 1
    assert len(batch.completes) == 1
    assert len(batch.events) == 3


def test_malformed_is_dropped_not_guessed() -> None:
    assert decode_log_line("Program data: !!!not-base64!!!").events == []
    short = "Program data: " + base64.b64encode(DISC_TRADE + b"\x00" * 10).decode()
    assert decode_log_line(short).events == []
    wrong = "Program data: " + base64.b64encode(b"\x00" * 200).decode()
    assert decode_log_line(wrong).events == []
    assert decode_log_line("not a program data line").events == []


def test_decode_program_data() -> None:
    raw = decode_program_data(trade_payload())
    assert raw is not None
    assert raw[:8] == DISC_TRADE
    assert decode_program_data("nope") is None


def test_decode_notification_ignores_failed_transactions() -> None:
    value = {
        "signature": "sig",
        "slot": 42,
        "err": {"InstructionError": [0, "Custom"]},
        "logs": [trade_payload()],
    }
    assert decode_notification(value).events == []
    value.pop("err")
    batch = decode_notification(value)
    assert len(batch.trades) == 1
    assert batch.trades[0].slot == 42
    assert batch.trades[0].signature == "sig"


def test_create_event_with_empty_strings() -> None:
    batch = decode_log_line(create_payload(name="", symbol="", uri=""))
    creation = batch.creates[0]
    assert creation.name == ""
    assert creation.symbol == ""
    assert creation.uri == ""


def test_trade_body_length_guard() -> None:
    # A body shorter than the 121-byte core must not produce a trade.
    body = b"\x01" * 32 + struct.pack("<Q", 1) + struct.pack("<Q", 1)
    line = "Program data: " + base64.b64encode(DISC_TRADE + body).decode()
    assert decode_log_line(line).events == []


def test_create_discriminator_is_correct() -> None:
    # sha256("event:CreateEvent")[:8]
    assert DISC_CREATE.hex() == "1b72a94ddeeb6376"
