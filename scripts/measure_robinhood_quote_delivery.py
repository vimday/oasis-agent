"""Capture or replay two Robinhood feed blocks and their fixed V4 quote probes."""

import argparse
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
from eth_hash.auto import keccak

from oasis_agent.arbitrage.cycle_evidence import read_bounded
from oasis_agent.arbitrage.cycle_plan import hash32
from oasis_agent.arbitrage.cycle_protocols import data_bytes
from oasis_agent.evidence.robinhood_nitro import MAX_RPC_BYTES, OFFICIAL_RPC, FeedMessage
from oasis_agent.evidence.robinhood_quote import (
    CHAIN_ID,
    MANAGER,
    QUOTER,
    STATE_VIEW,
    NativeRoute,
    assert_feed_header,
    parse_routes,
    quote_multicall,
    read_quotes,
    route_capacity_diagnostics,
)
from oasis_agent.evidence.rpc_validation import strict_json_object, strict_object
from oasis_agent.fastpath.multicall_universe import MULTICALL3
from oasis_agent.models.common import canonical_json, sha256_hex
from oasis_agent.rpc.budget import BudgetPolicy, RpcBudget
from oasis_agent.rpc.protocol import response_envelope
from scripts.capture_public_rpc import durable_new
from scripts.capture_robinhood_nitro_feed import HOST, capture, decode

SCHEMA = "oasis-robinhood-quote-delivery-v1"
SOURCES = {"official": OFFICIAL_RPC, "publicnode": "https://robinhood-rpc.publicnode.com"}
FRAME_CAP = 4
HEADS = 2
CODE_ADDRESSES = (MANAGER, STATE_VIEW, QUOTER, MULTICALL3)
POLICY = tuple(BudgetPolicy(source, HEADS * (2 + len(CODE_ADDRESSES)), 250) for source in SOURCES)
MAX_ROUTES_BYTES = 128 * 1024
TIMEOUT_S = 4
TRANSPORT_GAS = 20_000_000
CONFIGS = Path(__file__).resolve().parents[1] / "configs"


def _runtime_pins() -> dict[str, str]:
    observer = strict_json_object(
        read_bounded(CONFIGS / "observer.robinhood-stockhooks.example.json", 16384)
    )
    route = strict_json_object(
        read_bounded(CONFIGS / "route-probe.robinhood-stockhooks-eth.example.json", 16384)
    )
    hashes = strict_object(route["expected_hashes"], "Robinhood route code pins")
    if (
        observer["chain_id"] != route["chain_id"]
        or route["chain_id"] != CHAIN_ID
        or observer["pool_manager"] != route["pool_manager"]
        or route["pool_manager"] != MANAGER
        or observer["state_view"] != STATE_VIEW
        or route["quoter"] != QUOTER
        or observer["pool_manager_runtime_keccak"] != hashes["pool_manager"]
    ):
        raise ValueError("Robinhood observer and route contract identities disagree")
    return {
        MANAGER: hash32(hashes["pool_manager"])[2:],
        STATE_VIEW: hash32(observer["state_view_runtime_keccak"])[2:],
        QUOTER: hash32(hashes["quoter"])[2:],
    }


def _plan(route_raw: bytes) -> bytes:
    return canonical_json(
        {
            "schema": SCHEMA,
            "fee_cap_usd": 0,
            "sources": SOURCES,
            "socket": f"wss://{HOST}",
            "socket_connections": 1,
            "max_frames": FRAME_CAP,
            "max_heads": HEADS,
            "max_requests": {p.source: p.max_requests for p in POLICY},
            "pacing_ms": POLICY[0].min_interval_ms,
            "timeout_s": TIMEOUT_S,
            "routes_sha256": sha256_hex(route_raw),
            "call_gas_cap": TRANSPORT_GAS,
            "execution_eligible": False,
        }
    )


def _headers(root: Path, sources: Path) -> list[tuple[FeedMessage, int]]:
    feed = decode(root / "feed", sources)
    scope = strict_json_object(read_bounded(root / "feed" / "scope.json", 16384))
    stop = strict_json_object(read_bounded(root / "feed" / "STOP", 2048))
    if scope["max_frames"] != FRAME_CAP or stop != {
        "reason": "frame_cap",
        "attempts": 1,
        "requests_to_rpc": 0,
    }:
        raise ValueError("unexpected Robinhood feed coverage")
    heads = []
    for row in feed["messages"]:
        msg = FeedMessage.from_row(row)
        if msg.block_hash is None:
            if heads:
                raise ValueError("direct child feed hint lacks block hash")
            continue
        if heads and msg.sequence != heads[-1][0].sequence + 1:
            raise ValueError("feed block sequence is not contiguous")
        heads.append((msg, row["received"]["monotonic_ns"]))
        if len(heads) == HEADS:
            return heads
    raise ValueError("not enough consecutive hash-bearing feed blocks")


