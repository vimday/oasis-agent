"""Research joins must protect attribution and never settle a reverted native credit."""

import pytest

from oasis_agent.arbitrage.metric_builder import compressed_time
from oasis_agent.arbitrage.titan_state import attribution
from oasis_agent.evidence.public_receipts import ObservedReceipt
from oasis_agent.evidence.research_traces import trace_correction
from oasis_agent.models.common import BlockRef, canonical_json

ACCOUNT = "0x" + "aa" * 20
SENDER = "0x" + "bb" * 20
ROUTER = "0x" + "cc" * 20
BLOCK_HASH = "0x" + "11" * 32
PARENT = "0x" + "22" * 32
BUILDER = "0x" + "33" * 48


def test_relay_attribution_requires_hash_slot_and_builder_key():
    target = {"hash": BLOCK_HASH, "parent_hash": PARENT, "block": 11, "fee_recipient": ACCOUNT}
    bid = {
        "slot": "17",
        "block_hash": BLOCK_HASH,
        "parent_hash": PARENT,
        "block_number": "11",
        "builder_pubkey": BUILDER,
    }
    assert attribution([bid], 17, target, {BUILDER}) == ("titan_confirmed", BUILDER)
    assert attribution([bid], 17, target, set()) == ("unlisted_builder_key", BUILDER)
    assert attribution([], 17, target, {BUILDER}) == ("no_matching_delivery", None)
    assert attribution([bid | {"block_hash": PARENT}], 17, target, {BUILDER}) == (
        "no_matching_delivery",
        None,
    )
    with pytest.raises(ValueError, match="header context"):
        attribution([bid | {"parent_hash": BLOCK_HASH}], 17, target, {BUILDER})
    with pytest.raises(ValueError, match="changed slot"):
        attribution([bid | {"slot": "18"}], 17, target, {BUILDER})
    with pytest.raises(ValueError, match="conflicting"):
        attribution([bid, bid | {"builder_pubkey": "0x" + "44" * 48}], 17, target, {BUILDER})


def test_compressed_time_uses_only_timestamp_bits():
    timestamp = 1794657611000
    word = (1 << 255) | (timestamp << 8) | 255
    assert compressed_time("0x" + word.to_bytes(32, "big").hex()) == timestamp
    with pytest.raises(ValueError):
        compressed_time(hex(timestamp << 8))


def test_receipt_scope_trace_correction_drops_ancestor_revert():
    def frame(path, children, sender, to, value=0, error=None):
        result = {
            "type": "call",
            "traceAddress": path,
            "subtraces": children,
            "action": {
                "from": sender,
                "to": to,
                "value": hex(value),
                "input": "0x",
                "callType": "call",
            },
            "result": {"output": "0x"},
        }
        if error is not None:
            result["error"] = error
        return result

    body = canonical_json(
        [
            frame([], 2, SENDER, ROUTER),
            frame([0], 1, ROUTER, ROUTER, error="Reverted"),
            frame([0, 0], 0, ROUTER, ACCOUNT, 87),
            frame([1], 0, ROUTER, ACCOUNT, 7),
        ]
    )
    receipt = ObservedReceipt(
        BlockRef(1, 11, BLOCK_HASH, 1000), BLOCK_HASH, 2, SENDER, ROUTER, 1, 100, 2, ()
    )
    delta = trace_correction(body, receipt, ACCOUNT, 94)
    assert delta["correction"] == -87
    assert delta["movements"][0]["first_failed_path"] == (0,)
    assert delta["movements"][1]["first_failed_path"] is None
