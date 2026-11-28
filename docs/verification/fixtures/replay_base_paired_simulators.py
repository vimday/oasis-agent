"""Compare two anonymous Base simulators on identical fresh heads and fixed principal."""

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import httpx
from replay_base_mixed_whole import (
    CHAIN,
    DIR,
    WETH,
    bound_state,
    digest,
    read,
    settlement,
    simulation,
    state_params,
    write,
)
from replay_base_simulator_sources import SOURCES as PROBE_SOURCES
from replay_base_simulator_sources import read_runtime

from oasis_agent.evidence.base_newheads import (
    POLICIES as WS_POLICIES,
)
from oasis_agent.evidence.base_newheads import (
    NewHeadsSession,
    replay_head_observation,
)
from oasis_agent.models.common import sha256_hex
from oasis_agent.rpc.budget import BudgetPolicy, RpcBudget
from scripts.measure_robinhood_quote_delivery import _body, _fetch, _read

RESEARCH = Path(__file__).resolve().parents[3] / "reports/research"
PRECHECK = RESEARCH / "base-free-simulators-hash-20261128"
SOURCES = {
    "official": PROBE_SOURCES["official"],
    "drpc": PROBE_SOURCES["drpc"],
    "state": "https://base-rpc.publicnode.com",
}
POSITIONS = (1, 5, 9, 13)
FRAMES = POSITIONS[-1] + 1
POLICIES = tuple(BudgetPolicy(source, len(POSITIONS) + 1, 250) for source in SOURCES)


def plan(root, utc):
    preflight = read(PRECHECK / "stdout.json")
    assert preflight["sources"]["official"]["whole"] == preflight["sources"]["drpc"]["whole"]
    assert preflight["sources"]["official"]["whole"]["status"] == "settled"
    runtime_sha = sha256_hex(bytes.fromhex(read_runtime(root)[2:]))
    assert runtime_sha == read(DIR / "scope.json")["runtime_sha256"]
    return {
        "schema": "oasis.base.paired-public-simulators.v1",
        "frozen_utc": utc,
        "script_sha256": digest(Path(__file__)),
        "runtime_sha256": runtime_sha,
        "prior_precheck_sha256": digest(PRECHECK / "stdout.json"),
        "source_urls": SOURCES,
        "head_source": "wss://base-rpc.publicnode.com",
        "samples": list(POSITIONS),
        "max_frames": FRAMES,
        "max_http_by_source": len(POSITIONS) + 1,
        "usd": 0,
    }


