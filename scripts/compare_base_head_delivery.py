"""Capture or replay one bounded Base newHeads versus polling window."""

import argparse
import asyncio
import re
import sqlite3
import ssl
import sys
import time
from dataclasses import asdict
from pathlib import Path

import certifi
import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus, WebSocketException

from oasis_agent.arbitrage.cycle_evidence import read_bounded
from oasis_agent.arbitrage.event_quote_capture import Journal
from oasis_agent.evidence.base_denim import BASE_CHAIN_ID
from oasis_agent.evidence.base_head_compare import (
    NS_MS,
    POLL_ARM,
    WS_ARM,
    BaseHead,
    HeadPlan,
    HeadWindow,
    decode_head,
    decode_notification,
)
from oasis_agent.evidence.rpc_validation import rpc_quantity, strict_integer, strict_string
from oasis_agent.models.common import canonical_json, sha256_hex
from oasis_agent.rpc.budget import BudgetPolicy, RpcBudget
from oasis_agent.rpc.protocol import response_envelope, strict_json_object
from scripts.capture_public_rpc import durable_new

SCHEMA = "oasis-base-head-comparison-v1"
PUBLIC = "https://base-rpc.publicnode.com"
WEBSOCKET = "wss://base-rpc.publicnode.com"
OFFICIAL = "https://mainnet.base.org"
TIMEOUT_S = 4
SCOPE_TIMEOUT_S = 45
FRAME_MAX = 128
BODY_MAX = 128 * 1024
WIRE_MAX = 24 * 1024 * 1024


def _policies(plan: HeadPlan) -> tuple[BudgetPolicy, ...]:
    return (
        BudgetPolicy(WS_ARM, 3, 0),
        BudgetPolicy(POLL_ARM, plan.poll_slots + 1, 0),
        BudgetPolicy("witness", plan.witness_cap + 1, 250),
    )


def _plan_document(plan: HeadPlan, mode: str) -> dict[str, object]:
    return {
        "schema": SCHEMA,
        "chain_id": BASE_CHAIN_ID,
        "mode": mode,
        "execution_eligible": False,
        "sources": {WS_ARM: WEBSOCKET, POLL_ARM: PUBLIC, "witness": OFFICIAL},
        "settings": asdict(plan),
        "limits": [asdict(policy) for policy in _policies(plan)],
        "websocket_messages_max": FRAME_MAX,
        "timeout_s": TIMEOUT_S,
        "scope_timeout_s": SCOPE_TIMEOUT_S,
        "max_cost_usd": 0,
    }


def _rpc_result(raw: bytes, request_id: int) -> object:
    doc = response_envelope(raw, request_id)
    if "error" in doc:
        raise ValueError("json_rpc_error")
    return doc["result"]


def _chain(raw: bytes, request_id: int) -> None:
    if rpc_quantity(_rpc_result(raw, request_id)) != BASE_CHAIN_ID:
        raise ValueError("chain_id_mismatch")


def _failure_code(exc: Exception) -> str:
    detail = str(exc)
    return detail if re.fullmatch(r"[a-z0-9_]+", detail) else type(exc).__name__


