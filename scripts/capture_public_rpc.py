"""Capture frozen anonymous EVM reads on an original budget; replay exact bytes offline."""

import argparse
import fcntl
import os
import re
import sys
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from oasis_agent.arbitrage.cycle_evidence import header, read_bounded
from oasis_agent.arbitrage.titan_state import decode_snapshot
from oasis_agent.evidence.public_event_index import index_logs
from oasis_agent.evidence.public_ranges import range_context
from oasis_agent.evidence.public_receipts import decode_receipt, quantity, receipt_header
from oasis_agent.evidence.public_rpc_plan import (
    SOURCES,
    PublicEventIndexPlan,
    PublicHeadPlan,
    PublicProofPlan,
    PublicRangePlan,
    PublicReadPlan,
    PublicReceiptPlan,
    PublicScope,
    PublicTitanPlan,
)
from oasis_agent.evidence.rpc_validation import (
    rpc_quantity,
    strict_integer,
    strict_json_object,
    strict_list,
    strict_object,
)
from oasis_agent.evidence.state_trie import proof_context
from oasis_agent.models.common import canonical_json, sha256_hex
from oasis_agent.rpc.budget import BudgetPolicy, RpcBudget
from oasis_agent.rpc.protocol import response_envelope

from .research_metric import publish, write_new

MAX_BODY = 2 * 1024 * 1024
MAX_TOTAL = 32 * 1024 * 1024


def sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def durable_new(path: Path, raw: bytes) -> None:
    write_new(path, raw)
    sync_directory(path.parent)


def load(path: Path, model):
    raw = read_bounded(path, 128 * 1024)
    strict_json_object(raw)
    return model.model_validate_json(raw), raw


def load_plan(path: Path):
    raw = read_bounded(path, 128 * 1024)
    obj = strict_json_object(raw)
    model = {
        "oasis-public-rpc-head-plan-v1": PublicHeadPlan,
        "oasis-public-rpc-read-plan-v1": PublicReadPlan,
        "oasis-titan-state-capture-plan-v1": PublicTitanPlan,
        "oasis-public-receipt-plan-v1": PublicReceiptPlan,
        "oasis-public-log-range-plan-v1": PublicRangePlan,
        "oasis-public-state-proof-plan-v1": PublicProofPlan,
        "oasis-public-event-index-plan-v1": PublicEventIndexPlan,
    }.get(obj.get("schema"))
    if model is None:
        raise ValueError("unsupported_capture_schema")
    return model.model_validate_json(raw), raw


def policies(scope: PublicScope) -> tuple[BudgetPolicy, ...]:
    return tuple(BudgetPolicy(s.source, s.max_requests, 0) for s in scope.sources)


def identity(path: Path) -> list[int]:
    if path.is_symlink():
        raise ValueError("symlink_ledger")
    info = path.stat()
    return [info.st_dev, info.st_ino]


def initialize(scope_path: Path, root: Path) -> None:
    scope, raw = load(scope_path, PublicScope)
    if datetime.now(UTC) >= datetime.fromisoformat(scope.expires_utc):
        raise ValueError("expired_scope")
    root.mkdir()
    durable_new(root / "scope.json", raw)
    budget = RpcBudget(root / "original-budget.sqlite3", policies(scope))
    budget.db.close()
    durable_new(
        root / "identity.json",
        canonical_json(
            {
                "scope_sha256": sha256_hex(raw),
                "ledger_identity": identity(root / "original-budget.sqlite3"),
                "root": str(root.resolve()),
            }
        ),
    )
    durable_new(root / "campaign.lock", b"")


@contextmanager
def locked(root: Path):
    # No automatic replacement, resume or recovery of a missing lock/ledger/run marker.
    if root.is_symlink() or (root / "campaign.lock").is_symlink():
        raise ValueError("symlink_campaign")
    with (root / "campaign.lock").open("r+b") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if any((root / n).exists() or (root / n).is_symlink() for n in ("STOP", "ACTIVE.json")):
            raise ValueError("campaign_stopped_or_ambiguous")
        yield


