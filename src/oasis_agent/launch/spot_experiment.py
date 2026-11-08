"""Offline single-feature experiment on frozen spot decisions: the smart-wallet bonus.

Question. On the very same frozen assessment inputs, which decisions change if the
positive smart-wallet confluence term is removed from `assess`, and how do provider
forward-return labels of the two selections compare? The comparator is fixed:

    baseline(m) = assess(replace(m, smart_wallets=0), same policy)

No weight is renormalized and no threshold moves. Every hard blocker, LP routing,
`smart_money_exiting` and provider-disagreement rule is untouched, because they read
other fields. This measures the contribution of one score term only; it is not an
ablation of the signal source (signals also nominate tokens and set smart_sold_bps).

Inputs. Records produced by the production path: `assessment` records that carry the
exact `market` snapshot and `policy_sha256` (see SpotAgent._candidate), and `outcome`
labels. They are read from a capture replay (preferred: decisions re-derived offline)
or from a verified run journal. Before any comparison every assessment is recomputed
with the real `assess` and must equal the recorded action, score and reasons; any
mismatch refuses the whole experiment.

Sample rule (fixed before labels are read). Within [start, end), per token only the
earliest assessment with a registered label sample is the primary sample; later ones
are excluded as overlapping, whatever their labels. `end` must leave the 1 h horizon
plus the allowed lateness before `label_cutoff`, or the sample is right-censored.
Labels are joined by exact (token, assessed_at, action, score, horizon) identity.

Labels are provider spot-mid forward returns: not fills, not net of fees, gas,
slippage or exit capacity, which are all unobserved here. Two selected subsets are
not a randomized or causal comparison.
"""

from collections import Counter
from dataclasses import dataclass, replace
from statistics import median
from typing import Any, Literal

from ..models.common import canonical_json, sha256_hex
from .opportunity import Assessment, DecisionPolicy, Market, assess, market_from_json
from .outcomes import MAX_LATENESS_SECONDS
from .spot_agent import decision_policy, policy_digest

Record = dict[str, Any]
EXPERIMENT_SCHEMA = "oasis.spot-experiment.v1"
COMPARATOR = "assess_without_smart_wallet_bonus.v1"
HORIZON = 3_600
EvidenceSource = Literal["synthetic", "provider_claims", "unknown"]
LABEL_STATES = ("observed", "unobserved", "not_due", "not_registered")


class ExperimentRefused(Exception):
    def __init__(self, category: str, detail: str = ""):
        super().__init__(
            f"spot experiment refused: {category}" + (f" ({detail})" if detail else "")
        )
        self.category = category


def baseline(m: Market, policy: DecisionPolicy) -> Assessment:
    """The fixed comparator: the same rule with no smart-wallet confluence points."""
    return assess(replace(m, smart_wallets=0), policy)


@dataclass(frozen=True, slots=True)
class Sample:
    token: str
    at: int
    market: Market
    original: Assessment
    registration: str


def _assessment_record(a: Assessment) -> tuple[str, int, list[str]]:
    return a.action, a.score_bps, list(a.reasons)


def rebuild(records: list[Record], policy: DecisionPolicy) -> list[Sample]:
    """Every assessment, recomputed by the real `assess` and checked against the record."""
    digest = policy_digest(policy)
    out: list[Sample] = []
    for r in records:
        if r.get("kind") != "assessment":
            continue
        if "market" not in r or "policy_sha256" not in r:
            raise ExperimentRefused("assessment_without_market_snapshot", str(r.get("token")))
        if r["policy_sha256"] != digest:
            raise ExperimentRefused("policy_mismatch", str(r.get("token")))
        try:
            market = market_from_json(r["market"])
        except ValueError as error:
            raise ExperimentRefused("market_snapshot_invalid", str(r.get("token"))) from error
        if market.token != r.get("token") or market.observed_at != r.get("at"):
            raise ExperimentRefused("assessment_identity_mismatch", str(r.get("token")))
        recomputed = assess(market, policy)
        if _assessment_record(recomputed) != (r["action"], r["score_bps"], r["reasons"]):
            raise ExperimentRefused("original_rule_not_reproduced", str(r.get("token")))
        out.append(
            Sample(
                market.token,
                market.observed_at,
                market,
                recomputed,
                str(r.get("label_registration")),
            )
        )
    return out


def _labels(records: list[Record]) -> dict[tuple[str, int, int], Record]:
    """1 h labels by (token, assessed_at, horizon); a conflicting duplicate refuses."""
    out: dict[tuple[str, int, int], Record] = {}
    for r in records:
        if r.get("kind") != "outcome" or r.get("horizon_seconds") != HORIZON:
            continue
        key = (str(r.get("token")), int(r["assessed_at"]), HORIZON)
        if key in out:
            raise ExperimentRefused("duplicate_label", key[0])
        out[key] = r
    return out


