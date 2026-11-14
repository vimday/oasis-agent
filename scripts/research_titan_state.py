"""Interpret retained Titan public snapshots without applying their state overrides."""

import argparse
import sys
from pathlib import Path
from typing import Any

from oasis_agent.arbitrage import metric_builder, titan_state
from oasis_agent.arbitrage.cycle_evidence import read_bounded
from oasis_agent.evidence import public_rpc_plan
from oasis_agent.evidence.rpc_validation import strict_json_object
from oasis_agent.models.common import sha256_hex

from . import capture_public_rpc
from .research_metric import publish


def replay(
    plan_path: Path,
    capture_path: Path,
    feed_plan_path: Path | None = None,
    catalog_path: Path | None = None,
) -> dict[str, Any]:
    raw = read_bounded(plan_path, 32 * 1024)
    strict_json_object(raw)
    plan = titan_state.TitanAnalysisPlan.model_validate_json(raw)
    if (feed_plan_path is None) != (catalog_path is None):
        raise ValueError("feed_plan_and_catalog_required_together")
    feed_plan = None
    symbols = {}
    if feed_plan_path is not None and catalog_path is not None:
        feed_raw = read_bounded(feed_plan_path, 32 * 1024)
        strict_json_object(feed_raw)
        feed_plan = metric_builder.BuilderFeedPlan.model_validate_json(feed_raw)
        if feed_plan.oracle not in plan.metric_quote_addresses:
            raise ValueError("feed_oracle_outside_analysis_scope")
        symbols = metric_builder.catalog(feed_plan, read_bounded(catalog_path, 16 * 1024 * 1024))
    capture_plan, _ = capture_public_rpc.load_plan(capture_path / "plan.json")
    if not isinstance(capture_plan, public_rpc_plan.PublicTitanPlan):
        raise ValueError("titan_capture_required")
    transport = capture_public_rpc.replay(capture_path)
    manifest = strict_json_object(read_bounded(capture_path / "capture.json", 256 * 1024))
    snapshots = []
    transitions = []
    for record in transport["exports"]["records"]:
        if "result" not in record["response"]:
            continue
        index = record["request"]["id"] - 1
        observation = titan_state.inspect_snapshot(
            plan,
            record["response"]["result"],
            manifest["exchanges"][index]["received_ms"] * 1_000_000,
        )
        observation["response_sha256"] = manifest["exchanges"][index]["response_sha256"]
        observation["received_ms"] = manifest["exchanges"][index]["received_ms"]
        if feed_plan is not None:
            observation["metric_feeds"] = metric_builder.interpret(
                feed_plan,
                record["response"]["result"],
                observation["received_ms"] * 1_000_000,
                symbols,
            )
        if snapshots:
            transitions.append(
                {
                    "from_index": len(snapshots) - 1,
                    "to_index": len(snapshots),
                    "changes": titan_state.compare(snapshots[-1], observation),
                }
            )
            if feed_plan is not None:
                transitions[-1]["feed_changes"] = metric_builder.compare(
                    snapshots[-1]["metric_feeds"], observation["metric_feeds"]
                )
        snapshots.append(observation)
    problems = []
    if transport["status"] != "complete":
        problems.append("incomplete_capture")
    if not snapshots or any(s["missing"] for s in snapshots):
        problems.append("missing_requested_metric_venue")
    if any(r["reasons"] for s in snapshots for r in s["selected"]):
        problems.append("rejected_venue_declaration")
    if any(
        c["status"] in {"same_version_conflict", "sequence_regression", "context_regression"}
        for t in transitions
        for c in t["changes"]
    ):
        problems.append("conflicting_or_regressed_snapshots")
    if feed_plan is not None:
        if any(s["metric_feeds"]["reasons"] for s in snapshots):
            problems.append("rejected_or_incomplete_feed_declaration")
        if any(
            c["status"] in {"reference_time_regression", "same_time_conflict"}
            for t in transitions
            for c in t["feed_changes"]
        ):
            problems.append("conflicting_or_regressed_feed_declarations")
    return {
        "schema": "oasis-titan-state-report-v1",
        "evidence_class": "builder_state_declaration",
        "status": "incomplete_or_rejected" if problems else "observed_declarations",
        "reasons": problems,
        "chain_id_claim": 1,
        "canonical_base_block_hash": None,
        "plan_sha256": sha256_hex(raw),
        "capture_sha256": transport["capture_sha256"],
        "scope_sha256": transport["scope_sha256"],
        "implementation_hashes": {
            name: sha256_hex(Path(module.__file__).read_bytes())
            for name, module in (
                ("analysis", titan_state),
                ("feed_analysis", metric_builder),
                ("transport", capture_public_rpc),
                ("capture_plan", public_rpc_plan),
            )
        },
        "snapshots": snapshots,
        "transitions": transitions,
        "reserved_requests": transport["reserved_requests"],
        "retained_bytes_verified": transport["retained_bytes_verified"],
        "original_http_bytes_verified": transport["original_http_bytes_verified"],
        "clock_attestation": "local_only",
        "canonical_chain_verified": False,
        "independent_providers_verified": False,
        "feed_reference_freshness_verified": False,
        "orderflow_completeness_verified": False,
        "state_overrides_applied": False,
        "execution_eligible": False,
        "net_profit": None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--feed-plan", type=Path)
    parser.add_argument("--feed-catalog", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = replay(args.plan, args.capture, args.feed_plan, args.feed_catalog)
        publish(args.output, result)
        print(f"{result['status']} | builder declarations | execution eligible false")
        return 0
    except (ValueError, TypeError, OSError):
        print("Titan replay refused: invalid evidence or output exists", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