def validate_context(
    plan: PublicReadPlan
    | PublicHeadPlan
    | PublicTitanPlan
    | PublicReceiptPlan
    | PublicRangePlan
    | PublicProofPlan
    | PublicEventIndexPlan,
    index: int,
    result: object,
    first: object,
) -> object:
    if isinstance(plan, PublicTitanPlan):
        decode_snapshot(result)
        return None
    if index == 0:
        if rpc_quantity(result) != plan.chain_id:
            raise ValueError("chain_id_mismatch")
    elif isinstance(plan, PublicRangePlan):
        return range_context(plan, index, result, first)
    elif isinstance(plan, PublicProofPlan):
        return proof_context(plan, index, result, first)
    elif isinstance(plan, PublicEventIndexPlan):
        index_logs(plan, result)
    elif index in (1, len(plan.requests()) - 1):
        if isinstance(plan, PublicHeadPlan):
            number = rpc_quantity(strict_object(result, "head").get("number"))
            if isinstance(plan.block_tag, int) and number != plan.block_tag:
                raise ValueError("historical_observation_number_mismatch")
            return header(result, plan.chain_id, number)
        snap = header(result, plan.chain_id, plan.block_number)
        if isinstance(plan, PublicReceiptPlan):
            snap = receipt_header(result, plan.chain_id, plan.block_number, plan.transactions)
        if snap.ref.block_hash != plan.block_hash:
            raise ValueError("block_hash_mismatch")
        if index != 1 and snap != first:
            raise ValueError("closing_block_changed")
        return snap
    elif isinstance(plan, PublicReceiptPlan):
        if first is None:
            raise ValueError("receipt_without_opening_header")
        tx_hash = plan.transactions[index - 2]
        decode_receipt(result, first.ref, tx_hash, dict(first.receipt_positions)[tx_hash])
    elif plan.requests()[index][0] == "eth_getBalance":
        quantity(result)
    elif not isinstance(result, str) or re.fullmatch(r"0x(?:[0-9a-fA-F]{2})*", result) is None:
        raise ValueError("invalid_read_hex")
    elif plan.requests()[index][0] == "eth_getStorageAt" and len(result) != 66:
        raise ValueError("invalid_storage_word")
    return first


def capture(root: Path, plan_path: Path, name: str) -> int:
    if re.fullmatch(r"[a-z0-9_-]{1,64}", name) is None:
        raise ValueError("capture_name")
    with locked(root):
        scope, scope_raw = load(root / "scope.json", PublicScope)
        plan, plan_raw = load_plan(plan_path)
        expected = strict_json_object(read_bounded(root / "identity.json", 4096))
        if expected != {
            "scope_sha256": sha256_hex(scope_raw),
            "ledger_identity": identity(root / "original-budget.sqlite3"),
            "root": str(root.resolve()),
        }:
            raise ValueError("original_campaign_identity_changed")
        budget = RpcBudget(root / "original-budget.sqlite3", policies(scope), require_existing=True)
        try:
            if plan.source not in budget.policies:
                raise ValueError("source_outside_scope")
            if (
                budget.counts()[plan.source] + len(plan.requests())
                > budget.policies[plan.source].max_requests
            ):
                raise ValueError("insufficient_whole_capture_budget")
            if datetime.now(UTC) >= datetime.fromisoformat(scope.expires_utc):
                raise ValueError("expired_scope")
            if isinstance(plan, PublicRangePlan) and datetime.now(UTC) < datetime.fromisoformat(
                plan.locked_utc
            ):
                raise ValueError("capture_before_declared_lock")
            out = root / name
            out.mkdir()
            durable_new(out / "plan.json", plan_raw)
            durable_new(out / "scope.json", scope_raw)
            active_raw = canonical_json({"capture": name, "plan_sha256": sha256_hex(plan_raw)})
            durable_new(root / "ACTIVE.json", active_raw)

            def guard() -> None:
                if (
                    (root / "STOP").exists()
                    or (root / "STOP").is_symlink()
                    or identity(root / "original-budget.sqlite3") != expected["ledger_identity"]
                    or read_bounded(root / "ACTIVE.json", 4096) != active_raw
                    or read_bounded(root / "scope.json", 128 * 1024) != scope_raw
                ):
                    raise ValueError("campaign_control_changed")

            result = collect(scope, plan, plan_raw, scope_raw, out, budget, guard)
            if result and not ((root / "STOP").exists() or (root / "STOP").is_symlink()):
                durable_new(
                    root / "STOP", canonical_json({"capture": name, "reason": "capture_incomplete"})
                )
            if read_bounded(root / "ACTIVE.json", 4096) == active_raw:
                (root / "ACTIVE.json").unlink()
            sync_directory(root)
            return result
        finally:
            budget.db.close()


