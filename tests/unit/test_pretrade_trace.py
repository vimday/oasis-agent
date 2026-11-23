import pytest
from eth_abi import encode

from oasis_agent.evidence.public_receipts import ObservedReceipt
from oasis_agent.models.chain import LogRecord
from oasis_agent.models.common import BlockRef, canonical_json
from scripts.replay_pretrade_trace import ENTRYPOINT_V06, FAILED_OP, PretradeContext, replay_stage

SENDER = "0x" + "12" * 20
BLOCK = "0x" + "ab" * 32
PARENT = "0x" + "ac" * 32
NEXT = "0x" + "ad" * 32
TX_HASH = "0x" + "bc" * 32


def sample():
    args = {
        "from": SENDER,
        "to": ENTRYPOINT_V06,
        "input": "0x0102",
        "value": "0x0",
        "gas": "0xc350",
        "maxFeePerGas": "0xa",
        "maxPriorityFeePerGas": "0x1",
        "authorizationList": [{"nonce": "0x1"}],
    }
    log = LogRecord(ENTRYPOINT_V06, (), "0x1234", 20, TX_HASH, 11)
    ctx = PretradeContext(
        ObservedReceipt(
            BlockRef(8453, 20, BLOCK), TX_HASH, 3, SENDER, ENTRYPOINT_V06, 1, 30000, 9, (log,)
        ),
        args.copy(),
        PARENT,
        NEXT,
    )
    req = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "debug_traceCall",
        "params": [
            args,
            {"blockHash": BLOCK, "requireCanonical": True},
            {"txIndex": "0x3", "tracer": "callTracer", "tracerConfig": {"withLog": True}},
        ],
    }
    trace = {
        "type": "CALL",
        **args,
        "gasUsed": hex(30000),
        "logs": [{"address": ENTRYPOINT_V06, "topics": [], "data": "0x1234", "position": "0x1"}],
        "calls": [{"error": "execution reverted", "calls": [{"logs": [{"irrelevant": True}]}]}],
    }
    return ctx, req, {"jsonrpc": "2.0", "id": 1, "result": trace}


def test_indexed_match_requires_original_call_and_ordered_committed_logs():
    context, req, res = sample()
    row = replay_stage(context, canonical_json(req), canonical_json(res))
    assert row["matches_receipt"] and row["index_parameter"] == 3
    res["result"]["logs"][0]["data"] = "0x1235"
    assert not replay_stage(context, canonical_json(req), canonical_json(res))["matches_receipt"]
    req["params"][0].pop("authorizationList")
    with pytest.raises(ValueError, match="authorization"):
        replay_stage(context, canonical_json(req), canonical_json(res))


def test_next_block_nonce_failure_is_distinct_from_economics_and_rpc_errors():
    context, req, res = sample()
    req["params"][1]["blockHash"] = NEXT
    req["params"][2].pop("txIndex")
    res["result"].update(
        error="execution reverted",
        logs=[],
        output="0x"
        + (FAILED_OP + encode(["uint256", "string"], [0, "AA25 invalid account nonce"])).hex(),
    )
    first = replay_stage(context, canonical_json(req), canonical_json(res))
    assert first["position"] == "next_block_end"
    assert first["entrypoint_nonce_rejected"] and not first["success"]
    error = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "pruned"}}
    row = replay_stage(context, canonical_json(req), canonical_json(error))
    assert row["matches_receipt"] is None and row["rpc_error_code"] == -32000
    req["params"][2]["stateOverrides"] = {}
    with pytest.raises(ValueError, match="without overrides"):
        replay_stage(context, canonical_json(req), canonical_json(res))
