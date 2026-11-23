"""Find long simple marginal cycles in the retained BSC V2 pool snapshot."""

import argparse
import hashlib
import json
import time
from collections import Counter
from dataclasses import asdict
from fractions import Fraction
from pathlib import Path
from typing import Any

from oasis_agent.fastpath.long_cycle import bound_simple_cycles, search_long_marginal_cycle
from oasis_agent.fastpath.marginal_graph import MarginalEdge

if __package__:
    from .replay_v2_pool_expansion import _build_route, _read_snapshot
else:
    from replay_v2_pool_expansion import _build_route, _read_snapshot

BANDS = ((12, 24), (25, 100), (101, 300))
MAX_EDGE_VISITS = 250_000
DEEPER_BANDS = ((20, 20), (19, 19), (18, 18), (17, 17), (16, 16), (15, 15))


def _standalone_std_v2_gas_floor(hops: int) -> int:
    """Single route with distinct, cold standard V2 pairs and no access list."""
    # Every pair emits Sync and Swap. Include only intrinsic gas, cold
    # accesses and these logs, then grant the maximum transaction refund.
    before_refund = 21_000 + hops * (2600 + 1_262 + 2_524)
    return before_refund - before_refund // 5


def _best_rate_per_direction(edges: list[MarginalEdge]) -> list[MarginalEdge]:
    """Dominance for marginal existence at length >= 3 in this two-asset pool graph."""
    pairs_by_pool: dict[str, tuple[str, str]] = {}
    best: dict[tuple[str, str], MarginalEdge] = {}
    for edge in edges:
        direction = edge.token_in, edge.token_out
        token0, token1 = sorted(direction)
        pair = token0, token1
        if pairs_by_pool.setdefault(edge.pool, pair) != pair:
            raise ValueError("parallel rate dominance requires unique token pairs per pool")
        prior = best.get(direction)
        if prior is None or edge.rate > prior.rate:
            best[direction] = edge
    return list(best.values())