class Capture:
    def __init__(self, journal: Journal, budget: RpcBudget, http: httpx.AsyncClient) -> None:
        self.journal, self.budget, self.http = journal, budget, http
        self.request_id = 0
        self.failed = False
        self.window = HeadWindow()

    def stop(self, lane: str, exc: Exception) -> None:
        if not self.failed:
            self.failed = True
            self.journal.put(
                "local",
                "failure",
                canonical_json(
                    {
                        "lane": lane,
                        "reason": _failure_code(exc),
                    }
                ),
            )

    async def reserve(self, lane: str) -> None:
        if self.failed:
            raise ValueError("scope_already_failed")
        wait = self.budget.reserve(lane, now_ms=time.time_ns() // NS_MS)
        if wait:
            await asyncio.sleep(wait / 1_000)
            if self.failed:
                raise ValueError("scope_already_failed")
            if self.budget.reserve(lane, now_ms=time.time_ns() // NS_MS):
                raise ValueError("source_pacing_unresolved")

    async def request(self, lane: str, method: str, params: list[object]) -> tuple[int, bytes]:
        await self.reserve(lane)
        self.request_id += 1
        raw = canonical_json(
            {
                "jsonrpc": "2.0",
                "id": self.request_id,
                "method": method,
                "params": params,
            }
        )
        self.journal.put(lane, "request", raw)
        return self.request_id, raw

    async def rpc(
        self,
        lane: str,
        method: str,
        params: list[object],
    ) -> tuple[int, bytes, int]:
        request_id, raw = await self.request(lane, method, params)
        url = OFFICIAL if lane == "witness" else PUBLIC
        async with (
            asyncio.timeout(TIMEOUT_S),
            self.http.stream(
                "POST",
                url,
                content=raw,
                headers={"Content-Type": "application/json"},
            ) as response,
        ):
            body = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=8_192):
                if len(body) + len(chunk) > BODY_MAX:
                    self.journal.put(lane, f"truncated-{request_id}", bytes(body))
                    raise ValueError("response_byte_limit")
                body.extend(chunk)
            received = time.monotonic_ns()
            payload = bytes(body)
            self.journal.put(
                lane, f"response-{request_id}-{response.status_code}", payload, received
            )
            if response.status_code != 200:
                raise ValueError(f"http_{response.status_code}")
        return request_id, payload, received

    async def setup_ws(self, websocket) -> str:
        for method, params in (("eth_chainId", []), ("eth_subscribe", ["newHeads"])):
            request_id, raw = await self.request(WS_ARM, method, params)
            async with asyncio.timeout(TIMEOUT_S):
                await websocket.send(raw, text=True)
                ack = await websocket.recv(decode=False)
                self.journal.put(WS_ARM, f"ack-{request_id}", ack)
            if method == "eth_chainId":
                _chain(ack, request_id)
        subscription = strict_string(_rpc_result(ack, request_id), "subscription id")
        if not 1 <= len(subscription) <= 128:
            raise ValueError("invalid_subscription_id")
        return subscription

    async def stream(self, websocket, subscription: str, deadline: int) -> None:
        try:
            for _ in range(FRAME_MAX):
                remaining = deadline - time.monotonic_ns()
                if remaining <= 0:
                    return
                try:
                    async with asyncio.timeout(remaining / 1e9):
                        frame = await websocket.recv(decode=False)
                except TimeoutError:
                    return
                received = time.monotonic_ns()
                self.journal.put(WS_ARM, "notification", frame, received)
                if received >= deadline:
                    return
                head = decode_notification(strict_json_object(frame), subscription)
                self.window.observe(WS_ARM, head, received)
            raise ValueError("websocket_frame_limit")
        except (OSError, ValueError, TypeError, WebSocketException) as exc:
            self.stop(WS_ARM, exc)
            raise

    async def poll(self, plan: HeadPlan, start: int) -> None:
        try:
            interval = plan.interval_ms * NS_MS
            for slot in range(plan.poll_slots):
                scheduled = start + interval * slot
                await asyncio.sleep(max(0, (scheduled - time.monotonic_ns()) / 1e9))
                if time.monotonic_ns() >= scheduled + interval:
                    continue
                request, raw, received = await self.rpc(
                    POLL_ARM,
                    "eth_getBlockByNumber",
                    ["latest", False],
                )
                head = decode_head(_rpc_result(raw, request))
                if received < start + plan.window_ns:
                    self.window.observe(POLL_ARM, head, received)
        except (TimeoutError, httpx.HTTPError, OSError, ValueError, TypeError) as exc:
            self.stop(POLL_ARM, exc)
            raise

    async def run(self, plan: HeadPlan, socket_factory) -> None:
        lane = POLL_ARM
        try:
            for lane in (POLL_ARM, "witness"):
                request, raw, _ = await self.rpc(lane, "eth_chainId", [])
                _chain(raw, request)
            lane = WS_ARM
            await self.reserve(WS_ARM)
            self.journal.put(WS_ARM, "open-request", b"")
            async with socket_factory() as websocket:
                subscription = await self.setup_ws(websocket)
                started = time.monotonic_ns()
                self.journal.put("local", "observation-open", b"", started)
                try:
                    async with asyncio.TaskGroup() as tasks:
                        tasks.create_task(
                            self.stream(websocket, subscription, started + plan.window_ns)
                        )
                        tasks.create_task(self.poll(plan, started))
                except* (
                    TimeoutError,
                    httpx.HTTPError,
                    OSError,
                    ValueError,
                    TypeError,
                    WebSocketException,
                ):
                    if not self.failed:
                        self.stop("observation", ValueError("observation_failed"))
            if self.failed:
                return
            self.journal.put("local", "observation-close", b"")
            lane = "witness"
            previous = None
            for height in self.window.span(plan.witness_cap):
                request, raw, _ = await self.rpc(lane, "eth_getBlockByNumber", [hex(height), False])
                head = decode_head(_rpc_result(raw, request))
                self.window.check_witness(head, height, previous)
                previous = head
        except InvalidStatus as exc:
            status = exc.response.status_code
            self.journal.put(lane, f"upgrade-{status}", bytes(exc.response.body)[:BODY_MAX])
            self.stop(lane, ValueError(f"ws_upgrade_http_{status}"))
        except (
            TimeoutError,
            httpx.HTTPError,
            OSError,
            ValueError,
            TypeError,
            WebSocketException,
        ) as exc:
            self.stop(lane, exc)


def _default_socket():
    return connect(
        WEBSOCKET,
        proxy=None,
        ssl=ssl.create_default_context(cafile=certifi.where()),
        open_timeout=TIMEOUT_S,
        close_timeout=2,
        max_size=BODY_MAX,
        max_queue=16,
        compression=None,
        ping_interval=None,
    )


def _row_data(
    root: Path, plan: HeadPlan
) -> tuple[list[tuple[str, str, int, bytes]], dict[str, int]]:
    counts = dict.fromkeys((policy.source for policy in _policies(plan)), 0)
    wire, ledger = (root / "wire.sqlite3", root / "original-budget.sqlite3")
    if any(path.is_symlink() or not path.is_file() for path in (wire, ledger)):
        raise ValueError("missing_or_symlinked_head_evidence")
    with RpcBudget(ledger, _policies(plan), read_only=True) as budget:
        used = budget.counts()
    rows = []
    total = 0
    clock_ns = 0
    max_rows = 2 * sum(used.values()) + FRAME_MAX + 4
    with sqlite3.connect(wire.resolve().as_uri() + "?mode=ro", uri=True) as db:
        cursor = db.execute(
            "SELECT seq,channel,role,wall_ns,mono_ns,sha256,payload FROM raw ORDER BY seq"
        )
        for index, (seq, lane, role, wall, ns, digest, raw) in enumerate(cursor, 1):
            total += len(raw)
            if (
                seq != index
                or index > max_rows
                or total > WIRE_MAX
                or len(raw) > BODY_MAX
                or wall <= 0
                or ns < clock_ns
                or sha256_hex(raw) != digest
                or lane not in {*counts, "local"}
            ):
                raise ValueError("invalid_head_wire_archive")
            clock_ns = ns
            if role in {"open-request", "request"} and lane != "local":
                counts[lane] += 1
            rows.append((lane, role, ns, raw))
    if counts != used:
        raise ValueError("head_wire_ledger_disagrees")
    return rows, used


def _decode_window(rows: list[tuple[str, str, int, bytes]], plan: HeadPlan) -> dict[str, object]:
    window = HeadWindow()
    pending = {}
    head_pins: dict[int, BaseHead] = {}
    poll_slots: set[int] = set()
    inspected_slots: set[int] = set()
    chain_checks = set()
    subscription = None
    opened = False
    started = closed = None
    next_id = 1
    for lane, role, observed, raw in rows:
        if role == "open-request":
            if opened or lane != WS_ARM or chain_checks != {POLL_ARM, "witness"} or raw:
                raise ValueError("invalid_websocket_open")
            opened = True
        elif role == "request":
            req = strict_json_object(raw)
            if (
                set(req) != {"jsonrpc", "id", "method", "params"}
                or req["jsonrpc"] != "2.0"
                or type(req["id"]) is not int
                or req["id"] != next_id
            ):
                raise ValueError("invalid_head_request_identity")
            method, params = req["method"], req["params"]
            if method == "eth_chainId" and params == []:
                if started is not None or lane in chain_checks or (lane == WS_ARM) != opened:
                    raise ValueError("invalid_chain_check_order")
            elif (lane, method, params) == (WS_ARM, "eth_subscribe", ["newHeads"]):
                if WS_ARM not in chain_checks or started is not None or subscription is not None:
                    raise ValueError("invalid_new_heads_subscription_request")
            elif method == "eth_getBlockByNumber" and started is not None:
                if lane == POLL_ARM and closed is None and params == ["latest", False]:
                    slot = (observed - started) // (plan.interval_ms * NS_MS)
                    if not 0 <= slot < plan.poll_slots or slot in poll_slots:
                        raise ValueError("poll_request_outside_frozen_schedule")
                    poll_slots.add(slot)
                elif lane == "witness" and closed is not None:
                    span = window.span(plan.witness_cap)
                    offset = len(head_pins)
                    if offset >= len(span) or params != [hex(span[offset]), False]:
                        raise ValueError("witness_request_not_fixed_height")
                else:
                    raise ValueError("head_request_wrong_arm")
            else:
                raise ValueError("unexpected_head_request")
            pending[next_id] = lane, method, params, observed
            next_id += 1
        elif match := re.fullmatch(r"(ack|response)-(\d+)(?:-(\d+))?", role):
            transport, raw_id, raw_status = match.groups()
            req_id = int(raw_id)
            if req_id not in pending:
                raise ValueError("head_response_without_request")
            req_lane, method, params, sent = pending.pop(req_id)
            if (
                req_lane != lane
                or observed < sent
                or (
                    (transport, raw_status)
                    != (("ack", None) if lane == WS_ARM else ("response", "200"))
                )
            ):
                raise ValueError("head_response_wrong_origin_or_status")
            result = _rpc_result(raw, req_id)
            if method == "eth_chainId":
                _chain(raw, req_id)
                chain_checks.add(lane)
            elif method == "eth_subscribe":
                subscription = strict_string(result, "subscription id")
                if not 1 <= len(subscription) <= 128:
                    raise ValueError("invalid_subscription_id")
            elif lane == POLL_ARM and started is not None:
                head = decode_head(result)
                inspected_slots.add((sent - started) // (plan.interval_ms * NS_MS))
                if observed < started + plan.window_ns:
                    window.observe(POLL_ARM, head, observed)
            elif lane == "witness":
                height = rpc_quantity(params[0])
                head = decode_head(result)
                previous = head_pins.get(height - 1)
                window.check_witness(head, height, previous)
                head_pins[height] = head
            else:
                raise ValueError("unclassified_head_response")
        elif lane == WS_ARM and role == "notification":
            if subscription is None or started is None or closed is not None:
                raise ValueError("new_heads_outside_capture")
            if observed < started + plan.window_ns:
                window.observe(
                    WS_ARM, decode_notification(strict_json_object(raw), subscription), observed
                )
        elif (lane, role) == ("local", "observation-open") and not raw:
            if (
                started is not None
                or chain_checks != {POLL_ARM, "witness", WS_ARM}
                or subscription is None
                or pending
            ):
                raise ValueError("invalid_head_observation_open")
            started = observed
        elif (lane, role) == ("local", "observation-close") and not raw:
            if (
                started is None
                or closed is not None
                or pending
                or observed < started + plan.window_ns
            ):
                raise ValueError("invalid_head_observation_close")
            closed = observed
        else:
            raise ValueError("unexpected_head_wire_record")
    if pending or started is None or closed is None:
        raise ValueError("head_observation_unresolved")
    if tuple(head_pins) != tuple(window.span(plan.witness_cap)):
        raise ValueError("head_witness_not_complete")
    report = window.report(plan, head_pins)
    report["poll_slots_expected"] = plan.poll_slots
    report["poll_slots_completed"] = sorted(inspected_slots)
    report["poll_slots_skipped"] = sorted(set(range(plan.poll_slots)) - poll_slots)
    return report


def _readback(root: Path) -> dict[str, object]:
    plan_raw = read_bounded(root / "plan.json", 8_192)
    doc = strict_json_object(plan_raw)
    settings = doc.get("settings")
    mode = doc.get("mode")
    if not isinstance(settings, dict) or mode not in {"public-shadow", "injected"}:
        raise ValueError("invalid_head_plan")
    plan = HeadPlan(
        strict_integer(settings.get("poll_slots"), "poll slots"),
        strict_integer(settings.get("interval_ms"), "poll cadence"),
        strict_integer(settings.get("witness_cap"), "witness cap"),
    )
    if doc != _plan_document(plan, mode):
        raise ValueError("head_plan_changed")
    rows, counts = _row_data(root, plan)
    failures = [
        (i, strict_json_object(raw))
        for i, (lane, role, _, raw) in enumerate(rows)
        if (lane, role) == ("local", "failure")
    ]
    if len(failures) > 1:
        raise ValueError("multiple_first_failures")
    if failures:
        first, failure = failures[0]
        if (
            set(failure) != {"lane", "reason"}
            or failure["lane"] not in {WS_ARM, POLL_ARM, "witness", "observation", "scope"}
            or not re.fullmatch(r"[A-Za-z0-9_]+", strict_string(failure["reason"], "failure code"))
            or any(role in {"request", "open-request"} for _, role, _, _ in rows[first + 1 :])
        ):
            raise ValueError("bad_failure_record")
        report: dict[str, object] = {"status": "incomplete", "failure": failure}
    else:
        report = _decode_window(rows, plan)
    return {
        "schema": SCHEMA,
        "execution_eligible": False,
        "mode": mode,
        "plan_sha256": sha256_hex(plan_raw),
        "raw_records": len(rows),
        "wire_sha256": sha256_hex(read_bounded(root / "wire.sqlite3", 2 * WIRE_MAX)),
        "rpc_reservations": counts,
        **report,
    }


async def capture(
    root: Path,
    plan: HeadPlan,
    *,
    http: httpx.AsyncClient | None = None,
    socket_factory=None,
) -> dict[str, object]:
    if (http is None) != (socket_factory is None):
        raise ValueError("both comparison transports are required for injection")
    await asyncio.to_thread(root.mkdir)
    mode = "public-shadow" if http is None else "injected"
    durable_new(root / "plan.json", canonical_json(_plan_document(plan, mode)))
    durable_new(root / "ACTIVE", b"")
    journal = Journal(root / "wire.sqlite3")
    try:
        with RpcBudget(root / "original-budget.sqlite3", _policies(plan)) as budget:

            async def schedule(client: httpx.AsyncClient) -> None:
                observer = Capture(journal, budget, client)
                try:
                    async with asyncio.timeout(SCOPE_TIMEOUT_S):
                        await observer.run(plan, socket_factory or _default_socket)
                except TimeoutError:
                    observer.stop("scope", ValueError("scope_timeout"))

            if http is None:
                async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
                    await schedule(client)
            else:
                await schedule(http)
    finally:
        journal.db.close()
    report = _readback(root)
    encoded = canonical_json(report)
    durable_new(root / "summary.json", encoded)
    if report["status"] == "incomplete":
        durable_new(root / "STOP", encoded)
    (root / "ACTIVE").unlink()
    return report


def replay(root: Path) -> dict[str, object]:
    if (root / "ACTIVE").exists():
        raise ValueError("head_comparison_still_active")
    actual = _readback(root)
    if canonical_json(actual) != read_bounded(root / "summary.json", 64 * 1024):
        raise ValueError("head_comparison_summary_changed")
    stop = root / "STOP"
    if actual["status"] == "incomplete":
        if canonical_json(actual) != read_bounded(stop, 64 * 1024):
            raise ValueError("head_comparison_stop_changed")
    elif stop.exists():
        raise ValueError("successful_head_comparison_has_stop")
    return actual


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("capture")
    run.add_argument("--out", type=Path, required=True)
    run.add_argument("--poll-slots", type=int, required=True)
    run.add_argument("--interval-ms", type=int, required=True)
    run.add_argument("--witness-cap", type=int, required=True)
    offline = commands.add_parser("replay")
    offline.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "capture":
            plan = HeadPlan(args.poll_slots, args.interval_ms, args.witness_cap)
            output = asyncio.run(capture(args.out, plan))
        else:
            output = replay(args.out)
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        print(f"Base head comparison failed: {exc}", file=sys.stderr)
        return 2
    print(canonical_json(output).decode())
    return 0 if output["status"] == "witnessed_common_window" else 2


if __name__ == "__main__":
    raise SystemExit(main())
