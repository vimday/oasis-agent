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

    with pytest.raises(ValueError, match="bind the retained snapshot"):
        analyze(snapshot + b" ", dumps(plan).encode(), chain=56)
    with pytest.raises(ValueError, match="ambiguous JSON"):
        analyze(snapshot, b'{"batch_fixed_cost": 25,' + dumps(plan).encode()[1:], chain=56)
    with pytest.raises(ValueError, match="ambiguous JSON"):
        analyze(b'{"status": "incomplete",' + snapshot[1:], dumps(plan).encode(), chain=56)
