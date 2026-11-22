"""Frozen wider two-venue Ethereum V2 shadow search, with offline replay."""

import argparse
import itertools
import json
import math
import time
from collections import Counter, defaultdict
from pathlib import Path

if __package__:
    from . import run_eth_v2_forward_shadow as core
else:
    import run_eth_v2_forward_shadow as core

from oasis_agent.rpc.budget import BudgetPolicy, RpcBudget

ROOT = core.RESEARCH_ROOT.with_name("ethereum-wide")
ASSETS = {
    **core.ASSETS,
    "WBTC": ("0x2260fac5e5542a773aa44fbcfedf7c193bc2c599", 8),
    "LINK": ("0x514910771af9ca656af840dff83e8264ecf986ca", 18),
    "UNI": ("0x1f9840a85d5af5bf1d1762f925bdaddc4201f984", 18),
    "AAVE": ("0x7fc66500c84a76ad7e9c93437bfc5ac33e2ddae9", 18),
    "SHIB": ("0x95ad61b0a150d79219dcf64e1e6cc01f0b64c4ce", 18),
    "PEPE": ("0x6982508145454ce325ddbe47a25d4ec3d2311933", 18),
    "LDO": ("0x5a98fcbea516cf06857215779fd812ca3bef1b32", 18),
    "CRV": ("0xd533a949740bb3306d119cc777fa900ba034cd52", 18),
    "ENS": ("0xc18360217d8f7ab5e7c516566761ea12ce7f9d72", 18),
    "COMP": ("0xc00e94cb662c3520282e6f5717214004a7f26888", 18),
}
FACTORIES = {
    "uni": core.FACTORY,
    "sushi": "0xc0aee478e3658e2610c5f7a4a2e1777ce9e4f2ac",
}
PAIRS = (
    *(("WETH", symbol) for symbol in ASSETS if symbol != "WETH"),
    ("USDC", "USDT"),
    ("DAI", "USDC"),
    ("DAI", "USDT"),
    ("WBTC", "USDC"),
    ("WBTC", "USDT"),
    ("WBTC", "DAI"),
    ("LINK", "USDC"),
    ("LINK", "USDT"),
    ("UNI", "USDC"),
    ("AAVE", "USDC"),
    ("LDO", "USDC"),
    ("CRV", "USDC"),
    ("SHIB", "USDT"),
    ("PEPE", "USDT"),
)
OLD_COUNT = 11
HTTP_LIMIT = 36
RPC_LIMIT = 420
WALL_SECONDS = 160
NET_GATE = 10**12
GAS_LIMIT = 900_000
PRIORITY_WEI = 50_000_000
POLICY = (BudgetPolicy(core.SOURCE, HTTP_LIMIT, 200),)
PROBE = "0x000000000000000000000000000000000000d35d"
GRID = (
    *core.AMOUNTS[:2],
    3 * 10**15,
    *core.AMOUNTS[2:3],
    3 * 10**16,
    *core.AMOUNTS[3:4],
    3 * 10**17,
    *core.AMOUNTS[4:5],
    3 * 10**18,
    *core.AMOUNTS[5:],
)


def batches(reads, requests):
    out = []
    for start in range(0, len(requests), 16):
        result, _ = reads.batch(requests[start : start + 16])
        out.extend(result)
    return out


def costs(base_fee):
    next_base_cap = (9 * base_fee + 7) // 8
    return 21_000 * base_fee, GAS_LIMIT * (next_base_cap + PRIORITY_WEI)


