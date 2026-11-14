"""Historical receipt observations remain pinned and failures remain visible."""

import json

import httpx
import pytest

from oasis_agent.evidence.public_receipts import decode_receipt, receipt_header
from oasis_agent.evidence.public_rpc_plan import PublicHeadPlan
from oasis_agent.models.common import BlockRef, canonical_json
from scripts import capture_public_rpc as cli
from tests.unit.test_public_rpc_capture import HASH, TARGET, counts, setup

TX = "0x" + "56" * 32
BLOCK = BlockRef(1, 100, HASH, 1767225600)


def receipt():
    return {
        "transactionHash": TX,
        "blockHash": HASH,
        "blockNumber": "0x64",
        "transactionIndex": "0x2",
        "from": TARGET,
        "to": TARGET,
        "status": "0x1",
        "gasUsed": "0x5208",
        "effectiveGasPrice": "0x3b9aca00",
        "logs": [
            {
                "address": TARGET,
                "topics": ["0x" + "77" * 32],
                "data": "0x",
                "blockNumber": "0x64",
                "blockHash": HASH,
                "transactionHash": TX,
                "transactionIndex": "0x2",
                "logIndex": "0x3",
                "removed": False,
            }
        ],
    }


def test_receipt_exact_identity_and_gas_component():
    r = decode_receipt(receipt(), BLOCK, TX)
    assert r.transaction_index == 2 and r.logs[0].log_index == 3
    assert r.gas_used * r.effective_gas_price == 21000 * 10**9


def test_anonymous_log_retains_bytes_and_position():
    value = receipt()
    value["logs"][0].update(topics=[], data="0xabcd")
    result = decode_receipt(value, BLOCK, TX)
    assert result.logs[0].topics == ()
    assert result.logs[0].data == "0xabcd" and result.logs[0].log_index == 3
    value["logs"][0]["transactionIndex"] = "0x4"
    with pytest.raises(ValueError, match="position"):
        decode_receipt(value, BLOCK, TX)


@pytest.mark.parametrize(
    "field,value",
    [
        ("transactionHash", HASH),
        ("blockHash", TX),
        ("blockNumber", "0x65"),
        ("transactionIndex", "0x100000000"),
        ("transactionIndex", True),
        ("status", "0x2"),
        ("status", "0x0"),
        ("status", 1),
        ("gasUsed", "0x00"),
        ("effectiveGasPrice", hex(1 << 256)),
        ("effectiveGasPrice", hex((1 << 256) - 1)),
        ("from", "invalid"),
    ],
)
def test_invalid_receipt_cannot_become_accounting(field, value):
    r = receipt()
    r[field] = value
    with pytest.raises((ValueError, TypeError)):
        decode_receipt(r, BLOCK, TX)


@pytest.mark.parametrize(
    "field,value",
    [
        ("transactionHash", HASH),
        ("blockHash", TX),
        ("blockNumber", "0x65"),
        ("transactionIndex", "0x3"),
        ("logIndex", "0x100000000"),
        ("removed", True),
    ],
)
def test_foreign_removed_or_overflowed_log_rejected(field, value):
    r = receipt()
    r["logs"][0][field] = value
    with pytest.raises(ValueError):
        decode_receipt(r, BLOCK, TX)


def test_reverted_receipt_and_contract_creation_are_observations():
    r = receipt()
    r.update(status="0x0", logs=[], to=None)
    result = decode_receipt(r, BLOCK, TX)
    assert result.status == 0 and result.recipient is None and not result.logs


def test_duplicate_logs_rejected():
    r = receipt()
    r["logs"] *= 2
    with pytest.raises(ValueError, match="duplicate"):
        decode_receipt(r, BLOCK, TX)


@pytest.mark.parametrize("bad", [False, True, -1, 100.0])
def test_historical_observation_is_an_integer_not_boolean_or_float(bad):
    with pytest.raises(ValueError):
        PublicHeadPlan.model_validate_json(
            json.dumps(
                {
                    "schema": "oasis-public-rpc-head-plan-v1",
                    "source": "publicnode_ethereum",
                    "chain_id": 1,
                    "block_tag": bad,
                }
            )
        )


