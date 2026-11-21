"""Capture and replay a preselected public event window after a retained height watermark."""

import argparse
import sys
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from sqlite3 import Error as DatabaseError
from typing import Any

from oasis_agent.arbitrage.cycle_evidence import header, read_bounded
from oasis_agent.arbitrage.cycle_plan import utc
from oasis_agent.evidence.public_ranges import RangeContext, range_context
from oasis_agent.evidence.public_rpc_plan import (
    PublicEventWindowPlan,
    PublicHeadPlan,
    PublicRangePlan,
    PublicScope,
    PublicWatermarkPlan,
    PublicWindowFilter,
)
from oasis_agent.evidence.rpc_validation import rpc_quantity, strict_integer, strict_json_object
from oasis_agent.models.chain import BlockSnapshot
from oasis_agent.models.common import canonical_json, sha256_hex
from oasis_agent.rpc.budget import RpcBudget

from . import capture_public_rpc as rpc


def plan_bytes(plan):
    return canonical_json(plan.model_dump(mode="json", by_alias=True, exclude_none=True))


def scope_check(plan: PublicEventWindowPlan, scope: PublicScope) -> None:
    if len(scope.sources) != 1 or (
        scope.sources[0].source,
        scope.sources[0].max_requests,
    ) != (plan.source, plan.max_requests):
        raise ValueError("event_window_scope_mismatch")


def watermark_plan(plan: PublicEventWindowPlan) -> PublicWatermarkPlan:
    return PublicWatermarkPlan(
        schema="oasis-public-rpc-watermark-plan-v1", source=plan.source, chain_id=plan.chain_id
    )


def select_parent(plan: PublicEventWindowPlan, watermark: dict[str, Any]) -> PublicHeadPlan:
    height = rpc_quantity(watermark["exports"]["records"][-1]["response"]["result"])
    number = height + plan.parent_offset
    if number + plan.window_blocks >= 1 << 64:
        raise ValueError("event_window_height_overflow")
    return PublicHeadPlan(
        schema="oasis-public-rpc-head-plan-v1",
        source=plan.source,
        chain_id=plan.chain_id,
        block_tag=number,
    )


def parent_snapshot(plan: PublicEventWindowPlan, parent: dict[str, Any]) -> BlockSnapshot:
    result = parent["exports"]["records"][-1]["response"]["result"]
    return header(result, plan.chain_id, rpc_quantity(result["number"]))


def range_plan(
    plan: PublicEventWindowPlan,
    watch: PublicWindowFilter,
    block: BlockSnapshot,
    locked_utc: str,
    hashes: tuple[str, ...] | None,
) -> PublicRangePlan:
    return PublicRangePlan(
        schema="oasis-public-log-range-plan-v1",
        source=plan.source,
        chain_id=plan.chain_id,
        start_block=block.ref.number + 1,
        end_block=block.ref.number + plan.window_blocks,
        parent_hash=block.ref.block_hash,
        address=watch.address,
        topics=watch.topics,
        locked_utc=locked_utc,
        expected_hashes=hashes,
        parent_timestamp=block.ref.timestamp,
    )


def range_result(plan: PublicRangePlan, replayed: dict[str, Any]) -> RangeContext:
    context = None
    for pos, record in enumerate(replayed["exports"]["records"][1:], 1):
        context = range_context(plan, pos, record["response"]["result"], context)
    if not isinstance(context, RangeContext):
        raise ValueError("event_window_range_context_missing")
    return context


def capture_one(run: Path, name: str, plan) -> int:
    path = run / f"{name}-plan.json"
    rpc.durable_new(path, plan_bytes(plan))
    return rpc.capture(run / "campaign", path, name)


