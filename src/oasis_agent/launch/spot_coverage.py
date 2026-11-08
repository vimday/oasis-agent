"""Sample coverage of a spot run: the denominator behind every signal-quality number.

Derived only from the decision records a run (or its replay) produced. Per cycle, each
unique discovered token has exactly one fate (`spot_agent.CANDIDATE_FATES`), so the
fates reconcile to `discovered`; a token the enrichment quota never reached is deferred,
not skipped. Assessments are counted per action as cycle-opportunities and as unique
tokens, because one token seen in many cycles is not many independent samples. Label
registration and emitted labels keep failures and `unobserved` in the denominator.
Labels are provider spot-mid forward returns, not tradable net returns.
"""

from collections import Counter
from typing import Any

from .outcomes import REGISTRATION
from .spot_agent import CANDIDATE_FATES

Record = dict[str, Any]
ACTIONS = ("flip", "watch", "skip", "lp_candidate")


FAILED_KINDS = frozenset({"tick_error", "cycle_failed"})
LABEL_UNITS = ("samples_pending", "labels_pending", "labels_due", "samples_due", "tokens_due")


def coverage_report(records: list[Record]) -> dict[str, Any]:
    """Coverage of a run journal's or a replay's records; never a returns claim.

    Cycles: a `tick_summary` is a completed cycle; `tick_error` (run journal) or
    `cycle_failed` (replay) is a failed one, whose candidates and labels are unknown,
    never zero. A summary without a coverage block (older runs) is a cycle with no
    coverage evidence. Every count below states which cycles it covers.
    """
    summaries = [r for r in records if r.get("kind") == "tick_summary"]
    failed = [r for r in records if r.get("kind") in FAILED_KINDS]
    covered = [s for s in summaries if isinstance(s.get("coverage"), dict)]
    fates: Counter[str] = Counter()
    registration: Counter[str] = Counter()
    emitted: Counter[str] = Counter()
    discovery: Counter[str] = Counter()
    sources: dict[str, Counter[str]] = {}
    discovered = 0
    unreconciled: list[int] = []
    sourced_cycles = 0
    for summary in covered:
        coverage = summary["coverage"]
        discovery[str(coverage["discovery"])] += 1
        discovered += coverage["discovered"]
        fates.update({f: coverage[f] for f in CANDIDATE_FATES})
        if sum(coverage[f] for f in CANDIDATE_FATES) != coverage["discovered"]:
            unreconciled.append(int(summary["at"]))
        registration.update(coverage["label_registration"])
        emitted.update(coverage["labels_emitted"])
        per_source = coverage.get("discovery_sources")
        if coverage["discovery"] == "ok" and isinstance(per_source, dict):
            sourced_cycles += 1
            new = 0
            for name, counts in per_source.items():
                bucket = sources.setdefault(name, Counter())
                bucket.update({k: v for k, v in counts.items() if k != "status"})
                bucket[f"cycles_{counts['status']}"] += 1
                new += counts["new"]
            if new != coverage["discovered"]:
                unreconciled.append(int(summary["at"]))
    last = covered[-1]["coverage"].get("label_backlog") if covered else None
    assessments = [r for r in records if r.get("kind") == "assessment"]
    by_action = {
        action: {
            "cycle_opportunities": sum(a["action"] == action for a in assessments),
            "unique_tokens": len({a["token"] for a in assessments if a["action"] == action}),
        }
        for action in ACTIONS
    }
    outcomes = [r for r in records if r.get("kind") == "outcome"]
    observed = [o for o in outcomes if o.get("status") == "observed"]
    return {
        "schema": "oasis.spot-coverage-report.v2",
        "cycles": {
            "total": len(summaries) + len(failed),
            "completed": len(summaries),
            "failed": len(failed),
            "completed_with_coverage": len(covered),
            "completed_without_coverage_evidence": len(summaries) - len(covered),
            "by_discovery": dict(sorted(discovery.items())),
        },
        # Everything below covers only `completed_with_coverage` cycles. Failed cycles
        # and cycles without coverage evidence are unknown, not zero.
        "coverage_basis": "completed_with_coverage_cycles_only",
        "unknown_cycles": len(failed) + len(summaries) - len(covered),
        "discovered_candidate_cycles": discovered,
        "candidate_fates": {f: fates[f] for f in CANDIDATE_FATES},
        # True only says the observed partitions add up; it says nothing about cycles
        # whose coverage is unknown.
        "fates_reconcile_where_observed": not unreconciled,
        "unreconciled_cycle_times": unreconciled,
        "discovery_sources": {
            "cycles_with_source_counts": sourced_cycles,
            # rows/valid/unique are per source; `new` sums to the unique candidates.
            "by_source": {k: dict(sorted(v.items())) for k, v in sorted(sources.items())},
        },
        "assessments_by_action": by_action,
        "unique_assessed_tokens": len({a["token"] for a in assessments}),
        "label_registration": {k: registration[k] for k in REGISTRATION},
        # One label is one sample x one horizon.
        "labels_emitted": {"observed": emitted["observed"], "unobserved": emitted["unobserved"]},
        "label_backlog_at_last_covered_cycle": (
            {k: last[k] for k in LABEL_UNITS} if isinstance(last, dict) else None
        ),
        "observed_labels_by_action": {
            action: sum(o["action"] == action for o in observed) for action in ACTIONS
        },
        "label_basis": "provider_spot_mid_forward_return_not_tradable_net_return",
    }
