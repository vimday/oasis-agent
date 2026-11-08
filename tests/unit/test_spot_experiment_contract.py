"""O3 contract: the earliest decision is primary regardless of registration, and
sample identity and 1 h labels obey the OutcomeTracker contract exactly.

Synthetic fixtures through run_loop -> capture -> replay; they validate the tool only.
"""

import json
import sys
from pathlib import Path
from typing import Any

import pytest

import scripts.run_spot_experiment as experiment_cli
from oasis_agent.launch.spot_capture import replay_session
from oasis_agent.launch.spot_experiment import HORIZON, ExperimentRefused

from .test_spot_agent import NOW, TOKEN
from .test_spot_capture_replay import capture_run
from .test_spot_experiment import CUTOFF, END, QUIET, Market3, experiment


class PriceLater(Market3):
    """TOKEN has no reference price in cycle 1 (registration fails), then recovers."""

    def __init__(self) -> None:
        super().__init__()
        self.cycle = 1

    def __call__(self, argv: Any) -> bytes:
        argv = tuple(argv)
        if self.cycle == 1 and argv[:2] == ("token", "price-info") and TOKEN in argv:
            missing = {k: v for k, v in self.price.items() if k != "price"}
            return json.dumps({"ok": True, "data": [missing]}).encode()
        return super().__call__(argv)


def price_later_run(tmp_path: Path) -> dict[str, Any]:
    cli = PriceLater()

    def step(cycle: int, now: int) -> None:
        cli.cycle, cli.now = cycle, now

    run = tmp_path / "run"
    capture_run(run, cli, ticks=8, interval=900, each_cycle=step)
    return replay_session(run / "capture.db", run / "raw")


def first_of(records: list[dict[str, Any]], token: str) -> dict[str, Any]:
    return next(r for r in records if r["kind"] == "assessment" and r["token"] == token)


def test_unregistered_first_decision_stays_primary(tmp_path: Path) -> None:
    replay = price_later_run(tmp_path)
    first = first_of(replay["records"], TOKEN)
    assert first["label_registration"] == "no_reference_price" and first["at"] == NOW
    later = [
        r
        for r in replay["records"]
        if r["kind"] == "assessment" and r["token"] == TOKEN and r["at"] > NOW
    ]
    assert any(r["label_registration"] == "registered" for r in later)
    report = experiment(replay["records"])
    rows = {r["token"]: r for r in report["rows"]}
    assert rows[TOKEN]["assessed_at"] == NOW  # never replaced by the later, priced one
    assert rows[TOKEN]["registration"] == "no_reference_price"
    assert rows[TOKEN]["label"] == "not_registered"
    assert rows[TOKEN]["forward_return_bps"] is None  # unknown, never 0
    population = report["population"]
    assert population["primary_registration"]["no_reference_price"] == 1
    # Every group's label states sum to its samples, unregistered included.
    for name, group in report["groups"].items():
        assert sum(group["labels"].values()) == group["samples"], name
    assert report["groups"]["all_primary"]["labels"]["not_registered"] == 1
    # The unregistered sample still enters the agreement matrix.
    assert sum(report["agreement_matrix"].values()) == population["primary_samples"]


def test_negative_control_registered_first_rule_would_pick_a_later_sample(
    tmp_path: Path,
) -> None:
    """The old registered-first rule selects a different TOKEN sample: the test above
    would fail under it, so it really guards the rule."""
    replay = price_later_run(tmp_path)
    assessed = [r for r in replay["records"] if r["kind"] == "assessment" and r["token"] == TOKEN]
    registered_first = min(r["at"] for r in assessed if r["label_registration"] == "registered")
    report = experiment(replay["records"])
    chosen = {r["token"]: r["assessed_at"] for r in report["rows"]}[TOKEN]
    assert registered_first != chosen == NOW


