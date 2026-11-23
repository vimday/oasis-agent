"""Bounded anonymous capture for the prespecified event/clock quote comparison."""

import asyncio
import sqlite3
import ssl
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

import certifi
import httpx
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, WebSocketException

from ..evidence.public_receipts import quantity
from ..evidence.rpc_validation import strict_object, strict_string
from ..models.chain import BlockSnapshot, LogRecord
from ..models.common import canonical_json, sha256_hex
from ..rpc.budget import BudgetPolicy, RpcBudget
from ..rpc.protocol import response_envelope, strict_json_object
from .cycle_protocols import abi, calldata, data_bytes
from .event_quote import (
    BASE_AMOUNT,
    BYTES_MODEL,
    DEADLINE_NS,
    ELYSIUM_CODE,
    ELYSIUM_GETTERS,
    FACTORY,
    MAX_RANGE,
    MAX_WINDOW_BLOCKS,
    ORACLE,
    POLL_SECONDS,
    USDC,
    V3,
    V4,
    WETH,
    WINDOW_SECONDS,
    Chain,
    covered_logs,
    receipt_match,
    route_request,
    route_result,
    runtime_hash,
    same_opportunity,
    signaled_net,
    snap,
    swap,
)

CAPS = {"stream": 4, "event": 16, "poll": 16, "primary": 32, "witness": 24}
PLAN_SCHEMA = "oasis-event-quote-plan-v2"
BODY_BYTES = 512 * 1024
WS_BYTES = 128 * 1024
MAX_FRAMES = 1600
MAX_BYTES = 24 * 1024 * 1024
TRANSPORT_SECONDS = 4


class Refused(ValueError):
    """Source refused or failed a request, making the attempt one-shot."""


class Journal:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path)
        self.db.execute(
            "CREATE TABLE raw(seq INTEGER PRIMARY KEY, channel TEXT, role TEXT, "
            "wall_ns INTEGER, mono_ns INTEGER, sha256 TEXT, payload BLOB)"
        )
        self.total_bytes = 0

    def put(self, channel: str, role: str, raw: bytes, received_ns: int | None = None) -> None:
        self.total_bytes += len(raw)
        if self.total_bytes > MAX_BYTES:
            raise ValueError("raw_evidence_limit")
        with self.db:
            self.db.execute(
                "INSERT INTO raw VALUES(NULL,?,?,?,?,?,?)",
                (
                    channel,
                    role,
                    time.time_ns(),
                    received_ns or time.monotonic_ns(),
                    sha256_hex(raw),
                    raw,
                ),
            )


