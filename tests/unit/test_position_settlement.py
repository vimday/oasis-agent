"""Synthetic wire data: proves attribution boundaries, never live profitability."""

import json
from dataclasses import replace

import pytest
from eth_abi.abi import encode

from oasis_agent.farming.position_ledger import PositionLedgerEntry
from oasis_agent.farming.position_settlement import (
    MODIFY_SELECTOR,
    MODIFY_TYPES,
    PositionSettlementCapture,
    persist_position_settlement_evidence,
)
from oasis_agent.models.common import canonical_json
from oasis_agent.rpc.transport import RpcExchange
from oasis_agent.uniswap_v4.events import decode_modify_liquidity
from tests.unit.test_position_dataset import CORE, OWNER, POSM, scenario


def capture_fixture(chain_id=4663, amounts=(-100, 200, 3, 4)):
    opening, _, dataset, _ = scenario()
    header = replace(dataset.blocks[-1], ref=replace(dataset.blocks[-1].ref, chain_id=chain_id))
    log = dataset.logs[0]
    entry = PositionLedgerEntry(
        header.ref, log.transaction_hash, 0, 0, decode_modify_liquidity(log)
    )
    key = opening.pool_key
    data = (
        "0x"
        + (
            MODIFY_SELECTOR
            + encode(
                MODIFY_TYPES,
                [
                    (key.currency0, key.currency1, key.fee, key.tick_spacing, key.hooks),
                    (-120, 120, 20, (7).to_bytes(32, "big")),
                    b"",
                ],
            )
        ).hex()
    )
    packed = b"".join(x.to_bytes(16, "big", signed=True) for x in amounts)
    frame = {
        "type": "CALL",
        "from": POSM,
        "to": CORE,
        "input": data,
        "output": "0x" + packed.hex(),
        "calls": [],
    }
    trace = {
        "type": "CALL",
        "from": OWNER,
        "to": POSM,
        "input": "0x12345678",
        "output": "0x",
        "calls": [frame],
    }
    receipt = {
        "transactionHash": entry.transaction_hash,
        "blockHash": header.ref.block_hash,
        "blockNumber": hex(header.ref.number),
        "transactionIndex": "0x0",
        "status": "0x1",
        "from": OWNER,
        "to": POSM,
        "gasUsed": "0x64",
        "effectiveGasPrice": "0x2",
        "logs": [
            {
                "address": CORE,
                "topics": list(log.topics),
                "data": log.data,
                "blockNumber": hex(log.block_number),
                "blockHash": log.block_hash,
                "transactionHash": log.transaction_hash,
                "transactionIndex": "0x0",
                "logIndex": "0x0",
                "removed": False,
            }
        ],
    }
    hdr = {
        "number": hex(header.ref.number),
        "hash": header.ref.block_hash,
        "parentHash": header.parent_hash,
        "timestamp": hex(header.ref.timestamp),
    }
    pin = {"blockHash": header.ref.block_hash, "requireCanonical": True}
    specs = [
        ("eth_chainId", [], hex(chain_id)),
        ("eth_getBlockByNumber", [hex(header.ref.number), False], hdr),
        ("eth_getTransactionReceipt", [entry.transaction_hash], receipt),
        (
            "debug_traceTransaction",
            [
                entry.transaction_hash,
                {
                    "tracer": "callTracer",
                    "timeout": "10s",
                    "tracerConfig": {"onlyTopCall": False},
                },
            ],
            trace,
        ),
        ("eth_getCode", [CORE, pin], "0x6000"),
        ("eth_getCode", [POSM, pin], "0x6001"),
        ("eth_getBlockByNumber", [hex(header.ref.number), False], hdr),
    ]
    exchanges = tuple(
        RpcExchange(
            method,
            i,
            canonical_json(
                {
                    "jsonrpc": "2.0",
                    "id": i,
                    "method": method,
                    "params": params,
                }
            ),
            canonical_json({"jsonrpc": "2.0", "id": i, "result": result}),
            1,
        )
        for i, (method, params, result) in enumerate(specs, 1)
    )
    return PositionSettlementCapture("synthetic-rpc", header, CORE, key, entry, exchanges)


