"""Verify free Base simulation providers on the same historical call and state."""

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import httpx
from measure_base_fork_latency import historical_header
from replay_base_mixed_whole import (
    DIR,
    HEIGHT,
    WETH,
    bound_state,
    digest,
    read,
    settlement,
    simulation,
    state_params,
    write,
)

from oasis_agent.models.common import canonical_json, sha256_hex
from oasis_agent.rpc.budget import BudgetPolicy, RpcBudget
from scripts.measure_robinhood_quote_delivery import _body, _fetch, _read

ROOT = Path(__file__).resolve().parents[3]
SOURCE_REPORT = ROOT / "docs/verification/BASE_MIXED_WHOLE_20261128.json"
SOURCES = {
    "official": "https://mainnet.base.org",
    "drpc": "https://base.drpc.org",
    "alchemy_public": "https://base-mainnet.g.alchemy.com/public",
}
POLICIES = {name: (BudgetPolicy(name, 2, 250),) for name in SOURCES}


def plan(root, stamp):
    runtime = read_runtime(root)
    reference = read(SOURCE_REPORT)
    assert sha256_hex(bytes.fromhex(runtime[2:])) == reference["scope"]["runtime_sha256"]
    return {
        "schema": "oasis.base.free-simulator-method-probe.v1",
        "frozen_utc": stamp,
        "script_sha256": digest(Path(__file__)),
        "reference_sha256": digest(SOURCE_REPORT),
        "source_endpoints": SOURCES,
        "limits": dict.fromkeys(SOURCES, 2),
        "usd": 0,
        "height": HEIGHT,
        "hash": reference["scope"]["h"],
        "simulation_sha256": sha256_hex(canonical_json(simulation(HEIGHT, runtime, WETH))),
    }


def read_runtime(root):
    return "0x" + (root / "BaseMixedWholeProbe.bin-runtime").read_text().strip()


def source(root, name, capture):
    folder = root / name
    if capture:
        folder.mkdir()
    ledger = folder / "budget.sqlite3"
    policies = POLICIES[name]
    if capture:
        with RpcBudget(ledger, policies) as budget:
            assert sum(budget.counts().values()) == 0
    raw_scope = read(root / "scope.json")
    header = historical_header()
    assert header.block_hash == raw_scope["hash"]
    old_report = read(SOURCE_REPORT)
    args, specs = state_params({"blockHash": header.block_hash, "requireCanonical": True})
    state_body = (DIR / "11-state.request").read_bytes()
    state, _ = _read(DIR / "11-state", _body(11, "eth_call", args), 11, 1)
    assert state_body == _body(11, "eth_call", args)
    fee_state = bound_state(state, specs)
    reference = next(
        row for row in old_report["rows"] if row["height"] == HEIGHT and row["token"] == WETH
    )
    schedule = [
        ("eth_chainId", []),
        ("eth_simulateV1", simulation(HEIGHT, read_runtime(root), WETH)),
    ]
    result = None
    fault = None
    exchanges = []
    with httpx.Client(timeout=5, follow_redirects=False, trust_env=False) as client:
        for i, (method, params) in enumerate(schedule, 1):
            stem = folder / f"call-{i}"
            body = _body(i, method, params)
            try:
                if capture:
                    with RpcBudget(ledger, policies, require_existing=True) as budget:
                        if wait_ms := budget.reserve(name, now_ms=time.time_ns() // 10**6):
                            time.sleep((wait_ms + 1) / 1000)
                            if budget.reserve(name, now_ms=time.time_ns() // 10**6):
                                raise ValueError("budget interval did not advance")
                    _fetch(client, SOURCES[name], stem, body)
                item, _ = _read(stem, body, i, 1)
                if i == 1:
                    if item != "0x2105":
                        raise ValueError("chain ID differs")
                else:
                    result = settlement(
                        item,
                        header,
                        fee_state,
                        WETH,
                        (reference["middle_raw"], reference["final_received_raw"]),
                    )
            except (
                ValueError,
                AssertionError,
                OSError,
                KeyError,
                TypeError,
                httpx.HTTPError,
            ) as exc:
                fault = str(exc) or "simulation differs from verified historical reference"
            if stem.with_suffix(".meta").exists():
                clock = read(stem.with_suffix(".meta"))
                exchanges.append(
                    {
                        "method": method,
                        "elapsed_ns": clock["received_ns"] - clock["sent_ns"],
                        **clock,
                    }
                )
            if fault is not None:
                break
    with RpcBudget(ledger, policies, read_only=True) as budget:
        count = budget.counts()[name]
    stop = {"fault": fault, "requests": count}
    if capture:
        write(folder / "STOP.json", stop)
    else:
        assert read(folder / "STOP.json") == stop and count == len(exchanges)
    return name, {
        "reason": fault,
        "rpc_reservations": count,
        "exchanges": exchanges,
        "whole": result,
        "same_gas_as_prior_official": result["gas_used"] == reference["gas_used"]
        if result
        else None,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "capture", "replay"))
    parser.add_argument("root", type=Path)
    ns = parser.parse_args()
    if ns.mode == "prepare":
        write(ns.root / "scope.json", plan(ns.root, datetime.now(UTC).isoformat()))
        return
    frozen = read(ns.root / "scope.json")
    assert frozen == plan(ns.root, frozen["frozen_utc"])
    with ThreadPoolExecutor(max_workers=3) as workers:
        rows = dict(workers.map(lambda name: source(ns.root, name, ns.mode == "capture"), SOURCES))
    print(
        json.dumps(
            {"scope_sha256": digest(ns.root / "scope.json"), "sources": rows, "usd": 0},
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
