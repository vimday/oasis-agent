"""Compare the original frozen V2 batch menus and eight fixed-lot sizes on the same BSC state."""

import argparse
import json
from collections import Counter
from hashlib import sha256
from pathlib import Path

from scripts.replay_v2_batch_comparison import analyze

ROOT = Path("reports/research")
PLANS = ROOT / "multihop-prelive-engineering-20261127"
SCOPE = ROOT / "gold-book-20261127/lot-sensitivity-scope.json"
SNAPSHOT = ROOT / "v2-batch-20261123/snapshot.json"


def main(*, reinvest):
    scope = json.loads(SCOPE.read_bytes())
    snapshot = SNAPSHOT.read_bytes()
    if sha256(snapshot).hexdigest() != scope["snapshot_sha256"]:
        raise ValueError("retained snapshot digest differs")
    results = []
    for name, digest in sorted(scope["plans"].items()):
        raw = (PLANS / f"{name}.plan.json").read_bytes()
        if sha256(raw).hexdigest() != digest:
            raise ValueError("frozen candidate menu digest differs")
        for divisor in scope["divisors"]:
            plan = json.loads(raw)
            plan["schema"] = (
                "oasis-v2-batch-search-plan-v3" if reinvest else "oasis-v2-batch-search-plan-v2"
            )
            plan["search"]["max_repeat_rounds"] = scope["rounds"]
            for order in plan["orders"]:
                order["amount_in"] = max(1, order["amount_in"] // divisor)
            report = analyze(snapshot, json.dumps(plan, sort_keys=True).encode(), chain=56)
            repeat = report["fixed_principal_repetitions"]
            routes = repeat["routes"]
            if reinvest:
                full = report["full_output_reinvestments"]
                results.append(
                    {
                        "menu": name,
                        "divisor": divisor,
                        "fixed_best_rounds": repeat["best_rounds"],
                        "fixed_best_net_wbnb_wei": repeat["best_net"],
                        "reinvest_best_rounds": full["best_rounds"],
                        "reinvest_best_net_wbnb_wei": full["best_net"],
                        "reinvest_best_route": full["best_route_id"],
                        "reinvest_minus_fixed_best_wbnb_wei": full["best_net"] - repeat["best_net"],
                        "reinvest_evaluated_prefixes": sum(
                            len(row["prefixes"]) for row in full["routes"]
                        ),
                    }
                )
                continue
            win = next((r for r in routes if r["order_id"] == repeat["best_route_id"]), None)
            results.append(
                {
                    "menu": name,
                    "divisor": divisor,
                    "route_count": len(routes),
                    "best_route": repeat["best_route_id"],
                    "best_rounds": repeat["best_rounds"],
                    "best_net_wbnb_wei": repeat["best_net"],
                    "best_principal_wbnb_wei": win["principal_per_round"] if win else None,
                    "best_first_prefix_net_wbnb_wei": win["prefixes"][0]["cumulative_net"]
                    if win
                    else None,
                    "best_distinct_order_search_wbnb_wei": report["search"]["best_net"],
                    "distinct_order_search_complete": report["search"][
                        "complete_in_declared_scope"
                    ],
                    "evaluated_feasible_prefixes": sum(len(r["prefixes"]) for r in routes),
                    "stop_histogram": dict(Counter(r["stop_reason"] for r in routes)),
                    "positive_routes": sum(r["best_net"] > 0 for r in routes),
                }
            )
    print(
        json.dumps(
            {
                "scope": scope,
                "scope_sha256": sha256(SCOPE.read_bytes()).hexdigest(),
                "actual_owner_pnl": None,
                "execution_eligible": False,
                "results": results,
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reinvest", action="store_true", help="compare both funding policies")
    main(reinvest=parser.parse_args().reinvest)