def test_future_labels_and_recovered_price_do_not_change_primary_identity(
    tmp_path: Path,
) -> None:
    replay = price_later_run(tmp_path)
    base = experiment(replay["records"])

    def inverted(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out = []
        for r in records:
            if r["kind"] == "outcome" and r.get("status") == "observed":
                out.append(r | {"forward_return_bps": -r["forward_return_bps"] - 777})
            else:
                out.append(r)
        return out

    other = experiment(inverted(replay["records"]))
    key = [(r["token"], r["assessed_at"], r["registration"]) for r in base["rows"]]
    assert key == [(r["token"], r["assessed_at"], r["registration"]) for r in other["rows"]]
    assert other["population"] == base["population"]


def edit_first(
    records: list[dict[str, Any]], kind: str, token: str, change: Any
) -> list[dict[str, Any]]:
    out, done = [], False
    for r in records:
        if not done and r["kind"] == kind and r.get("token") == token:
            out.append(change(r))
            done = True
        else:
            out.append(r)
    return out


def hour_label(records: list[dict[str, Any]], status: str = "observed") -> dict[str, Any]:
    return next(
        r
        for r in records
        if r["kind"] == "outcome" and r["horizon_seconds"] == HORIZON and r["status"] == status
    )


def replace_record(
    records: list[dict[str, Any]], old: dict[str, Any], new: dict[str, Any]
) -> list[dict[str, Any]]:
    index = records.index(old)
    return [*records[:index], new, *records[index + 1 :]]


@pytest.fixture(scope="module")
def frozen_records(tmp_path_factory: pytest.TempPathFactory) -> list[dict[str, Any]]:
    replay = price_later_run(tmp_path_factory.mktemp("contract"))
    records: list[dict[str, Any]] = replay["records"]
    return records


def test_duplicate_and_conflicting_sample_identities_refuse(
    frozen_records: list[dict[str, Any]],
) -> None:
    first = first_of(frozen_records, QUIET)
    with pytest.raises(ExperimentRefused, match="duplicate_sample_identity"):
        experiment([*frozen_records, first])
    renamed = first | {"market": first["market"] | {"symbol": "OTHER"}, "symbol": "OTHER"}
    with pytest.raises(ExperimentRefused, match="duplicate_sample_identity"):
        experiment([*frozen_records, renamed])
    # Out of order: the duplicate placed before the original still refuses.
    with pytest.raises(ExperimentRefused, match="duplicate_sample_identity"):
        experiment([renamed, *frozen_records])


@pytest.mark.parametrize(
    ("change", "category"),
    [
        (lambda r: r | {"label_registration": "maybe"}, "registration_unknown"),
        (lambda r: r | {"label_registration": None}, "registration_unknown"),
        (lambda r: r | {"at": float(r["at"])}, "assessment_identity_mismatch"),
        (lambda r: r | {"at": str(r["at"])}, "assessment_identity_mismatch"),
        (lambda r: r | {"score_bps": True}, "assessment_malformed"),
        (lambda r: r | {"reasons": "x"}, "assessment_malformed"),
    ],
)
def test_malformed_assessment_fields_refuse(
    frozen_records: list[dict[str, Any]], change: Any, category: str
) -> None:
    with pytest.raises(ExperimentRefused, match=category):
        experiment(edit_first(frozen_records, "assessment", QUIET, change))


def early(r: dict[str, Any]) -> dict[str, Any]:
    return r | {"observed_at": r["assessed_at"] + HORIZON - 1}


def too_late(r: dict[str, Any]) -> dict[str, Any]:
    return r | {"observed_at": r["assessed_at"] + HORIZON + 601}


def without(*keys: str) -> Any:
    return lambda r: {k: v for k, v in r.items() if k not in keys}


@pytest.mark.parametrize(
    ("change", "category"),
    [
        (early, "label_time_outside_window"),
        (too_late, "label_time_outside_window"),
        (without("observed_at"), "label_malformed"),
        (without("forward_return_bps"), "label_malformed"),
        (lambda r: r | {"forward_return_bps": 12.5}, "label_malformed"),
        (lambda r: r | {"forward_return_bps": "12345"}, "label_malformed"),
        (lambda r: r | {"forward_return_bps": True}, "label_malformed"),
        (lambda r: r | {"observed_at": float(r["observed_at"])}, "label_malformed"),
        (lambda r: r | {"assessed_at": str(r["assessed_at"])}, "label_malformed"),
        (lambda r: r | {"status": "maybe"}, "label_malformed"),
        (lambda r: r | {"horizon_seconds": 3600.0}, "label_malformed"),
    ],
)
def test_observed_label_contract(
    frozen_records: list[dict[str, Any]], change: Any, category: str
) -> None:
    label = hour_label(frozen_records)
    with pytest.raises(ExperimentRefused, match=category):
        experiment(replace_record(frozen_records, label, change(label)))


def test_unobserved_label_must_not_carry_a_time_or_return(
    frozen_records: list[dict[str, Any]],
) -> None:
    label = hour_label(frozen_records)
    as_unobserved = without("observed_at", "forward_return_bps")(label) | {"status": "unobserved"}
    # The legitimate unobserved shape is accepted ...
    experiment(replace_record(frozen_records, label, as_unobserved))
    # ... but an unobserved label smuggling a return is not.
    smuggled = as_unobserved | {"forward_return_bps": 12_345}
    with pytest.raises(ExperimentRefused, match="label_malformed"):
        experiment(replace_record(frozen_records, label, smuggled))


def test_window_boundaries_are_inclusive_of_the_lateness_allowance(
    frozen_records: list[dict[str, Any]],
) -> None:
    label = hour_label(frozen_records)
    due = label["assessed_at"] + HORIZON
    for observed_at in (due, due + 600):
        experiment(replace_record(frozen_records, label, label | {"observed_at": observed_at}))


def test_cli_scope_and_determinism(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cli = PriceLater()

    def step(cycle: int, now: int) -> None:
        cli.cycle, cli.now = cycle, now

    run = tmp_path / "run"
    capture_run(run, cli, ticks=8, interval=900, each_cycle=step)
    reports = []
    for name in ("a.json", "b.json"):
        argv = [
            "x",
            "--run",
            str(run),
            "--start",
            str(NOW),
            "--end",
            str(END),
            "--label-cutoff",
            str(CUTOFF),
            "--evidence",
            "synthetic",
            "--out",
            str(tmp_path / name),
        ]
        monkeypatch.setattr(sys, "argv", argv)
        assert experiment_cli.main() == 0
        reports.append(json.loads((tmp_path / name).read_bytes()))
    assert reports[0] == reports[1]
    manifest = reports[0]["manifest"]
    assert manifest["scope"] == {"chains": [8453], "sessions": "single_capture_session"}
    assert manifest["sample_rule"] == "earliest_reproduced_decision_per_token_in_window"
    assert reports[0]["population"]["primary_samples"] == 3