def search(pools, gas_floor, gas_budget):
    graph = defaultdict(list)
    for pool in pools:
        x, y = pool["assets"]
        rin, rout = pool["reserves"]
        graph[x].append((y, pool, (rin, rout)))
        graph[y].append((x, pool, (rout, rin)))
    rows = []
    counts = Counter()
    asset_best = {}
    amount_best = {}

    def visit(assets, legs, chosen):
        for symbol, pool, direction in graph[assets[-1]]:
            if pool["address"] in chosen:
                continue
            extended = (*assets, symbol)
            path_legs = (*legs, direction)
            path_pools = (*chosen, pool["address"])
            if symbol != "WETH":
                if symbol not in assets and len(path_legs) < 6:
                    visit(extended, path_legs, path_pools)
                continue
            if len(path_legs) < 2:
                continue
            count = len(path_legs)
            counts[(count, "routes")] += 1
            upper = core.bound_and_quote(path_legs, GRID[0])[1]
            counts[(count, "continuous_gross_upper_above_floor")] += upper > gas_floor
            quotes = [(size, core.bound_and_quote(path_legs, size)[0] - size) for size in GRID]
            counts[(count, "grid_gross_positive")] += sum(delta > 0 for _, delta in quotes)
            counts[(count, "grid_above_intrinsic_floor")] += sum(
                delta > gas_floor for _, delta in quotes
            )
            counts[(count, "grid_above_conservative_gate")] += sum(
                delta - gas_budget >= NET_GATE for _, delta in quotes
            )
            size, delta = max(quotes, key=lambda quote: quote[1])
            row = {
                "assets": extended,
                "pools": path_pools,
                "input_wei": size,
                "gross_wei": delta,
                "projected_net_after_gas_upper_wei": delta - gas_budget,
                "gross_upper_wei": upper,
            }
            rows.append(row)
            for asset in extended[1:-1]:
                if asset not in asset_best or asset_best[asset]["gross_wei"] < delta:
                    asset_best[asset] = row
            for input_wei, profit in quotes:
                current = amount_best.get(input_wei)
                if current is None or current["gross_wei"] < profit:
                    amount_best[input_wei] = {
                        **row,
                        "input_wei": input_wei,
                        "gross_wei": profit,
                        "projected_net_after_gas_upper_wei": profit - gas_budget,
                    }

    visit(("WETH",), (), ())
    rows.sort(key=lambda row: row["projected_net_after_gas_upper_wei"], reverse=True)
    if not rows:
        raise ValueError("empty bounded route graph")
    return {
        "counts": {f"{hops}:{key}": n for (hops, key), n in counts.items()},
        "top_routes": rows[:15],
        "best_by_asset": sorted(asset_best.items(), key=lambda entry: -entry[1]["gross_wei"]),
        "best_by_input": sorted(amount_best.values(), key=lambda entry: -entry["gross_wei"]),
        "route_grid_count": len(rows) * len(GRID),
    }


