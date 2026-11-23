"""Recompute exact-input amounts and timing from the anonymous experiment archive."""

import re
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ..evidence.public_receipts import quantity
from ..evidence.rpc_validation import strict_integer, strict_list, strict_object
from ..models.chain import LogRecord
from ..models.common import canonical_json, sha256_hex
from ..rpc.budget import BudgetPolicy, RpcBudget
from ..rpc.protocol import response_envelope, strict_json_object
from .cycle_evidence import read_bounded
from .cycle_protocols import abi, calldata, data_bytes
from .event_quote import (
    BASE_AMOUNT,
    BYTES_MODEL,
    CHAINS,
    DEADLINE_NS,
    ORACLE,
    POLL_SECONDS,
    Chain,
    covered_logs,
    receipt_match,
    route_request,
    route_result,
    same_opportunity,
    signaled_net,
    snap,
    swap,
)
from .event_quote_capture import CAPS, MAX_BYTES, MAX_FRAMES, PLAN_SCHEMA, pin, quote_decision


def replay(root: Path) -> dict[str, Any]:
    if (root / "ACTIVE").exists():
        raise ValueError("capture_still_active")
    plan = strict_json_object(read_bounded(root / "plan.json", 16_384))
    stored = read_bounded(root / "summary.json", MAX_BYTES)
    result = strict_json_object(stored)
    chain = next(
        (
            value
            for value in CHAINS.values()
            if canonical_json(asdict(value)) == canonical_json(plan.get("chain"))
        ),
        None,
    )
    schema = plan.get("schema")
    if (
        chain is None
        or schema not in ("oasis-event-quote-plan-v1", PLAN_SCHEMA)
        or plan.get("budget") != CAPS
        or chain.chain_id != result["chain_id"]
    ):
        raise ValueError("unknown_execution_plan")
    ledger = root / "original-budget.sqlite3"
    with RpcBudget(
        ledger,
        tuple(BudgetPolicy(lane, cap, 0) for lane, cap in CAPS.items()),
        read_only=True,
    ) as budget:
        counts = budget.counts()
        if counts != result["counts"]:
            raise ValueError("retained_rpc_budget_changed")
    requests: dict[int, tuple[str, int, dict[str, object]]] = {}
    responses: dict[int, tuple[int, object]] = {}
    events: dict[LogRecord, int] = {}
    stream_requests = 0
    wire_path = root / "wire.sqlite3"
    size = 0
    terminal = False
    with sqlite3.connect(wire_path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        rows = db.execute("SELECT seq,channel,role,mono_ns,sha256,payload FROM raw ORDER BY seq")
        for index, (seq, lane, role, ns, digest, raw) in enumerate(rows, 1):
            size += len(raw)
            if seq != index or index > MAX_FRAMES + 2 * sum(CAPS.values()) + 1 or size > MAX_BYTES:
                raise ValueError("wire_archive_bounds")
            if sha256_hex(raw) != digest or terminal:
                raise ValueError("wire_archive_has_changed")
            if role == "request":
                request = strict_json_object(raw)
                req_id = strict_integer(request.get("id"), "request id")
                if req_id != len(requests) + 1 or request.get("jsonrpc") != "2.0":
                    raise ValueError("wire_request_identity")
                requests[req_id] = (lane, ns, request)
                stream_requests += lane == "stream"
            elif match := re.fullmatch(r"response-(\d+)-200", role):
                req_id = int(match.group(1))
                if req_id in responses or requests[req_id][0] != lane:
                    raise ValueError("wire_response_identity")
                if ns < requests[req_id][1]:
                    raise ValueError("wire_response_precedes_request")
                rsp = response_envelope(raw, req_id)
                if "result" in rsp:
                    responses[req_id] = (ns, rsp["result"])
            elif lane == "stream" and role == "message":
                frame = strict_json_object(raw)
                if frame.get("method") == "eth_subscription":
                    entry = strict_object(frame.get("params"), "subscription").get("result")
                    if (
                        isinstance(entry, dict)
                        and "logIndex" in entry
                        and entry.get("removed") is False
                    ):
                        events.setdefault(swap(entry, chain, stream=True), ns)
            elif (lane, role) == ("local", "summary"):
                if raw != stored:
                    raise ValueError("terminal_summary_changed")
                terminal = True
    expected = {lane: sum(lane == row[0] for row in requests.values()) for lane in CAPS}
    expected["stream"] += bool(stream_requests or counts["stream"])
    if not terminal or expected != counts:
        raise ValueError("capture_does_not_match_ledger")
    result_rows = {
        name: [strict_object(item, name) for item in strict_list(result[name], name)]
        for name in ("event", "poll")
    }

    def included_reads(lane: str, method: str) -> list[tuple[int, int, list[object], object]]:
        return [
            (sent, responses[req_id][0], strict_list(req["params"], "params"), responses[req_id][1])
            for req_id, (channel, sent, req) in requests.items()
            if channel == lane and req.get("method") == method and req_id in responses
        ]

    canon: dict[tuple[int, int], LogRecord] = {}
    for _, _, params, body in included_reads("primary", "eth_getLogs"):
        filt = strict_object(params[0], "filter")
        if {key: filt.get(key) for key in chain.log_filter} != chain.log_filter:
            raise ValueError("archive_backfill_filter_changed")
        low, high = (quantity(filt.get(key), 64) for key in ("fromBlock", "toBlock"))
        batch = covered_logs(body, chain, low, high)
        if canon.keys() & batch.keys():
            raise ValueError("archive_backfill_overlaps")
        canon.update(batch)

    receipts = {
        params[0]: body
        for _, _, params, body in included_reads("witness", "eth_getTransactionReceipt")
    }
    blocks = {
        params[0]: body for _, _, params, body in included_reads("witness", "eth_getBlockByNumber")
    }
    verified_logs = []
    for log in events:
        if log.transaction_hash in receipts:
            receipt_match(chain, log, blocks[hex(log.block_number)], receipts[log.transaction_hash])
            if canon.get((log.block_number, log.log_index)) != log:
                raise ValueError("archive_receipt_missing_from_backfill")
            verified_logs.append(log)
    covered_backfill = [log for log in canon.values() if log not in events]
    evidence = None
    if result["evidence"] is not None:
        evidence = strict_object(result["evidence"], "coverage summary")
        if (
            len(canon) != evidence["canonical_logs"]
            or len(verified_logs) < strict_integer(evidence["sampled_receipts"], "receipt count")
            or len(covered_backfill) != len(strict_list(evidence["backfilled"], "backfill summary"))
        ):
            raise ValueError("archive_coverage_changed")

    verified_positions = {
        (log.block_number, log.block_hash, log.transaction_hash, log.log_index)
        for log in verified_logs
    }
    replayed: dict[str, list[dict[str, Any]]] = {"event": [], "poll": []}
    for lane, summary_rows in result_rows.items():
        calls = included_reads(lane, "eth_call")
        anchors = included_reads(
            lane, "eth_getBlockByHash" if lane == "event" else "eth_getBlockByNumber"
        )
        if len(summary_rows) > len(POLL_SECONDS):
            raise ValueError("frozen_quote_count_exceeded")
        for row in summary_rows:
            if schema == PLAN_SCHEMA and lane == "event":
                log_index = strict_integer(row.get("log_index"), "quote trigger log index")
                position = (row["block"], row["block_hash"], row["tx"], log_index)
                if row["receipt_verified"] is not (position in verified_positions):
                    raise ValueError("event_quote_receipt_proof_does_not_match")
            if "gross_raw" not in row or (chain.chain_id == 8453 and row["net_model_raw"] is None):
                continue
            if schema == PLAN_SCHEMA and chain.chain_id == 8453 and "net_on_time" not in row:
                raise ValueError("fee_inclusive_quote_timing_missing")
            dispatch = strict_integer(row.get("dispatch_ns"), "dispatch time")
            finish = strict_integer(row.get("finish_ns"), "finish time")
            matching_anchor = [
                (ns, block)
                for sent, ns, params, block in anchors
                if dispatch <= sent <= ns <= finish
                and params == ([row["block_hash"], False] if lane == "event" else ["latest", False])
                and snap(block, chain).ref.block_hash == row["block_hash"]
            ]
            if len(matching_anchor) != 1:
                raise ValueError("quote_anchor_missing_or_ambiguous")
            anchor_received, anchor_body = matching_anchor[0]
            anchor = strict_object(anchor_body, "quote anchor")
            if (
                quantity(anchor.get("number"), 64) != row["block"]
                or quantity(anchor.get("baseFeePerGas")) != row["base_fee_raw"]
            ):
                raise ValueError("quote_anchor_fields_changed")
            if result["evidence"] is not None:
                witness = snap(blocks[hex(row["block"])], chain)
                if witness != snap(anchor, chain):
                    raise ValueError("quote_anchor_witness_changed")
            replayed[lane].append(
                replay_quote(
                    row, calls, events, lane, result, chain, anchor_received=anchor_received
                )
            )
    event, poll = replayed["event"], replayed["poll"]
    if schema == PLAN_SCHEMA:
        complete = (
            result["failure"] is None
            and result["stream_error"] is None
            and evidence is not None
            and evidence["internal_missing"] == 0
        )
        if result["decision"] != quote_decision(chain, result_rows["event"], complete):
            raise ValueError("event_quote_decision_does_not_match")
    return {
        "schema": "oasis-event-quote-replay-v1",
        "chain_id": chain.chain_id,
        "captured_counts": counts,
        "capture_decision": result["decision"],
        "live_logs_retained": len(events),
        "backfill_logs_retained": len(covered_backfill),
        "verified_swap_logs": len(verified_logs),
        "event": event,
        "poll": poll,
        "paired_polls": [
            [index for index, row in enumerate(poll) if same_opportunity(trigger, row)]
            for trigger in event
        ],
    }


def replay_quote(
    row: dict[str, Any],
    calls: list[tuple[int, int, list[object], object]],
    events: dict[LogRecord, int],
    lane: str,
    campaign: dict[str, Any],
    chain: Chain = CHAINS["base"],
    *,
    anchor_received: int | None = None,
) -> dict[str, Any]:
    start = row["start_ns"]
    block_hash = row["block_hash"]
    after = row["dispatch_ns"]
    finalized = row["finish_ns"]
    if lane == "event":
        matched = [
            ns
            for log, ns in events.items()
            if log.block_hash == block_hash
            and log.transaction_hash == row["tx"]
            and (
                "log_index" not in row
                or (log.block_number, log.log_index) == (row["block"], row["log_index"])
            )
        ]
        if not matched or min(matched) != start:
            raise ValueError("quote_start_not_bound_to_swap_receipt_time")
    elif start - campaign["start_ns"] not in {offset * 10**9 for offset in POLL_SECONDS}:
        raise ValueError("quote_start_not_on_poll_schedule")
    pin_arg = pin(block_hash) if chain.chain_id == 8453 else hex(row["block"])
    matched_calls = [
        (sent, ns, params, val)
        for sent, ns, params, val in calls
        if after <= sent <= ns <= finalized and params[1] == pin_arg
    ]
    amount = BASE_AMOUNT
    legs = []
    for leg in range(2 if chain.chain_id == 8453 else 3):
        target, data = route_request(chain, leg, amount)
        found = [
            (ns, val)
            for sent, ns, params, val in matched_calls
            if params[0] == {"to": target, "data": data} and sent >= after
        ]
        if len(found) != 1:
            raise ValueError("quote_call_missing_or_ambiguous")
        after, raw = found[0]
        output, gas = route_result(chain, leg, data_bytes(raw))
        legs.append({"input_raw": amount, "output_raw": output, "quoter_gas": gas})
        amount = output
    quote_done = row["quote_done_ns"]
    if (
        legs != row["legs"]
        or not start <= row["dispatch_ns"] <= after <= quote_done <= finalized
        or (quote_done - start <= DEADLINE_NS) != row["on_time"]
    ):
        raise ValueError("quote_bytes_or_timing_do_not_match")
    net = None
    net_timing = {}
    if chain.chain_id == 8453:
        fee_data = calldata("getL1FeeUpperBound(uint256)", ("uint256",), (BYTES_MODEL,))
        has_net_timing = "net_on_time" in row
        fees = [
            (ns, abi(("uint256",), data_bytes(val))[0])
            for _, ns, params, val in matched_calls
            if (has_net_timing or after <= ns) and params[0] == {"to": ORACLE, "data": fee_data}
        ]
        if len(fees) != 1 or fees[0][1] != row["l1_fee_raw"]:
            raise ValueError("quote_l1_fee_changed")
        fee_received, fee = fees[0]
        net = signaled_net(amount, row["base_fee_raw"], fee)
        if has_net_timing:
            fee_done = strict_integer(row.get("fee_done_ns"), "fee-inclusive quote completion")
            if (
                anchor_received is None
                or not max(anchor_received, fee_received, quote_done) <= fee_done <= finalized
                or row["net_on_time"] is not (fee_done - start <= DEADLINE_NS)
            ):
                raise ValueError("fee_inclusive_quote_timing_does_not_match")
            net_timing = {"fee_done_ns": fee_done, "net_on_time": row["net_on_time"]}
    if net != row["net_model_raw"] or amount - BASE_AMOUNT != row["gross_raw"]:
        raise ValueError("quote_net_model_does_not_match")
    return {
        key: row[key]
        for key in (
            "block",
            "block_hash",
            "start_ns",
            "dispatch_ns",
            "quote_done_ns",
            "on_time",
            "gross_raw",
            "net_model_raw",
        )
    } | net_timing