def mutate(capture, index, function):
    items = list(capture.exchanges)
    response = json.loads(items[index].response_payload)
    function(response)
    items[index] = replace(items[index], response_payload=canonical_json(response))
    return replace(capture, exchanges=tuple(items))


@pytest.mark.parametrize("chain", [1, 8453, 56, 4663, 84532, 11155111])
@pytest.mark.parametrize(
    "amounts", [(-100, 200, 3, 4), (0, 0, 0, 0), (-(2**127), 2**127 - 1, 0, 2**127 - 1)]
)
def test_core_return_amounts_are_not_owner_cash(chain, amounts):
    capture = capture_fixture(chain, amounts)
    result = capture.reconstruct()
    assert (
        result.caller_delta.amount0,
        result.caller_delta.amount1,
        result.fees_accrued.amount0,
        result.fees_accrued.amount1,
    ) == amounts
    assert result.execution_gas_native_base_units == 200
    assert result.token_id == 7
    assert capture.entry.settlement_resolved is False
    payload = json.loads(capture.payload)
    assert payload["owner_amount0"] is None
    assert payload["owner_amount1"] is None
    assert payload["net_pnl"] is None
    assert payload["settlement_complete"] is False


@pytest.mark.parametrize(
    "mutation",
    [
        "wrong_transaction",
        "wrong_block",
        "wrong_index",
        "removed",
        "duplicate_log",
        "wrong_nft",
        "wrong_pool",
        "wrong_caller",
        "duplicate_frame",
        "truncated_output",
        "reverted_parent",
        "delegatecall",
        "missing_logs",
        "missing_gas",
        "failed_receipt",
        "missing_code",
        "wrong_chain",
        "reorg",
        "wrong_topic",
        "negative_fees",
    ],
)
def test_rejects_misbound_or_ambiguous_data(mutation):
    capture = capture_fixture()

    def change(response):
        result = response["result"]
        if mutation == "wrong_transaction":
            result["transactionHash"] = "0x" + "ff" * 32
        elif mutation == "wrong_block":
            result["blockHash"] = "0x" + "ff" * 32
        elif mutation == "wrong_index":
            result["logs"][0]["transactionIndex"] = "0x1"
        elif mutation == "removed":
            result["logs"][0]["removed"] = True
        elif mutation == "duplicate_log":
            result["logs"].append(result["logs"][0])
        elif mutation == "missing_logs":
            result.pop("logs")
        elif mutation == "missing_gas":
            result.pop("gasUsed")
        elif mutation == "failed_receipt":
            result["status"] = "0x0"
        elif mutation == "missing_code":
            response["result"] = "0x"
        elif mutation == "wrong_chain":
            response["result"] = "0x1"
        elif mutation == "reorg":
            result["hash"] = "0x" + "ff" * 32
        elif mutation == "wrong_topic":
            result["logs"][0]["topics"][2] = "0x" + "ff" * 32
        elif mutation == "reverted_parent":
            result["error"] = "execution reverted"
        elif mutation == "duplicate_frame":
            result["calls"].append(result["calls"][0])
        else:
            frame = result["calls"][0]
            if mutation == "wrong_caller":
                frame["from"] = OWNER
            elif mutation == "delegatecall":
                frame["type"] = "DELEGATECALL"
            elif mutation == "truncated_output":
                frame["output"] = "0x00"
            elif mutation == "negative_fees":
                frame["output"] = "0x" + "ff" * 64
            elif mutation == "wrong_nft":
                raw = bytearray.fromhex(frame["input"][2:])
                raw[4 + 9 * 32 - 1] = 8
                frame["input"] = "0x" + raw.hex()
            elif mutation == "wrong_pool":
                raw = bytearray.fromhex(frame["input"][2:])
                raw[4 + 32 - 1] = 0x54
                frame["input"] = "0x" + raw.hex()

    index = 2
    if mutation in {
        "wrong_nft",
        "wrong_pool",
        "wrong_caller",
        "duplicate_frame",
        "truncated_output",
        "reverted_parent",
        "delegatecall",
        "negative_fees",
    }:
        index = 3
    elif mutation == "missing_code":
        index = 4
    elif mutation == "wrong_chain":
        index = 0
    elif mutation == "reorg":
        index = 6
    with pytest.raises((ValueError, TypeError)):
        mutate(capture, index, change).reconstruct()