def _distribution(values: list[int]) -> dict[str, Any]:
    if not values:
        return {"n": 0}
    ordered = sorted(values)
    return {
        "n": len(values),
        "median_bps": int(median(ordered)),
        "p10_bps": ordered[len(ordered) // 10],
        "p90_bps": ordered[min(len(ordered) - 1, len(ordered) * 9 // 10)],
        "positive_share_bps": sum(v > 0 for v in values) * 10_000 // len(values),
    }


def run_experiment(
    records: list[Record],
    config: DecisionPolicy,
    *,
    start: int,
    end: int,
    label_cutoff: int,
    evidence: EvidenceSource,
    inputs: Record,
) -> dict[str, Any]:
    """The comparison report. `inputs` names the frozen evidence (digests, completeness)."""
    if not start < end:
        raise ExperimentRefused("empty_window")
    if label_cutoff < end + HORIZON + MAX_LATENESS_SECONDS:
        # A sample near `end` could not yet have its 1 h label: right-censored by design.
        raise ExperimentRefused("label_cutoff_before_window_horizon")
    policy = decision_policy(config)
    samples = rebuild(records, policy)  # every assessment, before any label is read
    in_window = [s for s in samples if start <= s.at < end]
    # Fixed sample rule, using only decision-time fields.
    primary: dict[str, Sample] = {}
    unregistered: list[Sample] = []
    excluded_overlap = 0
    for s in sorted(in_window, key=lambda s: (s.at, s.token)):
        if s.registration != "registered":
            unregistered.append(s)
            continue
        if s.token in primary:
            excluded_overlap += 1
            continue
        primary[s.token] = s
    labels = _labels(records)
    # Every 1 h label must belong to exactly one rebuilt, registered assessment with the
    # same action and score, whether or not that sample is primary: a label that matches
    # no sample is a corrupted or foreign input, never ignored.
    by_identity = {(s.token, s.at): s for s in samples if s.registration == "registered"}
    for (token, at, _), joined in labels.items():
        owner = by_identity.get((token, at))
        if owner is None:
            raise ExperimentRefused("label_without_sample", token)
        if (joined.get("action"), joined.get("score_bps")) != (
            owner.original.action,
            owner.original.score_bps,
        ):
            raise ExperimentRefused("label_identity_mismatch", token)
    rows: list[Record] = []
    for s in primary.values():
        base = baseline(s.market, policy)
        label = labels.get((s.token, s.at, HORIZON))
        due_by = s.at + HORIZON
        observed_return: int | None = None
        if label is None:
            state = "not_due" if due_by > label_cutoff else "unobserved"
        else:
            if int(label.get("observed_at", due_by)) > label_cutoff:
                state = "not_due"  # observed only after the cutoff: unusable here
            else:
                state = "observed" if label["status"] == "observed" else "unobserved"
                if state == "observed":
                    observed_return = int(label["forward_return_bps"])
        rows.append(
            {
                "token": s.token,
                "assessed_at": s.at,
                "smart_wallets": s.market.smart_wallets,
                "original": {"action": s.original.action, "score_bps": s.original.score_bps},
                "baseline": {"action": base.action, "score_bps": base.score_bps},
                "baseline_reasons": list(base.reasons),
                "label": state,
                "forward_return_bps": observed_return,
            }
        )
    matrix = Counter(f"{r['original']['action']}->{r['baseline']['action']}" for r in rows)

    def group(selected: list[Record]) -> dict[str, Any]:
        states = Counter(r["label"] for r in selected)
        returns = [r["forward_return_bps"] for r in selected if r["label"] == "observed"]
        return {
            "samples": len(selected),
            "labels": {k: states[k] for k in LABEL_STATES if k != "not_registered"},
            "forward_return_bps": _distribution(returns),
        }

    original_flip = [r for r in rows if r["original"]["action"] == "flip"]
    baseline_flip = [r for r in rows if r["baseline"]["action"] == "flip"]
    both = [r for r in original_flip if r["baseline"]["action"] == "flip"]
    only_original = [r for r in original_flip if r["baseline"]["action"] != "flip"]
    only_baseline = [r for r in baseline_flip if r["original"]["action"] != "flip"]
    report = {
        "schema": EXPERIMENT_SCHEMA,
        "execution_eligible": False,
        "manifest": {
            "comparator": COMPARATOR,
            "policy_sha256": policy_digest(policy),
            "evidence_source": evidence,
            "inputs": inputs,
            "window": {"start": start, "end": end, "label_cutoff": label_cutoff},
            "horizon_seconds": HORIZON,
            "allowed_lateness_seconds": MAX_LATENESS_SECONDS,
            "sample_rule": "earliest_registered_assessment_per_token_in_window",
            "label_join": "token+assessed_at+horizon, action and score must match",
            "trials": 1,
            "fitted_parameters": 0,
        },
        "population": {
            "assessments_rebuilt": len(samples),
            "original_rule_reproduced": True,
            "in_window": len(in_window),
            "not_registered": len(unregistered),
            "excluded_overlapping_same_token": excluded_overlap,
            "primary_samples": len(rows),
            "unique_tokens": len(primary),
        },
        "agreement_matrix": dict(sorted(matrix.items())),
        "disagreements": [r for r in rows if r["original"]["action"] != r["baseline"]["action"]],
        "groups": {
            "original_flip": group(original_flip),
            "baseline_flip": group(baseline_flip),
            "both_flip": group(both),
            "original_only_flip": group(only_original),
            "baseline_only_flip": group(only_baseline),
            "all_primary": group(rows),
        },
        "limits": {
            "label_basis": "provider_spot_mid_forward_return_not_fill",
            "fees_gas_slippage_exit_capacity": "unknown",
            "net_return": "not_measured",
            "comparison": "descriptive_selected_subsets_not_causal_or_randomized",
            "out_of_sample": "not_claimed_single_fixed_comparison_no_fit",
            "scope": "smart_wallet_score_term_only_not_signal_source_ablation",
        },
    }
    report["report_sha256"] = sha256_hex(canonical_json(report))
    return report
