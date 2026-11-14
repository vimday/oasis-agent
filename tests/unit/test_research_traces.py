"""Trace consumers must count only real value transfers with successful ancestry."""

from copy import deepcopy

import pytest

from oasis_agent.arbitrage.aave_wallet import Movement
from oasis_agent.evidence.research_traces import first_failure, flat_calls, nested_calls
from scripts.research_ethereum_traces import compressed_slot, reconcile_trace

SENDER = "0x" + "11" * 20
PROXY = "0x" + "22" * 20
WALLET = "0x" + "33" * 20
IMPL = "0x" + "44" * 20
TX_HASH = "0x" + "ab" * 32


def flat(path, subtraces, sender, recipient, *, value=0, opcode="call", error=None):
    item = {
        "type": "call",
        "traceAddress": path,
        "subtraces": subtraces,
        "action": {
            "callType": opcode,
            "from": sender,
            "to": recipient,
            "input": "0x1122",
            "value": hex(value),
        },
        "result": {"output": "0x"},
    }
    if error is not None:
        item["error"] = error
    return item


def test_flat_trace_rolls_back_successful_grandchild_but_not_sibling():
    calls = flat_calls(
        [
            flat([], 2, SENDER, PROXY),
            flat([0], 1, PROXY, IMPL, opcode="delegatecall", error="Reverted"),
            flat([0, 0], 0, PROXY, WALLET, value=42),
            flat([1], 0, PROXY, WALLET, value=42),
        ]
    )
    row = {
        "transaction": TX_HASH,
        "block": 1,
        "block_hash": "0x" + "01" * 32,
        "transaction_index": 0,
        "receipt_capture_sha256": "11" * 32,
        "block_residual_wei": -42,
        "shapes": [{"from": PROXY, "to": WALLET, "value_wei": 42}],
    }
    tx = {"from": SENDER, "to": PROXY, "value": "0x0", "input": "0x1122"}
    moves = [
        Movement("internal", 1, None, TX_HASH, None, PROXY, WALLET, 42, None, str(i), True, 0)
        for i in range(2)
    ]
    result = reconcile_trace(WALLET, row, tx, calls, moves)
    assert result["corrected_block_residual_wei"] == 0
    assert result["committed_transaction_delta_wei"] == 42
    assert result["related_calls"][0]["first_reverted_path"] == (0,)
    assert result["related_calls"][1]["first_reverted_path"] is None
    with pytest.raises(ValueError, match="pinned transaction"):
        reconcile_trace(WALLET, row, {**tx, "value": "0x1"}, calls, moves)
    with pytest.raises(ValueError, match="trace-adjusted block"):
        reconcile_trace(WALLET, {**row, "block_residual_wei": 0}, tx, calls, moves)


@pytest.mark.parametrize(
    "mutation", ["missing", "duplicate", "outside", "invalid_caller", "value", "error"]
)
def test_flat_tree_rejects_corruption(mutation):
    root = flat([], 1, SENDER, PROXY)
    child = flat([0], 0, PROXY, WALLET, opcode="staticcall")
    changes = {
        "missing": [root],
        "duplicate": [root, child, child],
        "outside": [root, {**child, "traceAddress": [1]}],
        "invalid_caller": [root, {**child, "action": {**child["action"], "from": IMPL}}],
        "value": [root, {**child, "action": {**child["action"], "value": "0x1"}}],
        "error": [root, {**child, "error": None}],
    }
    with pytest.raises(ValueError):
        flat_calls(changes[mutation])


def test_failed_flat_frame_can_omit_result():
    child = flat([0], 0, PROXY, WALLET, opcode="staticcall", error="out of gas")
    child["result"] = None
    calls = flat_calls([flat([], 1, SENDER, PROXY), child])
    assert first_failure(calls, (0,)) == (0,)


def test_nested_propagates_revert_and_checks_value():
    child = {"type": "STATICCALL", "from": PROXY, "to": WALLET, "input": "0x"}
    root = {
        "type": "CALL",
        "from": SENDER,
        "to": PROXY,
        "input": "0x1122",
        "value": "0x0",
        "error": "execution reverted",
        "calls": [child],
    }
    assert first_failure(nested_calls(root), (0,)) == ()
    modified = deepcopy(root)
    modified["calls"][0]["value"] = "0x1"
    with pytest.raises(ValueError, match="static frame"):
        nested_calls(modified)


def test_compressed_feed_chain_and_position_are_bound():
    creator = int(SENDER, 16) << 96

    def key(packed):
        return "0x" + packed.to_bytes(32, "big").hex()

    assert compressed_slot(key(creator | 1 << 16 | 12 << 8 | 3)) == key(creator | 12)
    for invalid in (creator | 2 << 16, creator | 1 << 16 | 4, 1 << 16):
        with pytest.raises(ValueError, match="invalid Ethereum compressed feed ID"):
            compressed_slot(key(invalid))
