"""Every compared experiment field is strictly typed; malformed input refuses with a
stable category, never a KeyError/TypeError, a coercion or an equal-looking number.

Records come from a real run_loop -> capture -> replay (synthetic provider fixtures).
These are checks on the experiment API's inputs, not a claimed attack on capture.
"""

from typing import Any

import pytest

from oasis_agent.launch.spot_agent import AgentConfig
from oasis_agent.launch.spot_experiment import HORIZON, ExperimentRefused, run_experiment

from .test_spot_agent import NOW
from .test_spot_experiment import CUTOFF, END, QUIET, experiment
from .test_spot_experiment_contract import hour_label, price_later_run, replace_record

MISSING = object()


@pytest.fixture(scope="module")
def records(tmp_path_factory: pytest.TempPathFactory) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = price_later_run(tmp_path_factory.mktemp("fields"))["records"]
    return out


def mutated(r: dict[str, Any], field: str, value: object) -> dict[str, Any]:
    if value is MISSING:
        return {k: v for k, v in r.items() if k != field}
    return r | {field: value}


def typed(original: object, kind: object) -> object:
    """Swap a field for a value Python treats as equal, but of another type."""
    if kind == "FLOAT":
        return float(original)  # type: ignore[arg-type]
    if kind == "BOOL":
        return bool(original)
    if kind == "STR":
        return str(original)
    return kind


def assessment(records: list[dict[str, Any]], token: str = QUIET) -> dict[str, Any]:
    return next(r for r in records if r["kind"] == "assessment" and r["token"] == token)


def refused(records: list[dict[str, Any]], **window: Any) -> str:
    with pytest.raises(ExperimentRefused) as error:
        experiment(records, **window)
    return error.value.category


ASSESSMENT_CASES = [
    ("action", MISSING, "assessment_malformed"),
    ("action", None, "assessment_malformed"),
    ("action", "buy", "assessment_malformed"),
    ("action", 1, "assessment_malformed"),
    ("score_bps", MISSING, "assessment_malformed"),
    ("score_bps", "FLOAT", "assessment_malformed"),
    ("score_bps", "BOOL", "assessment_malformed"),
    ("score_bps", "STR", "assessment_malformed"),
    ("reasons", MISSING, "assessment_malformed"),
    ("reasons", ("a",), "assessment_malformed"),
    ("reasons", [1], "assessment_malformed"),
    ("token", MISSING, "assessment_malformed"),
    ("token", 7, "assessment_malformed"),
    ("at", MISSING, "assessment_malformed"),
    ("at", "FLOAT", "assessment_malformed"),
    ("at", "BOOL", "assessment_malformed"),
    ("label_registration", MISSING, "registration_unknown"),
    ("label_registration", 1, "registration_unknown"),
    ("label_registration", "maybe", "registration_unknown"),
    ("policy_sha256", MISSING, "assessment_without_market_snapshot"),
    ("policy_sha256", 1, "assessment_without_market_snapshot"),
    ("market", MISSING, "assessment_without_market_snapshot"),
    ("market", "x", "market_snapshot_invalid"),
]


@pytest.mark.parametrize(("field", "value", "category"), ASSESSMENT_CASES)
def test_assessment_field_matrix(
    records: list[dict[str, Any]], field: str, value: object, category: str
) -> None:
    original = assessment(records)
    new = mutated(original, field, typed(original.get(field), value))
    assert refused(replace_record(records, original, new)) == category


LABEL_CASES = [
    ("score_bps", "FLOAT", "label_malformed"),  # equal value, wrong type
    ("score_bps", "BOOL", "label_malformed"),
    ("score_bps", "STR", "label_malformed"),
    ("score_bps", MISSING, "label_malformed"),
    ("action", MISSING, "label_malformed"),
    ("action", "buy", "label_malformed"),
    ("assessed_at", "FLOAT", "label_malformed"),
    ("assessed_at", MISSING, "label_malformed"),
    ("horizon_seconds", "FLOAT", "label_malformed"),
    ("horizon_seconds", MISSING, "label_malformed"),
    ("token", MISSING, "label_malformed"),
    ("status", MISSING, "label_malformed"),
    ("observed_at", "FLOAT", "label_malformed"),
    ("observed_at", MISSING, "label_malformed"),
    ("forward_return_bps", "FLOAT", "label_malformed"),
    ("forward_return_bps", "BOOL", "label_malformed"),
    ("forward_return_bps", MISSING, "label_malformed"),
]