def _body(number: int, method: str, params: list[object]) -> bytes:
    return canonical_json({"jsonrpc": "2.0", "id": number, "method": method, "params": params})


def _fetch(client: httpx.Client, endpoint: str, prefix: Path, body: bytes) -> None:
    durable_new(prefix.with_suffix(".request"), body)
    start = time.monotonic_ns()
    with client.stream(
        "POST", endpoint, content=body, headers={"Content-Type": "application/json"}
    ) as reply:
        raw = bytearray()
        for chunk in reply.iter_bytes(chunk_size=8192):
            if (
                len(raw) + len(chunk) > MAX_RPC_BYTES
                or time.monotonic_ns() - start > TIMEOUT_S * 10**9
            ):
                durable_new(prefix.with_suffix(".partial"), bytes(raw))
                raise ValueError("RPC response limit reached")
            raw.extend(chunk)
        received = time.monotonic_ns()
        durable_new(prefix.with_suffix(".response"), bytes(raw))
        durable_new(
            prefix.with_suffix(".meta"),
            canonical_json(
                {
                    "status": reply.status_code,
                    "sent_ns": start,
                    "received_ns": received,
                    "retrieved_unix_ns": time.time_ns(),
                    "request_sha256": sha256_hex(body),
                    "response_sha256": sha256_hex(bytes(raw)),
                }
            ),
        )


def _read(prefix: Path, body: bytes, number: int, earliest_ns: int) -> tuple[object, int]:
    request = read_bounded(prefix.with_suffix(".request"), MAX_RPC_BYTES)
    response = read_bounded(prefix.with_suffix(".response"), MAX_RPC_BYTES)
    meta = strict_json_object(read_bounded(prefix.with_suffix(".meta"), 2048))
    start, received = meta["sent_ns"], meta["received_ns"]
    if (
        request != body
        or meta["request_sha256"] != sha256_hex(request)
        or meta["response_sha256"] != sha256_hex(response)
        or type(start) is not int
        or type(received) is not int
        or not 0 < earliest_ns <= start <= received
        or type(meta["retrieved_unix_ns"]) is not int
        or meta["retrieved_unix_ns"] <= 0
    ):
        raise ValueError("request, response or receive clock integrity failure")
    if meta["status"] != 200:
        raise ValueError(f"HTTP refusal {meta['status']}")
    env = response_envelope(response, number)
    if "error" in env:
        raise ValueError(f"RPC refusal {env['error']['code']}")
    return env["result"], received


