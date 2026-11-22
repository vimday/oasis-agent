"""Analyze topology and integer V2 depth against one captured Ethereum snapshot."""

import argparse
import itertools
import json
import random
from collections import Counter, defaultdict, deque

if __package__:
    from . import capture_eth_v2_tail_depth as capture
    from . import run_eth_v2_forward_shadow as core
    from . import run_eth_v2_wide_shadow as wide
else:
    import capture_eth_v2_tail_depth as capture
    import run_eth_v2_forward_shadow as core
    import run_eth_v2_wide_shadow as wide

WETH = core.ASSETS["WETH"][0]
USDC = core.ASSETS["USDC"][0]
TARGETS = (2, 4, 6, 10, 20, 50, 100, 200, 400)
SIZES = tuple(wide.GRID)
PREFIX_LIMIT = 1_000_000


def build_graph(pools):
    by_pool = {}
    graph = defaultdict(list)
    for pool in pools:
        if min(pool["reserves"]) <= 0:
            continue
        address = pool["address"]
        first, second = pool["tokens"]
        by_pool[address] = pool
        graph[first].append((second, address))
        graph[second].append((first, address))
    return by_pool, graph


def rooted_bridges(graph):
    discovery, low, bridges = {}, {}, set()

    def visit(token, incoming):
        discovery[token] = low[token] = len(discovery)
        for other, pool in graph[token]:
            if pool == incoming:
                continue
            if other not in discovery:
                visit(other, pool)
                low[token] = min(low[token], low[other])
                if low[other] > discovery[token]:
                    bridges.add(pool)
            else:
                low[token] = min(low[token], discovery[other])

    visit(WETH, None)
    return discovery, bridges


def rooted_simple_biconnected_sizes(graph):
    neighbors = {token: sorted({other for other, _ in links}) for token, links in graph.items()}
    discovery, low, stack, sizes = {}, {}, [], []

    def visit(token, parent):
        discovery[token] = low[token] = len(discovery)
        for other in neighbors[token]:
            if other == parent:
                continue
            if other not in discovery:
                stack.append((token, other))
                visit(other, token)
                low[token] = min(low[token], low[other])
                if low[other] >= discovery[token]:
                    component = set()
                    while stack:
                        edge = stack.pop()
                        component.update(edge)
                        if edge == (token, other):
                            break
                    if WETH in component:
                        sizes.append(len(component))
            elif discovery[other] < discovery[token]:
                low[token] = min(low[token], discovery[other])
                stack.append((token, other))

    visit(WETH, None)
    return sorted(sizes, reverse=True)


def root_edges(graph, discarded):
    todo, tokens, pools = deque([WETH]), {WETH}, set()
    while todo:
        token = todo.popleft()
        for other, addr in graph[token]:
            if addr in discarded:
                continue
            pools.add(addr)
            if other not in tokens:
                tokens.add(other)
                todo.append(other)
    return tokens, pools


def parity_witness(graph, by_pool, in_core):
    """Pick a deterministic Euler witness from several parity-corrected spanning trees."""
    remaining = defaultdict(list)
    for token, links in graph.items():
        remaining[token] = [(other, addr) for other, addr in links if addr in in_core]
    best_route = ()
    rng = random.Random(20261123)
    for _ in range(48):
        tree_root = {WETH: (None, None)}
        fifo = [WETH]
        for token in fifo:
            edges = remaining[token].copy()
            rng.shuffle(edges)
            for other, addr in edges:
                if other not in tree_root:
                    tree_root[other] = (token, addr)
                    fifo.append(other)
        odd = {token for token in fifo if len(remaining[token]) % 2}
        removed = set()
        for token in reversed(fifo[1:]):
            if token in odd:
                parent, addr = tree_root[token]
                removed.add(addr)
                odd.symmetric_difference_update({token, parent})
        if odd:
            raise ValueError("parity correction failed")
        _, subset = root_edges(remaining, removed)
        stack, reached, backward = [(WETH, None)], set(), []
        while stack:
            token, via = stack[-1]
            choice = next(
                (
                    (other, addr)
                    for other, addr in remaining[token]
                    if addr in subset and addr not in reached
                ),
                None,
            )
            if choice is not None:
                reached.add(choice[1])
                stack.append(choice)
            else:
                stack.pop()
                if via is not None:
                    backward.append(via)
        route = tuple(reversed(backward))
        if len(route) > len(best_route):
            best_route = route
    token = WETH
    for addr in best_route:
        first, second = by_pool[addr]["tokens"]
        if token not in (first, second):
            raise ValueError("Euler witness does not connect")
        token = second if first == token else first
    if token != WETH or len(set(best_route)) != len(best_route):
        raise ValueError("Euler witness is not pool-distinct and closed")
    return best_route


def quotes(legs):
    return tuple((x, core.bound_and_quote(legs, x)[0] - x) for x in SIZES)