def analyze(
    raw: bytes,
    *,
    deeper: bool = False,
    changed_pools: frozenset[str] | None = None,
    standalone_economics: bool = False,
) -> dict[str, Any]:
    snapshot = json.loads(raw)
    if snapshot["status"] != "complete" or snapshot["pin_rechecked"] is not True:
        raise ValueError("complete retained block-pinned snapshot required")
    pricing = _read_snapshot(raw, 56, None, None) if standalone_economics else None
    edges = []
    active: Counter[str] = Counter()
    for pool in snapshot["pools"]:
        reserve0, reserve1 = int(pool["r0"]), int(pool["r1"])
        multiplier, denominator = pool["fee_multiplier"], pool["fee_denominator"]
        if min(reserve0, reserve1) < 0 or not 0 < multiplier <= denominator:
            raise ValueError("invalid V2 reserve or fee")
        if min(reserve0, reserve1) == 0:
            continue
        active[pool["venue"]] += 1
        edges.extend(
            [
                MarginalEdge(
                    pool["token0"],
                    pool["token1"],
                    pool["pool"],
                    Fraction(reserve1 * multiplier, reserve0 * denominator),
                ),
                MarginalEdge(
                    pool["token1"],
                    pool["token0"],
                    pool["pool"],
                    Fraction(reserve0 * multiplier, reserve1 * denominator),
                ),
            ]
        )
    topology = bound_simple_cycles(edges)
    # A pool discarded by marginal dominance may be the required changed pool.
    projection = deeper and changed_pools is None
    searched = _best_rate_per_direction(edges) if projection else edges
    bands = []
    for minimum, maximum in DEEPER_BANDS if deeper else BANDS:
        start = time.perf_counter_ns()
        result = search_long_marginal_cycle(
            searched,
            min_hops=minimum,
            max_hops=maximum,
            max_edge_visits=MAX_EDGE_VISITS,
            required_pools=changed_pools,
        )
        witness = result.witness
        band: dict[str, Any] = {
            "min_hops": minimum,
            "max_hops": maximum,
            "max_edge_visits": MAX_EDGE_VISITS,
            "edge_visits": result.edge_visits,
            "analysis_ns": time.perf_counter_ns() - start,
            "status": "topology_excluded" if minimum > topology.max_hops_upper else "exhaustive",
            "witness": None,
        }
        if result.budget_exhausted:
            band["status"] = "budget_exhausted"
        if witness is not None:
            gain = witness.gain
            band["status"] = "positive_marginal_witness"
            band["witness"] = {
                "length": len(witness.edges),
                "gain_numerator": str(gain.numerator),
                "gain_denominator": str(gain.denominator),
                "gain_bps": float((gain - 1) * 10_000),
                "edges": [
                    {
                        "token_in": edge.token_in,
                        "token_out": edge.token_out,
                        "pool": edge.pool,
                        "rate": [str(edge.rate.numerator), str(edge.rate.denominator)],
                    }
                    for edge in witness.edges
                ],
            }
            if pricing is not None:
                anchor = next(
                    (
                        index
                        for index, edge in enumerate(witness.edges)
                        if edge.token_in == pricing.anchor
                    ),
                    None,
                )
                if anchor is None:
                    band["standalone_amount_screen"] = {"status": "unpriced_anchor"}
                else:
                    ordered = witness.edges[anchor:] + witness.edges[:anchor]
                    route = _build_route(pricing, tuple(edge.pool for edge in ordered))
                    gas_units = _standalone_std_v2_gas_floor(len(ordered))
                    gas_wei = gas_units * pricing.gas_price
                    bounds = route.all_input_bounds(total_cost_in_anchor=gas_wei)
                    band["standalone_amount_screen"] = {
                        "status": "excluded_by_net_bound"
                        if bounds.net_upper <= 0
                        else "requires_sizing_and_simulation",
                        "anchor": pricing.anchor,
                        "gas_floor_units": gas_units,
                        "gas_price_wei": str(pricing.gas_price),
                        "gas_floor_wei": str(gas_wei),
                        "gross_upper_wei": str(bounds.gross_upper),
                        "net_upper_wei": str(bounds.net_upper),
                    }
        bands.append(band)
    return {
        "schema": "oasis-bsc-long-marginal-cycles-v1",
        "input_sha256": hashlib.sha256(raw).hexdigest(),
        "head": snapshot["head"],
        "pool_rows": len(snapshot["pools"]),
        "positive_reserve_pools_by_venue": dict(active),
        "search_directed_edges": len(searched),
        "projection": "maximum marginal rate per direction"
        if projection
        else "all pool directions",
        "changed_pool_scope": None if changed_pools is None else sorted(changed_pools),
        "changed_pools_in_graph": None
        if changed_pools is None
        else sorted(changed_pools & {edge.pool for edge in edges}),
        "standalone_economics": standalone_economics,
        "topology": {
            **asdict(topology),
            "max_hops_upper": topology.max_hops_upper,
        },
        "depth_bands": bands,
        "model": "Distinct assets and pools at one snapped state. Positive marginal witness is "
        "a finite-size pricing candidate. No gas, token restrictions or atomic spend modeled.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--deeper", action="store_true")
    parser.add_argument(
        "--changed-pool",
        action="append",
        help="Keep paths touching at least one listed pool, from a separately verified update",
    )
    parser.add_argument(
        "--standalone-economics",
        action="store_true",
        help="Apply an all-input WBNB profit bound and scenario gas floor to each found witness",
    )
    args = parser.parse_args()
    result = analyze(
        args.snapshot.read_bytes(),
        deeper=args.deeper,
        changed_pools=frozenset(pool.lower() for pool in args.changed_pool)
        if args.changed_pool
        else None,
        standalone_economics=args.standalone_economics,
    )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
