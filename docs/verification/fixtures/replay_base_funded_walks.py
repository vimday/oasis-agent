"""Compare fixed-input walks and unchanged routes at four retained Base states, offline."""

import argparse
import json
from pathlib import Path

from oasis_agent.fastpath.bridge import FastpathCallError
from oasis_agent.fastpath.funded_walk import WalkEdge, search_funded_walks
from oasis_agent.fastpath.live_shadow_runner import canonical_base_weth_usdc_pools
from oasis_agent.fastpath.pool_graph import FastpathPoolGraph
from oasis_agent.fastpath.slipstream_depth import DepthCoverageError
from oasis_agent.models.common import canonical_json, sha256_hex
from scripts.replay_base_slipstream_depth import POOL, _inputs, _measure, _plan, _quote_control

WETH = "0x4200000000000000000000000000000000000006"
AMOUNT = 10**15


def quoter(block, depth):
    graph = FastpathPoolGraph()
    for node in block.state.nodes:
        graph.upsert_pool(node)

    def quote(edge, amount):
        if edge.venue == POOL:
            try:
                direction = edge.token_in == block.pool.token0
                result = depth.quote(amount, zero_for_one=direction).amount_out
            except DepthCoverageError:
                return None
            if result > block.state.address_state.balances_by_pool[POOL][int(direction)]:
                raise ValueError("output exceeds bound pool balance")
            return result
        try:
            leg = graph.quote_pool(8453, edge.venue, edge.token_in, amount)
        except FastpathCallError as exc:
            if "insufficient pool liquidity" in str(exc):
                return None
            raise
        if (leg.token_in, leg.amount_in, leg.token_out) != (edge.token_in, amount, edge.token_out):
            raise ValueError("incomplete exact-input fill")
        return leg.amount_out

    return quote


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("research_root", type=Path)
    root = parser.parse_args().research_root
    blocks, identity = _inputs(root / "base-newheads-overlap-four-20261124/run")
    scope = root / "base-slipstream-depth-20261127"
    control = root / "slipstream-probe-20261127"
    plan = canonical_json(_plan(blocks, identity, (control / "run/report.json").read_bytes()))
    if plan != (scope / "plan.json").read_bytes():
        raise ValueError("Base plan differs")
    report, depths = _measure(blocks, scope / "run", replay=True)
    report["plan_sha256"] = sha256_hex(plan)
    if (
        canonical_json(report) != (scope / "run/capture.json").read_bytes()
        or report["status"] != "complete"
    ):
        raise ValueError("depth report differs")
    _quote_control(control, blocks[0], depths[blocks[0].state.block.number])
    control_ids = {pool.address for pool in canonical_base_weth_usdc_pools()}
    quotes = [quoter(block, depths[block.state.block.number]) for block in blocks]
    rows = []
    for i, block in enumerate(blocks):
        address_nodes = block.state.address_state.pool_nodes
        for count, nodes in (
            (4, [n for n in address_nodes if n.pool_id in control_ids]),
            (58, address_nodes),
            (66, block.state.nodes),
        ):
            if len(nodes) != count:
                raise ValueError("changed Base cohort")
            edges = [
                WalkEdge(n.pool_id, a, b)
                for n in nodes
                for a, b in ((n.token0, n.token1), (n.token1, n.token0))
            ]
            for repeat in (False, True):
                result = search_funded_walks(
                    edges,
                    anchor=WETH,
                    amount_in=AMOUNT,
                    quote=quotes[i],
                    cost_by_hops=(0,) * 13,
                    min_hops=2,
                    max_edge_visits=2000,
                    top_k=5,
                    revisit_assets=repeat,
                )
                paths = []
                for candidate in result.top:
                    child = None
                    if i + 1 < len(blocks):
                        child = AMOUNT
                        for edge in candidate.edges:
                            child = quotes[i + 1](edge, child)
                            if not child:
                                break
                    paths.append(
                        {
                            "venues": [e.venue for e in candidate.edges],
                            "output": str(candidate.amounts[-1]),
                            "direct_child_output": str(child) if child is not None else None,
                        }
                    )
                rows.append(
                    {
                        "height": block.state.block.number,
                        "block_hash": block.state.block.block_hash,
                        "pools": count,
                        "revisit_assets": repeat,
                        "edge_visits": result.edge_visits,
                        "complete": result.complete,
                        "quotes": result.quotes,
                        "missing": result.missing_quotes,
                        "gross_positive": result.positive_gross,
                        "closures": result.closures,
                        "paths": paths,
                    }
                )
    print(json.dumps(rows, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
