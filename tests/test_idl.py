from __future__ import annotations

import base64
import json
import struct
from pathlib import Path

from pumpfire.constants import PUMP_AMM_PROGRAM, PUMP_PROGRAM, WSOL_MINT
from pumpfire.decoder import decode_log_line, decode_notification
from pumpfire.idl import default_registry
from pumpfire.models import CoinBook, RawEvent

FIXTURES = Path(__file__).parent / "fixtures"


def test_registry_loads_both_programs() -> None:
    summary = default_registry().summary()
    assert summary["pump"]["address"] == PUMP_PROGRAM
    assert summary["pump_amm"]["address"] == PUMP_AMM_PROGRAM
    assert summary["pump"]["events"] >= 30
    assert summary["pump_amm"]["events"] >= 25
    assert summary["pump"]["types"] >= 40
    assert summary["pump_amm"]["types"] >= 35


def test_set_params_event_via_idl() -> None:
    discriminator = bytes.fromhex("dfc39ff63e308f83")
    body = (
        struct.pack("<Q", 1) * 6
        + b"\x02" * 32
        + b"\x01"
        + struct.pack("<Q", 2)
        + struct.pack("<Q", 3)
        + b"\x03" * 32 * 8
        + struct.pack("<q", 1_700_000_000)
        + b"\x04" * 32
        + b"\x05" * 32
    )
    line = "Program data: " + base64.b64encode(discriminator + body).decode()
    batch = decode_log_line(line, program="pump")
    assert len(batch.raw_events) == 1
    event = batch.raw_events[0]
    assert event.program == "pump"
    assert event.name == "SetParamsEvent"
    assert event.fields["initial_virtual_token_reserves"] == 1
    assert event.fields["withdraw_authority"] == "8qbHbw2BbbTHBW1sbeqakYXVKRQM8Ne7pLK7m6CVfeR"
    assert event.fields["enable_migrate"] is True
    assert len(event.fields["fee_recipients"]) == 8
    assert event.fields["timestamp"] == 1_700_000_000


def test_live_amm_sell_fixture_decodes() -> None:
    record = json.loads((FIXTURES / "amm_sell.json").read_text())
    batch = decode_notification(record, seen_ts=1793830309.0)
    assert batch.trades == []
    assert len(batch.raw_events) == 1
    event = batch.raw_events[0]
    assert event.program == "pump_amm"
    assert event.name == "SellEvent"
    assert event.fields["pool"] == "EMSoAeXPAEvLGe7Pj2eGfEHiyJbWxw6SvN4qQsEHzzgk"
    assert event.fields["quote_amount_out"] == 3375384347224
    assert event.extra_bytes >= 0


def test_amm_sell_maps_to_trade_once_pool_is_known() -> None:
    book = CoinBook()
    book.apply_raw(
        RawEvent(
            program="pump_amm",
            name="CreatePoolEvent",
            signature="pool-sig",
            slot=1,
            seen_ts=1_700_000_000.0,
            fields={
                "timestamp": 1_700_000_000,
                "pool": "POOL",
                "base_mint": "MINT",
                "base_mint_decimals": 6,
                "quote_mint": WSOL_MINT,
                "quote_mint_decimals": 9,
            },
        )
    )
    book.apply_raw(
        RawEvent(
            program="pump_amm",
            name="SellEvent",
            signature="sell-sig",
            slot=1,
            seen_ts=1_700_000_001.0,
            fields={
                "timestamp": 1_700_000_001,
                "base_amount_in": 1_000_000,
                "quote_amount_out": 2_000_000_000,
                "pool_base_token_reserves": 100_000_000,
                "pool_quote_token_reserves": 50_000_000_000,
                "lp_fee": 1_000_000,
                "protocol_fee": 500_000,
                "coin_creator_fee": 250_000,
                "pool": "POOL",
                "user": "USER",
                "coin_creator": "CREATOR",
                "base_supply": 1_000_000_000_000_000,
            },
        )
    )
    assert book.stats.pools == 1
    assert book.stats.amm_trades == 1
    assert book.stats.trades == 1
    coin = book.get("MINT")
    assert coin is not None
    assert coin.venue == "pumpswap"
    assert coin.pool == "POOL"
    assert coin.sells == 1
    assert abs(coin.volume_sol - 2.0) < 1e-9
    trade = book.recent_trades(1)[0]
    assert trade.venue == "pumpswap"
    assert trade.sol == 2.0
    assert trade.tokens == 1.0
    assert trade.price_sol == 0.5
    assert trade.fee_sol == 0.00175


def test_migration_event_registers_pool() -> None:
    book = CoinBook()
    book.apply_raw(
        RawEvent(
            program="pump",
            name="CompletePumpAmmMigrationEvent",
            signature="migration-sig",
            slot=1,
            seen_ts=1_700_000_000.0,
            fields={
                "mint": "MINT",
                "pool": "POOL",
                "quote_mint": WSOL_MINT,
                "timestamp": 1_700_000_000,
            },
        )
    )
    assert "POOL" in book.pools
    assert book.pools["POOL"].base_mint == "MINT"
    assert book.get("MINT").graduated is True
    assert book.get("MINT").pool == "POOL"


def test_non_sol_quote_pool_is_raw_only() -> None:
    book = CoinBook()
    book.apply_raw(
        RawEvent(
            program="pump_amm",
            name="CreatePoolEvent",
            signature="pool-sig",
            slot=1,
            seen_ts=1_700_000_000.0,
            fields={
                "timestamp": 1_700_000_000,
                "pool": "POOL",
                "base_mint": "MINT",
                "base_mint_decimals": 6,
                "quote_mint": "USDC",
                "quote_mint_decimals": 6,
            },
        )
    )
    book.apply_raw(
        RawEvent(
            program="pump_amm",
            name="BuyEvent",
            signature="buy-sig",
            slot=1,
            seen_ts=1_700_000_001.0,
            fields={
                "timestamp": 1_700_000_001,
                "base_amount_out": 1_000_000,
                "quote_amount_in": 1_000_000,
                "pool": "POOL",
                "user": "USER",
            },
        )
    )
    assert book.stats.pools == 1
    assert book.stats.amm_trades == 0
    assert book.stats.trades == 0
    assert len(book.raw_events) == 2