def envelope(selection, pools, code):
    venues = {pool["address"]: pool["venue"] for pool in pools}
    mask = sum(1 << i for i, addr in enumerate(selection["pools"]) if venues[addr] == "sushi")
    assets = [ASSETS[symbol][0] for symbol in selection["assets"]]
    tokens = core.word(len(assets)) + "".join(map(core.word, assets))
    pairs = core.word(len(selection["pools"])) + "".join(map(core.word, selection["pools"]))
    data = "0xe2f99ee1" + core.word(96) + core.word(96 + len(tokens) // 2) + core.word(mask)
    return (
        {
            "to": PROBE,
            "from": core.SENDER,
            "value": hex(selection["input_wei"]),
            "gas": hex(GAS_LIMIT),
            "data": data + tokens + pairs,
        },
        {
            core.SENDER: {"balance": hex(100 * 10**18), "code": "0x"},
            PROBE: {"balance": "0x0", "code": "0x" + code.decode()},
        },
    )


def exact_on_integer_sizes(legs, midpoint):
    """Maximize integer gross by visiting each reachable output of one middle leg."""
    a, b, c = 1, 1, 0
    for rin, rout in legs:
        a, b, c = (997 * rout) * a, (1_000 * rin) * b, (1_000 * rin) * c + 997 * a
    if a <= b:
        return 0, 0, 0, 0
    max_input = (a - b) // c
    continuous_best_input = (math.isqrt(a * b) - b) // c
    middle_max = core.bound_and_quote(legs[:midpoint], max_input)[0]
    best_amount, best_gross = 0, 0
    prior = 0
    for middle_out in range(1, middle_max + 1):
        amount = middle_out
        for rin, rout in reversed(legs[:midpoint]):
            denominator = 997 * (rout - amount)
            if denominator <= 0:
                raise ValueError("reserve bounds failed in integer inverse")
            amount = (1_000 * rin * amount + denominator - 1) // denominator
        if amount == prior:
            continue
        prior = amount
        gross = core.bound_and_quote(legs, amount)[0] - amount
        if gross > best_gross:
            best_amount, best_gross = amount, gross
    return best_amount, best_gross, continuous_best_input, middle_max


def diagnose(result, code):
    """Refine above-intrinsic curves without extra live samples or changing the frozen gate."""
    findings = []
    for sample in result["samples"]:
        ranking = sample["ranking"]
        selected = [
            r for r in ranking["top_routes"] if r["gross_upper_wei"] > sample["gas_floor_wei"]
        ]
        count = sum(
            n for key, n in ranking["counts"].items() if key.endswith("gross_upper_above_floor")
        )
        if len(selected) != count:
            raise ValueError("retained top routes do not cover all above-intrinsic curves")
        pools = sample["pool_states"]
        by_address = {p["address"]: p for p in pools}
        base_fee = sample["base_fee_wei"]
        routes = []
        for row in selected:
            usdc_index = row["assets"].index("USDC")
            legs = []
            for addr, symbol in zip(row["pools"], row["assets"][:-1], strict=True):
                entry = by_address[addr]
                reserves = entry["reserves"]
                legs.append(reserves if entry["assets"][0] == symbol else reserves[::-1])
            amount, gross, continuous_size, middle_count = exact_on_integer_sizes(legs, usdc_index)
            call, _ = envelope(row, pools, code)
            calldata = bytes.fromhex(call["data"][2:])
            zeros = calldata.count(0)
            nonzeros = len(calldata) - zeros
            intrinsic = 21_000 + 4 * zeros + 16 * nonzeros
            max_gross = row["gross_upper_wei"]
            routes.append(
                {
                    "assets": row["assets"],
                    "pools": row["pools"],
                    "venues": [by_address[p]["venue"] for p in row["pools"]],
                    "integer_optimal_input_wei": amount,
                    "integer_optimal_gross_wei": gross,
                    "continuous_optimal_input_floor_wei": continuous_size,
                    "continuous_gross_upper_wei": max_gross,
                    "searched_usdc_middle_atoms": middle_count,
                    "calldata_bytes": len(calldata),
                    "zero_bytes": zeros,
                    "nonzero_bytes": nonzeros,
                    "envelope_intrinsic_gas_floor": intrinsic,
                    "envelope_intrinsic_base_fee_floor_wei": intrinsic * base_fee,
                    "integer_optimum_less_intrinsic_wei": gross - intrinsic * base_fee,
                    "remaining_execution_gas_from_continuous_upper": (max_gross - 1) // base_fee
                    - intrinsic,
                    "all_sizes_excluded_with_envelope": max_gross <= intrinsic * base_fee,
                }
            )
        findings.append({"block": sample["block"], "hash": sample["hash"], "routes": routes})
    return {"scope_sha256": result["scope_sha256"], "bindings": findings}


def observe(reads, report, code):
    pools = []
    selected = None
    payload = None
    for tick in range(3):
        if tick == 2 and len(report["simulations"]) != 1:
            break
        if tick and reads.budget is not None:
            time.sleep(13)
        (chain_id, head), clock = reads.batch(
            [
                ("eth_chainId", []),
                ("eth_getBlockByNumber", ["latest", False]),
            ]
        )
        if chain_id != "0x1":
            raise ValueError("unexpected chain")
        for key in ("hash", "parentHash", "stateRoot"):
            core.hex_bytes(head[key], 32)
        number = int(head["number"], 16)
        block_ms = int(head["timestamp"], 16) * 1000
        if not 0 <= clock["received_ms"] - block_ms <= 30_000:
            raise ValueError("unfresh head observation")
        if report["samples"]:
            last = report["samples"][-1]
            if number <= last["block"] or (
                number == last["block"] + 1 and head["parentHash"] != last["hash"]
            ):
                raise ValueError("repeated, reversed or reorged head")
        block = {"blockHash": head["hash"], "requireCanonical": True}
        if tick == 0:
            grid = list(itertools.product(FACTORIES, PAIRS))
            calls = [
                core.abi_call(
                    FACTORIES[v],
                    "0xe6a43905" + core.word(ASSETS[x][0]) + core.word(ASSETS[y][0]),
                    block,
                )
                for v, (x, y) in grid
            ]
            matches = map(core.address, batches(reads, calls))
            pools = [
                {
                    "venue": venue,
                    "address": addr,
                    "assets": sorted(pair, key=lambda s: ASSETS[s][0]),
                }
                for (venue, pair), addr in zip(grid, matches, strict=True)
                if int(addr, 16)
            ]
            if len({p["address"] for p in pools}) != len(pools):
                raise ValueError("duplicate factory pair")
        calls = [core.abi_call(p["address"], "0x0902f1ac", block) for p in pools]
        if tick == 0:
            for pool in pools:
                addr = pool["address"]
                calls.extend(
                    [
                        core.abi_call(addr, "0x0dfe1681", block),
                        core.abi_call(addr, "0xd21220a7", block),
                        ("eth_getCode", [addr, block]),
                    ]
                )
            calls.extend(("eth_getCode", [address, block]) for address in FACTORIES.values())
        answers = batches(reads, calls)
        if tick == 0:
            for index, pool in enumerate(pools):
                offset = len(pools) + 3 * index
                t0, t1, runtime = answers[offset : offset + 3]
                if [core.address(t0), core.address(t1)] != [ASSETS[s][0] for s in pool["assets"]]:
                    raise ValueError("factory pair asset mismatch")
                pool["code_sha256"] = core.digest(core.hex_bytes(runtime))
            report["factory_runtime_sha256"] = {
                venue: core.digest(core.hex_bytes(v))
                for venue, v in zip(FACTORIES, answers[-len(FACTORIES) :], strict=True)
            }
            code_by_venue = defaultdict(set)
            for pool in pools:
                code_by_venue[pool["venue"]].add(pool["code_sha256"])
            if any(len(s) != 1 for s in code_by_venue.values()):
                raise ValueError("unexpected pair code heterogeneity within venue")
        active = []
        full_state = []
        for pool, value in zip(pools, answers[: len(pools)], strict=True):
            rin, rout, timestamp = core.words(value, 3)
            if max(rin, rout) >= 2**112 or timestamp >= 2**32:
                raise ValueError("invalid reserves ABI data")
            state = {**pool, "reserves": [rin, rout], "last_timestamp_modulo_2_32": timestamp}
            full_state.append(state)
            if rin and rout:
                active.append(state)
        (again,), state_clock = reads.batch([("eth_getBlockByNumber", [head["number"], False])])
        if any(again[key] != head[key] for key in ("number", "hash", "stateRoot")):
            raise ValueError("state binding changed")
        base_fee = int(head["baseFeePerGas"], 16)
        floor, gas_budget = costs(base_fee)
        ranking = search(active, floor, gas_budget)
        sample = {
            "block": number,
            "hash": head["hash"],
            "state_root": head["stateRoot"],
            "parent_hash": head["parentHash"],
            "block_ms": block_ms,
            "head_received_ms": clock["received_ms"],
            "state_ready_ms": state_clock["received_ms"],
            "base_fee_wei": base_fee,
            "gas_floor_wei": floor,
            "gas_upper_budget_wei": gas_budget,
            "pool_states": full_state,
            "active_pool_count": len(active),
            "ranking": ranking,
        }
        report["samples"].append(sample)
        best = ranking["top_routes"][0]
        if selected is None and best["projected_net_after_gas_upper_wei"] >= NET_GATE:
            selected = best
            payload = envelope(selected, active, code)
        if payload is None or len(report["simulations"]) >= 2:
            continue
        call, override = payload
        active_by_addr = {pool["address"]: pool for pool in active}
        legs = []
        for addr, asset in zip(selected["pools"], selected["assets"][:-1], strict=True):
            entry = active_by_addr[addr]
            reserves = entry["reserves"]
            legs.append(reserves if entry["assets"][0] == asset else reserves[::-1])
        model_out = core.bound_and_quote(legs, selected["input_wei"])[0]
        outcome = {
            "block": number,
            "hash": head["hash"],
            "selection": selected,
            "model_out_at_this_block_wei": model_out,
            "status": "attempted",
        }
        report["simulations"].append(outcome)
        (result,), sim_clock = reads.batch([("eth_call", [call, block, override])])
        before, after, gas_sample = core.words(result, 3)
        if after - before != model_out or not 0 < gas_sample < GAS_LIMIT:
            raise ValueError("modeled output and execution disagree")
        outcome.update(
            {
                "status": "simulated_success",
                "before_weth_wei": before,
                "after_weth_wei": after,
                "function_body_gas": gas_sample,
                "gross_wei": after - before - selected["input_wei"],
                "gross_less_gas_upper_budget_wei": after
                - before
                - selected["input_wei"]
                - gas_budget,
                "sent_ms": sim_clock["started_ms"],
                "received_ms": sim_clock["received_ms"],
            }
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("capture", "replay", "diagnose"))
    parser.add_argument("--runtime", type=Path)
    args = parser.parse_args()
    old_ledger = core.RESEARCH_ROOT / "original-budget.sqlite3"
    old = RpcBudget(old_ledger, core.POLICY, read_only=True)
    try:
        if old.counts()[core.SOURCE] != OLD_COUNT:
            raise ValueError("prior completed scope count changed")
    finally:
        old.db.close()
    budget = None
    runtime_path = ROOT / "probe-runtime.hex"
    if args.mode == "capture":
        if args.runtime is None:
            parser.error("capture requires compiler output --runtime")
        code = args.runtime.read_bytes().strip()
        core.hex_bytes("0x" + code.decode())
        ROOT.mkdir(parents=True, exist_ok=False)
        core.write_new(runtime_path, code)
    else:
        code = runtime_path.read_bytes()
    scope_path = ROOT / "scope.json"
    scope = core.canonical(
        {
            "schema": 1,
            "chain_id": 1,
            "provider": core.ENDPOINT,
            "paid_usd": 0,
            "prior_http_count": OLD_COUNT,
            "prior_ledger_sha256": core.digest(old_ledger.read_bytes()),
            "new_max_http": HTTP_LIMIT,
            "cumulative_http_cap": OLD_COUNT + HTTP_LIMIT,
            "new_max_rpc": RPC_LIMIT,
            "deadline_seconds": WALL_SECONDS,
            "max_batch": 16,
            "min_batch_interval_ms": 200,
            "next_poll_delay_seconds": 13,
            "head_policy": "two fresh heads, third if discovery on second requires follow-on",
            "transport_policy": "first failure stops, no retry or source switch",
            "factories": FACTORIES,
            "assets": ASSETS,
            "pairs_by_factory": PAIRS,
            "min_hops": 2,
            "max_hops": 6,
            "grid_weth_wei": GRID,
            "min_net_gate_wei": NET_GATE,
            "gas_cap": GAS_LIMIT,
            "tip_wei_per_gas": PRIORITY_WEI,
            "gas_bound": "gas cap times next-block EIP-1559 base ceiling plus fixed tip",
            "selection": "best grid net with integer output, one route if it clears net gate",
            "probe_runtime_sha256": core.digest(code),
            "registered_code": "eth_v2_wide_shadow_probe.sol",
        }
    )
    if args.mode == "capture":
        core.write_new(scope_path, scope)
        budget = RpcBudget(ROOT / "original-budget.sqlite3", POLICY)
    elif scope_path.read_bytes() != scope:
        raise ValueError("scope or code changed")
    result = {"schema": 1, "scope_sha256": core.digest(scope), "samples": [], "simulations": []}
    reads = core.Reads(
        ROOT, budget, http_limit=HTTP_LIMIT, rpc_limit=RPC_LIMIT, deadline_seconds=WALL_SECONDS
    )
    try:
        observe(reads, result, code)
        result["stop_reason"] = None
    except (ValueError, KeyError, TypeError, OSError) as failure:
        result["stop_reason"] = str(failure)
    result.update(
        {
            "new_http": reads.http,
            "new_logical_rpc": reads.logical,
            "cumulative_http": OLD_COUNT + reads.http,
            "started_ms": reads.first_ms,
            "finished_ms": reads.last_ms,
        }
    )
    encoded = core.canonical(result)
    if budget is not None:
        budget.db.close()
        core.write_new(ROOT / "result.json", encoded)
    else:
        ledger = RpcBudget(ROOT / "original-budget.sqlite3", POLICY, read_only=True)
        try:
            reads.verify_budget(ledger)
        finally:
            ledger.db.close()
        if (ROOT / "result.json").read_bytes() != encoded:
            raise ValueError("results differ on replay")
    output = (
        diagnose(result, code)
        if args.mode == "diagnose"
        else {key: value for key, value in result.items() if key != "samples"}
    )
    print(json.dumps(output))
    return 3 if result["stop_reason"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
