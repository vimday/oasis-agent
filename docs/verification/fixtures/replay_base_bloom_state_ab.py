"""Read-only, exact-hash paired Base address-state experiment and offline replay."""

import argparse
import json
import signal
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from threading import Event, Lock

import httpx
from eth_abi import encode
from eth_hash.auto import keccak

from oasis_agent.arbitrage.cycle_protocols import abi, data_bytes
from oasis_agent.evidence.base_head_compare import BaseHead
from oasis_agent.evidence.base_newheads import NewHeadsSession, replay_head_observation
from oasis_agent.evidence.log_bloom import possible_log_emitters
from oasis_agent.fastpath.base_mixed_snapshot import (
    MANAGER,
    MANAGER_RUNTIME,
    OFFICIAL,
    PUBLICNODE,
    SOURCES,
    USDC,
    WETH,
    _decode_v4,
    _header,
    _request,
    _result,
    _v4_calls,
    address_roster,
)
from oasis_agent.fastpath.live_shadow_runner import canonical_base_weth_usdc_pools
from oasis_agent.fastpath.multicall_universe import (
    GET_RESERVES_SELECTOR,
    LIQUIDITY_SELECTOR,
    MULTICALL3,
    SLOT0_SELECTOR,
    _decode_aggregate3_results,
    _encode_aggregate3_calldata,
    fetch_pinned_universe_snapshot,
)
from oasis_agent.fastpath.pool_graph import FastpathBridge, FastpathPoolGraph
from oasis_agent.models.common import canonical_json
from oasis_agent.rpc.budget import BudgetPolicy, RpcBudget
from scripts.benchmark_resident_pool_graph import report_signature
from scripts.capture_public_rpc import durable_new
from scripts.measure_robinhood_quote_delivery import _fetch, _read
from scripts.run_multicall_100tick_campaign import (
    USDC_SCAN_MAX_RAW,
    USDC_SCAN_MIN_RAW,
    WETH_SCAN_MAX_WEI,
    WETH_SCAN_MIN_WEI,
    _compute_eth_price_micro_usdc,
)

MAX_HEADS = 5
PINS = MAX_HEADS - 1
POLICIES = (BudgetPolicy(PUBLICNODE, 11, 250), BudgetPolicy(OFFICIAL, 12, 250))
BASELINE = Path(
    "reports/research/base-latest-head-opportunity-20261127/run/mixed_base_state/attempt-00000001/manifest.json"
)
TIMING_NS = 1_000_000
L2_UNITS = 550_000
TIP_WEI = 1_000_000
MUTABLE = {data_bytes(x) for x in (SLOT0_SELECTOR, LIQUIDITY_SELECTOR, GET_RESERVES_SELECTOR)}


def digest(data):
    return sha256(data).hexdigest()


def head_for(block):
    return BaseHead(
        block.number,
        block.block_hash,
        block.parent_hash,
        block.state_root,
        block.timestamp_ms // 1000,
        block.timestamp_ms,
    )


def select_calls(calls, previous, dirty):
    return [c for c in calls if c not in previous or c[0] in dirty or c[2] not in MUTABLE]


def order_calls(body):
    request = json.loads(body)
    (encoded,) = abi(("(address,bool,bytes)[]",), data_bytes(request["params"][0]["data"])[4:])
    if request["params"][0]["data"] != _encode_aggregate3_calldata(encoded):
        raise ValueError("unexpected address Multicall")
    return tuple(encoded), request["params"][1]


def aggregate_response(data, n):
    raw = data_bytes(_result(data, 2))
    return _decode_aggregate3_results(raw, n)


def digest_rows(rows):
    return digest(
        canonical_json([{"success": success, "value": data.hex()} for success, data in rows])
    )