@pytest.mark.parametrize(("field", "value", "category"), LABEL_CASES)
def test_label_field_matrix(
    records: list[dict[str, Any]], field: str, value: object, category: str
) -> None:
    label = hour_label(records)
    new = mutated(label, field, typed(label.get(field), value))
    assert refused(replace_record(records, label, new)) == category


def test_reviewer_counterexamples_are_refused(records: list[dict[str, Any]]) -> None:
    label = hour_label(records)
    # 1. outcome.score_bps as an equal float (5783 == 5783.0 in Python).
    equal_float = label | {"score_bps": float(label["score_bps"])}
    assert refused(replace_record(records, label, equal_float)) == "label_malformed"
    # Score 0 versus False (0 == False in Python): the int is a well-formed label whose
    # score disagrees with its sample; the bool must be refused as malformed first.
    as_int_zero = label | {"score_bps": 0}
    assert refused(replace_record(records, label, as_int_zero)) == "label_identity_mismatch"
    as_false = label | {"score_bps": False}
    assert refused(replace_record(records, label, as_false)) == "label_malformed"
    # 2. assessment.action deleted: a stable refusal, not a KeyError.
    original = assessment(records)
    no_action = mutated(original, "action", MISSING)
    assert refused(replace_record(records, original, no_action)) == "assessment_malformed"
    # 3. A float (or bool) window bound.
    for field in ("start", "end", "label_cutoff"):
        window: dict[str, Any] = {"start": NOW, "end": END, "label_cutoff": CUTOFF}
        window[field] = float(window[field])
        assert refused(records, **window) == "window_malformed"
    assert refused(records, start=True) == "window_malformed"


def test_malformed_label_at_another_horizon_still_refuses(
    records: list[dict[str, Any]],
) -> None:
    fifteen = next(r for r in records if r["kind"] == "outcome" and r["horizon_seconds"] != HORIZON)
    broken = fifteen | {"score_bps": float(fifteen["score_bps"])}
    assert refused(replace_record(records, fifteen, broken)) == "label_malformed"


def test_container_and_evidence_inputs_refuse() -> None:
    def run(records: Any, evidence: Any = "synthetic") -> str:
        with pytest.raises(ExperimentRefused) as error:
            run_experiment(
                records,
                AgentConfig(),
                start=NOW,
                end=END,
                label_cutoff=CUTOFF,
                evidence=evidence,
                inputs={},
            )
        return error.value.category

    assert run([], evidence="real") == "evidence_source_unknown"
    assert run([1]) == "records_malformed"
    assert run(({"kind": "x"},)) == "records_malformed"


def test_valid_controls_still_pass_and_are_deterministic(
    records: list[dict[str, Any]],
) -> None:
    report = experiment(records)
    assert experiment(records) == report
    population = report["population"]
    assert population["primary_samples"] == 3
    assert population["primary_registration"]["no_reference_price"] == 1
    states = report["groups"]["all_primary"]["labels"]
    assert states["not_registered"] == 1 and states["observed"] == 2
    assert (
        population["in_window"]
        == population["primary_samples"] + population["excluded_later_same_token"]
    )
    # Legitimate unobserved labels and the exact time boundaries are still accepted.
    label = hour_label(records)
    unobserved = {k: v for k, v in label.items() if k not in ("observed_at", "forward_return_bps")}
    experiment(replace_record(records, label, unobserved | {"status": "unobserved"}))
    due = label["assessed_at"] + HORIZON
    for observed_at in (due, due + 600):
        experiment(replace_record(records, label, label | {"observed_at": observed_at}))
