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


def coverage_report(records: list[Record]) -> dict[str, Any]:
    cycles = [r for r in records if r.get("kind") == "tick_summary"]
    fates: Counter[str] = Counter()
    registration: Counter[str] = Counter()
    emitted: Counter[str] = Counter()
    discovery: Counter[str] = Counter()
    discovered = 0
    unreconciled: list[int] = []
    for index, summary in enumerate(cycles, start=1):
        coverage = summary.get("coverage")
        if not isinstance(coverage, dict):
            discovery["not_recorded"] += 1
            continue
        discovery[str(coverage["discovery"])] += 1
        discovered += coverage["discovered"]
        fates.update({f: coverage[f] for f in CANDIDATE_FATES})
        if sum(coverage[f] for f in CANDIDATE_FATES) != coverage["discovered"]:
            unreconciled.append(index)
        registration.update(coverage["label_registration"])
        emitted.update(coverage["labels_emitted"])
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
        "schema": "oasis.spot-coverage-report.v1",
        "cycles": len(cycles),
        "cycles_by_discovery": dict(sorted(discovery.items())),
        "discovered_candidate_cycles": discovered,
        "candidate_fates": {f: fates[f] for f in CANDIDATE_FATES},
        "fates_reconcile": not unreconciled,
        "unreconciled_cycles": unreconciled,
        "assessments_by_action": by_action,
        "unique_assessed_tokens": len({a["token"] for a in assessments}),
        "label_registration": {k: registration[k] for k in REGISTRATION},
        "labels_emitted": {"observed": emitted["observed"], "unobserved": emitted["unobserved"]},
        "observed_labels_by_action": {
            action: sum(o["action"] == action for o in observed) for action in ACTIONS
        },
        "label_basis": "provider_spot_mid_forward_return_not_tradable_net_return",
    }