def scan(address, v4, controls, bridge):
    av = address.pool_nodes
    anchors = (
        (WETH, WETH_SCAN_MIN_WEI, WETH_SCAN_MAX_WEI),
        (USDC, USDC_SCAN_MIN_RAW, USDC_SCAN_MAX_RAW),
    )
    composed = [node for node in av if node.pool_id in controls]
    price = _compute_eth_price_micro_usdc(av)
    gas = L2_UNITS * (address.base_fee_per_gas_wei + TIP_WEI)
    l1 = address.l1_fee_wei

    def in_usdc(amount):
        return (amount * price + 10**18 - 1) // 10**18

    reports = {}
    for nodes in (composed, av, (*av, *v4)):
        graph = FastpathPoolGraph(bridge)
        for node in nodes:
            graph.upsert_pool(node)
        signatures = []
        for asset, low, high in anchors:
            tracked = graph.scan_cycles(
                [asset],
                max_hops=3,
                min_amount_in=low,
                max_amount_in=high,
                gas_cost_in_base=gas if asset == WETH else in_usdc(gas),
                l1_fee_in_base=l1 if asset == WETH else in_usdc(l1),
                min_net_profit=1,
                block_number=address.block_number,
                timestamp_ns=address.base_time_ms * 1_000_000,
            )
            signatures.append(report_signature(tracked))
        reports[str(len(nodes))] = {
            "scan_signature_sha256": digest(canonical_json(signatures)),
            "candidates": sum(len(s["candidates"]) for s in signatures),
            "positive_candidates": [x for sig in signatures for x in sig["candidates"]],
            "residuals": [
                None if s["sizing_residuals"] is None else len(s["sizing_residuals"])
                for s in signatures
            ],
        }
    return reports


def plan():
    metadata = json.loads(BASELINE.read_bytes())
    return {
        "schema": "oasis-base-bloom-state-ab-1",
        "usd": 0,
        "chain_id": 8453,
        "scope": "one warmup, three paired, and one additional direct-child timing witness",
        "max_heads": MAX_HEADS,
        "timeout_s": 35,
        "ws_primitives": 3,
        "max_all_primitive": 26,
        "http_caps": {p.source: p.max_requests for p in POLICIES},
        "both_arms_budget": "one pinned address-state read per arm per paired head",
        "pacing_ms_per_http_source": 250,
        "sources": SOURCES,
        "retry": False,
        "pool_model": "58 frozen address pools; four canonical subset; eight V4 common full reseed",
        "watch": "58 emitters, two HTTP and WS Blooms; fees and balances re-read",
        "order": ["full_first", "incremental_first", "full_first"],
        "original_refusal_sha256": digest(
            Path("reports/research/base-incremental-state-ab-20261127/STOP.json").read_bytes()
        ),
        "gas_model": {"gas_units": L2_UNITS, "priority_wei_per_gas": TIP_WEI},
        "roster_source": str(BASELINE),
        "roster_source_sha256": digest(BASELINE.read_bytes()),
        "roster": metadata["address_roster"],
        "script_sha256": digest(Path(__file__).read_bytes()),
    }


