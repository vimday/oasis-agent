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

BANDS = ((12, 24), (25, 100), (101, 300))
MAX_EDGE_VISITS = 250_000
DEEPER_BANDS = ((20, 20), (19, 19), (18, 18), (17, 17), (16, 16), (15, 15))


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


def analyze(raw: bytes, *, deeper: bool = False) -> dict[str, Any]:
    snapshot = json.loads(raw)
    if snapshot["status"] != "complete" or snapshot["pin_rechecked"] is not True:
        raise ValueError("complete retained block-pinned snapshot required")
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
    searched = _best_rate_per_direction(edges) if deeper else edges
    bands = []
    for minimum, maximum in DEEPER_BANDS if deeper else BANDS:
        start = time.perf_counter_ns()
        result = search_long_marginal_cycle(
            searched, min_hops=minimum, max_hops=maximum, max_edge_visits=MAX_EDGE_VISITS
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
        bands.append(band)
    return {
        "schema": "oasis-bsc-long-marginal-cycles-v1",
        "input_sha256": hashlib.sha256(raw).hexdigest(),
        "head": snapshot["head"],
        "pool_rows": len(snapshot["pools"]),
        "positive_reserve_pools_by_venue": dict(active),
        "search_directed_edges": len(searched),
        "projection": "maximum marginal rate per direction" if deeper else "all pool directions",
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
    args = parser.parse_args()
    result = analyze(args.snapshot.read_bytes(), deeper=args.deeper)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