def exchange(root, source, ordinal, method, args, earliest, capture, stop, client):
    body = _body(ordinal, method, args)
    prefix = root / f"{source}-{ordinal:02}"
    if capture:
        if stop.is_set():
            raise ValueError("the frozen panel stopped")
        with RpcBudget(root / "original.sqlite3", POLICIES, require_existing=True) as budget:
            if wait_ms := budget.reserve(source, now_ms=time.time_ns() // 10**6):
                time.sleep((wait_ms + 1) / 1000)
                if stop.is_set():
                    raise ValueError("the frozen panel stopped")
                if budget.reserve(source, now_ms=time.time_ns() // 10**6):
                    raise ValueError("RPC budget interval did not advance")
        try:
            _fetch(client, SOURCES[source], prefix, body)
        except ValueError, TypeError, OSError, httpx.HTTPError:
            stop.set()
            raise
    try:
        result, _ = _read(prefix, body, ordinal, earliest)
    except ValueError, TypeError, OSError:
        stop.set()
        raise
    return result, read(prefix.with_suffix(".meta"))


def validate_refs(root, refs):
    if len(refs) != FRAMES:
        raise ValueError("fewer than the frozen number of canonical heads")
    raw = root / "heads"
    observations = [replay_head_observation(raw, row) for row in refs]
    for left, right in pairwise(observations):
        assert right.head.height == left.head.height + 1
        assert right.head.parent_hash == left.head.block_hash
        assert left.received_monotonic_ns < right.received_monotonic_ns
    return observations


def run(root, capture):
    stop = Event()
    if capture:
        with RpcBudget(root / "original.sqlite3", POLICIES):
            pass
    refs, results, fault = [], [], None
    ws_count = None
    runtime = read_runtime(root)
    clients = (
        {name: httpx.Client(timeout=5, follow_redirects=False, trust_env=False) for name in SOURCES}
        if capture
        else dict.fromkeys(SOURCES)
    )
    try:
        with ThreadPoolExecutor(max_workers=3) as workers:
            calls = [
                workers.submit(
                    exchange, root, source, 1, "eth_chainId", [], 1, capture, stop, clients[source]
                )
                for source in SOURCES
            ]
            assert all(f.result()[0] == hex(CHAIN) for f in calls)
            work = {}
            stream = NewHeadsSession(root / "heads", FRAMES, stop) if capture else None
            if stream is not None:
                stream.start()
                observations = (stream.next_head().to_wire() for _ in range(FRAMES))
            else:
                observations = iter(read(root / "heads.json"))
            try:
                for pos, row in enumerate(observations, 1):
                    refs.append(row)
                    if pos not in POSITIONS:
                        continue
                    h = row["head"]
                    earliest = row["received_monotonic_ns"]
                    number = POSITIONS.index(pos) + 2
                    sim = simulation(h["height"], runtime, WETH)
                    state, specifications = state_params(
                        {"blockHash": h["block_hash"], "requireCanonical": True}
                    )
                    requests = {
                        "official": ("eth_simulateV1", sim),
                        "drpc": ("eth_simulateV1", sim),
                        "state": ("eth_call", state),
                    }
                    work[pos] = (
                        specifications,
                        {
                            source: workers.submit(
                                exchange,
                                root,
                                source,
                                number,
                                method,
                                args,
                                earliest,
                                capture,
                                stop,
                                clients[source],
                            )
                            for source, (method, args) in requests.items()
                        },
                    )
                if stream is not None:
                    stream.check()
            finally:
                if stream is not None:
                    stream.close()
                    ws_count = stream.counts()
                    write(root / "heads.json", refs)
            heads = validate_refs(root, refs)
            for pos, (specifications, pending) in work.items():
                head, child = heads[pos - 1 : pos + 1]
                block = SimpleNamespace(number=head.head.height, block_hash=head.head.block_hash)
                state, fee_proof = pending["state"].result()
                fees = bound_state(state, specifications)
                output = {}
                for source in ("official", "drpc"):
                    raw, proof = pending[source].result()
                    whole = settlement(raw, block, fees, WETH)
                    complete = max(proof["received_ns"], fee_proof["received_ns"])
                    output[source] = {
                        "whole": whole,
                        "proof": proof,
                        "request_from_head_ns": proof["sent_ns"] - head.received_monotonic_ns,
                        "whole_wire_ns": proof["received_ns"] - proof["sent_ns"],
                        "ready_from_head_ns": complete - head.received_monotonic_ns,
                        "available_before_child": complete < child.received_monotonic_ns,
                    }
                results.append(
                    {
                        "height": block.number,
                        "hash": block.block_hash,
                        "child_height": child.head.height,
                        "observed_child_gap_ns": child.received_monotonic_ns
                        - head.received_monotonic_ns,
                        "fee_proof": fee_proof,
                        "whole_cash_parity": output["official"]["whole"] == output["drpc"]["whole"],
                        "lanes": output,
                    }
                )
    except (
        ValueError,
        TypeError,
        OSError,
        KeyError,
        AssertionError,
        RuntimeError,
        httpx.HTTPError,
    ) as exc:
        fault = str(exc) or "frozen head or chain validation failed"
    finally:
        for client in clients.values():
            if client is not None:
                client.close()
    with RpcBudget(root / "original.sqlite3", POLICIES, read_only=True) as ledger:
        counts = ledger.counts()
    if ws_count is None and (root / "heads/original-budget.sqlite3").exists():
        with RpcBudget(
            root / "heads/original-budget.sqlite3", WS_POLICIES, read_only=True
        ) as ledger:
            ws_count = ledger.counts()
    result = {
        "scope_sha256": digest(root / "scope.json"),
        "rows": results,
        "http_counts": counts,
        "ws_counts": ws_count,
        "fault": fault,
        "actual_paid_wei": None,
    }
    if capture:
        write(root / "STOP.json", {"fault": fault})
    else:
        assert read(root / "STOP.json") == {"fault": fault}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "capture", "replay"))
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    if args.mode == "prepare":
        write(args.root / "scope.json", plan(args.root, datetime.now(UTC).isoformat()))
        return
    active = read(args.root / "scope.json")
    assert active == plan(args.root, active["frozen_utc"])
    print(json.dumps(run(args.root, args.mode == "capture"), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
