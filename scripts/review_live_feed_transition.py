"""Review two retained service journals for a strict-feed transition, offline only."""

import argparse
import json
import time
from contextlib import closing
from pathlib import Path

from oasis_agent.evidence.store import ReadOnlyEvidenceStore
from oasis_agent.live.config import LiveConfig
from oasis_agent.live.feed_transition import review_service
from oasis_agent.live.state import StateStore
from oasis_agent.models.common import canonical_json, evidence_ref_from_json, sha256_hex
from scripts.inspect_live_state import inspect

ROOT = Path(__file__).resolve().parents[1]


def review(strategy, authority, evidence_paths, config, fresh, sources, now):
    baseline = inspect(strategy, authority, config)
    report = {
        "schema": "oasis.feed-transition-review.v1",
        "execution_eligible": False,
        "migration_applied": False,
        "state_write_supported": False,
        "reviewed_at": now,
        "services": {},
        "blockers": [],
        "limitations": [
            "not_a_migration_or_release_permit",
            "settlement_finality_not_rechecked_against_current_chain",
            "historical_checkpoint_review_is_not_full_receipt_or_unwind_acceptance",
            "both_services_must_be_stopped_during_future_application",
            "future_application_requires_journal_tip_and_freshness_rechecks",
        ],
    }
    if "service_position_disagreement" in baseline["blockers"]:
        report["blockers"].append("service_position_disagreement")
    for name, path in (("strategy", strategy), ("authority", authority)):
        store = StateStore(path)
        state = store.load("live", read_only=True)
        assert store.journal is not None
        prior = baseline["services"][name]
        if (store.journal.revision, store.journal.config_hash) != (
            prior["journal_tip"],
            prior["config_hash"],
        ):
            raise ValueError("journal changed during review")
        with closing(ReadOnlyEvidenceStore(evidence_paths[name])) as evidence:
            result = review_service(
                config,
                state,
                prior["config_hash"],
                evidence,
                source_payload=store.retained_payload(state),
                sources=sources,
                fresh=tuple(evidence_ref_from_json(r) for r in fresh.get(name, [])),
                now=now,
            )
        result["journal_tip"] = prior["journal_tip"]
        report["services"][name] = result
        report["blockers"].extend(f"{name}_{reason}" for reason in result["blockers"])
    a, b = (report["services"][name]["fresh_nav"] for name in ("strategy", "authority"))
    if a and b and (a["block"], a["nav_usd_micros"]) != (b["block"], b["nav_usd_micros"]):
        report["blockers"].append("service_fresh_nav_disagreement")
    report["evidence_review_passed"] = not report["blockers"]
    report["evidence_hash"] = sha256_hex(canonical_json(report))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("strategy", "authority"):
        parser.add_argument(f"--{name}-state", type=Path, required=True)
        parser.add_argument(f"--{name}-evidence", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=ROOT / "configs/live.base-eth-usdc.json")
    parser.add_argument("--fresh-nav", type=Path)
    parser.add_argument("--sources", nargs=2, default=["live_primary", "live_secondary"])
    args = parser.parse_args()
    try:
        fresh = json.loads(args.fresh_nav.read_bytes()) if args.fresh_nav else {}
        if not isinstance(fresh, dict) or set(fresh) - {"strategy", "authority"}:
            raise ValueError("explicit per-service reference lists required")
        report = review(
            args.strategy_state,
            args.authority_state,
            {"strategy": args.strategy_evidence, "authority": args.authority_evidence},
            LiveConfig.load(args.config),
            fresh,
            tuple(args.sources),
            int(time.time()),
        )
    except Exception:  # noqa: BLE001 - never expose local/provider diagnostics
        report = {
            "schema": "oasis.feed-transition-review.v1",
            "execution_eligible": False,
            "migration_applied": False,
            "state_write_supported": False,
            "evidence_review_passed": False,
            "blockers": ["retained_transition_inputs_unresolved"],
        }
    print(json.dumps(report, indent=2))
    raise SystemExit(0 if report["evidence_review_passed"] else 3)


if __name__ == "__main__":
    main()