class Exchange:
    def __init__(self, root, stopped, client):
        self.root = root
        self.stopped = stopped
        self.client = client
        self.locks = {source: Lock() for source in SOURCES}
        self.clocks = {}

    def __call__(self, name, source, request):
        prefix = self.root / name
        if self.client is not None:
            with (
                self.locks[source],
                RpcBudget(
                    self.root / "original.sqlite3", POLICIES, require_existing=True
                ) as budget,
            ):
                if self.stopped.is_set():
                    raise ValueError("Base comparison stopped before reservation")
                delay = budget.reserve(source, now_ms=time.time_ns() // 1_000_000)
                if delay:
                    time.sleep((delay + 1) / 1000)
                    if self.stopped.is_set() or budget.reserve(
                        source, now_ms=time.time_ns() // 1_000_000
                    ):
                        raise ValueError("Base comparison pacing stopped")
            # A reservation was recorded before physical transport, including rejected calls.
            _fetch(self.client, SOURCES[source], prefix, request)
        request_id = json.loads(request)["id"]
        _, clock = _read(prefix, request, request_id, 1)
        self.clocks[name] = (json.loads(prefix.with_suffix(".meta").read_bytes())["sent_ns"], clock)
        return prefix.with_suffix(".response").read_bytes()


def state_head(frame, specs, exchange, previous, workers, bridge, *, incremental_first):
    n = frame.head.height
    head_request = _request("eth_getBlockByNumber", [hex(n), False])

    def get_head(role):
        evidence = exchange(f"{n}-{role}", role, head_request)
        block = _header(evidence)
        if not frame.head.agrees(head_for(block)):
            raise ValueError("Base WS and HTTP pin disagree")
        return evidence, block

    clocks = {}

    def official(block):
        pin = {"blockHash": block.block_hash, "requireCanonical": True}
        code = data_bytes(
            _result(exchange(f"{n}-manager", OFFICIAL, _request("eth_getCode", [MANAGER, pin])))
        )
        if "0x" + keccak(code).hex() != MANAGER_RUNTIME:
            raise ValueError("unexpected fixed Base PoolManager runtime")
        calls = _v4_calls()
        body = _request(
            "eth_call",
            [{"to": MULTICALL3, "data": _encode_aggregate3_calldata(calls)}, pin],
            req_id=2,
        )
        raw = exchange(f"{n}-v4", OFFICIAL, body)
        v4 = _decode_v4(raw, calls, block, bitmap_aware=True)
        clocks["official_decoded_ns"] = time.monotonic_ns()
        return v4

    def guard(callable_, *args):
        try:
            return callable_(*args)
        except Exception:
            exchange.stopped.set()
            raise

    peer_header = workers.submit(guard, get_head, OFFICIAL)
    header, block = guard(get_head, PUBLICNODE)
    official_header, official_block = peer_header.result()
    if official_block != block:
        raise ValueError("Base A/B HTTP headers disagree")
    clocks["headers_confirmed_ns"] = time.monotonic_ns()
    piv = workers.submit(guard, official, official_block)
    pool_set = {spec.pool_id for spec in specs}
    pin = {"blockHash": block.block_hash, "requireCanonical": True}
    ws_path = exchange.root / "heads" / f"frame-{frame.frame:08d}.raw"
    ws_raw = ws_path.read_bytes()
    if frame.frame_sha256 != digest(ws_raw):
        raise ValueError("observed WS Bloom frame digest changed")
    ws = json.loads(ws_raw)["params"]["result"]
    cpu = {}
    cpu_start = time.thread_time_ns()
    blooms = (_result(header)["logsBloom"], _result(official_header)["logsBloom"], ws["logsBloom"])
    dirty = possible_log_emitters(pool_set, matching_header_blooms=blooms)
    cpu["bloom_selection"] = time.thread_time_ns() - cpu_start
    address_data = {}

    def full(_endpoint, body, _timeout):
        if body == head_request:
            return header
        calls, pinned = order_calls(body)
        if pinned != pin:
            raise ValueError("full state not hash-pinned")
        raw = exchange(f"{n}-full", PUBLICNODE, body)
        address_data["full"] = dict(zip(calls, aggregate_response(raw, len(calls)), strict=True))
        return raw

    def inc(_endpoint, body, _timeout):
        if body == head_request:
            return header
        calls, pinned = order_calls(body)
        if pinned != pin:
            raise ValueError("incremental state not hash-pinned")
        picked = select_calls(calls, previous, dirty)
        query = _request(
            "eth_call",
            [{"to": MULTICALL3, "data": _encode_aggregate3_calldata(picked)}, pin],
            req_id=2,
        )
        selected = aggregate_response(exchange(f"{n}-delta", PUBLICNODE, query), len(picked))
        map_selected = dict(zip(picked, selected, strict=True))
        rebuilt = {
            call: map_selected[call] if call in map_selected else previous[call] for call in calls
        }
        address_data["incremental"] = rebuilt
        address_data["selected"] = picked

        return canonical_json(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "result": "0x" + encode(("(bool,bytes)[]",), (tuple(rebuilt.values()),)).hex(),
            }
        )

    def acquire(which, do):
        before = time.thread_time_ns()
        item = fetch_pinned_universe_snapshot(
            SOURCES[PUBLICNODE], specs, transport=do, block_tag=hex(n), pin_call_to_hash=True
        )
        cpu[f"{which}_address_total"] = time.thread_time_ns() - before
        clocks[which + "_decoded_ns"] = time.monotonic_ns()
        return item

    address_inc = None
    if previous and incremental_first:
        address_inc = guard(acquire, "incremental", inc)
    address_full = guard(acquire, "full", full)
    if previous and address_inc is None:
        address_inc = guard(acquire, "incremental", inc)
    v4, bounds, scalar, constant = piv.result()
    if address_inc is not None and (
        address_data["full"] != address_data["incremental"] or address_full != address_inc
    ):
        raise ValueError("incremental rebuild differs from hash-pinned full snapshot")
    clocks["state_parity_ns"] = time.monotonic_ns()
    controls = {spec.address for spec in canonical_base_weth_usdc_pools()}
    arm_reports = {}
    for which, state in (("full", address_full), ("incremental", address_inc)):
        if state is not None:
            before = time.thread_time_ns()
            arm_reports[which] = scan(state, v4, controls, bridge)
            cpu[f"{which}_scanning"] = time.thread_time_ns() - before
            clocks[which + "_scan_ns"] = time.monotonic_ns()
    if address_inc is not None and arm_reports["incremental"] != arm_reports["full"]:
        raise ValueError("A/B Rust candidate or sizing interval disagreement")
    picked = address_data.get("selected")
    proof = {
        "observation": frame.to_wire(),
        "binding": asdict(block),
        "phase": "bootstrap" if not previous else "paired",
        "full_address_calls": len(address_data["full"]),
        "delta_address_calls": None if picked is None else len(picked),
        "possible_log_emitters": sorted(dirty),
        "control_possible_log_emitters": sorted(dirty & controls),
        "arm_order": "incremental_first" if incremental_first else "full_first",
        "address_pool_sha256": {
            node.pool_id: digest(
                canonical_json(
                    {
                        "node": node.to_wire(),
                        "balances": address_full.balances_by_pool[node.pool_id],
                    }
                )
            )
            for node in address_full.pool_nodes
        },
        "shared_v4_pools": len(v4),
        "v4_state_sha256": digest(
            canonical_json(
                {
                    "v4": [node.to_wire() for node in v4],
                    "bounds": bounds,
                    "operator": [scalar, constant],
                }
            )
        ),
        "address_raw_rows_sha256": digest_rows(address_data["full"].values()),
        "reports": arm_reports,
    }
    return proof, clocks, cpu, address_data.get("incremental", address_data["full"])


