"""Same-hash Base runtime/StateView serial versus overlapping read control."""

import argparse
import json
import signal
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from threading import Event

import httpx
import replay_base_bloom_state_ab as bloom_fixture
from eth_hash.auto import keccak
from replay_base_bloom_state_ab import Exchange, digest, head_for

from oasis_agent.arbitrage.cycle_protocols import data_bytes
from oasis_agent.evidence.base_newheads import NewHeadsSession, replay_head_observation
from oasis_agent.fastpath.base_mixed_snapshot import (
    MANAGER,
    MANAGER_RUNTIME,
    OFFICIAL,
    PUBLICNODE,
    SOURCES,
    _decode_v4,
    _header,
    _request,
    _result,
    _v4_calls,
)
from oasis_agent.fastpath.multicall_universe import MULTICALL3, _encode_aggregate3_calldata
from oasis_agent.models.common import canonical_json
from oasis_agent.rpc.budget import BudgetPolicy, RpcBudget
from scripts.capture_public_rpc import durable_new

POLICIES = (BudgetPolicy(PUBLICNODE, 2, 250), BudgetPolicy(OFFICIAL, 10, 250))
WINDOW = 3
ORDERS = [("serial", "overlap"), ("overlap", "serial")]


def make_plan():
    return {
        "schema": "oasis-base-v4-transport-overlap-v1",
        "chain_id": 8453,
        "usd": 0,
        "max_heads": WINDOW,
        "overall_read_cap": 15,
        "per_source_caps": {p.source: p.max_requests for p in POLICIES},
        "websocket_primitives": 3,
        "wall_seconds": 25,
        "retry": False,
        "http": SOURCES,
        "arm_order": [list(row) for row in ORDERS],
        "notes": "two blocks paired, next head witness only. Runtime hash verified before release.",
        "code_sha256": digest(Path(__file__).read_bytes()),
    }


def pair(frame, rpc, workers, stopped, arm_order):
    n = frame.head.height
    header_req = _request("eth_getBlockByNumber", [hex(n), False])

    def guarded(name, source, request):
        try:
            return rpc(name, source, request)
        except Exception:
            stopped.set()
            raise

    def head(source):
        return _header(guarded(f"{n}-{source}", source, header_req))

    other = workers.submit(head, OFFICIAL)
    block = head(PUBLICNODE)
    if block != other.result() or not frame.head.agrees(head_for(block)):
        raise ValueError("independent block witnesses disagree")
    header_done = time.monotonic_ns()
    pin = {"blockHash": block.block_hash, "requireCanonical": True}
    calls = _v4_calls()
    code_request = _request("eth_getCode", [MANAGER, pin])
    value_request = _request(
        "eth_call", [{"to": MULTICALL3, "data": _encode_aggregate3_calldata(calls)}, pin], req_id=2
    )

    def arm(kind):
        code_label = f"{n}-{kind}-runtime"
        v4_label = f"{n}-{kind}-v4"
        began = time.monotonic_ns()

        def verified_code():
            runtime = guarded(code_label, OFFICIAL, code_request)
            if "0x" + keccak(data_bytes(_result(runtime))).hex() != MANAGER_RUNTIME:
                stopped.set()
                raise ValueError("Base PoolManager runtime hash differs")
            return runtime

        def verified_v4():
            v4raw = guarded(v4_label, OFFICIAL, value_request)
            try:
                return v4raw, _decode_v4(v4raw, calls, block, bitmap_aware=True)
            except Exception:
                stopped.set()
                raise

        if kind == "serial":
            runtime = verified_code()
            v4raw, rows = verified_v4()
        else:
            code = workers.submit(verified_code)
            state = workers.submit(verified_v4)
            for task in as_completed((code, state)):
                if task.exception() is not None:
                    raise task.exception()
            runtime = code.result()
            v4raw, rows = state.result()
        end = time.monotonic_ns()
        return {
            "code_response_sha256": digest(runtime),
            "v4_response_sha256": digest(v4raw),
            "logical_state_sha256": digest(
                canonical_json(
                    {
                        "pools": [node.to_wire() for node in rows[0]],
                        "bitmap_bounds": rows[1],
                        "operator": [rows[2], rows[3]],
                    }
                )
            ),
            "begin_ns": began,
            "end_ns": end,
            "v4_subcalls": len(calls),
            "wire_labels": [code_label, v4_label],
        }

    rows = {kind: arm(kind) for kind in arm_order}
    if any(
        rows["serial"][key] != rows["overlap"][key]
        for key in ("code_response_sha256", "v4_response_sha256", "logical_state_sha256")
    ):
        raise ValueError("same-hash V4 bytes or state differ across arms")
    return {
        "head": frame.to_wire(),
        "header_done_ns": header_done,
        "arm_order": arm_order,
        "arms": rows,
    }


