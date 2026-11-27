from hashlib import sha256
from json import dumps

import pytest

from scripts.replay_v2_batch_comparison import analyze
from scripts.replay_v2_pool_expansion import WBNB


def test_batch_plan_reserves_shared_cost_and_compares_best_single_or_hold() -> None:
    asset = "0x" + "01" * 20
    first, second = ("0x" + f"{n:040x}" for n in (11, 12))
    snapshot = dumps(
        {
            "status": "complete",
            "pin_rechecked": True,
            "head": {"number": "0x2a", "hash": "0x" + "01" * 32},
            "gas_price_wei": 1,
            "pools": [
                {
                    "pool": pool,
                    "token0": asset,
                    "token1": WBNB,
                    "r0": reserves[0],
                    "r1": reserves[1],
                    "fee_multiplier": 9975,
                    "fee_denominator": 10000,
                    "venue": "pancake",
                    "strata": ["named_pairs"],
                }
                for pool, reserves in ((first, (2_000, 1_000)), (second, (1_000, 1_000)))
            ],
        }
    ).encode()
    order = {
        "id": "route",
        "pools": [first, second],
        "amount_in": 100,
        "min_net": 1,
        "separate_success_cost": 100,
        "separate_failure_cost": 20,
        "group_success_cost": 1,
        "group_failure_cost": 5,
    }
    plan = {
        "schema": "oasis-v2-batch-plan-v1",
        "snapshot_sha256": sha256(snapshot).hexdigest(),
        "initial_capital": 1_000,
        "batch_fixed_cost": 15,
        "orders": [order, {**order, "id": "same-route-again"}],
    }
    result = analyze(snapshot, dumps(plan).encode(), chain=56)
    assert result["best_standalone_or_hold_net"] == 0
    assert result["ordered_separate"]["net"] == -40
    assert result["ordered_batch"]["results"][0]["capital_before"] == 985
    assert result["ordered_batch"]["net"] == result["ordered_batch"]["final_capital"] - 1_000
    assert (
        result["ordered_batch"]["gross"] - result["ordered_batch"]["cost"]
        == (result["ordered_batch"]["net"])
    )
    assert result["ordered_batch"]["net"] < result["batch_reset_pool_diagnostic"]["net"]
    assert result["batch_delta_vs_ordered_separate"] == result["ordered_batch"]["net"] + 40
    assert "search" not in result

    search_plan = {
        **plan,
        "schema": "oasis-v2-batch-search-plan-v1",
        "search": {"max_batch_orders": 2, "beam_width": 4, "max_leg_evaluations": 20},
    }
    report = analyze(snapshot, dumps(search_plan).encode(), chain=56)
    assert report["schema"] == "oasis-v2-batch-search-report-v1"
    assert report["execution_eligible"] is False
    search = report["search"]
    assert search["complete_in_declared_scope"] is True
    assert search["extensions_evaluated"] == sum(search["extension_status_counts"].values()) == 4
    assert search["best_net"] == search["best_batch"]["net"] > 0
    assert search["best_batch_marginals"][0]["without_order_net"] == 0
    repeated_plan = {
        **search_plan,
        "schema": "oasis-v2-batch-search-plan-v2",
        "search": {**search_plan["search"], "max_repeat_rounds": 71},
    }
    repeated = analyze(snapshot, dumps(repeated_plan).encode(), chain=56)
    assert repeated["execution_eligible"] is False
    assert repeated["search"] == search
    comparison = repeated["fixed_principal_repetitions"]
    assert comparison["best_route_id"] == "route"
    assert comparison["best_rounds"] == 1
    assert comparison["best_net"] == search["best_net"]
    assert comparison["routes"][0]["stop_reason"] == "below_minimum_net"
    assert comparison["routes"][0]["prefixes"][0].keys() == {
        "rounds",
        "amount_out",
        "marginal_net",
        "cumulative_net",
    }
    assert "fixed_principal_repetitions" not in report
    assert "full_output_reinvestments" not in repeated

    reinvest_plan = {
        **repeated_plan,
        "schema": "oasis-v2-batch-search-plan-v3",
        "orders": [{**order, "amount_in": 10}],
    }
    reinvest = analyze(snapshot, dumps(reinvest_plan).encode(), chain=56)
    assert reinvest["schema"] == "oasis-v2-batch-search-report-v3"
    assert reinvest["execution_eligible"] is False
    fixed = reinvest["fixed_principal_repetitions"]
    rolling = reinvest["full_output_reinvestments"]
    fixed_rows, rolling_rows = (summary["routes"][0]["prefixes"] for summary in (fixed, rolling))
    assert len(fixed_rows) > 1 and len(rolling_rows) > 1
    assert fixed_rows[0] == rolling_rows[0]
    assert fixed_rows[1]["amount_in"] == 10
    assert rolling_rows[1]["amount_in"] == rolling_rows[0]["amount_out"]
    assert rolling_rows[1]["cumulative_cost"] == 17
    assert rolling["best_cost"] == rolling_rows[rolling["best_rounds"] - 1]["cumulative_cost"]
    assert rolling["delta_vs_fixed_principal_or_hold"] == rolling["best_net"] - fixed["best_net"]
    assert "full_output_pool_switches" not in reinvest

    switch_plan = {
        **reinvest_plan,
        "schema": "oasis-v2-batch-search-plan-v4",
        "search": {**reinvest_plan["search"], "max_switch_leg_evaluations": 5},
    }
    switch = analyze(snapshot, dumps(switch_plan).encode(), chain=56)
    assert switch["schema"] == "oasis-v2-batch-search-report-v4"
    assert switch["fixed_principal_repetitions"] == fixed
    assert switch["full_output_reinvestments"] == rolling
    assert switch["search"] == reinvest["search"]
    greedy = switch["full_output_pool_switches"]
    assert greedy["strategy"] == "greedy_per_equal_initial_principal"
    assert greedy["best_order_ids"] == ["route", "route"]
    assert greedy["budget_exhausted"] and not greedy["complete_for_greedy"]
    assert greedy["quoted_legs"] == greedy["search_leg_evaluations"] == 4
    assert greedy["groups"][0]["prefixes"][1]["amount_in"] == rolling_rows[0]["amount_out"]
    with pytest.raises(ValueError, match="invalid batch search fields"):
        analyze(
            snapshot, dumps({**switch_plan, "search": reinvest_plan["search"]}).encode(), chain=56
        )
    with pytest.raises(ValueError, match="invalid leg evaluation budget"):
        limited = {**search_plan, "search": {**search_plan["search"], "max_leg_evaluations": 0}}
        analyze(snapshot, dumps(limited).encode(), chain=56)

    with pytest.raises(ValueError, match="bind the retained snapshot"):
        analyze(snapshot + b" ", dumps(plan).encode(), chain=56)
    with pytest.raises(ValueError, match="ambiguous JSON"):
        analyze(snapshot, b'{"batch_fixed_cost": 25,' + dumps(plan).encode()[1:], chain=56)
    with pytest.raises(ValueError, match="ambiguous JSON"):
        analyze(b'{"status": "incomplete",' + snapshot[1:], dumps(plan).encode(), chain=56)
