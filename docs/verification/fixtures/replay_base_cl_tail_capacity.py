"""Isolate Base TS1 liquidity ceilings at the protocol price limits on retained blocks."""

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from threading import Event

import httpx
import replay_base_mixed_whole as mixed
from measure_base_batched_quote import Lane, pin
from run_base_cl_coverage import ARTIFACT, LEG, POOLS, artifact, leg, query, retained

from oasis_agent.arbitrage.cycle_protocols import abi, calldata, data_bytes
from oasis_agent.models.common import sha256_hex
from oasis_agent.rpc.budget import BudgetPolicy, RpcBudget

PRIMARY = mixed.DIR.parent / "base-cl-coverage-bootstrap-20261128"
TAIL = mixed.DIR.parent / "base-cl-tail-gas-20261128"
GAS = 15_000_000
OUTER = 16_000_000
LIMIT_OFFSET = 2127
TARGETS = ((63345200, 4), (63345203, 3))
POLICIES = tuple(BudgetPolicy(s, 5, 250) for s in ("state", "sim2"))


def runtime():
    original = data_bytes(artifact(PRIMARY))
    old = bytes.fromhex("621e8480")
    assert original.count(old) == 1 and original[LIMIT_OFFSET : LIMIT_OFFSET + 4] == old
    replacement = bytes([0x62]) + GAS.to_bytes(3)
    return "0x" + (original[:LIMIT_OFFSET] + replacement + original[LIMIT_OFFSET + 4 :]).hex()


def plan(stamp):
    return {
        "schema": "oasis.base-ts1-full-range-control.v1",
        "frozen_utc": stamp,
        "original_runtime_sha256": mixed.digest(PRIMARY / ARTIFACT),
        "driver_sha256": mixed.digest(Path(__file__)),
        "patched_runtime_sha256": sha256_hex(data_bytes(runtime())),
        "bounded_report_sha256": mixed.digest(PRIMARY / "stdout.json"),
        "prior_wide_report_sha256": mixed.digest(TAIL / "stdout.json"),
        "max_inner_gas": GAS,
        "call_gas": OUTER,
        "patch": "only PUSH3(2000000) replaced with PUSH3(15000000); no branch removed",
        "targets": [list(row) for row in TARGETS],
        "sources": ["state", "sim2"],
        "usd": 0,
        "retry": False,
    }


def diagnose(lane, header, pool, direction, amount):
    payload = calldata(
        "diagnose(uint8,bool,uint256,uint16,uint256)",
        ("uint8", "bool", "uint256", "uint16", "uint256"),
        (pool, direction, amount, 0, GAS),
    )
    label = f"full-{pool}-{int(direction)}"
    raw, proof = query(lane, label, header, runtime(), payload, OUTER)
    [decoded] = abi((LEG,), raw)
    result = leg(decoded)
    assert result["consumed"] <= amount
    assert result["status"] != 1 or result["consumed"] == amount
    assert result["status"] != 2 or result["consumed"] < amount
    return result | {
        "height": header.number,
        "hash": header.block_hash,
        "zero_for_one": direction,
        "pool": POOLS[pool],
        "amount_specified": amount,
        "proof": proof,
    }


def run(root, capture):
    history = retained()
    bounded = mixed.read(PRIMARY / "stdout.json")
    assert not bounded["fault"]
    assert len(bounded["rows"]) == 68
    assert sum(len(row["missing"]) for row in bounded["rows"]) == 936
    if capture:
        with RpcBudget(root / "original.sqlite3", POLICIES):
            pass
    rows, failures = {}, {}
    known = next(iter(history.values()))[0]
    # The older scope confirmed the override address empty before the observations.
    empty = bounded["override_proof"]
    assert empty["response_sha256"] == mixed.digest(
        next(PRIMARY.glob("state-*override-empty.response"))
    )
    assert pin(known)["requireCanonical"]
    for key in ("state", "sim2"):
        rows[key] = []
        fault = None
        try:
            with httpx.Client(timeout=8, follow_redirects=False, trust_env=False) as client:
                lane = Lane(root, key, POLICIES, Event(), client if capture else None)
                assert lane.rpc("chain", "eth_chainId", [])[0] == "0x2105"
                for height, pool in TARGETS:
                    header, lot, earlier = history[height]
                    # The first frozen production path buys in POOLS[0] before selling
                    # in the target thin pool. Use the exact retained first-leg input.
                    right = next(
                        row["left"]["output"]
                        for head in bounded["rows"]
                        if head["height"] == height
                        for row in head["missing"]
                        if row["pools"][0] == POOLS[0] and row["left"]["status"] == 1
                    )
                    assert right > 0 and earlier[4][pool] == POOLS[pool]
                    for direction, amount in ((True, lot), (False, right)):
                        rows[key].append(diagnose(lane, header, pool, direction, amount))
        except (AssertionError, ValueError, TypeError, KeyError, OSError, httpx.HTTPError) as e:
            fault = str(e) or "invalid callback or input witness"
        failures[key] = fault
    paired = []
    for a, b in zip(rows["state"], rows["sim2"], strict=False):
        ignored = {"proof", "spent_gas"}
        assert {k: v for k, v in a.items() if k not in ignored} == {
            k: v for k, v in b.items() if k not in ignored
        }, "independent providers disagree on callback"
        paired.append(a["height"])
    if capture:
        mixed.write(root / "STOP.json", failures)
    else:
        assert mixed.read(root / "STOP.json") == failures
    with RpcBudget(root / "original.sqlite3", POLICIES, read_only=True) as budget:
        counts = budget.counts()
    return {
        "rows": rows,
        "failures": failures,
        "paired": paired,
        "requests": counts,
        "scope_sha256": mixed.digest(root / "scope.json"),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "capture", "replay"))
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    p = args.root / "scope.json"
    if args.action == "prepare":
        mixed.write(p, plan(datetime.now(UTC).isoformat()))
        return
    scope = mixed.read(p)
    assert scope == plan(scope["frozen_utc"])
    assert scope["driver_sha256"] == mixed.digest(args.root / "capture.py")
    print(json.dumps(run(args.root, args.action == "capture"), sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