def capture(plan_path: Path, scope_path: Path, run: Path) -> int:
    plan, plan_raw = rpc.load(plan_path, PublicEventWindowPlan)
    scope, scope_raw = rpc.load(scope_path, PublicScope)
    scope_check(plan, scope)
    locked_ms = time.time_ns() // 1_000_000
    if utc(scope.expires_utc) <= datetime.fromtimestamp((locked_ms + plan.wait_ms) / 1000, UTC):
        raise ValueError("event_window_scope_expires_before_wait")
    run.mkdir()
    rpc.durable_new(run / "design.json", plan_raw)
    rpc.durable_new(run / "scope.json", scope_raw)
    rpc.durable_new(
        run / "lock.json",
        canonical_json(
            {
                "locked_ms": locked_ms,
                "design_sha256": sha256_hex(plan_raw),
                "scope_sha256": sha256_hex(scope_raw),
            }
        ),
    )
    campaign = run / "campaign"
    rpc.initialize(run / "scope.json", campaign)
    if capture_one(run, "watermark", watermark_plan(plan)):
        return 3
    watermark = rpc.replay(campaign / "watermark")
    locked_utc = watermark["exports"]["records"][-1]["fetched_utc"]
    parent = select_parent(plan, watermark)
    time.sleep(plan.wait_ms / 1000)
    if capture_one(run, "parent", parent):
        return 3
    block = parent_snapshot(plan, rpc.replay(campaign / "parent"))
    hashes = None
    for index, watch in enumerate(plan.filters):
        item = range_plan(plan, watch, block, locked_utc, hashes)
        name = f"range-{index}"
        if capture_one(run, name, item):
            return 3
        if index == 0:
            context = range_result(item, rpc.replay(campaign / name))
            hashes = tuple(block.ref.block_hash for block in context.blocks)
    return 0