def main():
    if not __debug__:
        raise SystemExit("run this verifier without Python optimization")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("prepare", "capture", "replay"))
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    root = args.root
    scope = root / "scope.json"
    plan = make_plan()
    if args.operation == "prepare":
        durable_new(scope, canonical_json(dict(plan, frozen_utc=datetime.now(UTC).isoformat())))
        return
    frozen = json.loads(scope.read_bytes())
    assert all(frozen[k] == v for k, v in plan.items())
    capture = args.operation == "capture"
    stop = Event()
    session = None
    seen = []
    pairs = []
    failure = None
    record = root / "clocks.json"
    if capture:
        durable_new(root / "START.json", canonical_json({"utc": datetime.now(UTC).isoformat()}))
        with RpcBudget(root / "original.sqlite3", POLICIES):
            pass
        session = NewHeadsSession(root / "heads", WINDOW, stop)

        def timeout(*_):
            raise TimeoutError("fixed V4 overlap deadline")

        signal.signal(signal.SIGALRM, timeout)
        signal.alarm(plan["wall_seconds"])
        session.start()
    else:
        observed = json.loads(record.read_bytes())
        assert observed["failure"] is None
        seen = observed["heads"]
    try:
        with (
            httpx.Client(timeout=4, trust_env=False, follow_redirects=False) as client,
            ThreadPoolExecutor(max_workers=3) as workers,
        ):
            rpc = Exchange(root, stop, client if capture else None)
            # The shared exchange's reserve method reads this experiment's fixed source caps.
            bloom_fixture.POLICIES = POLICIES
            for i in range(WINDOW):
                frame = (
                    session.next_head()
                    if capture
                    else replay_head_observation(root / "heads", seen[i])
                )
                if capture:
                    seen.append(frame.to_wire())
                if i == WINDOW - 1:
                    break
                result = pair(frame, rpc, workers, stop, ORDERS[i])
                if capture:
                    pairs.append(result)
                else:
                    real = observed["pairs"][i]
                    assert (
                        result["head"] == real["head"]
                        and list(result["arm_order"]) == real["arm_order"]
                    )
                    for kind, arm in result["arms"].items():
                        assert all(
                            arm[k] == real["arms"][kind][k] for k in arm if not k.endswith("_ns")
                        )
                    pairs.append(real)
        if session is not None:
            session.check()
    except Exception as exc:
        if not capture:
            raise
        stop.set()
        failure = {"type": type(exc).__name__, "reason": str(exc) or type(exc).__name__}
    finally:
        if session is not None:
            session.close()
            signal.alarm(0)
            durable_new(record, canonical_json({"heads": seen, "pairs": pairs, "failure": failure}))
            durable_new(
                root / "STOP.json",
                canonical_json({"utc": datetime.now(UTC).isoformat(), "failure": failure}),
            )
    for i, row in enumerate(pairs):
        receive = row["head"]["received_monotonic_ns"]
        assert receive <= row["header_done_ns"]
        row["received_to_headers_ns"] = row["header_done_ns"] - receive
        row["received_to_child_ns"] = (
            seen[i + 1]["received_monotonic_ns"] - receive if i + 1 < len(seen) else None
        )
        for arm in row["arms"].values():
            assert row["header_done_ns"] <= arm["begin_ns"]
            wire = [rpc.clocks[label] for label in arm["wire_labels"]]
            assert all(arm["begin_ns"] <= a <= b <= arm["end_ns"] for a, b in wire)
            arm["transport_overlapped_ns"] = max(
                0, min(a[1] for a in wire) - max(a[0] for a in wire)
            )
            arm["latency_ns"] = arm["end_ns"] - arm["begin_ns"]
            arm["heads_to_v4_ns"] = arm["end_ns"] - receive
            arm["transport_ns"] = [b - a for a, b in wire]
    with RpcBudget(root / "original.sqlite3", POLICIES, read_only=True) as ledger:
        counts = ledger.counts()
    report = {
        "schema": "oasis-base-v4-transport-overlap-result-v1",
        "scope_sha256": digest(scope.read_bytes()),
        "heads": seen,
        "pairs": pairs,
        "counts": counts,
        "failure": failure,
    }
    output = canonical_json(report) + b"\n"
    if capture:
        durable_new(root / "result.json", output)
    print(output.decode(), end="")
    raise SystemExit(0 if failure is None else 2)


if __name__ == "__main__":
    main()
