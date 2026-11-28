"""Run a bounded Base discovery holdout with single-read quotes and natural H/K/C."""

import argparse
import json
import time
from bisect import bisect_right
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import UTC, datetime
from itertools import pairwise, product
from pathlib import Path
from threading import Event

import httpx
import replay_base_mixed_whole as mixed
from measure_base_batched_quote import (
    DISCOVERY_GAS,
    ENDPOINTS,
    EXPECTED,
    LOTS,
    PAIRS,
    POOLS,
    PROBE,
    Lane,
    envelope,
    historical_context,
    packed_quote,
    quote_runtime,
)
from run_base_fixed_hkc import BASE_FEE_VAULT, market
from run_base_head_first_window import check_origin, read_runtime

from oasis_agent.arbitrage.base_survival import decode_base_simulation
from oasis_agent.arbitrage.cycle_protocols import abi, data_bytes
from oasis_agent.errors import OasisError
from oasis_agent.evidence.base_newheads import POLICIES as WS_POLICIES
from oasis_agent.evidence.base_newheads import NewHeadsSession, replay_head_observation
from oasis_agent.evidence.rpc_validation import rpc_quantity
from oasis_agent.models.common import sha256_hex
from oasis_agent.rpc.budget import BudgetPolicy, RpcBudget

FRAMES = 64
PANELS = 4
ANCHORS = tuple(range(0, 51, 3))
MAX_REPLAYS = 3
PLAN_GAS_FLOOR = 293_025
CALIBRATION = Path("docs/verification/BASE_BATCHED_QUOTE_AB_20261128.json")
CALIBRATION_SCOPE = mixed.DIR.parent / "base-quote-lot-20261128/scope.json"
POLICIES = (
    BudgetPolicy("state", 6 + PANELS * len(ANCHORS) + MAX_REPLAYS * 2, 250),
    BudgetPolicy("sim1", 3 + MAX_REPLAYS * 3, 250),
    BudgetPolicy("sim2", 1 + MAX_REPLAYS * 3, 250),
)


def freeze(root, stamp):
    calibration = mixed.read(CALIBRATION)
    assert calibration["fault"] is None
    assert mixed.digest(CALIBRATION_SCOPE) == calibration["scope_sha256"]
    quote_hash = mixed.read(CALIBRATION_SCOPE)["quote_runtime_sha256"]
    scope = mixed.read(mixed.DIR / "scope.json")
    runtime = sha256_hex(data_bytes(read_runtime(root)))
    assert runtime == scope["runtime_sha256"]
    assert sha256_hex(data_bytes(quote_runtime(root))) == quote_hash
    return {
        "schema": "oasis.base-batched-natural.v1",
        "frozen_utc": stamp,
        "chain_id": mixed.CHAIN,
        "sources": ENDPOINTS,
        "fees": "L1/operator upper bounds, WETH and ETH at par, predeclared gas bracket",
        "driver_sha256": mixed.digest(Path(__file__)),
        "calibration_sha256": mixed.digest(CALIBRATION),
        "quote_runtime_sha256": quote_hash,
        "whole_runtime_sha256": runtime,
        "pool_code": calibration["rows"][-1]["batched"]["pool_code"],
        "chains": 1,
        "pools": list(POOLS),
        "lots_wei": list(LOTS),
        "lot_assignment": "one deterministic round-robin principal per sampled head",
        "discovery_gas": DISCOVERY_GAS,
        "exploratory_gas_floor": PLAN_GAS_FLOOR,
        "max_live_simulations": MAX_REPLAYS,
        "panels": PANELS,
        "heads_per_panel": FRAMES,
        "head_indexes_zero_based": list(ANCHORS),
        "budgets": {p.source: p.max_requests for p in POLICIES},
        "minimum_ms_between_same_source_requests": 250,
        "trigger": "top same-hash projected net >= 1 wei, max once per sampled head",
        "usd": 0,
    }


def fixed_pairs(runtime, left, right):
    """Replace only the literal PUSH20 pool addresses of the frozen simulation fixture."""
    originals = [data_bytes(pool) for pool in (mixed.UNI, mixed.AERO)]
    replacements = {a: data_bytes(b) for a, b in zip(originals, (left, right), strict=True)}
    patched = bytearray(data_bytes(runtime))
    position = 0
    hits = dict.fromkeys(originals, 0)
    while position < len(patched):
        opcode = patched[position]
        if 0x60 <= opcode <= 0x7F:
            size = opcode - 0x5F
            end = position + size + 1
            if size == 20:
                arg = bytes(patched[position + 1 : end])
                if arg in replacements:
                    hits[arg] += 1
                    patched[position + 1 : end] = replacements[arg]
            position = end
        else:
            position += 1
    assert [hits[pool] for pool in originals] == [5, 3], "whole fixture bytecode changed"
    return "0x" + patched.hex()


