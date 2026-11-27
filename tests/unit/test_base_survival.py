from copy import deepcopy

import pytest

from oasis_agent.arbitrage.base_survival import (
    COHORTS,
    TRANSFER,
    WETH,
    decode_base_simulation,
    summarize_base_shadow,
)

PARENT = "0x" + "12" * 32
CHILD = "0x" + "34" * 32
RECIPIENT = "0x" + "55" * 20
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"


def test_full_simulation_checks_ancestry_gas_and_committed_weth() -> None:
    logs = [
        {
            "address": WETH,
            "topics": [TRANSFER, "0x" + "00" * 12 + "44" * 20, "0x" + "00" * 12 + "55" * 20],
            "data": "0x" + (800).to_bytes(32, "big").hex(),
        }
    ]
    call = {"status": "0x1", "gasUsed": "0x5208", "returnData": "0x", "logs": logs}
    result = [
        {
            "number": "0x2",
            "parentHash": PARENT,
            "baseFeePerGas": "0x9",
            "gasUsed": "0x5208",
            "calls": [call],
        }
    ]
    opts = {"parent_number": 1, "parent_hash": PARENT, "recipient": RECIPIENT, "gas_limit": 30_000}
    observed = decode_base_simulation(result, **opts)
    assert observed.success and observed.weth_delta == 800
    assert observed.gas_used == 21_000 and observed.base_fee_wei_per_gas == 9
    with pytest.raises(ValueError, match="exact state"):
        decode_base_simulation(result, **{**opts, "parent_hash": CHILD})
    call["status"] = "0x0"
    with pytest.raises(ValueError, match="committed"):
        decode_base_simulation(result, **opts)
    call["logs"] = []
    rejected = decode_base_simulation(result, **opts)
    assert not rejected.success and rejected.weth_delta == 0 and rejected.gas_used == 21_000


def _row(seq: int, block_hash: str, parent: str, receive: int) -> dict:
    return {
        "seq": seq,
        "chain_id": 8453,
        "execution_eligible": False,
        "block_number": seq,
        "block_hash": block_hash,
        "rust_raw_candidates_count": 0,
        "atomic_candidate_dispositions": [],
        "shadow_cycle_lifecycle": {
            "state_ready_monotonic_ns": receive + 5,
            "discovery_finished_monotonic_ns": receive + 8,
            "previous_block_reprice": None,
            "mixed_base_state": {
                "newheads_observation": {
                    "received_monotonic_ns": receive,
                    "head": {"height": seq, "block_hash": block_hash, "parent_hash": parent},
                },
                "state_capture_started_monotonic_ns": receive + 2,
                "state_ready_monotonic_ns": receive + 4,
                "cohorts": {
                    name: {
                        "pools": size,
                        "anchors": [
                            {"base_token": base, "model_candidates": 0, "sizing_residuals": 2}
                            for base in (WETH, USDC)
                        ],
                    }
                    for name, size in COHORTS.items()
                },
            },
        },
    }


def test_shadow_denominator_keeps_model_and_missing_whole_stages() -> None:
    first = _row(1, PARENT, "0x" + "aa" * 32, 10)
    second = _row(2, CHILD, PARENT, 20)
    out = summarize_base_shadow([first, second])
    assert out["head_relations"] == {"first": 1, "direct_child": 1}
    assert out["positive_survival_rate"] is None and out["whole_transaction_simulations"] == 0
    assert out["heads"][1]["head_to_state_ns"] == 4
    assert out["heads"][1]["queue_before_state_ns"] == 2
    assert out["heads"][1]["cohorts"]["mixed_66"]["unresolved_sizing_intervals"] == 4
    fork = deepcopy(second)
    fork["shadow_cycle_lifecycle"]["mixed_base_state"]["newheads_observation"]["head"][
        "parent_hash"
    ] = CHILD
    with pytest.raises(ValueError, match="parent hash conflict"):
        summarize_base_shadow([first, fork])


def test_arrival_deadlines_measure_observed_children_only() -> None:
    first = _row(1, PARENT, "0x" + "aa" * 32, 10)
    second = _row(2, CHILD, PARENT, 16)
    third = _row(3, "0x" + "56" * 32, CHILD, 30)
    third["block_number"] = 4
    third["shadow_cycle_lifecycle"]["mixed_base_state"]["newheads_observation"]["head"][
        "height"
    ] = 4

    result = summarize_base_shadow([first, second, third], arrival_deadlines=True)
    report = result["arrival_deadlines"]
    assert report["witnessed_direct_child_pairs"] == 1
    assert report["state_ready_before_child_observed"] == 1
    assert report["discovery_finished_before_child_observed"] == 0
    assert report["blocks_without_observed_direct_child"] == [2, 4]
    assert report["pairs"][0]["child_receive_minus_discovery_finished_ns"] == -2
