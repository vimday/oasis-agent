"""Expand bounded exact-input Base discovery and resolve natural H/K/C candidates."""

import argparse
import json
import time
from bisect import bisect_right
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from threading import Event

import httpx
import replay_base_mixed_whole as mixed
from measure_base_batched_quote import DISCOVERY_GAS, ENDPOINTS, LOTS, Lane, envelope
from run_base_batched_natural import fixed_pairs
from run_base_cl_coverage import BAND, MARKETS, PROBE, SOURCE, artifact, observe, retained
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

PREDECESSOR = mixed.DIR.parent / "base-cl-coverage-bootstrap-20261128"
FRAMES = 64
PANELS = 4
POSITIONS = tuple(range(0, 51, 3))
LIMIT = 3
POLICIES = (
    BudgetPolicy("state", 8 + PANELS * len(POSITIONS) + 2 * LIMIT, 250),
    BudgetPolicy("sim1", 5 + LIMIT * 3, 250),
    BudgetPolicy("sim2", 3 + LIMIT * 3, 250),
)


def frozen(root, stamp):
    assert mixed.read(PREDECESSOR / "STOP.json") == {"fault": None}
    preflight = mixed.read(PREDECESSOR / "scope.json")
    assert sha256_hex(data_bytes(artifact(root))) == preflight["runtime_sha256"]
    assert (
        sha256_hex(data_bytes(read_runtime(root)))
        == mixed.read(mixed.DIR / "scope.json")["runtime_sha256"]
    )
    return {
        "schema": "oasis.base-cl-expanded.v1",
        "frozen_utc": stamp,
        "driver_sha256": mixed.digest(Path(__file__)),
        "source_sha256": mixed.digest(SOURCE),
        "covered_scope_sha256": mixed.digest(PREDECESSOR / "scope.json"),
        "covered_report_sha256": mixed.digest(PREDECESSOR / "stdout.json"),
        "quote_runtime_sha256": preflight["runtime_sha256"],
        "markets": [[token, list(pools)] for token, pools in MARKETS],
        "lots": list(LOTS),
        "sqrt_band_bps": BAND,
        "head_frames": FRAMES,
        "positions": list(POSITIONS),
        "panels": PANELS,
        "maximum_natural_simulations": LIMIT,
        "historical_controls": {"height": 63345200, "markets": [1, 2]},
        "budgets": {policy.source: policy.max_requests for policy in POLICIES},
        "usd": 0,
    }


def selected_runtime(runtime, token, pair):
    encoded = fixed_pairs(runtime, *pair)
    if token == mixed.USDC:
        return encoded
    data = data_bytes(encoded)
    source = bytes((0x73,)) + data_bytes(mixed.USDC)
    target = bytes((0x73,)) + data_bytes(token)
    if data.count(source) != 3 or data.count(data_bytes(mixed.USDC)) != 3:
        raise ValueError("whole runtime literal count changed")
    return "0x" + data.replace(source, target).hex()


def decode(raw, header, token, pair, amount, fees):
    whole = decode_base_simulation(
        raw,
        parent_number=header.number,
        parent_hash=header.block_hash,
        recipient=mixed.ACTOR,
        gas_limit=mixed.GAS,
    )
    [block] = raw
    seconds = rpc_quantity(block["timestamp"])
    if (
        seconds != (header.timestamp_ms + 200) // 1000
        or block["miner"] != BASE_FEE_VAULT
        or whole.base_fee_wei_per_gas > mixed.CAP
    ):
        raise ValueError("whole response differs from the frozen child context")
    middle, final, logs = None, None, []
    if whole.success:
        middle, final = abi(("uint256", "uint256"), whole.return_data)
        first, second = pair
        assert whole.weth_delta == final
        transfer = "Transfer(address,address,uint256)"
        movements = (
            (mixed.WETH, "Deposit(address,uint256)", (mixed.HARNESS,), amount),
            (token, transfer, (first, mixed.HARNESS), middle),
            (mixed.WETH, transfer, (mixed.HARNESS, first), amount),
            (mixed.WETH, transfer, (second, mixed.HARNESS), final),
            (token, transfer, (mixed.HARNESS, second), middle),
            (mixed.WETH, transfer, (mixed.HARNESS, mixed.ACTOR), final),
        )
        logs = [mixed.movement(block["calls"][0]["logs"], *movement) for movement in movements]
        assert logs == sorted(set(logs))
    network = whole.gas_used * min(mixed.CAP, whole.base_fee_wei_per_gas + mixed.TIP) + sum(fees)
    return {
        "status": "settled" if whole.success else "reverted",
        "intermediate_raw": middle,
        "received_wei": final,
        "gas": whole.gas_used,
        "parent_hash": block["parentHash"],
        "simulated_timestamp": seconds,
        "base_fee": whole.base_fee_wei_per_gas,
        "network_fee_wei": network,
        "net_wei": (final - amount if final is not None else 0) - network,
        "payment_logs": logs,
        "revert": whole.return_data[:4].hex() if not whole.success else None,
    }