def exact_call(raw, header, lot, pair, l1, operator):
    result = decode_base_simulation(
        raw,
        parent_number=header.number,
        parent_hash=header.block_hash,
        recipient=mixed.ACTOR,
        gas_limit=mixed.GAS,
    )
    [block] = raw
    timestamp = rpc_quantity(block["timestamp"])
    if timestamp != (header.timestamp_ms + 200) // 1000 or block["miner"] != BASE_FEE_VAULT:
        raise ValueError("whole simulation returned different child context")
    if result.base_fee_wei_per_gas > mixed.CAP:
        raise ValueError("whole simulation exceeds frozen fee cap")
    fee = result.gas_used * min(mixed.CAP, result.base_fee_wei_per_gas + mixed.TIP) + l1 + operator
    if result.success:
        middle, received = abi(("uint256", "uint256"), result.return_data)
        first, second = pair
        assert result.weth_delta == received
        logs = block["calls"][0]["logs"]
        transfer = "Transfer(address,address,uint256)"
        payment_steps = [
            (mixed.WETH, "Deposit(address,uint256)", (mixed.HARNESS,), lot),
            (mixed.USDC, transfer, (first, mixed.HARNESS), middle),
            (mixed.WETH, transfer, (mixed.HARNESS, first), lot),
            (mixed.WETH, transfer, (second, mixed.HARNESS), received),
            (mixed.USDC, transfer, (mixed.HARNESS, second), middle),
            (mixed.WETH, transfer, (mixed.HARNESS, mixed.ACTOR), received),
        ]
        log_indexes = [mixed.movement(logs, *step) for step in payment_steps]
        assert log_indexes == sorted(set(log_indexes))
    else:
        middle = received = None
        log_indexes = []
    gross = received - lot if received is not None else 0
    return {
        "status": "settled" if result.success else "reverted",
        "gas_used": result.gas_used,
        "base_fee": result.base_fee_wei_per_gas,
        "simulated_timestamp": timestamp,
        "intermediate_raw": middle,
        "received_raw": received,
        "revert_selector": result.return_data[:4].hex() if not result.success else None,
        "payment_log_indexes": log_indexes,
        "fees_wei": fee,
        "net_wei": gross - fee,
    }


def simulate(pool, lanes, header, runtime, candidate, l1, operator, earliest):
    pair = candidate["pools"]
    amount = candidate["principal_wei"]
    selected = fixed_pairs(runtime, *pair)
    params = mixed.simulation(header.number, selected, mixed.WETH)
    params[0]["blockStateCalls"][0]["calls"] = [envelope(amount)]
    pending = {
        key: pool.submit(
            lanes[key].rpc, f"{header.number}-whole", "eth_simulateV1", params, earliest
        )
        for key in ("sim1", "sim2")
    }
    decoded, proofs = {}, {}
    for key, future in pending.items():
        raw, proofs[key] = future.result()
        decoded[key] = exact_call(raw, header, amount, pair, l1, operator)
    if decoded["sim1"] != decoded["sim2"]:
        raise ValueError("independent whole execution sources disagree")
    return decoded["sim1"] | {"sources": proofs}


def ranking(header, snapshot, amounts):
    price = min(mixed.CAP, header.base_fee + mixed.TIP)
    rows = []
    by_input = product(enumerate(amounts), PAIRS)
    for ((index, amount), (i, j)), (middle, out) in zip(by_input, snapshot["quotes"], strict=True):
        if not middle or not out:
            continue
        l1 = snapshot["l1_wei"][index]
        operator = snapshot["operator_wei"]
        net = out - amount - price * DISCOVERY_GAS - l1 - operator
        rows.append(
            {
                "pools": [POOLS[i], POOLS[j]],
                "principal_wei": amount,
                "intermediate_raw": middle,
                "quoted_received_wei": out,
                "l1_upper_wei": l1,
                "operator_upper_wei": operator,
                "net_wei": net,
                "net_at_exploratory_gas_floor_wei": net + price * (DISCOVERY_GAS - PLAN_GAS_FLOOR),
            }
        )
    by_net = max(rows, key=lambda r: r["net_wei"], default=None)
    return {
        "completed_quotes": len(rows),
        "gross_positive": sum(r["quoted_received_wei"] > r["principal_wei"] for r in rows),
        "at_gas_floor_net_positive": sum(r["net_at_exploratory_gas_floor_wei"] > 0 for r in rows),
        "net_positive": sum(r["net_wei"] > 0 for r in rows),
        "best": by_net,
    }