class Capture:
    def __init__(self, chain: Chain, budget: RpcBudget, raw: Journal, http: httpx.AsyncClient):
        self.chain = chain
        self.budget = budget
        self.raw = raw
        self.http = http
        self.request_id = 0
        self.stopped = False
        self.stream_error: str | None = None
        self.heads: dict[int, BlockSnapshot] = {}
        self.logs: dict[tuple[int, int], LogRecord] = {}
        self.events: dict[int, LogRecord] = {}
        self.log_blocks: dict[int, str] = {}
        self.rows: dict[str, list[dict[str, Any]]] = {"event": [], "poll": []}
        self.tasks: list[asyncio.Task[None]] = []
        self.quoter_hash: str | None = None

    def request(self, lane: str, method: str, params: list[object]) -> tuple[int, bytes]:
        if self.stopped:
            raise Refused("prior_rpc_failure")
        if self.budget.reserve(lane, now_ms=time.time_ns() // 1_000_000):
            raise Refused("budget_wait")
        self.request_id += 1
        raw = canonical_json(
            {"jsonrpc": "2.0", "id": self.request_id, "method": method, "params": params}
        )
        self.raw.put(lane, "request", raw)
        return self.request_id, raw

    async def rpc(self, lane: str, method: str, params: list[object]) -> object:
        request_id, raw = self.request(lane, method, params)
        host = self.chain.witness if lane == "witness" else self.chain.primary
        try:
            async with asyncio.timeout(TRANSPORT_SECONDS):
                async with self.http.stream(
                    "POST",
                    f"https://{host}",
                    content=raw,
                    headers={"Content-Type": "application/json"},
                ) as response:
                    body = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=8192):
                        if len(body) + len(chunk) > BODY_BYTES:
                            self.raw.put(lane, f"truncated-{request_id}", bytes(body))
                            raise Refused("response_byte_limit")
                        body.extend(chunk)
                    self.raw.put(lane, f"response-{request_id}-{response.status_code}", bytes(body))
                    if response.status_code != 200:
                        raise Refused(f"http_{response.status_code}")
            reply = response_envelope(bytes(body), request_id)
            if "error" in reply:
                error = strict_object(reply["error"], "RPC error")
                raise Refused(f"{method}_rpc_{error['code']}")
            return reply["result"]
        except (TimeoutError, httpx.HTTPError, ValueError, TypeError) as exc:
            self.stopped = True
            self.raw.put(lane, f"failure-{request_id}", type(exc).__name__.encode())
            raise Refused(str(exc) if isinstance(exc, Refused) else type(exc).__name__) from exc

    async def latest(self, lane: str) -> object:
        return await self.rpc(lane, "eth_getBlockByNumber", ["latest", False])

    async def at(self, lane: str, height: int) -> object:
        return await self.rpc(lane, "eth_getBlockByNumber", [hex(height), False])

    async def call(self, lane: str, block_hash: str, target: str, data: str) -> bytes:
        return data_bytes(
            await self.rpc(lane, "eth_call", [{"to": target, "data": data}, pin(block_hash)])
        )

    async def check_code(
        self, lane: str, block_hash: str, address: str, expected: str | None
    ) -> str:
        digest = runtime_hash(await self.rpc(lane, "eth_getCode", [address, pin(block_hash)]))
        if expected is not None and digest != expected:
            raise ValueError("startup_runtime_hash_mismatch")
        return digest

    async def preflight(self) -> BlockSnapshot:
        if quantity(await self.rpc("primary", "eth_chainId", []), 64) != self.chain.chain_id:
            raise ValueError("primary_chain_mismatch")
        anchor = snap(await self.latest("primary"), self.chain)
        if not self.chain.quote_enabled:
            return anchor
        if self.chain.chain_id == 99901:
            await self.preflight_elysium(anchor)
            return anchor
        h = anchor.ref.block_hash
        assert V3.quoter is not None and V4.quoter is not None
        for target, expected in (
            (V3.address, V3.runtime_hash),
            (FACTORY, V3.factory_hash),
            (V4.address, V4.runtime_hash),
            (V4.quoter, V4.quoter_hash),
            (ORACLE, None),
            (WETH, None),
            (USDC, None),
        ):
            await self.check_code("primary", h, target, expected)
        self.quoter_hash = await self.check_code("primary", h, V3.quoter, None)
        for target, data, abi_type, expected_value in (
            (V3.quoter, calldata("factory()"), "address", FACTORY),
            (V4.quoter, calldata("poolManager()"), "address", V4.address),
            (
                FACTORY,
                calldata(
                    "getPool(address,address,uint24)",
                    ("address", "address", "uint24"),
                    (WETH, USDC, 500),
                ),
                "address",
                V3.address,
            ),
            (WETH, calldata("decimals()"), "uint8", 18),
            (USDC, calldata("decimals()"), "uint8", 6),
        ):
            if abi((abi_type,), await self.call("primary", h, target, data))[0] != expected_value:
                raise ValueError("startup_deployment_mismatch")
        return anchor

    async def preflight_elysium(self, anchor: BlockSnapshot) -> None:
        height = anchor.ref.number
        number = hex(height)
        if snap(await self.at("witness", height), self.chain) != anchor:
            raise ValueError("elysium_preflight_anchor_mismatch")
        for target, expected in ELYSIUM_CODE.items():
            raw = await self.rpc("primary", "eth_getCode", [target, number])
            if runtime_hash(raw) != expected:
                raise ValueError("elysium_runtime_hash_mismatch")
        for target, signature, kind, expected_value in ELYSIUM_GETTERS:
            raw = await self.rpc(
                "primary", "eth_call", [{"to": target, "data": calldata(signature)}, number]
            )
            if abi((kind,), data_bytes(raw))[0] != expected_value:
                raise ValueError("elysium_deployment_mismatch")
        if snap(await self.at("primary", height), self.chain) != anchor:
            raise ValueError("elysium_preflight_reorg")

    async def quote(self, lane: str, start: int, log: LogRecord | None = None) -> None:
        row: dict[str, Any] = {
            "start_ns": start,
            "dispatch_ns": time.monotonic_ns(),
            "receipt_verified": False,
            "net_model_raw": None,
            "on_time": False,
        }
        if log is not None:
            row.update(
                block=log.block_number,
                block_hash=log.block_hash,
                tx=log.transaction_hash,
                log_index=log.log_index,
            )
        self.rows[lane].append(row)
        anchor_task: asyncio.Task[object] | None = None
        fee_task: asyncio.Task[bytes] | None = None
        try:
            if log is None:
                block = await self.latest(lane)
                h = snap(block, self.chain)
                row.update(block=h.ref.number, block_hash=h.ref.block_hash)
            else:
                anchor_task = asyncio.create_task(
                    self.rpc(lane, "eth_getBlockByHash", [row["block_hash"], False])
                )
            block_hash: str = row["block_hash"]
            if self.chain.chain_id == 8453:
                fee_data = calldata("getL1FeeUpperBound(uint256)", ("uint256",), (BYTES_MODEL,))
                fee_task = asyncio.create_task(self.call(lane, block_hash, ORACLE, fee_data))
            amount = BASE_AMOUNT
            row["legs"] = []
            for leg in range(2 if self.chain.chain_id == 8453 else 3):
                target, data = route_request(self.chain, leg, amount)
                raw = (
                    await self.call(lane, block_hash, target, data)
                    if self.chain.chain_id == 8453
                    else data_bytes(
                        await self.rpc(
                            lane, "eth_call", [{"to": target, "data": data}, hex(row["block"])]
                        )
                    )
                )
                output, gas = route_result(self.chain, leg, raw)
                row["legs"].append({"input_raw": amount, "output_raw": output, "quoter_gas": gas})
                amount = output
            row["quote_done_ns"] = time.monotonic_ns()
            row["on_time"] = row["quote_done_ns"] - start <= DEADLINE_NS
            row["gross_raw"] = amount - BASE_AMOUNT
            if anchor_task is not None:
                block = await anchor_task
                h = snap(block, self.chain)
                if (h.ref.number, h.ref.block_hash) != (row["block"], block_hash):
                    raise ValueError("trigger_block_mismatch")
            base_fee = quantity(strict_object(block, "quote block").get("baseFeePerGas"))
            if fee_task is None:
                row["base_fee_raw"] = base_fee
                return
            l1_fee = abi(("uint256",), await fee_task)[0]
            net_model = signaled_net(amount, base_fee, l1_fee)
            fee_done = time.monotonic_ns()
            row.update(
                base_fee_raw=base_fee,
                l1_fee_raw=l1_fee,
                net_model_raw=net_model,
                fee_done_ns=fee_done,
                net_on_time=fee_done - start <= DEADLINE_NS,
            )
        except (ValueError, TypeError) as exc:
            self.stopped = True
            row["error"] = str(exc) if isinstance(exc, Refused) else type(exc).__name__
        finally:
            auxiliaries = [task for task in (anchor_task, fee_task) if task is not None]
            for task in auxiliaries:
                if not task.done():
                    task.cancel()
            if auxiliaries:
                await asyncio.gather(*auxiliaries, return_exceptions=True)
            row["finish_ns"] = time.monotonic_ns()

    async def poll(self, start: int) -> None:
        for offset in POLL_SECONDS:
            scheduled = start + offset * 10**9
            await asyncio.sleep(max(0, (scheduled - time.monotonic_ns()) / 10**9))
            if self.stopped:
                return
            await self.quote("poll", scheduled)

    def check_head(self, h: BlockSnapshot) -> None:
        height = h.ref.number
        existing = self.heads.get(height)
        parent = self.heads.get(height - 1)
        child = self.heads.get(height + 1)
        if (
            (existing is not None and existing != h)
            or (parent is not None and parent.ref.block_hash != h.parent_hash)
            or (child is not None and child.parent_hash != h.ref.block_hash)
            or self.log_blocks.get(height, h.ref.block_hash) != h.ref.block_hash
        ):
            raise ValueError("stream_head_reorg")
        self.heads[height] = h

    def on_log(self, raw: object, received: int, *, quote_allowed: bool) -> None:
        log = swap(raw, self.chain, stream=True)
        height = log.block_number
        assert log.block_hash is not None
        expected = self.heads.get(height)
        if (expected is not None and expected.ref.block_hash != log.block_hash) or (
            self.log_blocks.setdefault(height, log.block_hash) != log.block_hash
        ):
            raise ValueError("stream_log_reorg")
        key = (height, log.log_index)
        if key in self.logs and self.logs[key] != log:
            raise ValueError("stream_log_position_conflict")
        self.logs[key] = log
        if quote_allowed and height not in self.events and len(self.events) < len(POLL_SECONDS):
            self.events[height] = log
            if self.chain.quote_enabled and not self.stopped:
                self.tasks.append(asyncio.create_task(self.quote("event", received, log)))

    async def frame(self, websocket: ClientConnection) -> tuple[dict[str, object], int]:
        frame = await websocket.recv(decode=False)
        now = time.monotonic_ns()
        if not isinstance(frame, bytes):
            raise ValueError("unexpected_ws_frame")
        self.raw.put("stream", "message", frame, now)
        return strict_json_object(frame), now

    async def stream(self) -> tuple[int | None, int | None]:
        start = None
        if self.budget.reserve("stream", now_ms=time.time_ns() // 1_000_000):
            raise Refused("ws_budget_wait")
        try:
            async with connect(
                f"wss://{self.chain.primary}",
                proxy=None,
                ssl=ssl.create_default_context(cafile=certifi.where()),
                open_timeout=8,
                close_timeout=2,
                max_size=WS_BYTES,
                max_queue=32,
                compression=None,
            ) as websocket:
                pending: dict[int, str] = {}
                subscriptions: dict[str, str] = {}
                setup: tuple[tuple[str, list[object], str], ...] = (
                    ("eth_chainId", [], "chain"),
                    ("eth_subscribe", ["newHeads"], "head"),
                    ("eth_subscribe", ["logs", self.chain.log_filter], "log"),
                )
                for method, params, role in setup:
                    request_id, raw = self.request("stream", method, params)
                    pending[request_id] = role
                    await websocket.send(raw, text=True)
                async with asyncio.timeout(TRANSPORT_SECONDS):
                    for _ in range(len(pending)):
                        reply, _ = await self.frame(websocket)
                        ack_id = reply.get("id")
                        if type(ack_id) is not int or ack_id not in pending:
                            raise ValueError("unexpected_subscription_ack")
                        role = pending.pop(ack_id)
                        if reply.get("jsonrpc") != "2.0" or "error" in reply:
                            raise Refused("subscription_refused")
                        if role == "chain":
                            if quantity(reply.get("result"), 64) != self.chain.chain_id:
                                raise ValueError("stream_chain_mismatch")
                        else:
                            token = strict_string(reply.get("result"), "subscription ID")
                            if not token or token in subscriptions:
                                raise ValueError("duplicate_subscription_id")
                            subscriptions[token] = role
                start = time.monotonic_ns()
                if self.chain.quote_enabled:
                    self.tasks.append(asyncio.create_task(self.poll(start)))
                deadline = start + WINDOW_SECONDS * 10**9
                for _ in range(MAX_FRAMES):
                    try:
                        async with asyncio.timeout(
                            max(0, (deadline - time.monotonic_ns()) / 10**9)
                        ):
                            msg, received = await self.frame(websocket)
                    except TimeoutError:
                        break
                    if self.stopped or received > deadline:
                        break
                    if msg.get("jsonrpc") != "2.0" or msg.get("method") != "eth_subscription":
                        raise ValueError("invalid_subscription_message")
                    notification = strict_object(msg.get("params"), "subscription")
                    role = subscriptions[
                        strict_string(notification.get("subscription"), "subscription ID")
                    ]
                    if role == "head":
                        self.check_head(snap(notification.get("result"), self.chain))
                    else:
                        self.on_log(
                            notification.get("result"), received, quote_allowed=received <= deadline
                        )
                else:
                    raise ValueError("ws_frame_limit")
        except ConnectionClosed:
            if start is None:
                self.stopped = True
            self.stream_error = "disconnect"
        except (TimeoutError, WebSocketException, OSError, ValueError, TypeError, KeyError) as exc:
            self.stopped = True
            self.stream_error = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        return start, time.monotonic_ns() if start is not None else None

    async def reconcile(self, start: BlockSnapshot, opening: BlockSnapshot) -> dict[str, Any]:
        if quantity(await self.rpc("witness", "eth_chainId", []), 64) != self.chain.chain_id:
            raise ValueError("witness_chain_mismatch")
        if self.chain.quote_enabled and self.chain.chain_id == 8453:
            assert V3.quoter is not None and self.quoter_hash is not None
            await self.check_code("witness", start.ref.block_hash, V3.quoter, self.quoter_hash)
        end = snap(await self.latest("primary"), self.chain)
        low, high = opening.ref.number, end.ref.number
        if not 0 <= high - low < MAX_WINDOW_BLOCKS:
            raise ValueError("unbounded_backfill")
        closing_witness = snap(await self.at("witness", high), self.chain)
        if opening != snap(await self.at("witness", low), self.chain) or end != closing_witness:
            raise ValueError("independent_window_anchor_mismatch")
        canonical: dict[tuple[int, int], LogRecord] = {}
        for first in range(low, high + 1, MAX_RANGE):
            last = min(high, first + MAX_RANGE - 1)
            params = {**self.chain.log_filter, "fromBlock": hex(first), "toBlock": hex(last)}
            data = await self.rpc("primary", "eth_getLogs", [params])
            canonical.update(covered_logs(data, self.chain, first, last))
        if end != snap(await self.at("primary", high), self.chain):
            raise ValueError("closing_block_reorg")
        for key, log in self.logs.items():
            if canonical.get(key) != log:
                raise ValueError("stream_log_missing_or_changed_in_backfill")
        if not self.heads:
            raise ValueError("no_stream_heads")
        heights = self.heads.keys() | self.log_blocks.keys()
        first_stream_height, last_stream_height = min(heights), max(heights)
        missing = [log for key, log in canonical.items() if key not in self.logs]
        gaps = [
            log for log in missing if first_stream_height < log.block_number < last_stream_height
        ]
        for log in self.events.values():
            block = await self.at("witness", log.block_number)
            receipt = await self.rpc("witness", "eth_getTransactionReceipt", [log.transaction_hash])
            receipt_match(self.chain, log, block, receipt)
            for row in self.rows["event"]:
                if (row["block"], row["block_hash"], row["tx"], row["log_index"]) == (
                    log.block_number,
                    log.block_hash,
                    log.transaction_hash,
                    log.log_index,
                ):
                    row["receipt_verified"] = True
        for row in self.rows["poll"]:
            if "block" in row:
                anchor = snap(await self.at("witness", row["block"]), self.chain)
                if anchor.ref.block_hash != row["block_hash"]:
                    raise ValueError("poll_block_reorg")
                row["canonical_verified"] = True
        if end != snap(await self.at("witness", high), self.chain):
            raise ValueError("closing_witness_reorg")
        return {
            "first_height": low,
            "last_height": high,
            "canonical_logs": len(canonical),
            "backfilled": [asdict(log) for log in missing],
            "internal_missing": len(gaps),
            "sampled_receipts": len(self.events),
        }


def pin(block_hash: str) -> dict[str, object]:
    return {"blockHash": block_hash, "requireCanonical": True}


def run(chain: Chain, root: Path, plan: dict[str, Any]) -> dict[str, Any]:
    root.mkdir()
    (root / "plan.json").write_bytes(canonical_json(plan))
    (root / "ACTIVE").touch(exist_ok=False)
    return asyncio.run(capture_run(chain, root))


async def capture_run(chain: Chain, root: Path) -> dict[str, Any]:
    raw = Journal(root / "wire.sqlite3")
    reconciliation = None
    failure = None
    start = end = None
    try:
        with RpcBudget(
            root / "original-budget.sqlite3",
            tuple(BudgetPolicy(lane, cap, 0) for lane, cap in CAPS.items()),
        ) as budget:
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as http:
                capture = Capture(chain, budget, raw, http)
                try:
                    anchor = await capture.preflight()
                    opening = snap(await capture.latest("primary"), chain)
                    start, end = await capture.stream()
                    if capture.stream_error or capture.stopped:
                        # Cancel scheduling and preserve partial evidence from in-flight RPCs.
                        for task in capture.tasks:
                            task.cancel()
                        await asyncio.gather(*capture.tasks, return_exceptions=True)
                    else:
                        await asyncio.gather(*capture.tasks)
                    if capture.stopped:
                        raise Refused("capture_stopped")
                    if start is None:
                        raise ValueError("no_stream_window")
                    reconciliation = await capture.reconcile(anchor, opening)
                except (ValueError, TypeError, OSError) as exc:
                    failure = str(exc)
            summary = summarize(capture, budget.counts(), start, end, reconciliation, failure)
        raw.put("local", "summary", canonical_json(summary))
        (root / "summary.json").write_bytes(canonical_json(summary))
        if summary["decision"] == "incomplete":
            (root / "STOP").write_bytes(canonical_json(summary))
        (root / "ACTIVE").unlink()
        return summary
    finally:
        raw.db.close()


def summarize(
    capture: Capture,
    used: dict[str, int],
    start: int | None,
    end: int | None,
    reconciliation: dict[str, Any] | None,
    failure: str | None,
) -> dict[str, Any]:
    events = capture.rows["event"]
    polls = capture.rows["poll"]
    joined = []
    for event in events:
        matching = next(
            (i for i, p in enumerate(polls) if "block" in p and same_opportunity(event, p)), None
        )
        next_poll = next(
            (
                i
                for i, p in enumerate(polls)
                if "block" in p
                and p["block"] >= event["block"]
                and p["dispatch_ns"] >= event["start_ns"]
            ),
            None,
        )
        joined.append(
            {"block": event["block"], "poll_within_deadline": matching, "next_poll": next_poll}
        )
    complete = (
        failure is None
        and capture.stream_error is None
        and reconciliation is not None
        and reconciliation["internal_missing"] == 0
    )
    return {
        "schema": "oasis-event-quote-result-v1",
        "chain_id": capture.chain.chain_id,
        "decision": quote_decision(capture.chain, events, complete),
        "start_ns": start,
        "end_ns": end,
        "failure": failure,
        "stream_error": capture.stream_error,
        "head_count": len(capture.heads),
        "stream_logs": [asdict(log) for log in capture.logs.values()],
        "evidence": reconciliation,
        "counts": used,
        "event": events,
        "poll": polls,
        "pairs": joined,
        "quote_v3_runtime_hash": capture.quoter_hash,
    }


def quote_decision(chain: Chain, events: list[dict[str, Any]], complete: bool) -> str:
    if not complete:
        return "incomplete"
    if not chain.quote_enabled:
        return "coverage_only"
    positive = [
        row
        for row in events
        if row["receipt_verified"] and row["net_model_raw"] is not None and row["net_model_raw"] > 0
    ]
    if any(row.get("net_on_time") is True for row in positive):
        return "model_positive_observed"
    if any(row.get("net_on_time") is False for row in positive):
        return "model_positive_after_deadline"
    if positive:
        return "incomplete"
    if len(events) == len(POLL_SECONDS) and all(
        row["receipt_verified"]
        and (row["net_model_raw"] is not None or row.get("gross_raw", 1) <= 0)
        for row in events
    ):
        return "reject_frozen_route"
    return "incomplete"