def exact(lanes, workers, root, header, candidate, fee, earliest):
    amount, token, pair = (candidate[k] for k in ("principal", "token", "pools"))
    runtime = selected_runtime(read_runtime(root), token, pair)
    params = mixed.simulation(header.number, runtime, mixed.WETH)
    params[0]["blockStateCalls"][0]["calls"] = [envelope(amount)]
    tasks = {
        key: workers.submit(
            lanes[key].rpc, f"{header.number}-whole", "eth_simulateV1", params, earliest
        )
        for key in ("sim1", "sim2")
    }
    output, proof = {}, {}
    for key, job in tasks.items():
        raw, proof[key] = job.result()
        output[key] = decode(raw, header, token, pair, amount, fee)
    if output["sim1"] != output["sim2"]:
        raise ValueError("whole simulation sources disagree")
    return output["sim1"] | {"sources": proof, "runtime_sha256": sha256_hex(data_bytes(runtime))}


def rank(snapshot, header, amount):
    price = min(mixed.CAP, header.base_fee + mixed.TIP)
    fees = snapshot["l1_wei"] + snapshot["operator_wei"] + DISCOVERY_GAS * price
    rows = [
        {
            "token": snapshot["token"],
            "pools": row["pools"],
            "principal": amount,
            "intermediate": row["left"]["output"],
            "received_wei": row["right"]["output"],
            "model_fee_wei": fees,
            "model_net_wei": row["right"]["output"] - amount - fees,
        }
        for row in snapshot["routes"]
        if row["left"]["status"] == row["right"]["status"] == 1
    ]
    return {
        "planned_quotes": len(snapshot["routes"]),
        "complete_quotes": len(rows),
        "gross_positive": sum(r["received_wei"] > amount for r in rows),
        "net_positive": sum(r["model_net_wei"] > 0 for r in rows),
        "best": max(rows, key=lambda row: row["model_net_wei"], default=None),
    }


def assess(root, folder, lanes, workers, work, code, remaining, clock=None):
    ref, cohort, amount = work
    header = market(folder, ref.head, ref)
    start = ref.received_monotonic_ns
    snapshot = observe(lanes["state"], header, artifact(root), cohort, amount, start)
    if snapshot["pool_code"] != code[cohort]:
        raise ValueError("pool identities changed from historical preflight")
    info = rank(snapshot, header, amount)
    now = time.monotonic_ns() if clock is None else clock["discovery_ns"]
    assert now >= snapshot["proof"]["received_ns"]
    result = {
        "h": header.number,
        "hash": header.block_hash,
        "head_ns": start,
        "discovery_ns": now,
        "cohort": cohort,
        "principal": amount,
        **info,
        "quote_proof": snapshot["proof"],
    }
    if info["net_positive"] and remaining[0] > 0:
        remaining[0] -= 1
        ack = exact(
            lanes,
            workers,
            root,
            header,
            info["best"],
            (snapshot["l1_wei"], snapshot["operator_wei"]),
            now,
        )
        returned = time.monotonic_ns() if clock is None else clock["return_ns"]
        assert returned >= max(p["received_ns"] for p in ack["sources"].values())
        result.update(whole_on_h=ack, return_ns=returned)
    return result