def sampled(root, scope, panel, executor, lanes, controls, runtime, row, lot, clock=None):
    h = market(panel, row.head, row)
    start = row.received_monotonic_ns
    snapshot = packed_quote(lanes["state"], h, quote_runtime(root), (lot,), start)
    if snapshot["pool_code"] != scope["pool_code"]:
        raise ValueError("quote pool code changed")
    rates = ranking(h, snapshot, (lot,))
    discovered = time.monotonic_ns() if clock is None else clock["discovery_ns"]
    assert discovered >= snapshot["proof"]["received_ns"]
    record = {
        "h": h.number,
        "h_hash": h.block_hash,
        "principal_wei": lot,
        "head_ns": start,
        "quote_proof": snapshot["proof"],
        "discovery_ns": discovered,
        **rates,
    }
    best = rates["best"]
    if best and best["net_wei"] > 0 and controls[0] < MAX_REPLAYS:
        controls[0] += 1
        full = simulate(
            executor,
            lanes,
            h,
            runtime,
            best,
            best["l1_upper_wei"],
            best["operator_upper_wei"],
            discovered,
        )
        returned = time.monotonic_ns() if clock is None else clock["return_ns"]
        assert returned >= max(p["received_ns"] for p in full["sources"].values())
        record.update(whole_on_h=full, return_ns=returned)
    return record


def window(root, scope, panel, executor, lanes, controls, runtime, capture):
    lots = tuple(LOTS[i % len(LOTS)] for i in range(len(ANCHORS)))
    if capture:
        panel.mkdir()
        stream = NewHeadsSession(panel / "heads", FRAMES, lanes["state"].stop)
        refs, futures = [], []
        with ThreadPoolExecutor(max_workers=1) as quotes:
            stream.start()
            try:
                for pos in range(FRAMES):
                    item = stream.next_head()
                    refs.append(item)
                    if pos in ANCHORS:
                        futures.append(
                            quotes.submit(
                                sampled,
                                root,
                                scope,
                                panel,
                                executor,
                                lanes,
                                controls,
                                runtime,
                                item,
                                lots[ANCHORS.index(pos)],
                            )
                        )
                stream.check()
            finally:
                stream.close()
                mixed.write(panel / "heads.json", [row.to_wire() for row in refs])
            results = [future.result() for future in futures]
            clocks = [{k: r[k] for k in ("discovery_ns", "return_ns") if k in r} for r in results]
            mixed.write(panel / "clock.json", clocks)
    else:
        refs = [
            replay_head_observation(panel / "heads", row)
            for row in mixed.read(panel / "heads.json")
        ]
        clocks = mixed.read(panel / "clock.json")
        results = [
            sampled(root, scope, panel, executor, lanes, controls, runtime, refs[i], lot, clock)
            for i, lot, clock in zip(ANCHORS, lots, clocks, strict=True)
        ]
    assert len(refs) == FRAMES and len(results) == len(ANCHORS)
    heights = {r.head.height: r for r in refs}
    arrivals = [r.received_monotonic_ns for r in refs]
    for old, new in pairwise(refs):
        assert new.head.parent_hash == old.head.block_hash
        assert new.head.height == old.head.height + 1
        assert new.received_monotonic_ns > old.received_monotonic_ns
    for row in results:
        c = heights[row["h"] + 1]
        row["c"] = c.head.height
        row["child_observed_ns"] = c.received_monotonic_ns
        row["discovery_before_child"] = row["discovery_ns"] < c.received_monotonic_ns
        if "whole_on_h" not in row:
            continue
        i = bisect_right(arrivals, row["return_ns"]) - 1
        if not 0 <= i < len(refs) - 1:
            raise ValueError("candidate return clock has no independent later head")
        row["returned_before_child"] = row["return_ns"] < c.received_monotonic_ns
        k = refs[i]
        row["k"] = k.head.height
        row["k_hash"] = k.head.block_hash
        row["c_hash"] = c.head.block_hash
        whole_by_height = {row["h"]: row["whole_on_h"]}
        for stage, item in (("k", k), ("c", c)):
            height = item.head.height
            if height not in whole_by_height:
                header = market(panel, item.head, item)
                control = row["best"]
                cost = packed_quote(
                    lanes["state"],
                    header,
                    quote_runtime(root),
                    (control["principal_wei"],),
                    item.received_monotonic_ns,
                    True,
                )
                assert cost["pool_code"] == scope["pool_code"]
                whole_by_height[height] = simulate(
                    executor,
                    lanes,
                    header,
                    runtime,
                    control,
                    cost["l1_wei"][0],
                    cost["operator_wei"],
                    item.received_monotonic_ns,
                ) | {"fee_proof": cost["proof"]}
            row[f"whole_on_{stage}"] = whole_by_height[height]
        for height, settled in whole_by_height.items():
            nxt = heights[height + 1]
            successor = market(panel, nxt.head, nxt)
            if (
                settled["simulated_timestamp"] != successor.timestamp_ms // 1000
                or settled["base_fee"] != successor.base_fee
            ):
                raise ValueError(
                    "simulated fee/timestamp conflicts with directly witnessed context"
                )
    with RpcBudget(panel / "heads/original-budget.sqlite3", WS_POLICIES, read_only=True) as ledger:
        counts = ledger.counts()
    return {
        "first": refs[0].to_wire(),
        "last": refs[-1].to_wire(),
        "ws": counts,
        "rows": results,
    }


