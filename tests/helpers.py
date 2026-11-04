from __future__ import annotations

import base64
import struct

from pumpfire.constants import DISC_COMPLETE, DISC_CREATE, DISC_TRADE

MINT = b"\x01" * 32
USER = b"\x02" * 32
CREATOR = b"\x03" * 32
CURVE = b"\x04" * 32
FEE_RECIPIENT = b"\x05" * 32


def anchor_string(value: str) -> bytes:
    raw = value.encode()
    return struct.pack("<I", len(raw)) + raw


def trade_payload(
    *,
    mint: bytes = MINT,
    user: bytes = USER,
    sol_lamports: int = 1_500_000_000,
    token_raw: int = 10_000_000_000,
    is_buy: bool = True,
    timestamp: int = 1_700_000_000,
    virtual_sol: int = 45_000_000_000,
    virtual_token: int = 900_000_000_000_000,
    real_sol: int = 15_000_000_000,
    real_token: int = 700_000_000_000_000,
    extended: bool = False,
    fee_lamports: int = 1_500_000,
    creator_fee_lamports: int = 750_000,
    creator: bytes = CREATOR,
) -> str:
    body = (
        mint
        + struct.pack("<Q", sol_lamports)
        + struct.pack("<Q", token_raw)
        + bytes([1 if is_buy else 0])
        + user
        + struct.pack("<q", timestamp)
        + struct.pack("<Q", virtual_sol)
        + struct.pack("<Q", virtual_token)
        + struct.pack("<Q", real_sol)
        + struct.pack("<Q", real_token)
    )
    if extended:
        body += (
            FEE_RECIPIENT
            + struct.pack("<Q", 95)
            + struct.pack("<Q", fee_lamports)
            + creator
            + struct.pack("<Q", 30)
            + struct.pack("<Q", creator_fee_lamports)
            + bytes([1])
            + struct.pack("<Q", 0)
            + struct.pack("<Q", 0)
            + struct.pack("<Q", sol_lamports)
            + struct.pack("<q", timestamp)
        )
    return "Program data: " + base64.b64encode(DISC_TRADE + body).decode()


def create_payload(
    *,
    name: str = "Test Coin",
    symbol: str = "TEST",
    uri: str = "ipfs://bafy-test",
    mint: bytes = MINT,
    curve: bytes = CURVE,
    user: bytes = USER,
    extended: bool = False,
    creator: bytes = CREATOR,
    timestamp: int = 1_700_000_000,
    virtual_token: int = 1_073_000_000_000_000,
    virtual_sol: int = 30_000_000_000,
    real_token: int = 793_100_000_000_000,
    total_supply: int = 1_000_000_000_000_000,
) -> str:
    body = anchor_string(name) + anchor_string(symbol) + anchor_string(uri) + mint + curve + user
    if extended:
        body += (
            creator
            + struct.pack("<q", timestamp)
            + struct.pack("<Q", virtual_token)
            + struct.pack("<Q", virtual_sol)
            + struct.pack("<Q", real_token)
            + struct.pack("<Q", total_supply)
        )
    return "Program data: " + base64.b64encode(DISC_CREATE + body).decode()


def complete_payload(
    *,
    user: bytes = USER,
    mint: bytes = MINT,
    curve: bytes = CURVE,
    timestamp: int = 1_700_000_100,
) -> str:
    body = user + mint + curve + struct.pack("<q", timestamp)
    return "Program data: " + base64.b64encode(DISC_COMPLETE + body).decode()
