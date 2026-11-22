from collections import Counter

import pytest

from scripts.run_multihop_toy_controls import (
    CAPITAL_TOKEN,
    EXIT,
    MERGE_INPUT,
    MERGE_POOLS,
    RING_POOLS,
    Step,
    branch_steps,
    cycles,
    inventory,
    merge_control,
    ring_control,
    simulate,
)


def test_six_hop_control_exhausts_same_graph_and_capital_range() -> None:
    short, long = ring_control(4), ring_control(6)
    assert sorted(len(route) for route in cycles(RING_POOLS, 4)) == [2, 2]
    assert sorted(len(route) for route in cycles(RING_POOLS, 6)) == [2, 2, 6, 6, 6, 6]
    assert short["positive_routes"] == 0
    assert short["best_net_including_hold"] == 0
    assert long["positive_routes"] == 2
    assert long["best_net_including_hold"] == 134


def test_split_pays_once_for_common_exit_and_does_not_duplicate_reserves() -> None:
    result = merge_control()
    assert result["feasible_points"] == result["allocation_points"] == 601
    assert result["best_single"] == {
        "left": 600,
        "right": 0,
        "amount_in": 600,
        "amount_out": 651,
        "swaps": 3,
        "cost": 9,
        "net": 42,
    }
    assert result["best_merged"] == {
        "left": 292,
        "right": 308,
        "amount_in": 600,
        "amount_out": 731,
        "swaps": 5,
        "cost": 15,
        "net": 116,
    }
    assert result["same_allocation_two_mutating_exit_swaps"] == {
        "amount_in": 600,
        "amount_out": 730,
        "swaps": 6,
        "cost": 18,
        "net": 112,
    }
    assert result["fresh_reserve_per_branch_incorrect_output"] == 745
    assert result["fresh_reserve_overstatement_vs_mutating_two_exits"] == 15


def test_integer_swaps_conserve_assets_and_settle_all_intermediate_inventory() -> None:
    merged_steps = [step for branch in branch_steps(292, 308) for step in branch]
    outcome = simulate(MERGE_POOLS, MERGE_INPUT, (*merged_steps, EXIT))
    assert outcome is not None
    assert outcome.final_pools[-1].reserve0 == 15_619
    assert outcome.final_pools[-1].reserve1 == 17_769
    assert inventory(
        outcome.final_pools, Counter({CAPITAL_TOKEN: outcome.amount_out})
    ) == inventory(MERGE_POOLS, Counter({CAPITAL_TOKEN: MERGE_INPUT}))
    assert simulate(RING_POOLS, 1, (Step("AB", "A"),)) is None
    with pytest.raises(ValueError, match="funded token balance"):
        simulate(MERGE_POOLS, 600, (Step("AB", "A", 601),))
