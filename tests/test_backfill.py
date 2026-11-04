from __future__ import annotations

import base64
import json

import httpx
import pytest
from typer.testing import CliRunner

from pumpfire.backfill import (
    RpcClient,
    RpcError,
    backfill_events,
    decode_record,
    read_records,
    replay_records,
)
from pumpfire.cli import app
from pumpfire.constants import PUMP_PROGRAM
from pumpfire.decoder import EVENT_IX_TAG, decode_transaction
from pumpfire.models import CoinBook
from tests.helpers import trade_payload


def trade_record() -> dict:
    return {
        "signature": "trade-sig",
        "slot": 42,
        "logs": [
            f"Program {PUMP_PROGRAM} invoke [1]",
            trade_payload(),
            f"Program {PUMP_PROGRAM} success",
        ],
    }


def transaction_record() -> dict:
    return {
        "slot": 42,
        "blockTime": 1_700_000_000,
        "transaction": {
            "signatures": ["trade-sig"],
            "message": {"accountKeys": [PUMP_PROGRAM]},
        },
        "meta": {
            "logMessages": [
                f"Program {PUMP_PROGRAM} invoke [1]",
                trade_payload(),
                f"Program {PUMP_PROGRAM} success",
            ],
            "innerInstructions": [],
        },
    }


class FakeRpc:
    def __init__(self, signatures: list, transactions: dict[str, dict | None]):
        self._signatures = signatures
        self._transactions = transactions

    async def signatures(self, address: str, limit: int = 100) -> list:
        return self._signatures

    async def transaction(self, signature: str) -> dict | None:
        return self._transactions.get(signature)

    async def close(self) -> None:
        pass


def test_read_records_jsonl_and_array(tmp_path) -> None:
    jsonl = tmp_path / "records.jsonl"
    jsonl.write_text(json.dumps(trade_record()) + "\n", encoding="utf-8")
    assert len(read_records(jsonl)) == 1
    array = tmp_path / "records.json"
    array.write_text(json.dumps([trade_record(), trade_record()]), encoding="utf-8")
    assert len(read_records(array)) == 2


def test_replay_records_applies_trade() -> None:
    book = CoinBook()
    stats = replay_records([trade_record()], book)
    assert stats.records == 1
    assert stats.transactions == 1
    assert stats.trades == 1
    assert book.stats.trades == 1
    # The synthetic trade helper mints 0x01*32, not a real coin; verify by volume.
    assert book.stats.volume_sol == 1.5
    assert len(book.coins) == 1
    assert next(iter(book.coins.values())).volume_sol == 1.5


def test_decode_record_accepts_notification_and_transaction() -> None:
    notification = {
        "params": {
            "result": {
                "value": {
                    "signature": "sig",
                    "slot": 1,
                    "logs": [trade_payload()],
                }
            }
        }
    }
    batch = decode_record(notification)
    assert len(batch.trades) == 1
    batch = decode_record(transaction_record())
    assert len(batch.trades) == 1


def test_decode_transaction_reads_emit_cpi_inner_instruction() -> None:
    raw = base64.b64decode(trade_payload().split("Program data: ", 1)[1])
    from pumpfire.base58 import b58encode

    data = b58encode(EVENT_IX_TAG + raw)
    result = {
        "slot": 42,
        "blockTime": 1_700_000_000,
        "transaction": {
            "signatures": ["inner-sig"],
            "message": {"accountKeys": [PUMP_PROGRAM]},
        },
        "meta": {
            "logMessages": [],
            "innerInstructions": [
                {"index": 0, "instructions": [{"programIdIndex": 0, "data": data}]}
            ],
        },
    }
    batch = decode_transaction(result)
    assert len(batch.trades) == 1
    assert batch.trades[0].signature == "inner-sig"
    assert batch.trades[0].sol == 1.5


def test_decode_transaction_dedupes_logs_and_inner_instructions() -> None:
    raw = base64.b64decode(trade_payload().split("Program data: ", 1)[1])
    from pumpfire.base58 import b58encode

    data = b58encode(EVENT_IX_TAG + raw)
    result = {
        "slot": 42,
        "blockTime": 1_700_000_000,
        "transaction": {
            "signatures": ["dup-sig"],
            "message": {"accountKeys": [PUMP_PROGRAM]},
        },
        "meta": {
            "logMessages": [
                f"Program {PUMP_PROGRAM} invoke [1]",
                trade_payload(),
                f"Program {PUMP_PROGRAM} success",
            ],
            "innerInstructions": [
                {"index": 0, "instructions": [{"programIdIndex": 0, "data": data}]}
            ],
        },
    }
    batch = decode_transaction(result)
    assert len(batch.trades) == 1


@pytest.mark.asyncio
async def test_backfill_events_with_fake_client() -> None:
    book = CoinBook()
    client = FakeRpc(
        [{"signature": "trade-sig", "err": None}],
        {"trade-sig": transaction_record()},
    )
    stats = await backfill_events(client, address="ADDR", limit=10, book=book)
    assert stats.records == 1
    assert stats.transactions == 1
    assert stats.trades == 1
    assert book.stats.trades == 1


@pytest.mark.asyncio
async def test_backfill_skips_failed_signatures() -> None:
    book = CoinBook()
    client = FakeRpc(
        [{"signature": "bad", "err": {"InstructionError": [0, "Custom"]}}],
        {},
    )
    stats = await backfill_events(client, address="ADDR", book=book)
    assert stats.records == 1
    assert stats.transactions == 0
    assert stats.skipped == 1


@pytest.mark.asyncio
async def test_rpc_client_with_mock_transport() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if payload["method"] == "getSignaturesForAddress":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": [{"signature": "s"}]})
        if payload["method"] == "getTransaction":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": transaction_record()})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": None})

    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    rpc = RpcClient("http://test", rps=1000, client=http)
    assert await rpc.signatures("ADDR") == [{"signature": "s"}]
    assert (await rpc.transaction("s"))["slot"] == 42
    await http.aclose()


@pytest.mark.asyncio
async def test_rpc_error_is_raised() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "error": {"code": -32000, "message": "nope"},
            },
        )

    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    rpc = RpcClient("http://test", rps=1000, client=http)
    with pytest.raises(RpcError):
        await rpc.signatures("ADDR")
    await http.aclose()


def test_replay_cli(tmp_path) -> None:
    path = tmp_path / "records.jsonl"
    path.write_text(json.dumps(trade_record()) + "\n", encoding="utf-8")
    result = CliRunner().invoke(app, ["replay", "--file", str(path), "--json"])
    assert result.exit_code == 0, result.output
    assert '"kind": "trade"' in result.output or '"kind":"trade"' in result.output
    assert "replayed 1 transactions" in result.output