@pytest.mark.parametrize("wrong_block", [False, True])
def test_historical_header_request_checks_requested_number(tmp_path, monkeypatch, wrong_block):
    root, path, plan = setup(tmp_path)
    path.write_bytes(
        canonical_json(
            {
                "schema": "oasis-public-rpc-head-plan-v1",
                "source": plan["source"],
                "chain_id": 1,
                "block_tag": 101,
            }
        )
    )
    original = httpx.Client

    def handler(request):
        q = json.loads(request.content)
        if q["id"] == 2:
            assert q["params"] == ["0x65", False]
        result = (
            "0x1"
            if q["id"] == 1
            else {
                "number": "0x64" if wrong_block else "0x65",
                "hash": HASH,
                "parentHash": TX,
                "timestamp": hex(BLOCK.timestamp),
            }
        )
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": q["id"], "result": result})

    monkeypatch.setattr(
        cli.httpx, "Client", lambda **kw: original(transport=httpx.MockTransport(handler), **kw)
    )
    assert cli.capture(root, path, "head") == (3 if wrong_block else 0)
    result = cli.replay(root / "head")
    assert result["status"] == ("incomplete" if wrong_block else "complete")


@pytest.mark.parametrize("failed", [False, "hash", "null"])
def test_receipt_capture_uses_frozen_hash_and_stops_on_mismatch(tmp_path, monkeypatch, failed):
    root, path, plan = setup(tmp_path)
    path.write_bytes(
        canonical_json(
            {
                "schema": "oasis-public-receipt-plan-v1",
                "source": plan["source"],
                "chain_id": 1,
                "block_number": 100,
                "block_hash": HASH,
                "transactions": [TX],
            }
        )
    )
    original = httpx.Client
    calls = []

    def handler(request):
        q = json.loads(request.content)
        calls.append(q)
        assert counts(root)[plan["source"]] == len(calls)
        if q["method"] == "eth_chainId":
            value = "0x1"
        elif q["method"] == "eth_getBlockByNumber":
            value = {
                "number": "0x64",
                "hash": HASH,
                "parentHash": TX,
                "timestamp": hex(BLOCK.timestamp),
                "transactions": ["0x" + "ab" * 32, "0x" + "cd" * 32, TX],
            }
        else:
            assert q["method"] == "eth_getTransactionReceipt" and q["params"] == [TX]
            value = receipt()
            if failed == "hash":
                value["blockHash"] = TX
            elif failed == "null":
                value = None
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": q["id"], "result": value})

    monkeypatch.setattr(
        cli.httpx, "Client", lambda **kw: original(transport=httpx.MockTransport(handler), **kw)
    )
    assert cli.capture(root, path, "receipt") == (3 if failed else 0)
    r = cli.replay(root / "receipt")
    assert r["plan_kind"] == "pinned_receipts"
    assert r["attempted_requests"] == (3 if failed else 4)
    assert len(r["exports"]["records"]) == (2 if failed else 4)
    assert (root / "STOP").exists() is bool(failed)


def test_receipt_position_must_match_the_block_transaction_list():
    with pytest.raises(ValueError, match="index_differs"):
        decode_receipt(receipt(), BLOCK, TX, 3)
    head = {
        "number": "0x64",
        "hash": HASH,
        "parentHash": TX,
        "timestamp": hex(BLOCK.timestamp),
        "transactions": [HASH],
    }
    with pytest.raises(ValueError, match="not_in_block"):
        receipt_header(head, 1, 100, (TX,))


@pytest.mark.parametrize("data", ["0x+1", "0x-1", "0x 1"])
def test_log_data_does_not_accept_integer_parser_shortcuts(data):
    r = receipt()
    r["logs"][0]["data"] = data
    with pytest.raises(ValueError, match="not_hex_bytes"):
        decode_receipt(r, BLOCK, TX)
