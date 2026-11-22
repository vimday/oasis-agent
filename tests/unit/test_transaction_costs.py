import json
from dataclasses import replace

import pytest
from eth_abi.abi import encode

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.farming.transaction_costs import (
    OP_ORACLE,
    OP_SELECTOR,
    TransactionCostCapture,
    persist_transaction_cost,
    replay_transaction_cost,
)
from oasis_agent.models.common import canonical_json
from oasis_agent.rpc.transport import RpcExchange
from tests.unit.test_position_settlement import capture_fixture


def fixture(chain=1, status=1, tx_type=2):
    core = capture_fixture(chain)
    header = json.loads(core.exchanges[1].response_payload)["result"]
    receipt = json.loads(core.exchanges[2].response_payload)["result"]
    receipt.update(
        status=hex(status),
        type=hex(tx_type),
        l1Fee="0x7",
        gasUsedForL1="0x14",
        blobGasUsed="0x20000",
        blobGasPrice="0x5",
    )
    specs = [
        ("eth_chainId", [], hex(chain)),
        ("eth_getBlockByNumber", [hex(core.header.ref.number), False], header),
        ("eth_getTransactionReceipt", [core.entry.transaction_hash], receipt),
    ]
    if chain in (8453, 84532):
        pin = {"blockHash": core.header.ref.block_hash, "requireCanonical": True}
        specs += [
            ("eth_getCode", [OP_ORACLE, pin], "0x6000"),
            (
                "eth_call",
                [{"to": OP_ORACLE, "data": OP_SELECTOR + encode(["uint256"], [100]).hex()}, pin],
                "0x" + encode(["uint256"], [11]).hex(),
            ),
        ]
    specs += [("eth_getBlockByNumber", [hex(core.header.ref.number), False], header)]
    xs = tuple(
        RpcExchange(
            m,
            i,
            canonical_json({"jsonrpc": "2.0", "id": i, "method": m, "params": p}),
            canonical_json({"jsonrpc": "2.0", "id": i, "result": r}),
            1,
        )
        for i, (m, p, r) in enumerate(specs, 1)
    )
    return TransactionCostCapture(core.header, core.entry.transaction_hash, core.source, xs)


@pytest.mark.parametrize("status", [0, 1])
@pytest.mark.parametrize(
    "chain,total", [(1, 200), (11155111, 200), (8453, 218), (84532, 218), (4663, 200), (42161, 200)]
)
def test_failed_and_successful_fees_include_correct_chain_components(chain, total, status):
    c = fixture(chain, status).reconstruct()
    assert c.status == status and c.total_native == total
    assert c.total_native == c.execution_native + c.l1_native + c.blob_native + c.operator_native
    if chain in (4663, 42161):
        assert (c.execution_native, c.l1_native) == (160, 40)
    if chain in (8453, 84532):
        assert c.blob_native == 0  # OP footprint not ETH blobs


def test_ethereum_blob_is_additional():
    assert fixture(tx_type=3).reconstruct().total_native == 655560


@pytest.mark.parametrize(
    "field,value",
    [
        ("l1Fee", None),
        ("status", "0x2"),
        ("transactionHash", "0x" + "ff" * 32),
        ("gasUsed", "0x10000000000000000"),
    ],
)
def test_missing_or_invalid_receipt_data_fails(field, value):
    c = fixture(84532)
    xs = list(c.exchanges)
    r = json.loads(xs[2].response_payload)
    r["result"][field] = value
    xs[2] = replace(xs[2], response_payload=canonical_json(r))
    with pytest.raises((ValueError, TypeError)):
        replace(c, exchanges=tuple(xs))


@pytest.mark.parametrize("failure", ["missing", "wrong_gas", "no_result"])
def test_op_requires_operator_fee_result_for_exact_pinned_receipt_gas(failure):
    c = fixture(8453)
    exchanges = list(c.exchanges)
    if failure == "missing":
        del exchanges[4]
    elif failure == "wrong_gas":
        request = json.loads(exchanges[4].request_payload)
        request["params"][0]["data"] = OP_SELECTOR + encode(["uint256"], [101]).hex()
        exchanges[4] = replace(exchanges[4], request_payload=canonical_json(request))
    else:
        response = json.loads(exchanges[4].response_payload)
        del response["result"]
        response["error"] = {"code": -32000, "message": "historical state unavailable"}
        exchanges[4] = replace(exchanges[4], response_payload=canonical_json(response))
    with pytest.raises(ValueError, match=r"incomplete|request/response mismatch"):
        replace(c, exchanges=tuple(exchanges))


def test_unsupported_chain_never_defaults_to_eth():
    with pytest.raises(ValueError, match="unknown chain"):
        fixture(999999)


def test_cost_persistence_reconstructs_wire_and_rejects_forged_total(tmp_path):
    c = fixture(84532)
    store = EvidenceStore(tmp_path / "cost.db")
    ref = persist_transaction_cost(store, c)
    assert (
        replay_transaction_cost(store, ref, header=c.header, transaction_hash=c.transaction_hash)
        == c
    )
    record = store.get_record(ref)
    payload = json.loads(record.payload)
    payload["cost"]["total_native"] = 0
    bad = store.put(
        ref.kind,
        canonical_json(payload),
        ref.block,
        ref.source,
        request=json.loads(record.request_payload),
        raw_response=json.loads(record.raw_response_payload),
        interpretation=json.loads(record.interpretation_payload),
    )
    with pytest.raises(ValueError, match="interpretation differs"):
        replay_transaction_cost(store, bad, header=c.header, transaction_hash=c.transaction_hash)
    store.close()
