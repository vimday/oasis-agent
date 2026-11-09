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
earliest reproduced decision is the primary sample, whether or not the tracker
registered it; later decisions of that token are excluded and never replace it, so
price or label availability cannot select the sample. Unregistered primary samples
stay in every denominator as `not_registered` with an unknown return. `end` must
leave the 1 h horizon plus the allowed lateness before `label_cutoff`, or the sample
is right-censored.
Labels are joined by exact (token, assessed_at, action, score, horizon) identity.

Labels are provider spot-mid forward returns: not fills, not net of fees, gas,
slippage or exit capacity, which are all unobserved here. Two selected subsets are
not a randomized or causal comparison.
"""

import re
from collections import Counter
from dataclasses import dataclass, replace
from statistics import median
from typing import Any, Literal

from ..models.common import canonical_json, sha256_hex
from .opportunity import Assessment, DecisionPolicy, Market, assess, market_from_json
from .outcomes import MAX_LATENESS_SECONDS, REGISTRATION
from .spot_agent import decision_policy, policy_digest

Record = dict[str, Any]
# v3: the manifest states where its window came from (`window_basis`, `plan_sha256`).
EXPERIMENT_SCHEMA = "oasis.spot-experiment.v3"
_HEX64 = re.compile(r"[0-9a-f]{64}")
COMPARATOR = "assess_without_smart_wallet_bonus.v1"
HORIZON = 3_600
EvidenceSource = Literal["synthetic", "provider_claims", "unknown"]
WindowBasis = Literal["analysis_time_arguments", "pre_registered_plan"]
# Mutually exclusive state of one primary sample's 1 h label:
# - not_registered: the tracker never took the sample (reason in `registration`), so no
#   label can exist; its return is unknown, never zero;
# - observed: a valid observed label inside [due, due + lateness] and <= label_cutoff;
# - unobserved: the label was emitted unobserved, or is due before the cutoff but absent;
# - after_cutoff: the label could only be observed after label_cutoff (right-censored).
LABEL_STATES = ("not_registered", "observed", "unobserved", "after_cutoff")
SCOPE = {"chains": [8453], "sessions": "single_capture_session"}


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


def replay_inputs(replay: Record) -> Record:
    """The frozen evidence one capture replay names, as an experiment manifest records it."""
    return {
        "capture_session_id": replay["session_id"],
        "capture_archive_head_digest": replay["archive_head_digest"],
        "code": replay["code"],
        "run_policy_sha256": replay["run_policy_sha256"],
        "session_complete": replay["session_complete"],
        "termination": replay["termination"],
        "cycles_replayed": replay["cycles_replayed"],
    }


def _assessment_record(a: Assessment) -> tuple[str, int, list[str]]:
    return a.action, a.score_bps, list(a.reasons)


def _int(value: object) -> int | None:
    """A plain int (never bool, float or numeric string)."""
    return value if type(value) is int else None


def check_window(start: object, end: object, label_cutoff: object) -> None:
    """The window contract of an analysis-time window and of a pre-registered plan alike.

    Plain integer unix seconds (UTC), a non-empty [start, end), and a label cutoff that
    leaves the 1 h horizon plus the allowed lateness after `end`.
    """
    s, e, cutoff = _int(start), _int(end), _int(label_cutoff)
    if s is None or e is None or cutoff is None:
        raise ExperimentRefused("window_malformed")
    if not s < e:
        raise ExperimentRefused("empty_window")
    if cutoff < e + HORIZON + MAX_LATENESS_SECONDS:
        # A sample near `end` could not yet have its 1 h label: right-censored by design.
        raise ExperimentRefused("label_cutoff_before_window_horizon")


# The assessment actions `assess` can emit for a candidate (hold/exit are positions).
ASSESSED_ACTIONS = frozenset({"skip", "watch", "flip", "lp_candidate"})


def _action(value: object) -> bool:
    """A known action; the str check runs first so a JSON array/object never reaches
    the set lookup (an unhashable value would raise TypeError there)."""
    return type(value) is str and value in ASSESSED_ACTIONS


def _assessment_fields(r: Record) -> None:
    """Shape of a production assessment record; never coerced, never defaulted.

    Python equality treats 5783 == 5783.0 == True + 5782, so every compared field is
    type-checked here before any comparison can run.
    """
    token = r.get("token")
    if not isinstance(token, str):
        raise ExperimentRefused("assessment_malformed", "token")
    if _int(r.get("at")) is None or _int(r.get("score_bps")) is None:
        raise ExperimentRefused("assessment_malformed", token)
    if not _action(r.get("action")):
        raise ExperimentRefused("assessment_malformed", token)
    reasons = r.get("reasons")
    if not isinstance(reasons, list) or not all(type(x) is str for x in reasons):
        raise ExperimentRefused("assessment_malformed", token)
    if not isinstance(r.get("label_registration"), str):
        raise ExperimentRefused("registration_unknown", token)
    if not isinstance(r.get("policy_sha256"), str) or "market" not in r:
        raise ExperimentRefused("assessment_without_market_snapshot", token)


def _label_fields(r: Record) -> None:
    """Shape of a production outcome label (OutcomeTracker.label) before any use."""
    token = r.get("token")
    if not isinstance(token, str):
        raise ExperimentRefused("label_malformed", "token")
    for name in ("assessed_at", "score_bps", "horizon_seconds"):
        if _int(r.get(name)) is None:
            raise ExperimentRefused("label_malformed", token)
    if not _action(r.get("action")):
        raise ExperimentRefused("label_malformed", token)


def rebuild(records: list[Record], policy: DecisionPolicy) -> list[Sample]:
    """Every assessment, recomputed by the real `assess` and checked against the record.

    A sample's identity is (token, at) on the one chain of this run. One identity may
    appear once: the agent assesses a token at most once per cycle, so a second record
    with that identity (equal or not) is a malformed input, never an overlap.
    """
    digest = policy_digest(policy)
    out: list[Sample] = []
    seen: set[tuple[str, int]] = set()
    for r in records:
        if r.get("kind") != "assessment":
            continue
        _assessment_fields(r)
        if r["policy_sha256"] != digest:
            raise ExperimentRefused("policy_mismatch", str(r.get("token")))
        try:
            market = market_from_json(r["market"])
        except ValueError as error:
            raise ExperimentRefused("market_snapshot_invalid", str(r.get("token"))) from error
        if market.token != r.get("token") or market.observed_at != _int(r.get("at")):
            raise ExperimentRefused("assessment_identity_mismatch", str(r.get("token")))
        if (market.token, market.observed_at) in seen:
            raise ExperimentRefused("duplicate_sample_identity", market.token)
        seen.add((market.token, market.observed_at))
        if r.get("label_registration") not in REGISTRATION:
            raise ExperimentRefused("registration_unknown", market.token)
        recomputed = assess(market, policy)
        if _assessment_record(recomputed) != (r["action"], r["score_bps"], r["reasons"]):
            raise ExperimentRefused("original_rule_not_reproduced", str(r.get("token")))
        out.append(
            Sample(
                market.token,
                market.observed_at,
                market,
                recomputed,
                r["label_registration"],
            )
        )
    return out


def _labels(records: list[Record]) -> dict[tuple[str, int, int], Record]:
    """1 h labels by (token, assessed_at, horizon), under the OutcomeTracker contract.

    observed: integer observed_at in [assessed_at + 1 h, + lateness] and an integer
    forward_return_bps. unobserved: neither field. Anything else, and any duplicate,
    refuses: a label is never repaired, defaulted or coerced.
    """
    out: dict[tuple[str, int, int], Record] = {}
    for r in records:
        if r.get("kind") != "outcome":
            continue
        _label_fields(r)  # every horizon: a malformed 15 min label is still malformed
        if r["horizon_seconds"] != HORIZON:
            continue
        token, at = r["token"], r["assessed_at"]
        status = r.get("status")
        if status == "observed":
            observed_at, value = _int(r.get("observed_at")), _int(r.get("forward_return_bps"))
            if observed_at is None or value is None:
                raise ExperimentRefused("label_malformed", token)
            due = at + HORIZON
            if not due <= observed_at <= due + MAX_LATENESS_SECONDS:
                raise ExperimentRefused("label_time_outside_window", token)
        elif status == "unobserved":
            if "observed_at" in r or "forward_return_bps" in r:
                raise ExperimentRefused("label_malformed", token)
        else:
            raise ExperimentRefused("label_malformed", token)
        key = (token, at, HORIZON)
        if key in out:
            raise ExperimentRefused("duplicate_label", token)
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
    window_basis: WindowBasis = "analysis_time_arguments",
    plan_sha256: str | None = None,
) -> dict[str, Any]:
    """The comparison report. `inputs` names the frozen evidence (digests, completeness).

    `window_basis` says where the window came from: arguments given at analysis time
    (exploration; the window may have been chosen after the data was seen) or a plan
    bound into the capture before its first provider call (`spot_plan`), whose digest
    must then be given. The basis is reported; this function does not verify a plan.
    """
    if any(_int(v) is None for v in (start, end, label_cutoff)):
        raise ExperimentRefused("window_malformed")
    if evidence not in ("synthetic", "provider_claims", "unknown"):
        raise ExperimentRefused("evidence_source_unknown")
    if window_basis == "analysis_time_arguments":
        basis_ok = plan_sha256 is None
    elif window_basis == "pre_registered_plan":
        basis_ok = isinstance(plan_sha256, str) and _HEX64.fullmatch(plan_sha256) is not None
    else:
        basis_ok = False
    if not basis_ok:
        raise ExperimentRefused("window_basis_malformed")
    if not isinstance(records, list) or not all(isinstance(r, dict) for r in records):
        raise ExperimentRefused("records_malformed")
    check_window(start, end, label_cutoff)
    policy = decision_policy(config)
    samples = rebuild(records, policy)  # every assessment, before any label is read
    in_window = [s for s in samples if start <= s.at < end]
    # Fixed sample rule: per token the earliest reproduced decision in the window,
    # whatever its label registration, price or later outcome. A later decision of the
    # same token never replaces it, so label availability cannot select the sample.
    primary: dict[str, Sample] = {}
    excluded_overlap = 0
    for s in sorted(in_window, key=lambda s: (s.at, s.token)):
        if s.token in primary:
            excluded_overlap += 1
            continue
        primary[s.token] = s
    labels = _labels(records)
    # Every 1 h label must belong to exactly one rebuilt, registered assessment with the
    # same action and score, whether or not that sample is primary: a label that matches
    # no sample is a corrupted or foreign input, never ignored.
    by_identity = {(s.token, s.at): s for s in samples if s.registration == "registered"}
    # A label for a sample the tracker refused cannot exist.
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
        if s.registration != "registered":
            state = "not_registered"
        elif label is None:
            # Due before the cutoff yet absent is a missing observation, not a zero.
            state = "after_cutoff" if due_by > label_cutoff else "unobserved"
        elif label["status"] == "unobserved":
            state = "unobserved"
        elif label["observed_at"] > label_cutoff:
            state = "after_cutoff"
        else:
            state = "observed"
            observed_return = label["forward_return_bps"]
        rows.append(
            {
                "token": s.token,
                "assessed_at": s.at,
                "smart_wallets": s.market.smart_wallets,
                "original": {"action": s.original.action, "score_bps": s.original.score_bps},
                "baseline": {"action": base.action, "score_bps": base.score_bps},
                "baseline_reasons": list(base.reasons),
                "registration": s.registration,
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
            "labels": {k: states[k] for k in LABEL_STATES},
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
            "scope": SCOPE,
            "window": {"start": start, "end": end, "label_cutoff": label_cutoff},
            "window_basis": window_basis,
            "plan_sha256": plan_sha256,
            "horizon_seconds": HORIZON,
            "allowed_lateness_seconds": MAX_LATENESS_SECONDS,
            "sample_rule": "earliest_reproduced_decision_per_token_in_window",
            "label_states": list(LABEL_STATES),
            "label_join": "token+assessed_at+horizon, action and score must match",
            "trials": 1,
            "fitted_parameters": 0,
        },
        "population": {
            "assessments_rebuilt": len(samples),
            "original_rule_reproduced": True,
            # in_window = primary_samples + excluded_later_same_token (one per token).
            "in_window": len(in_window),
            "primary_samples": len(rows),
            "excluded_later_same_token": excluded_overlap,
            "unique_tokens": len(primary),
            "primary_registration": {
                k: sum(r["registration"] == k for r in rows) for k in REGISTRATION
            },
        },
        "agreement_matrix": dict(sorted(matrix.items())),
        # Every primary sample, so each count below can be recomputed by hand.
        "rows": rows,
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
