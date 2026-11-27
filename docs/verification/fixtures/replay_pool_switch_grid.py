"""Replay the equal-starting-principal switching sensitivity on the retained BSC reserve state."""

import json
from hashlib import sha256
from pathlib import Path

from scripts.replay_v2_batch_comparison import analyze

from .replay_fixed_lot_batch_20261127 import PLANS, SNAPSHOT

SCOPE = Path(__file__).resolve().parents[1] / "POOL_SWITCH_SCOPE_20261127.json"


def selected_fee_check(prefix, orders):
    if not prefix:
        return None
    paths = {order["id"]: order["pools"] for order in orders}
    visits = [pool for row in prefix for pool in paths[row["order_id"]]]
    distinct = len(set(visits))
    # Same conditional standard-V2 cost floor as the original BSC fee-floor replay.
    gas = 21_000 + 3_786 * len(visits) + 2_400 * (distinct - 1) + 1_900 * distinct
    floor_gas = gas - gas // 5
    gross = sum(row["gross"] for row in prefix)
    return {
        "market_legs": len(visits),
        "distinct_pools": distinct,
        "gross_wbnb_wei": gross,
        "standard_v2_minimum_gas": floor_gas,
        "gas_price_wei": 50_000_000,
        "net_upper_wbnb_wei": gross - 50_000_000 * floor_gas,
    }


def main():
    scope = json.loads(SCOPE.read_bytes())
    snapshot = SNAPSHOT.read_bytes()
    if sha256(snapshot).hexdigest() != scope["snapshot_sha256"]:
        raise ValueError("reserve snapshot differs from the predeclared scope")
    results = []
    for name, digest in sorted(scope["plans"].items()):
        raw = (PLANS / f"{name}.plan.json").read_bytes()
        if sha256(raw).hexdigest() != digest:
            raise ValueError("source menu differs from the predeclared scope")
        plan = json.loads(raw)
        amounts = sorted({order["amount_in"] for order in plan["orders"]})
        median = amounts[(len(amounts) - 1) // 2]
        plan["schema"] = "oasis-v2-batch-search-plan-v4"
        plan["search"].update(
            max_repeat_rounds=scope["max_rounds"],
            max_switch_leg_evaluations=scope["max_switch_leg_evaluations"],
        )
        for numerator, denominator in scope["principal_ratios"]:
            seed = max(1, median * numerator // denominator)
            for order in plan["orders"]:
                order["amount_in"] = seed
            report = analyze(snapshot, json.dumps(plan, sort_keys=True).encode(), chain=56)
            greedy = report["full_output_pool_switches"]
            fixed = report["fixed_principal_repetitions"]
            single = report["full_output_reinvestments"]
            (group,) = greedy["groups"]
            prefix = group["prefixes"][: greedy["best_rounds"]]
            results.append(
                {
                    "menu": name,
                    "principal_multiplier": [numerator, denominator],
                    "common_first_principal_wbnb_wei": seed,
                    "candidate_routes": len(plan["orders"]),
                    "same_route_fixed_best_net_wbnb_wei": fixed["best_net"],
                    "same_route_reinvest_best_net_wbnb_wei": single["best_net"],
                    "switch_greedy_best_net_wbnb_wei": greedy["best_net"],
                    "switch_delta_vs_single_reinvestment_wbnb_wei": greedy["best_net"]
                    - single["best_net"],
                    "switch_greedy_best_rounds": greedy["best_rounds"],
                    "selected_sequence": [r["order_id"] for r in prefix],
                    "post_selection_fee_check": selected_fee_check(prefix, plan["orders"]),
                    "switch_greedy_modeled_cost_wbnb_wei": greedy["best_cost"],
                    "evaluated_legs": greedy["search_leg_evaluations"],
                    "budget_exhausted": greedy["budget_exhausted"],
                    "stop": group["stop_reason"],
                }
            )
    print(
        json.dumps(
            {
                "scope_sha256": sha256(SCOPE.read_bytes()).hexdigest(),
                "owner_net_wei": None,
                "execution_eligible": False,
                "results": results,
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