def test_trace_unavailable_stays_unresolved():
    capture = mutate(
        capture_fixture(),
        3,
        lambda r: (r.pop("result"), r.update(error={"code": -32601, "message": "unsupported"})),
    )
    assert capture.reconstruct().caller_delta is None
    assert "trace_unavailable" in json.loads(capture.payload)["unresolved"]


def test_persistence_reconstructs_raw_evidence(tmp_path):
    from oasis_agent.evidence.rpc_validation import restore_exchanges
    from oasis_agent.evidence.store import EvidenceStore

    capture = capture_fixture()
    store = EvidenceStore(tmp_path / "evidence.db")
    try:
        reference = persist_position_settlement_evidence(store, capture)
        record = store.get_record(reference)
        restored = replace(
            capture,
            exchanges=restore_exchanges(record.request_payload, record.raw_response_payload),
        )
        assert restored.payload == record.payload
        assert json.loads(record.interpretation_payload)["execution_eligible"] is False
    finally:
        store.close()


def test_collector_records_exact_session():
    from oasis_agent.evidence.rpc_capture import BoundedExchangeRecorder
    from oasis_agent.farming.position_settlement import collect_position_settlement
    from oasis_agent.rpc.transport import JsonRpcTransport

    fixture = capture_fixture()

    class Poster:
        def post(self, payload):
            request = json.loads(payload)
            return fixture.exchanges[request["id"] - 1].response_payload

    recorder = BoundedExchangeRecorder()
    result = collect_position_settlement(
        JsonRpcTransport("https://fixture.invalid", http=Poster(), recorder=recorder, retries=0),
        recorder,
        source=fixture.source,
        header=fixture.header,
        pool_manager=fixture.pool_manager,
        pool_key=fixture.pool_key,
        entry=fixture.entry,
    )
    assert result.payload == fixture.payload


def test_pipeline_retains_core_deltas_without_resolving_owner(tmp_path):
    from oasis_agent.evidence.ingestion import persist_and_verify_block_range
    from oasis_agent.evidence.store import EvidenceStore
    from oasis_agent.farming.position_pipeline import replay_captured_position_datasets
    from tests.rpc_evidence_helpers import range_exchanges
    from tests.unit.test_position_capture import snapshot_capture

    store = EvidenceStore(tmp_path / "pipeline.db")
    try:
        _, _, core, nft = scenario()
        verified = tuple(
            persist_and_verify_block_range(
                store,
                ds,
                range_exchanges(ds),
                research_single_provider=True,
            )
            for ds in (core, nft)
        )
        args = {
            "opening": snapshot_capture(),
            "closing": snapshot_capture(closing=True),
            "pool_manager": CORE,
            "core": verified[0],
            "ownership": verified[1],
        }
        capture = capture_fixture()
        result = replay_captured_position_datasets(store, **args, settlements=(capture,))
        assert len(result.core_settlement_evidence) == 1
        assert store.get(result.core_settlement_evidence[0]) == capture.payload
        assert result.position.ledger.unresolved_settlements == 1
        assert result.position.ledger.cumulative_owner_amount0 is None
        with pytest.raises(ValueError, match="duplicate settlement"):
            replay_captured_position_datasets(store, **args, settlements=(capture, capture))
        with pytest.raises(ValueError, match="does not match"):
            replay_captured_position_datasets(store, **args, settlements=(capture_fixture(1),))
    finally:
        store.close()


