"""Compare fixed V2 cycle orders on one retained snapshot with explicit cost scenarios."""

import argparse
import json
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path

from oasis_agent.evidence.rpc_validation import strict_json_object
from oasis_agent.fastpath.v2_batch import V2BatchOrder, V2BatchRun, compare_v2_batch
from oasis_agent.fastpath.v2_sizing import MAX_V2_LEGS

if __package__:
    from .replay_v2_pool_expansion import _build_route, _hex, _read_snapshot, _uint
else:
    from replay_v2_pool_expansion import _build_route, _hex, _read_snapshot, _uint

_PLAN_SCHEMA = "oasis-v2-batch-plan-v1"


def analyze(
    snapshot_raw: bytes,
    plan_raw: bytes,
    *,
    chain: int,
    eth_scope: bytes | None = None,
    eth_baseline: bytes | None = None,
) -> dict:
    snapshot = _read_snapshot(snapshot_raw, chain, eth_scope, eth_baseline)
    plan = strict_json_object(plan_raw)
    if plan.keys() != {
        "schema",
        "snapshot_sha256",
        "initial_capital",
        "batch_fixed_cost",
        "orders",
    }:
        raise ValueError("invalid batch plan fields")
    if plan["schema"] != _PLAN_SCHEMA or plan["snapshot_sha256"] != snapshot.digest:
        raise ValueError("batch plan must bind the retained snapshot")
    capital, fixed = _uint(plan["initial_capital"]), _uint(plan["batch_fixed_cost"])
    if fixed >= capital:
        raise ValueError("batch fixed cost must leave positive working capital")
    rows = plan["orders"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= 64:
        raise ValueError("expected 1 to 64 ordered cycles")

    separate = []
    shared = []
    for row in rows:
        if not isinstance(row, dict) or row.keys() != {
            "id",
            "pools",
            "amount_in",
            "min_net",
            "separate_success_cost",
            "separate_failure_cost",
            "group_success_cost",
            "group_failure_cost",
        }:
            raise ValueError("invalid batch order fields")
        pools = row["pools"]
        if not isinstance(pools, list) or not 2 <= len(pools) <= MAX_V2_LEGS:
            raise ValueError("invalid order pool count")
        pool_ids = tuple(_hex(pool, 40) for pool in pools)
        if any(pool not in snapshot.pools for pool in pool_ids):
            raise ValueError("order references a pool outside the active snapshot")
        route = _build_route(snapshot, pool_ids)
        order_id, amount, minimum = row["id"], _uint(row["amount_in"]), _uint(row["min_net"])
        for prefix, target in (("separate", separate), ("group", shared)):
            target.append(
                V2BatchOrder(
                    order_id=order_id,
                    route=route,
                    amount_in=amount,
                    success_cost_in_anchor=_uint(row[f"{prefix}_success_cost"]),
                    failure_cost_in_anchor=_uint(row[f"{prefix}_failure_cost"]),
                    min_net_in_anchor=minimum,
                )
            )

    baseline = compare_v2_batch(separate, initial_capital=capital, anchor_token=snapshot.anchor)
    batch = compare_v2_batch(shared, initial_capital=capital - fixed, anchor_token=snapshot.anchor)
    batch_net = batch.ordered.net_profit - fixed
    best_or_hold = max(0, baseline.best_standalone_net)

    return {
        "schema": "oasis-v2-batch-comparison-v1",
        "status": "constant_product_model_only",
        "chain_id": snapshot.chain,
        "block_number": snapshot.block,
        "block_hash": snapshot.block_hash,
        "snapshot_sha256": snapshot.digest,
        "plan_sha256": sha256(plan_raw).hexdigest(),
        "anchor_token": snapshot.anchor,
        "initial_capital": capital,
        "ordered_separate": _series(baseline.ordered, fixed_cost=0),
        "ordered_batch": _series(batch.ordered, fixed_cost=fixed),
        "batch_reset_pool_diagnostic": _series(batch.independent, fixed_cost=fixed),
        "best_standalone_or_hold_net": best_or_hold,
        "batch_delta_vs_ordered_separate": batch_net - baseline.ordered.net_profit,
        "batch_delta_vs_best_standalone_or_hold": batch_net - best_or_hold,
    }


def _series(run: V2BatchRun, *, fixed_cost: int) -> dict:
    return {
        "net": run.net_profit - fixed_cost,
        "gross": run.gross_profit,
        "cost": run.cost_in_anchor + fixed_cost,
        "final_capital": run.final_capital,
        "fixed_cost": fixed_cost,
        "success_count": run.success_count,
        "results": [asdict(result) for result in run.results],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("plan", type=Path)
    parser.add_argument("--chain", type=int, choices=(1, 56), required=True)
    parser.add_argument("--eth-scope", type=Path)
    parser.add_argument("--eth-baseline", type=Path)
    args = parser.parse_args()
    result = analyze(
        args.snapshot.read_bytes(),
        args.plan.read_bytes(),
        chain=args.chain,
        eth_scope=args.eth_scope.read_bytes() if args.eth_scope else None,
        eth_baseline=args.eth_baseline.read_bytes() if args.eth_baseline else None,
    )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