def collect(scope, plan, plan_raw, scope_raw, out, budget, guard: Callable[[], None]) -> int:
    started = time.time_ns() // 1_000_000
    deadline = time.monotonic() + scope.max_capture_seconds
    prior = started
    used_bytes = 0
    first = None
    manifest: dict[str, Any] = {
        "schema": "oasis-public-rpc-capture-v1",
        "scope_sha256": sha256_hex(scope_raw),
        "plan_sha256": sha256_hex(plan_raw),
        "started_ms": started,
        "status": "incomplete",
        "exchanges": [],
        "budget_before": budget.counts(),
    }

    def admission() -> None:
        guard()
        if time.monotonic() >= deadline or datetime.now(UTC) >= datetime.fromisoformat(
            scope.expires_utc
        ):
            raise ValueError("capture_deadline")

    with httpx.Client(timeout=10, follow_redirects=False, trust_env=False) as client:
        for index, (method, params) in enumerate(plan.requests()):
            raw = canonical_json(
                {"jsonrpc": "2.0", "id": index + 1, "method": method, "params": params}
            )
            row: dict[str, Any] = {
                "started_ms": time.time_ns() // 1_000_000,
                "received_ms": None,
                "request_sha256": sha256_hex(raw),
                "response_sha256": None,
                "http_status": None,
                "reserved": False,
                "body_complete": False,
                "error": None,
            }
            durable_new(out / f"{index}.request.json", raw)
            body = bytearray()
            try:
                admission()
                if row["started_ms"] < prior:
                    raise ValueError("clock_regressed")
                if budget.reserve(plan.source, now_ms=row["started_ms"], admission=admission):
                    raise ValueError("unexpected_interval")
                row["reserved"] = True
                with client.stream(
                    "POST",
                    SOURCES[plan.source][1],
                    content=raw,
                    headers={"Content-Type": "application/json"},
                ) as response:
                    row["http_status"] = response.status_code
                    for chunk in response.iter_bytes():
                        if (
                            len(body) + len(chunk) > MAX_BODY
                            or used_bytes + len(body) + len(chunk) > MAX_TOTAL
                        ):
                            raise ValueError("response_size_limit")
                        body.extend(chunk)
                        admission()
                row["body_complete"] = True
                admission()
                if response.status_code != 200:
                    row["error"] = "http_error"
                else:
                    value = response_envelope(bytes(body), index + 1)
                    if "error" in value:
                        row["error"] = "rpc_error"
                    else:
                        first = validate_context(plan, index, value["result"], first)
            except (httpx.HTTPError, ValueError, TypeError, OSError):
                row["error"] = "transport_context_or_resource_failure"
            row["received_ms"] = time.time_ns() // 1_000_000
            if first is not None and first.ref.timestamp * 1000 > row["received_ms"]:
                row["error"] = "transport_context_or_resource_failure"
            if row["received_ms"] < row["started_ms"]:
                row["error"] = "clock_regressed"
            prior = row["received_ms"]
            used_bytes += len(body)
            row["response_sha256"] = sha256_hex(bytes(body))
            durable_new(out / f"{index}.response.json", bytes(body))
            durable_new(out / f"{index}.meta.json", canonical_json(row))
            manifest["exchanges"].append(row)
            if row["error"]:
                break
        else:
            manifest["status"] = "complete"
    manifest["budget_after"] = budget.counts()
    durable_new(out / "capture.json", canonical_json(manifest))
    return 0 if manifest["status"] == "complete" else 3