def sample_plan(panel, index, ref):
    return ref, (index + panel) % len(MARKETS), LOTS[(index // len(MARKETS) + panel) % len(LOTS)]


def panel_capture(root, folder, lanes, workers, panel, code, remaining, capture):
    if capture:
        folder.mkdir()
        ws = NewHeadsSession(folder / "heads", FRAMES, lanes["state"].stop)
        refs, pending = [], []
        with ThreadPoolExecutor(max_workers=1) as quote:
            ws.start()
            try:
                for index in range(FRAMES):
                    event = ws.next_head()
                    refs.append(event)
                    if index in POSITIONS:
                        work = sample_plan(panel, POSITIONS.index(index), event)
                        pending.append(
                            quote.submit(
                                assess, root, folder, lanes, workers, work, code, remaining
                            )
                        )
                ws.check()
            finally:
                ws.close()
                mixed.write(folder / "heads.json", [ref.to_wire() for ref in refs])
            rows = [job.result() for job in pending]
            mixed.write(
                folder / "clock.json",
                [
                    {name: row[name] for name in ("discovery_ns", "return_ns") if name in row}
                    for row in rows
                ],
            )
    else:
        refs = [
            replay_head_observation(folder / "heads", row)
            for row in mixed.read(folder / "heads.json")
        ]
        clocks = mixed.read(folder / "clock.json")
        rows = [
            assess(
                root,
                folder,
                lanes,
                workers,
                sample_plan(panel, idx, refs[position]),
                code,
                remaining,
                clk,
            )
            for idx, (position, clk) in enumerate(zip(POSITIONS, clocks, strict=True))
        ]
    assert len(refs) == FRAMES and len(rows) == len(POSITIONS)
    for left, right in pairwise(refs):
        assert (
            right.head.height == left.head.height + 1
            and right.head.parent_hash == left.head.block_hash
        )
        assert right.received_monotonic_ns > left.received_monotonic_ns
    direct = {r.head.height: r for r in refs}
    time_series = [r.received_monotonic_ns for r in refs]
    for row in rows:
        child = direct[row["h"] + 1]
        row["c"] = child.head.height
        row["c_seen_ns"] = child.received_monotonic_ns
        row["discovery_before_child"] = row["discovery_ns"] < row["c_seen_ns"]
        if "whole_on_h" not in row:
            continue
        seen = bisect_right(time_series, row["return_ns"]) - 1
        if not 0 <= seen < len(refs) - 1:
            raise ValueError("whole return has no following canonical witness")
        k = refs[seen]
        row["k"] = k.head.height
        row["whole_before_child"] = row["return_ns"] < row["c_seen_ns"]
        computed = {row["h"]: row["whole_on_h"]}
        for kind, held in (("k", k), ("c", child)):
            bound = market(folder, held.head, held)
            if bound.number not in computed:
                state = observe(
                    lanes["state"],
                    bound,
                    artifact(root),
                    row["cohort"],
                    row["principal"],
                    held.received_monotonic_ns,
                )
                assert state["pool_code"] == code[row["cohort"]]
                computed[bound.number] = exact(
                    lanes,
                    workers,
                    root,
                    bound,
                    row["best"],
                    (state["l1_wei"], state["operator_wei"]),
                    held.received_monotonic_ns,
                ) | {"fee_proof": state["proof"]}
            row[f"whole_on_{kind}"] = computed[bound.number]
        for number, value in computed.items():
            next_ref = direct[number + 1]
            next_head = market(folder, next_ref.head, next_ref)
            if (
                value["base_fee"] != next_head.base_fee
                or value["simulated_timestamp"] != next_head.timestamp_ms // 1000
            ):
                raise ValueError("whole simulation context differs from next witnessed block")
    with RpcBudget(folder / "heads/original-budget.sqlite3", WS_POLICIES, read_only=True) as budget:
        usage = budget.counts()
    return {"first": refs[0].to_wire(), "last": refs[-1].to_wire(), "rows": rows, "ws": usage}


def run(root, scope, capture):
    stop = Event()
    if capture:
        with RpcBudget(root / "original.sqlite3", POLICIES):
            pass
    try:
        with ExitStack() as stack:
            lanes = {
                key: Lane(
                    root,
                    key,
                    POLICIES,
                    stop,
                    stack.enter_context(
                        httpx.Client(timeout=6, follow_redirects=False, trust_env=False)
                    )
                    if capture
                    else None,
                )
                for key in ENDPOINTS
            }
            origin = check_origin(lanes["sim1"], lanes["state"])
            assert lanes["sim2"].rpc("chain", "eth_chainId", [])[0] == "0x2105"
            owner_pin = {"blockHash": origin["block_hash"], "requireCanonical": True}
            raw, proof = lanes["state"].rpc("empty-override", "eth_getCode", [PROBE, owner_pin])
            assert not data_bytes(raw)
            header, amount, _ = retained()[63345200]
            preflight = [
                observe(lanes["state"], header, artifact(root), i, amount)
                for i in range(len(MARKETS))
            ]
            references = [row["pool_code"] for row in preflight]
            arm = [LIMIT]
            controls = []
            with ThreadPoolExecutor(max_workers=2) as workers:
                windows = [
                    panel_capture(
                        root, root / f"window-{i + 1}", lanes, workers, i, references, arm, capture
                    )
                    for i in range(PANELS)
                ]
                for cohort in (1, 2):
                    first = preflight[cohort]
                    chosen = rank(first, header, amount)["best"]
                    if chosen is None:
                        controls.append({"cohort": cohort, "whole": None})
                        continue
                    outcome = exact(
                        lanes,
                        workers,
                        root,
                        header,
                        chosen,
                        (first["l1_wei"], first["operator_wei"]),
                        1,
                    )
                    controls.append({"cohort": cohort, "chosen": chosen, "whole": outcome})
            for left, right in pairwise(windows):
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
            mixed.write(root / "STOP.json", {"fault": str(exc) or "assertion failed"})
        raise
    if capture:
        mixed.write(root / "STOP.json", {"fault": None})
    assert mixed.read(root / "STOP.json") == {"fault": None}
    with RpcBudget(root / "original.sqlite3", POLICIES, read_only=True) as budget:
        used = budget.counts()
    assert used == {key: lane.count for key, lane in lanes.items()}
    return {
        "scope": scope,
        "scope_sha256": mixed.digest(root / "scope.json"),
        "preflight": [{k: r[k] for k in ("pool_code", "proof")} for r in preflight],
        "origin": origin,
        "override_proof": proof,
        "http_reservations": used,
        "historical_controls": controls,
        "windows": windows,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "capture", "replay"))
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    if args.mode == "prepare":
        mixed.write(args.root / "scope.json", frozen(args.root, datetime.now(UTC).isoformat()))
    else:
        scope = mixed.read(args.root / "scope.json")
        assert scope == frozen(args.root, scope["frozen_utc"])
        print(json.dumps(run(args.root, scope, args.mode == "capture"), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
