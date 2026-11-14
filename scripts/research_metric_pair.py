"""Compare a pinned real Ethereum parent with and without a scoped Titan feed diff."""

import argparse
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from sqlite3 import Error as DatabaseError
from typing import Any

from oasis_agent.arbitrage import metric_builder, metric_pair, titan_state
from oasis_agent.arbitrage.cycle_evidence import read_bounded
from oasis_agent.evidence.state_trie import account_proof, prague_header
from oasis_agent.models.common import sha256_hex

from . import capture_public_rpc as rpc


def prepare(args, cutoff: str) -> tuple[metric_pair.PairedCalls, bytes, dict[str, Any]]:
    design, raw = rpc.load(args.design, metric_pair.Design)

    def capture(name: str, kind: str) -> list[dict[str, Any]]:
        result = rpc.replay(getattr(args, name))
        if (
            result["capture_sha256"] != design.evidence_sha256[name]
            or result["status"] != "complete"
            or result["plan_kind"] != kind
        ):
            raise ValueError("pair_capture_binding:" + name)
        return result["exports"]["records"]

    parent_rows = capture("parent", "pinned_reads")
    target_rows = capture("target", "head_observation")
    parents = (parent_rows[1], capture("peer_parent", "head_observation")[1])
    targets = (target_rows[1], capture("peer_target", "head_observation")[1])
    if parents[0]["endpoint"] == parents[1]["endpoint"] or any(
        parent["endpoint"] != target["endpoint"]
        for parent, target in zip(parents, targets, strict=True)
    ):
        raise ValueError("pair_distinct_header_endpoints_required")
    primary, peer = (
        metric_pair.headers(design, parent["response"]["result"], target["response"]["result"])
        for parent, target in zip(parents, targets, strict=True)
    )
    if primary != peer:
        raise ValueError("pair_header_disagreement")
    feed_plan, feed_raw = rpc.load(args.feeds, metric_builder.BuilderFeedPlan)
    analysis, analysis_raw = rpc.load(args.analysis, titan_state.TitanAnalysisPlan)
    if design.evidence_sha256["analysis"] != sha256_hex(analysis_raw) or design.evidence_sha256[
        "feeds"
    ] != sha256_hex(feed_raw):
        raise ValueError("pair_feed_plan_binding")
    identity = metric_pair.check_identity(design, feed_plan, parent_rows)
    proof = capture("proof", "pinned_state_proofs")
    _, root = prague_header(
        proof[1]["response"]["result"], design.target_number - 1, design.parent_hash
    )
    if proof[2]["request"]["params"][:2] != [metric_pair.PROBE, []]:
        raise ValueError("pair_wrong_empty_account_proof")
    account = account_proof(root, metric_pair.PROBE, (), proof[2]["response"]["result"])
    account.verify_code(proof[3]["response"]["result"])
    if account.present:
        raise ValueError("pair_probe_account_must_be_absent")
    record = capture("titan", "builder_state_declaration")[0]
    elapsed = datetime.fromisoformat(record["fetched_utc"]) - datetime(1970, 1, 1, tzinfo=UTC)
    receive_ns = (
        elapsed.days * 86400 + elapsed.seconds
    ) * 1_000_000_000 + elapsed.microseconds * 1000
    symbols = metric_builder.catalog(feed_plan, read_bounded(args.catalog, 16 * 1024 * 1024))
    diff = metric_pair.selected_diff(
        design, feed_plan, symbols, analysis, record["response"]["result"], receive_ns
    )
    calls = metric_pair.build_calls(
        design,
        identity,
        feed_plan,
        diff,
        primary[1],
        read_bounded(args.artifact, 4 * 1024 * 1024),
        cutoff,
        rpc.SOURCES[design.source][1],
    )
    return (
        calls,
        raw,
        {
            **identity,
            "parent": asdict(primary[0]),
            "parent_state_root": root,
            "target_hash": design.target_hash,
            "block_context": primary[1],
            "overlay_slots": list(diff),
            "first_titan_response_receive_ns": receive_ns,
            "evidence_sha256": design.evidence_sha256,
        },
    )


def review(calls, raw, context, directory: Path) -> dict[str, Any]:
    capture = rpc.replay_loaded(directory, calls, raw)
    arms = []
    for row in capture["exports"]["records"]:
        request = row["request"]
        if request["method"] == "eth_call":
            index = request["id"] - 1
            plan = calls.plans[(index - 2) // 2]
            arms.append(
                {
                    "order": "metric_first" if plan.request.metric_first else "v3_first",
                    "asset": plan.assets[0 if plan.request.start_with_a else 1].symbol,
                    "amount_raw": plan.request.amount_in,
                    "arm": "titan_two_feed_slots" if index % 2 else "parent_without_titan",
                    "outcome": calls.outcome(index, row["response"]),
                }
            )
    complete = capture["status"] == "complete"
    return {
        "schema": "oasis-metric-parent-pair-review-v1",
        "status": capture["status"],
        "context": context,
        "validated_arm_count": len(arms),
        "complete_two_leg_count": sum(
            arm["outcome"]["status"] == "both_legs_repaid" for arm in arms
        )
        if complete
        else None,
        "arms": arms if complete else [],
        "partial_arm_count": 0 if complete else len(arms),
        "capture_sha256": capture["capture_sha256"],
        "reserved_requests": capture["reserved_requests"],
        "source": calls.source,
        "evidence_class": "provider_simulation_of_historical_parent_and_builder_feed_subset",
        "independent_providers_verified": False,
        "settlement_verified": False,
        "probe_runtime_hash": calls.plans[0].probe_runtime_hash,
        "net_profit": None,
        "execution_eligible": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("capture", "replay"))
    for name in (
        "design",
        "parent",
        "target",
        "peer-parent",
        "peer-target",
        "proof",
        "titan",
        "analysis",
        "feeds",
        "catalog",
        "artifact",
        "root",
    ):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if (args.command == "replay") != (args.output is not None):
        parser.error("--output is required for replay only")
    try:
        run = args.root / "paired"
        scope, _ = rpc.load(
            (run if args.command == "replay" else args.root) / "scope.json", rpc.PublicScope
        )
        calls, raw, context = prepare(args, scope.expires_utc)
        if args.command == "capture":
            if (
                len(scope.sources) != 1
                or scope.sources[0].source != calls.source
                or scope.sources[0].max_requests != len(calls.requests())
            ):
                raise ValueError("pair_exact_scope_required")
            return rpc.capture_loaded(args.root, calls, raw, "paired")
        result = review(calls, raw, context, run)
        rpc.publish(args.output, result)
        print(result["status"])
        return 0 if result["status"] == "complete" else 3
    except (ValueError, TypeError, KeyError, IndexError, OSError, DatabaseError):
        print(
            "Metric comparison refused. Inspect original capture and prerequisites.",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