def replay(run: Path) -> dict[str, Any]:
    plan, plan_raw = rpc.load(run / "design.json", PublicEventWindowPlan)
    scope, scope_raw = rpc.load(run / "scope.json", PublicScope)
    scope_check(plan, scope)
    lock_raw = read_bounded(run / "lock.json", 4096)
    lock = strict_json_object(lock_raw)
    previous = strict_integer(lock.get("locked_ms"), "locked_ms")
    if (
        lock
        != {
            "locked_ms": previous,
            "design_sha256": sha256_hex(plan_raw),
            "scope_sha256": sha256_hex(scope_raw),
        }
        or previous < 0
    ):
        raise ValueError("event_window_lock_mismatch")
    campaign = run / "campaign"
    if read_bounded(campaign / "scope.json", 128 * 1024) != scope_raw:
        raise ValueError("event_window_campaign_scope_mismatch")
    identity = strict_json_object(read_bounded(campaign / "identity.json", 4096))
    ledger = campaign / "original-budget.sqlite3"
    if identity != {
        "scope_sha256": sha256_hex(scope_raw),
        "ledger_identity": rpc.identity(ledger),
        "root": str(campaign.resolve()),
    }:
        raise ValueError("event_window_original_ledger_required")
    with RpcBudget(ledger, rpc.policies(scope), read_only=True) as budget:
        ledger_count = budget.counts()[plan.source]
    parts: dict[str, Any] = {}
    count = 0
    terminal = "complete"
    found = {p.name for p in campaign.iterdir() if p.is_dir()}

    def component(name: str, expected, wait_ms: int = 0):
        nonlocal previous, count, terminal
        if terminal != "complete":
            return None
        if name not in found or not (campaign / name / "capture.json").is_file():
            terminal = "interrupted"
            found.discard(name)
            return None
        path = campaign / name
        result = rpc.replay(path)
        raw = read_bounded(path / "capture.json", 256 * 1024)
        if (
            result["scope_sha256"] != lock["scope_sha256"]
            or result["plan_sha256"] != sha256_hex(plan_bytes(expected))
            or result["capture_sha256"] != sha256_hex(raw)
        ):
            raise ValueError("event_window_component_identity")
        manifest = strict_json_object(raw)
        if manifest["started_ms"] < previous + wait_ms or manifest["budget_before"] != {
            plan.source: count
        }:
            raise ValueError("event_window_component_order")
        last_received = manifest["exchanges"][-1]["received_ms"]
        previous = last_received
        count += result["reserved_requests"]
        found.remove(name)
        parts[name] = {
            "capture_sha256": result["capture_sha256"],
            "reserved_requests": result["reserved_requests"],
            "status": result["status"],
            "started_ms": manifest["started_ms"],
            "last_received_ms": last_received,
        }
        if result["status"] != "complete":
            terminal = "stopped"
            return None
        return result

    rows: dict[str, Any] = {}
    snapshots: tuple[BlockSnapshot, ...] = ()
    reported_height = None
    selected_parent = None
    locked_utc = None
    observed = component("watermark", watermark_plan(plan))
    if observed is not None:
        reported_height = rpc_quantity(observed["exports"]["records"][-1]["response"]["result"])
        locked_utc = observed["exports"]["records"][-1]["fetched_utc"]
        parent = select_parent(plan, observed)
        selected_parent = parent.block_tag
        anchor = component("parent", parent, plan.wait_ms)
        if anchor is not None:
            block = parent_snapshot(plan, anchor)
            for index, watch in enumerate(plan.filters):
                hashes = tuple(block.ref.block_hash for block in snapshots) if snapshots else None
                item = range_plan(plan, watch, block, locked_utc, hashes)
                result = component(f"range-{index}", item)
                if result is None:
                    break
                records = result["exports"]["records"]
                context = range_result(item, result)
                if snapshots and context.blocks != snapshots:
                    raise ValueError("event_window_filter_lineage_disagreement")
                snapshots = context.blocks
                rows[watch.name] = {
                    "coverage_ready_utc": records[-1]["fetched_utc"],
                    "logs_response_utc": records[-2]["fetched_utc"],
                    "logs": [asdict(log) for log in context.logs],
                }
    if found or count > ledger_count or (terminal != "interrupted" and count != ledger_count):
        raise ValueError("event_window_unbound_campaign_state")
    if terminal == "stopped":
        stop = strict_json_object(read_bounded(campaign / "STOP", 4096))
        if stop != {"capture": next(reversed(parts)), "reason": "capture_incomplete"}:
            raise ValueError("event_window_stop_mismatch")
    elif terminal == "complete" and any((campaign / n).exists() for n in ("STOP", "ACTIVE.json")):
        raise ValueError("event_window_inconsistent_terminal_state")
    return {
        "schema": "oasis-public-event-window-replay-v1",
        "status": terminal,
        "chain_id": plan.chain_id,
        "source": plan.source,
        "design_sha256": lock["design_sha256"],
        "lock_sha256": sha256_hex(lock_raw),
        "watermark": reported_height,
        "locked_utc": locked_utc,
        "selected_parent": selected_parent,
        "block_denominator": plan.window_blocks,
        "filter_denominator": len(plan.filters),
        "blocks": [asdict(block) for block in snapshots] if terminal == "complete" else [],
        "filters": rows if terminal == "complete" else {},
        "components": parts,
        "missing_filters": [watch.name for watch in plan.filters if watch.name not in rows],
        "usable_filter_count": len(rows) if terminal == "complete" else 0,
        "reserved_requests": ledger_count,
        "max_requests": plan.max_requests,
        "time_basis": "provider_height_and_local_receive_clock",
        "independent_providers_verified": False,
        "finality_independently_verified": False,
        "execution_eligible": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("capture")
    for name in ("plan", "scope", "run"):
        run.add_argument(f"--{name}", type=Path, required=True)
    offline = sub.add_parser("replay")
    offline.add_argument("--run", type=Path, required=True)
    offline.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "capture":
            return capture(args.plan, args.scope, args.run)
        report = replay(args.run)
        rpc.publish(args.output, report)
        print(report["status"])
        return 0 if report["status"] == "complete" else 3
    except OSError, ValueError, TypeError, DatabaseError:
        print("Event window refused; retain the original campaign", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