def scan_simple(graph, by_pool, in_core, limit, fee_floor):
    counts, best, witness = defaultdict(Counter), {}, ()
    prefixes = 0

    def enumerator(path, used, legs):
        nonlocal prefixes, witness
        prefixes += 1
        if prefixes > PREFIX_LIMIT:
            raise ValueError("prefix limit hit before complete enumeration")
        token = path[-1]
        for other, address in graph[token]:
            if address not in in_core or address in used:
                continue
            pool = by_pool[address]
            reserves = pool["reserves"]
            direction = reserves if token == pool["tokens"][0] else reserves[::-1]
            path_pools = (*used, address)
            path_legs = (*legs, direction)
            hops = len(path_pools)
            if other != WETH:
                if other not in path and hops < limit:
                    enumerator((*path, other), path_pools, path_legs)
                continue
            if hops < 2:
                continue
            counts[hops]["cycles"] += 1
            _, upper, a, b = core.bound_and_quote(path_legs, SIZES[0])
            counts[hops]["positive_marginal"] += a > b
            counts[hops]["continuous_gross_upper_gt_intrinsic"] += upper > fee_floor
            if len(path_pools) > len(witness):
                witness = path_pools
            grid = quotes(path_legs)
            counts[hops]["gross_positive_grid"] += sum(gross > 0 for _, gross in grid)
            counts[hops]["positive_after_fee_floor_grid"] += sum(
                gross > fee_floor for _, gross in grid
            )
            amount, gross = max(grid, key=lambda row: row[1])
            if hops not in best or gross > best[hops]["gross_wei"]:
                best[hops] = {
                    "hops": hops,
                    "pool_path": path_pools,
                    "asset_path": (*path, WETH),
                    "input_wei": amount,
                    "gross_wei": gross,
                    "gross_upper_wei": upper,
                    "gross_after_fee_floor_wei": gross - fee_floor,
                    "grid_wei": grid,
                }

    enumerator((WETH,), (), ())
    return counts, best, witness, prefixes


def model_route(by_pool, route, amount, *, mutable):
    token = WETH
    current = amount
    updates = {}
    closes = []
    for i, addr in enumerate(route, 1):
        pool = by_pool[addr]
        if token not in pool["tokens"]:
            raise ValueError("path token/pool disagree")
        in_idx = pool["tokens"].index(token)
        reserves = updates.setdefault(addr, pool["reserves"].copy())
        rin, rout = reserves[in_idx], reserves[1 - in_idx]
        output = current * 997 * rout // (1_000 * rin + 997 * current)
        if not 0 < output < rout:
            return {"input_wei": amount, "completed_hops": i - 1, "status": "zero_output"}
        if mutable:
            reserves[in_idx] += current
            reserves[1 - in_idx] -= output
            if max(reserves) >= 2**112 or reserves[0] * reserves[1] < rin * rout:
                raise ValueError("V2 reserve update violates width or invariant")
        token = pool["tokens"][1 - in_idx]
        current = output
        if token == WETH:
            closes.append((i, output))
    if token != WETH:
        raise ValueError("route did not finish in WETH")
    return {
        "input_wei": amount,
        "output_wei": current,
        "gross_wei": current - amount,
        "completed_hops": len(route),
        "status": "closed",
        "closes": closes,
        "final_touched_reserves": updates,
    }