def _run(
    root: Path, sources: Path, routes: tuple[NativeRoute, ...], budget: RpcBudget, client=None
):
    number = 0
    prior_response_ns = 0
    mode = "capture" if client is not None else "replay"
    heads = _headers(root, sources)
    runtime = _runtime_pins()
    previous_hash = None
    rows = []
    quoted_call = {"to": MULTICALL3, "data": quote_multicall(routes), "gas": hex(TRANSPORT_GAS)}
    with ThreadPoolExecutor(max_workers=2) as executor:

        def step(frame_ns, method, params):
            nonlocal number, prior_response_ns
            number += 1
            body = _body(number, method, params)
            prefixes = {source: root / f"{number:02}-{source}" for source in SOURCES}
            if mode == "capture":
                for source in SOURCES:
                    wait_ms = budget.reserve(source, now_ms=time.time_ns() // 1_000_000)
                    if wait_ms:
                        time.sleep((wait_ms + 1) / 1000)
                        if budget.reserve(source, now_ms=time.time_ns() // 1_000_000):
                            raise ValueError("RPC pacing reservation did not resolve")
                jobs = [
                    executor.submit(_fetch, client, endpoint, prefixes[source], body)
                    for source, endpoint in SOURCES.items()
                ]
                for job in jobs:
                    job.result()
            pair = tuple(
                _read(prefix, body, number, max(frame_ns, prior_response_ns))
                for prefix in prefixes.values()
            )
            prior_response_ns = max(ns for _, ns in pair)
            return pair

        for message, frame_ns in heads:
            block = {"blockHash": message.block_hash, "requireCanonical": True}
            pair = step(frame_ns, "eth_getBlockByNumber", [hex(message.sequence), False])
            pins = [assert_feed_header(raw, message) for raw, _ in pair]
            if pins[0] != pins[1]:
                raise ValueError("independent block commitments disagree")
            header, base_fee = pins[0]
            if previous_hash is not None and header.parent_hash != previous_hash:
                raise ValueError("second state is not the direct canonical child")
            previous_hash = header.ref.block_hash
            header_ns = max(ns for _, ns in pair)
            codes = {}
            for address in CODE_ADDRESSES:
                responses = step(frame_ns, "eth_getCode", [address, block])
                left, right = (data_bytes(raw) for raw, _ in responses)
                if not left or left != right:
                    raise ValueError("independent runtime bytecode disagrees")
                digest = keccak(left).hex()
                if address in runtime and runtime[address] != digest:
                    raise ValueError("frozen runtime bytecode changed")
                codes[address] = digest
            if rows and codes != rows[-1]["runtime_keccak"]:
                raise ValueError("runtime bytecode changed across quote states")
            pair = step(frame_ns, "eth_call", [quoted_call, block])
            results = [read_quotes(raw, routes, base_fee) for raw, _ in pair]
            if results[0] != results[1]:
                raise ValueError("independent finite quotes disagree")
            rows.append(
                {
                    "number": header.ref.number,
                    "hash": previous_hash,
                    "parent_hash": header.parent_hash,
                    "state_root": header.state_root,
                    "base_fee_per_gas_wei": base_fee,
                    "runtime_keccak": codes,
                    "frame_receive_ns": frame_ns,
                    "frame_to_dual_header_ns": header_ns - frame_ns,
                    "frame_to_recorded_quote_ns": max(ns for _, ns in pair) - frame_ns,
                    "request_ids": list(range(number - len(CODE_ADDRESSES) - 1, number + 1)),
                    **results[0],
                }
            )
    if budget.counts() != {p.source: p.max_requests for p in POLICY}:
        raise ValueError("original ledger disagrees with observed quote scope")
    return {"schema": SCHEMA, "status": "complete", "states": rows, "execution_eligible": False}


def run(
    root: Path, sources: Path, route_path: Path | None, *, capacity_diagnostics: bool = False
) -> dict[str, Any]:
    if capacity_diagnostics and route_path is not None:
        raise ValueError("capacity diagnostics require a completed retained capture")
    if route_path is None:
        if strict_json_object(read_bounded(root / "STOP", 2048)) != {"status": "complete"}:
            raise ValueError("incomplete original scope")
        route_raw = read_bounded(root / "routes.json", MAX_ROUTES_BYTES)
        if read_bounded(root / "plan.json", 16384) != _plan(route_raw):
            raise ValueError("retained quote plan changed")
        routes = parse_routes(strict_json_object(route_raw))
        with RpcBudget(root / "original.sqlite3", POLICY, read_only=True) as budget:
            result = _run(root, sources, routes, budget)
        if read_bounded(root / "report.json", MAX_RPC_BYTES) != canonical_json(result):
            raise ValueError("quote report differs from retained evidence")
        if capacity_diagnostics:
            result["route_capacity_diagnostics"] = [
                {
                    "block_hash": state["hash"],
                    "routes": route_capacity_diagnostics(routes, state["pools"], state["quotes"]),
                }
                for state in result["states"]
            ]
        return result

    route_raw = read_bounded(route_path, MAX_ROUTES_BYTES)
    routes = parse_routes(strict_json_object(route_raw))
    root.mkdir()
    durable_new(root / "routes.json", route_raw)
    durable_new(root / "plan.json", _plan(route_raw))
    feed = root / "feed"
    feed.mkdir()
    try:
        flight = capture(feed, sources, max_frames=FRAME_CAP)
        if flight != "frame_cap":
            raise ValueError("bounded Robinhood WebSocket scope stopped before frame cap")
        with (
            RpcBudget(root / "original.sqlite3", POLICY) as budget,
            httpx.Client(timeout=TIMEOUT_S, follow_redirects=False, trust_env=False) as client,
        ):
            result = _run(root, sources, routes, budget, client)
        durable_new(root / "report.json", canonical_json(result))
    except (httpx.HTTPError, ValueError, OSError, TypeError, KeyError) as exc:
        reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        durable_new(root / "STOP", canonical_json({"status": "incomplete", "reason": reason}))
        raise
    durable_new(root / "STOP", canonical_json({"status": "complete"}))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out", type=Path)
    parser.add_argument("--sources-dir", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--capture-routes", type=Path, help="one new public live scope")
    mode.add_argument(
        "--capacity-diagnostics",
        action="store_true",
        help="show directional grid and liquidity reverts from the retained replay only",
    )
    args = parser.parse_args()
    report = run(
        args.out,
        args.sources_dir,
        args.capture_routes,
        capacity_diagnostics=args.capacity_diagnostics,
    )
    print(canonical_json(report).decode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