def replay(directory: Path) -> dict[str, Any]:
    scope, scope_raw = load(directory / "scope.json", PublicScope)
    plan, plan_raw = load_plan(directory / "plan.json")
    manifest_raw = read_bounded(directory / "capture.json", 256 * 1024)
    m = strict_json_object(manifest_raw)
    if set(m) != {
        "schema",
        "scope_sha256",
        "plan_sha256",
        "started_ms",
        "status",
        "exchanges",
        "budget_before",
        "budget_after",
    } or (
        m["schema"] != "oasis-public-rpc-capture-v1"
        or m["scope_sha256"] != sha256_hex(scope_raw)
        or m["plan_sha256"] != sha256_hex(plan_raw)
        or m["status"] not in {"complete", "incomplete"}
    ):
        raise ValueError("capture_identity_mismatch")
    rows = strict_list(m["exchanges"], "exchanges")
    requests = plan.requests()
    if not 1 <= len(rows) <= len(requests) or (
        m["status"] == "complete" and len(rows) != len(requests)
    ):
        raise ValueError("capture_coverage")
    prior = started = strict_integer(m["started_ms"], "started_ms")
    if (
        isinstance(plan, PublicRangePlan)
        and started / 1000 < datetime.fromisoformat(plan.locked_utc).timestamp()
    ):
        raise ValueError("capture_before_declared_lock")
    first = None
    records = []
    reserved = 0
    total = 0
    for index, item in enumerate(rows):
        row = strict_object(item, "exchange")
        if set(row) != {
            "started_ms",
            "received_ms",
            "request_sha256",
            "response_sha256",
            "http_status",
            "reserved",
            "body_complete",
            "error",
        }:
            raise ValueError("exchange_schema")
        raw = read_bounded(directory / f"{index}.request.json", 32 * 1024)
        body = read_bounded(directory / f"{index}.response.json", MAX_BODY)
        meta = read_bounded(directory / f"{index}.meta.json", 4096)
        method, params = requests[index]
        if raw != canonical_json(
            {"jsonrpc": "2.0", "id": index + 1, "method": method, "params": params}
        ) or (
            row["request_sha256"] != sha256_hex(raw)
            or row["response_sha256"] != sha256_hex(body)
            or meta != canonical_json(row)
            or type(row["reserved"]) is not bool
            or type(row["body_complete"]) is not bool
        ):
            raise ValueError("wire_integrity_failure")
        reserved += row["reserved"]
        begin = strict_integer(row["started_ms"], "started_ms")
        received = strict_integer(row["received_ms"], "received_ms")
        total += len(body)
        if total > MAX_TOTAL:
            raise ValueError("capture_size_limit")
        failed = row["error"] is not None
        if row["error"] not in {
            None,
            "http_error",
            "rpc_error",
            "transport_context_or_resource_failure",
            "clock_regressed",
        }:
            raise ValueError("invalid_failure_kind")
        if row["http_status"] is not None and (
            type(row["http_status"]) is not int or not 100 <= row["http_status"] <= 599
        ):
            raise ValueError("invalid_http_status")
        if failed:
            if index != len(rows) - 1 or m["status"] != "incomplete":
                raise ValueError("continued_after_failure")
        else:
            if (
                not started
                <= prior
                <= begin
                <= received
                <= started + scope.max_capture_seconds * 1000
            ):
                raise ValueError("capture_clock_incoherent")
            if received / 1000 >= datetime.fromisoformat(scope.expires_utc).timestamp():
                raise ValueError("capture_after_scope_expiry")
            if row["http_status"] != 200 or not row["reserved"] or not row["body_complete"]:
                raise ValueError("successful_capture_without_http_or_budget")
        prior = received
        try:
            response = response_envelope(body, index + 1)
        except (ValueError, TypeError):
            if not failed:
                raise
            continue
        if not failed:
            if "error" in response:
                raise ValueError("hidden_rpc_failure")
            first = validate_context(plan, index, response["result"], first)
            if first is not None and first.ref.timestamp * 1000 > received:
                raise ValueError("retrieval_before_block")
        # A failed context/clock/resource read must never be exported as a usable result.
        # A complete, well-timed RPC error can retain its diagnostic in parsed replay.
        error_reply = (
            row["error"] == "rpc_error"
            and "error" in response
            and started <= begin <= received <= started + scope.max_capture_seconds * 1000
            and received / 1000 < datetime.fromisoformat(scope.expires_utc).timestamp()
        )
        if (
            row["http_status"] == 200
            and row["reserved"]
            and row["body_complete"]
            and (not failed or error_reply)
        ):
            records.append(
                {
                    "endpoint": SOURCES[plan.source][1],
                    "fetched_utc": datetime.fromtimestamp(received / 1000, UTC).isoformat(),
                    "request": strict_json_object(raw),
                    "response": response,
                }
            )
    if (m["status"] == "incomplete") != (rows[-1]["error"] is not None):
        raise ValueError("incomplete_status_without_failure")
    before, after = (strict_object(m[k], k) for k in ("budget_before", "budget_after"))
    if (
        set(before) != set(after)
        or set(before) != {s.source for s in scope.sources}
        or plan.source not in before
    ):
        raise ValueError("budget_source_mismatch")
    for limit in scope.sources:
        a, b = (
            strict_integer(before[limit.source], "before"),
            strict_integer(after[limit.source], "after"),
        )
        if not 0 <= a <= b <= limit.max_requests or b - a != (
            reserved if limit.source == plan.source else 0
        ):
            raise ValueError("budget_count_mismatch")
    block_verified = first is not None and (
        not isinstance(plan, PublicProofPlan) or m["status"] == "complete"
    )
    return {
        "schema": "oasis-public-rpc-replay-v1",
        "status": m["status"],
        "plan_kind": "builder_state_declaration"
        if isinstance(plan, PublicTitanPlan)
        else "pinned_receipts"
        if isinstance(plan, PublicReceiptPlan)
        else "prospective_log_range"
        if isinstance(plan, PublicRangePlan)
        else "pinned_state_proofs"
        if isinstance(plan, PublicProofPlan)
        else "historical_index_leads"
        if isinstance(plan, PublicEventIndexPlan)
        else "head_observation"
        if isinstance(plan, PublicHeadPlan)
        else "pinned_reads",
        "block": asdict(first) if block_verified else None,
        "finality_independently_verified": False,
        "plan_sha256": sha256_hex(plan_raw),
        "scope_sha256": sha256_hex(scope_raw),
        "capture_sha256": sha256_hex(manifest_raw),
        "retained_bytes_verified": True,
        "original_http_bytes_verified": all(row["body_complete"] for row in rows),
        "independent_providers_verified": False,
        "clock_attestation": "local_only",
        "execution_eligible": False,
        "planned_requests": len(requests),
        "attempted_requests": len(rows),
        "reserved_requests": reserved,
        "exports": {"schema": "oasis-parsed-rpc-bundle-v1", "records": records},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="freeze a new free public read scope; no network")
    init.add_argument("--scope", type=Path, required=True)
    init.add_argument("--root", type=Path, required=True)
    run = sub.add_parser("capture", help="explicit read-only network action; never retries")
    run.add_argument("--root", type=Path, required=True)
    run.add_argument("--plan", type=Path, required=True)
    run.add_argument("--name", required=True)
    offline = sub.add_parser("replay")
    offline.add_argument("--capture", type=Path, required=True)
    offline.add_argument("--output", type=Path, required=True)
    offline.add_argument("--exports-only", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "init":
            initialize(args.scope, args.root)
        elif args.command == "capture":
            result = capture(args.root, args.plan, args.name)
            print("Capture complete" if result == 0 else "Capture incomplete; campaign stopped")
            return result
        else:
            result = replay(args.capture)
            publish(args.output, result["exports"] if args.exports_only else result)
            print(f"Verified retained bytes; status {result['status']}; no execution authority")
        return 0
    except (OSError, ValueError, TypeError):
        print(
            "Public RPC operation refused; inspect retained state, no automatic retry",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