def depth_examples(by_pool, fee_floor, parity_route):
    deep_pools = {
        p["venue"]: p["address"] for p in by_pool.values() if set(p["tokens"]) == {WETH, USDC}
    }
    if set(deep_pools) != set(wide.FACTORIES):
        raise ValueError("missing WETH/USDC depth-controls")
    stateful = []
    for hops, amount, order in itertools.product(
        (100, 200, 400),
        (10**14, 10**16, 10**18),
        (tuple(deep_pools), tuple(reversed(deep_pools))),
    ):
        route = tuple(deep_pools[v] for v in order) * (hops // 2)
        actual = model_route(by_pool, route, amount, mutable=True)
        frozen = model_route(by_pool, route, amount, mutable=False)
        remaining = actual["gross_wei"] - fee_floor if actual["status"] == "closed" else None
        stateful.append(
            {
                "target_hops": hops,
                "venues": order,
                "cycle": route[:2],
                "actual": actual,
                "frozen_wrong_control": frozen,
                "compare_intrinsic_remaining_wei": remaining,
            }
        )
    distinct = [model_route(by_pool, parity_route, amount, mutable=True) for amount in SIZES]
    return stateful, distinct


def integer_route_cost_probe(top, by_pool, base_fee):
    names = {addr: symbol for symbol, (addr, _) in wide.ASSETS.items()}
    direction = []
    pools = []
    for addr, token in zip(top["pool_path"], top["asset_path"][:-1], strict=True):
        row = by_pool[addr]
        pools.append(row)
        direction.append(row["reserves"] if token == row["tokens"][0] else row["reserves"][::-1])
    symbols = [names[t] for t in top["asset_path"]]
    amount, gross, _, count = wide.exact_on_integer_sizes(direction, symbols.index("USDC"))
    candidate = {"assets": symbols, "pools": top["pool_path"], "input_wei": amount}
    call, _ = wide.envelope(candidate, pools, b"")
    calldata = bytes.fromhex(call["data"][2:])
    z = calldata.count(0)
    intrinsic = 21_000 + 4 * z + 16 * (len(calldata) - z)
    return {
        "assets": symbols,
        "pools": top["pool_path"],
        "midpoint_count": count,
        "exact_input_wei": amount,
        "exact_gross_wei": gross,
        "envelope_intrinsic_gas": intrinsic,
        "envelope_intrinsic_cost_wei": intrinsic * base_fee,
        "upper_extra_gas_vs_plain_intrinsic": (top["gross_upper_wei"] - 1) // base_fee - 21_000,
        "exact_net_after_envelope_intrinsic_wei": gross - intrinsic * base_fee,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--save", action="store_true")
    args = parser.parse_args()
    raw = (capture.ROOT / "graph.json").read_bytes()
    snapshot = json.loads(raw)
    if snapshot["stop_reason"]:
        raise ValueError("capture incomplete")
    by_pool, graph = build_graph(snapshot["pools"])
    reachable, bridges = rooted_bridges(graph)
    token_core, pool_core = root_edges(graph, bridges)
    bcc = rooted_simple_biconnected_sizes(graph)
    largest = max(bcc)
    fee_floor = snapshot["block"]["base_fee_wei"] * 21_000
    counts, best, longest, visited = scan_simple(graph, by_pool, pool_core, largest, fee_floor)
    parity_route = parity_witness(graph, by_pool, pool_core)
    stateful, distinct = depth_examples(by_pool, fee_floor, parity_route)
    known = dict(wide.ASSETS.values())
    known_liquidity_core = {
        addr
        for addr in pool_core
        if all(
            t in known and r >= 10 ** max(known[t] - 8, 0)
            for t, r in zip(
                by_pool[addr]["tokens"],
                by_pool[addr]["reserves"],
                strict=True,
            )
        )
    }
    known_route = parity_witness(graph, by_pool, known_liquidity_core)
    known_grid = [model_route(by_pool, known_route, amount, mutable=True) for amount in SIZES]
    above_intrinsic = sum(c["continuous_gross_upper_gt_intrinsic"] for c in counts.values())
    top = max(best.values(), key=lambda row: row["gross_wei"])
    exact_cost = None
    if above_intrinsic == 1 and top["gross_upper_wei"] > fee_floor:
        exact_cost = integer_route_cost_probe(top, by_pool, snapshot["block"]["base_fee_wei"])
    strata = Counter((p["venue"], p["stratum"]) for p in by_pool.values())
    weth_reserves = [
        p["reserves"][p["tokens"].index(WETH)] for p in by_pool.values() if WETH in p["tokens"]
    ]
    data = {
        "scope_sha256": snapshot["scope_sha256"],
        "graph_sha256": core.digest(raw),
        "block": snapshot["block"],
        "fee_floor_wei": fee_floor,
        "fetched_pools": len(snapshot["pools"]),
        "nonzero_pools": len(by_pool),
        "nonzero_by_stratum": {f"{venue}:{stratum}": n for (venue, stratum), n in strata.items()},
        "active_assets": len(graph),
        "weth_reachable_assets": len(reachable),
        "weth_side_depth_buckets": dict(
            zip(
                ("<0.01", "0.01..<1", "1..<100", ">=100"),
                (
                    sum(x < 10**16 for x in weth_reserves),
                    sum(10**16 <= x < 10**18 for x in weth_reserves),
                    sum(10**18 <= x < 10**20 for x in weth_reserves),
                    sum(x >= 10**20 for x in weth_reserves),
                ),
                strict=True,
            )
        ),
        "weth_component_bridge_pools": len(bridges),
        "weth_nonbridge_assets": len(token_core),
        "weth_nonbridge_pools": len(pool_core),
        "weth_bcc_sizes": bcc,
        "simple_prefixes_visited": visited,
        "simple_counts_by_hop": dict(sorted(counts.items())),
        "simple_best_by_hop": best,
        "simple_longest_pool_path": longest,
        "simple_by_cap": {
            cap: sum(c["cycles"] for h, c in counts.items() if h <= cap) for cap in TARGETS
        },
        "pool_distinct_witness_path": parity_route,
        "pool_distinct_witness_grid": distinct,
        "known_asset_pool_distinct_witness_path": known_route,
        "known_asset_pool_distinct_witness_grid": known_grid,
        "only_above_intrinsic_curve_diagnostics": exact_cost,
        "repeated_pool_depth_runs": stateful,
    }
    if args.save:
        core.write_new(capture.ROOT / "depth_with_funded_witness.json", core.canonical(data))
    else:
        print(core.canonical(data).decode())


if __name__ == "__main__":
    main()