def run(root, scope, capture):
    if capture:
        with RpcBudget(root / "original.sqlite3", POLICIES):
            pass
    stop = Event()
    panels = []
    try:
        with ExitStack() as stack:
            lanes = {
                key: Lane(
                    root,
                    key,
                    POLICIES,
                    stop,
                    stack.enter_context(
                        httpx.Client(timeout=5, follow_redirects=False, trust_env=False)
                    )
                    if capture
                    else None,
                )
                for key in ENDPOINTS
            }
            origin = check_origin(lanes["sim1"], lanes["state"])
            assert lanes["sim2"].rpc("chain", "eth_chainId", [])[0] == hex(mixed.CHAIN)
            pin = {"blockHash": origin["block_hash"], "requireCanonical": True}
            raw, override_proof = lanes["state"].rpc("batched-code", "eth_getCode", [PROBE, pin])
            assert not data_bytes(raw)
            historical = historical_context()[1]
            sample = packed_quote(lanes["state"], historical, quote_runtime(root), LOTS[:1], 1)
            assert sample["pool_code"] == scope["pool_code"]
            assert tuple(sample["quotes"][PAIRS.index((0, 1))]) == EXPECTED
            control = ranking(historical, sample, LOTS[:1])
            with ThreadPoolExecutor(max_workers=2) as quotes:
                cap = [0]
                runtime = read_runtime(root)
                for i in range(1, PANELS + 1):
                    panel = root / f"window-{i}"
                    panels.append(window(root, scope, panel, quotes, lanes, cap, runtime, capture))
            for left, right in pairwise(panels):
                assert left["last"]["head"]["height"] < right["first"]["head"]["height"]
    except (
        ValueError,
        AssertionError,
        RuntimeError,
        KeyError,
        OSError,
        OasisError,
        httpx.HTTPError,
    ) as exc:
        if capture:
            mixed.write(root / "STOP.json", {"fault": str(exc) or "experiment assertion failed"})
        raise
    if capture:
        mixed.write(root / "STOP.json", {"fault": None})
    assert mixed.read(root / "STOP.json") == {"fault": None}
    with RpcBudget(root / "original.sqlite3", POLICIES, read_only=True) as ledger:
        counts = ledger.counts()
    assert counts == {key: lane.count for key, lane in lanes.items()}
    return {
        "scope": scope,
        "scope_sha256": mixed.digest(root / "scope.json"),
        "identity": origin,
        "quote_override_witness": override_proof,
        "historical_control": control | {"proof": sample["proof"]},
        "rpc_reservations": counts,
        "panels": panels,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "capture", "replay"))
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    if args.mode == "prepare":
        mixed.write(args.root / "scope.json", freeze(args.root, datetime.now(UTC).isoformat()))
        return
    scope = mixed.read(args.root / "scope.json")
    assert scope == freeze(args.root, scope["frozen_utc"])
    print(json.dumps(run(args.root, scope, args.mode == "capture"), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