def test_null_trace_is_not_an_rpc_unavailable_error():
    with pytest.raises(ValueError, match="null settlement"):
        mutate(capture_fixture(), 3, lambda r: r.update(result=None))


@pytest.mark.parametrize(
    "mutation",
    [
        "latest",
        "numeric_canonical",
        "other_transaction",
        "top_call_only",
        "wrong_code_target",
        "duplicate_id",
        "missing_close",
        "extra_exchange",
    ],
)
def test_requests_cannot_relabel_unpinned_or_partial_evidence(mutation):
    capture = capture_fixture()
    items = list(capture.exchanges)
    index = 4 if mutation in {"latest", "numeric_canonical", "wrong_code_target"} else 3
    req = json.loads(items[index].request_payload)
    if mutation == "latest":
        req["params"][1] = "latest"
    elif mutation == "numeric_canonical":
        req["params"][1]["requireCanonical"] = 1
    elif mutation == "wrong_code_target":
        req["params"][0] = OWNER
    elif mutation == "other_transaction":
        req["params"][0] = "0x" + "ff" * 32
    elif mutation == "top_call_only":
        req["params"][1]["tracerConfig"]["onlyTopCall"] = True
    items[index] = replace(items[index], request_payload=canonical_json(req))
    if mutation == "missing_close":
        items.pop()
    elif mutation == "extra_exchange":
        items.append(items[-1])
    elif mutation == "duplicate_id":
        req = json.loads(items[-1].request_payload)
        res = json.loads(items[-1].response_payload)
        req["id"] = res["id"] = 1
        items[-1] = replace(
            items[-1],
            request_id=1,
            request_payload=canonical_json(req),
            response_payload=canonical_json(res),
        )
    with pytest.raises(ValueError):
        replace(capture, exchanges=tuple(items))


def test_selector_matches_compiled_canonical_pool_manager():
    # Independently read from contracts/out/PoolManager.sol/PoolManager.json,
    # @uniswap/v4-core 1.0.2 compiled by solc 0.8.26 in the full repository gate.
    assert MODIFY_SELECTOR.hex() == "5a6bcfda"


def test_oversized_capture_is_rejected_before_json_decoding():
    capture = capture_fixture()
    items = list(capture.exchanges)
    items[3] = replace(items[3], response_payload=b" " * 5_000_001)
    with pytest.raises(ValueError, match="byte capacity"):
        replace(capture, exchanges=tuple(items))


def test_delegate_trace_preserves_execution_context_not_implementation_address():
    capture = capture_fixture()
    implementation = "0x" + "99" * 20

    def proxy(response):
        root = response["result"]
        root["calls"] = [
            {
                "type": "DELEGATECALL",
                "from": POSM,
                "to": implementation,
                "input": "0x12345678",
                "output": "0x",
                "calls": root["calls"],
            }
        ]

    proxied = mutate(capture, 3, proxy)
    assert proxied.reconstruct().caller_delta == capture.reconstruct().caller_delta

    def forged_sender(response):
        response["result"]["calls"][0]["calls"][0]["from"] = implementation

    with pytest.raises(ValueError, match="child caller"):
        mutate(proxied, 3, forged_sender)


@pytest.mark.parametrize("kind", ["CREATE", "CREATE2"])
def test_constructor_trace_children_use_created_address(kind):
    def constructor(response):
        root = response["result"]
        created = "0x" + "99" * 20
        root["calls"] = [
            {
                "type": kind,
                "from": POSM,
                "to": created,
                "input": "0x6000",
                "output": "0x6001",
                "calls": [
                    {
                        "type": "CALL",
                        "from": created,
                        "to": POSM,
                        "input": "0x12345678",
                        "output": "0x",
                        "calls": root["calls"],
                    }
                ],
            }
        ]

    original = capture_fixture()
    observed = mutate(original, 3, constructor)
    assert observed.reconstruct().caller_delta == original.reconstruct().caller_delta
    assert json.loads(observed.payload)["execution_eligible"] is False