def main():
    if not __debug__:
        raise SystemExit("run without Python optimization")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("prepare", "capture", "replay"))
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    folder = args.root
    if args.operation == "prepare":
        durable_new(
            folder / "scope.json",
            canonical_json(dict(plan(), frozen_utc=datetime.now(UTC).isoformat())),
        )
        return
    frozen = json.loads((folder / "scope.json").read_bytes())
    assert all(frozen[k] == v for k, v in plan().items())
    raw_stop = folder / "STOP.json"
    capture = args.operation == "capture"
    rows = []
    stopped = Event()
    cache = None
    status = "complete"
    bridge = FastpathBridge()
    observations = []
    timing = {}
    session = None
    if capture:
        durable_new(folder / "START.json", canonical_json({"utc": datetime.now(UTC).isoformat()}))
        with RpcBudget(folder / "original.sqlite3", POLICIES):
            pass
        session = NewHeadsSession(folder / "heads", MAX_HEADS, stopped)

        def time_limit(*_):
            raise TimeoutError("fixed 35 second Base comparison deadline")

        signal.signal(signal.SIGALRM, time_limit)
        signal.alarm(35)
        session.start()
    else:
        check = json.loads(raw_stop.read_bytes())
        assert check["status"] == "complete"
        observations = check["observations"]
        timing = json.loads((folder / "timings.json").read_bytes())

    try:
        with (
            httpx.Client(timeout=4, trust_env=False, follow_redirects=False) as client,
            ThreadPoolExecutor(max_workers=3) as workers,
        ):
            rpc = Exchange(folder, stopped, client if capture else None)
            specs = address_roster(frozen["roster"])
            for i in range(MAX_HEADS):
                if capture:
                    frame = session.next_head()
                    observations.append(frame.to_wire())
                else:
                    frame = replay_head_observation(folder / "heads", observations[i])
                n = str(frame.head.height)
                if i == PINS:
                    break
                began = time.monotonic_ns()
                proof, clocks, cpu, cache = state_head(
                    frame, specs, rpc, cache, workers, bridge, incremental_first=i == 2
                )
                if capture:
                    timing[n] = {"mono": dict(clocks, state_started_ns=began), "thread_cpu_ns": cpu}
                rows.append(proof)
        if session is not None:
            session.check()
    except Exception as exc:
        stopped.set()
        status = "incomplete"
        if capture:
            durable_new(
                folder / "failure.json",
                canonical_json({"type": type(exc).__name__, "reason": str(exc)}),
            )
        else:
            raise
    finally:
        if session is not None:
            session.close()
            signal.alarm(0)
        if capture:
            durable_new(folder / "timings.json", canonical_json(timing))
            durable_new(
                raw_stop,
                canonical_json(
                    {
                        "status": status,
                        "observations": observations,
                        "utc": datetime.now(UTC).isoformat(),
                    }
                ),
            )
    with RpcBudget(folder / "original.sqlite3", POLICIES, read_only=True) as budget:
        counts = budget.counts()
    for i, row in enumerate(rows):
        obs = row["observation"]
        child = observations[i + 1] if i + 1 < len(observations) else None
        row["received_to_child_ms"] = (
            None
            if child is None
            else (child["received_monotonic_ns"] - obs["received_monotonic_ns"]) / TIMING_NS
        )
        block = str(obs["head"]["height"])
        clocks = timing[block]["mono"]
        row["thread_cpu_ns"] = timing[block]["thread_cpu_ns"]
        x = {
            name[len(block) + 1 :]: {
                "start": start,
                "reply": reply,
                "elapsed_ms": (reply - start) / TIMING_NS,
            }
            for name, (start, reply) in rpc.clocks.items()
            if name.startswith(block + "-")
        }
        row["rpc_clocks"] = x
        assert x["full"]["reply"] <= clocks["full_decoded_ns"]
        assert x["v4"]["reply"] <= clocks["official_decoded_ns"]
        if "delta" in x:
            assert x["delta"]["reply"] <= clocks["incremental_decoded_ns"]
        assert (
            obs["received_monotonic_ns"] <= clocks["state_started_ns"] <= clocks["state_parity_ns"]
        )
        row["receive_to_ms"] = {
            name: (value - obs["received_monotonic_ns"]) / TIMING_NS
            for name, value in clocks.items()
        }
        row["receive_to_ms"]["full_ready_ns"] = max(
            row["receive_to_ms"]["full_decoded_ns"], row["receive_to_ms"]["official_decoded_ns"]
        )
        if "incremental_decoded_ns" in clocks:
            row["receive_to_ms"]["incremental_ready_ns"] = max(
                row["receive_to_ms"]["incremental_decoded_ns"],
                row["receive_to_ms"]["official_decoded_ns"],
            )
    report = {
        "schema": "oasis-base-bloom-state-ab-result-1",
        "status": status,
        "http_attempts": counts,
        "rows": rows,
        "scope_sha256": digest((folder / "scope.json").read_bytes()),
        "observed": len(observations),
        "ws_primitives": 3 if observations else None,
    }
    output = canonical_json(report) + b"\n"
    if capture:
        durable_new(folder / "result.json", output)
    print(output.decode(), end="")
    raise SystemExit(0 if status == "complete" else 2)


if __name__ == "__main__":
    main()
