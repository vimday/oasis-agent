"""Pre-registered acceptance of a shadow spot experiment: a plan bound before capture.

Scripted providers run through the real shadow run_loop, capture, replay and the fixed
no-smart-wallet-bonus experiment. These fixtures validate the plan and acceptance
contract only; they say nothing about real markets.
"""

import hashlib
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any

import pytest

import oasis_agent.launch.spot_plan as spot_plan
import scripts.run_spot_agent as runner
import scripts.run_spot_experiment as experiment_cli
from oasis_agent.launch.onchainos_source import paced
from oasis_agent.launch.spot_agent import AgentConfig
from oasis_agent.launch.spot_capture import code_identity, replay_session
from oasis_agent.launch.spot_experiment import (
    COMPARATOR,
    EXPERIMENT_SCHEMA,
    ExperimentRefused,
    run_experiment,
)
from oasis_agent.launch.spot_plan import (
    PLAN_SCHEMA,
    PlanRefused,
    accept_plan,
    binding,
    parse_plan,
    plan_digest,
    validate_plan,
)
from oasis_agent.launch.spot_report import read_journal
from oasis_agent.models.common import canonical_json, sha256_hex

from .test_spot_agent import NOW
from .test_spot_capture_replay import SHADOW, archive_records, capture_run, rewrite
from .test_spot_capture_termination import fail_capture
from .test_spot_experiment import Market3, advance

# The window opens after the plan is bound (the session starts at NOW); the label cutoff
# is the earliest the experiment allows (end + 1 h horizon + 600 s lateness).
START, END, CUTOFF = NOW + 600, NOW + 7_200, NOW + 11_400
# Cycles start about 900 s apart from NOW; the 14th (~NOW + 11 700) is past the cutoff.
INTERVAL, TICKS = 900, 14


def plan_data(**window: Any) -> dict[str, Any]:
    frame = {"start": START, "end": END, "label_cutoff": CUTOFF} | window
    return {"schema": PLAN_SCHEMA, "comparator": COMPARATOR, "window": frame}


PLAN = validate_plan(plan_data())
OTHER = validate_plan(plan_data(end=END + 900, label_cutoff=CUTOFF + 900))


class Flaky(Market3):
    """Market3 whose discovery fails (provider error) or whose tick raises, by cycle."""

    def __init__(
        self,
        discovery_fails: frozenset[int] = frozenset(),
        raises: frozenset[int] = frozenset(),
    ) -> None:
        super().__init__()
        self.cycle = 1
        self.discovery_fails, self.raises = discovery_fails, raises

    def __call__(self, argv: Any) -> bytes:
        if tuple(argv)[:2] == ("token", "hot-tokens"):
            if self.cycle in self.raises:
                raise OSError("pipe closed")  # raises out of tick: a rolled-back cycle
            if self.cycle in self.discovery_fails:
                return b'{"ok":false,"error":"x"}'  # SourceFailure: discovery_failed
        return super().__call__(argv)


def research_run(
    run: Path, *, plan: Any = PLAN, ticks: int = TICKS, cli: Market3 | None = None, **kw: Any
) -> dict[str, Any]:
    """The real run_loop as a research run: the session starts through start_capture."""
    market = Flaky() if cli is None else cli

    def step(cycle: int, now: int) -> None:
        market.cycle, market.now = cycle, now

    return capture_run(
        run, market, ticks=ticks, interval=INTERVAL, each_cycle=step, plan=plan, **kw
    )


def accept(run: Path, plan: Any = PLAN, session: str | None = None) -> dict[str, Any]:
    return accept_plan(
        run / "capture.db", run / "raw", plan, session_id=session, evidence="synthetic"
    )


def refused(run: Path, plan: Any = PLAN, session: str | None = None) -> str:
    with pytest.raises(PlanRefused) as error:
        accept(run, plan, session)
    return error.value.category


def evidence_digests(run: Path) -> dict[str, str]:
    files = [run / "capture.db", run / "journal.db", *sorted((run / "raw").iterdir())]
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in files}


def write_plan(path: Path, data: Any) -> Path:
    path.write_text(json.dumps(data))
    return path


def experiment(monkeypatch: pytest.MonkeyPatch, *argv: str) -> int:
    monkeypatch.setattr(sys, "argv", ["experiment", *argv])
    return experiment_cli.main()


def accept_cli(
    monkeypatch: pytest.MonkeyPatch, run: Path, plan: Path, out: Path, *extra: str
) -> int:
    argv = ["--run", str(run), "--plan", str(plan), "--evidence", "synthetic"]
    return experiment(monkeypatch, *argv, "--out", str(out), *extra)


def copied(run: Path, target: Path) -> Path:
    shutil.copytree(run, target)
    return target


def phase_kinds(document: dict[str, Any]) -> dict[str, dict[str, int]]:
    """The acceptance record's non-zero cycle counts, by phase and kind."""
    phases = document["observation"]["cycles_by_phase"]
    return {p: {k: n for k, n in kinds.items() if n} for p, kinds in phases.items()}


def edit_start(change: Any) -> Any:
    """An archive edit of the first session's start record (re-chained by `rewrite`)."""

    def edit(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        records[0] = change(records[0])
        return records

    return edit


@pytest.fixture(scope="module")
def planned(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One complete research session under PLAN; tests that edit it work on a copy."""
    run = tmp_path_factory.mktemp("planned") / "run"
    assert research_run(run)["code"] == 0
    return run


@pytest.fixture(scope="module")
def plain(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A shadow session started without a plan, as every archive before plans was."""
    run = tmp_path_factory.mktemp("plain") / "run"
    cli = Market3()
    capture_run(run, cli, ticks=8, interval=INTERVAL, each_cycle=advance(cli))
    return run


def test_plan_file_is_strict_and_reuses_the_experiment_window_contract(tmp_path: Path) -> None:
    assert parse_plan(canonical_json(plan_data())) == PLAN
    # Layout is not identity: what is bound and digested is the normalized plan.
    spaced = json.dumps(plan_data(), indent=2).encode()
    assert plan_digest(parse_plan(spaced)) == plan_digest(PLAN)
    assert plan_digest(PLAN) == sha256_hex(canonical_json(plan_data()))
    refusals = {
        b"{": "plan_not_json",
        b"\xff": "plan_not_json",
        b"[]": "plan_malformed",
        b'{"schema": "a", "schema": "b"}': "plan_malformed",
        json.dumps(plan_data() | {"note": 1}).encode(): "plan_malformed",
        json.dumps(plan_data() | {"schema": "oasis.spot-experiment-plan.v0"}).encode(): (
            "plan_malformed"
        ),
        json.dumps(plan_data(start=float(START))).encode(): "plan_malformed",
        json.dumps(plan_data(start=True)).encode(): "plan_malformed",
        json.dumps(plan_data(start=str(START))).encode(): "plan_malformed",
        json.dumps(plan_data() | {"comparator": "assess.v2"}).encode(): "comparator_not_fixed",
        json.dumps(plan_data(end=START)).encode(): "empty_window",
        json.dumps(plan_data(label_cutoff=CUTOFF - 1)).encode(): (
            "label_cutoff_before_window_horizon"
        ),
        b" " * (spot_plan.PLAN_FILE_LIMIT + 1): "plan_too_large",
    }
    for raw, category in refusals.items():
        with pytest.raises(PlanRefused) as error:
            parse_plan(raw)
        assert error.value.category == category, raw[:60]
    with pytest.raises(PlanRefused) as error:
        spot_plan.read_plan(tmp_path / "missing.json")
    assert error.value.category == "plan_unreadable"


def test_research_run_binds_the_plan_ahead_of_every_provider_call(
    planned: Path, plain: Path
) -> None:
    records = archive_records(planned)
    start = records[0]
    assert start["kind"] == "session_start"
    assert {k: start[k] for k in ("plan", "plan_sha256")} == binding(PLAN)
    # The same record already carries code, strategy config and run policy identity.
    assert start["code"] == code_identity()
    assert start["config"] == AgentConfig().model_dump(mode="json")
    assert start["run_policy_sha256"] == sha256_hex(canonical_json(SHADOW))
    calls = [r for r in records if r["kind"] == "call"]
    assert calls and all(c["seq"] > start["seq"] for c in calls)
    assert start["wall_ms"] <= min(c["started_ms"] for c in calls)
    assert start["wall_ms"] < START * 1000
    # An ordinary shadow session's start record is unchanged: no plan keys at all.
    assert not {"plan", "plan_sha256"} & set(archive_records(plain)[0])


def test_a_research_run_that_cannot_bind_its_plan_never_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fail_capture(monkeypatch, lambda kind, payload: kind == "session_start")
    cli = Flaky()
    result = research_run(tmp_path / "run", cli=cli)
    assert result["code"] == 2 and result["refusal"] == "plan_not_bound"
    assert cli.calls == [] and result["run_records"] == []
    assert archive_records(tmp_path / "run") == []
    # An ordinary shadow run under the same loss keeps running (evidence loss alone never
    # changes it); its archive just stays empty.
    plain_cli = Market3()
    assert research_run(tmp_path / "plain", plan=None, ticks=1, cli=plain_cli)["code"] == 0
    assert plain_cli.calls and archive_records(tmp_path / "plain") == []


def test_frozen_plan_and_session_give_one_reproducible_accepted_record(planned: Path) -> None:
    before = evidence_digests(planned)
    document = accept(planned)
    assert document["status"] == "accepted" and document["blocking"] == []
    assert document["execution_eligible"] is False
    assert document["experiment"]["execution_eligible"] is False
    # Traceable to the plan digest, the strategy identity and the observed range.
    start = archive_records(planned)[0]
    assert document["plan"] == binding(PLAN)["plan"]
    assert document["plan_sha256"] == plan_digest(PLAN) == start["plan_sha256"]
    session = document["session"]
    assert session["session_id"] == start["session_id"] and session["session_complete"] is True
    assert session["code"] == start["code"]
    assert session["run_policy_sha256"] == start["run_policy_sha256"]
    assert session["config_sha256"] == sha256_hex(canonical_json(start["config"]))
    manifest = document["experiment"]["manifest"]
    assert manifest["window"] == PLAN.window.model_dump()
    assert manifest["window_basis"] == "pre_registered_plan"
    assert manifest["plan_sha256"] == document["plan_sha256"]
    assert manifest["policy_sha256"] == session["decision_policy_sha256"]
    assert manifest["inputs"]["capture_session_id"] == session["session_id"]
    observed = document["observation"]
    assert observed["plan_bound_at_ms"] == start["wall_ms"] < START * 1000
    assert observed["first_cycle_at"] <= START and observed["last_label_query_at"] >= CUTOFF
    assert observed["termination"] == {"status": "ended", "reason": "max_ticks", "cycles": TICKS}
    phases = observed["cycles_by_phase"]
    assert sum(sum(kinds.values()) for kinds in phases.values()) == observed["cycles_total"]
    assert observed["cycles_total"] == TICKS and phases["window"]["discovery"] >= 1
    assert phases["after_label_cutoff"]["discovery"] >= 1
    # The denominator and every label state, never a return.
    labels = document["labels"]
    assert labels["primary_samples"] == document["experiment"]["population"]["primary_samples"]
    assert labels["primary_samples"] > 0
    assert sum(labels["states"].values()) == labels["primary_samples"]
    assert "forward_return_bps" not in json.dumps(labels)
    assert document["limits"]["net_return"] == "not_measured"
    assert document["binding_evidence"]["independent_time_proof"] == "not_established"
    # Reproducible, by plan alone or with the session named; the evidence is unchanged.
    assert accept(planned) == document
    assert accept(planned, session=session["session_id"]) == document
    assert evidence_digests(planned) == before
    body = {k: v for k, v in document.items() if k != "acceptance_sha256"}
    assert document["acceptance_sha256"] == sha256_hex(canonical_json(body))


def test_report_states_where_its_window_came_from(plain: Path) -> None:
    records = replay_session(plain / "capture.db", plain / "raw")["records"]
    window = {"start": START, "end": END, "label_cutoff": CUTOFF}

    def report(**basis: Any) -> dict[str, Any]:
        return run_experiment(
            records, AgentConfig(), evidence="synthetic", inputs={}, **window, **basis
        )

    explored = report()["manifest"]
    assert explored["window_basis"] == "analysis_time_arguments"
    assert explored["plan_sha256"] is None
    digest = plan_digest(PLAN)
    planned = report(window_basis="pre_registered_plan", plan_sha256=digest)["manifest"]
    assert planned["window_basis"] == "pre_registered_plan" and planned["plan_sha256"] == digest
    for bad in (
        {"window_basis": "pre_registered_plan"},
        {"window_basis": "pre_registered_plan", "plan_sha256": digest.upper()},
        {"plan_sha256": digest},
        {"window_basis": "chosen_later"},
    ):
        with pytest.raises(ExperimentRefused) as error:
            report(**bad)
        assert error.value.category == "window_basis_malformed"


def test_zero_cycle_budget_stop_is_complete_but_blocked(tmp_path: Path) -> None:
    run = tmp_path / "run"
    assert research_run(run, budget=1)["code"] == 0  # stops before cycle 1
    document = accept(run)
    assert document["session"]["session_complete"] is True
    assert document["status"] == "blocked"
    assert document["blocking"] == [
        "window_start_not_observed",
        "no_discovery_cycle_in_window",
        "label_cutoff_not_reached",
    ]
    observed = document["observation"]
    assert observed["termination"] == {"status": "ended", "reason": "budget_stop", "cycles": 0}
    assert observed["cycles_total"] == 0 and observed["first_cycle_at"] is None
    assert document["labels"]["primary_samples"] == 0
    assert document["execution_eligible"] is False


def test_an_early_normal_end_is_blocked_before_the_label_cutoff(tmp_path: Path) -> None:
    run = tmp_path / "run"
    assert research_run(run, ticks=6)["code"] == 0
    document = accept(run)
    assert document["blocking"] == ["label_cutoff_not_reached"]
    observed = document["observation"]
    assert observed["termination"] == {"status": "ended", "reason": "max_ticks", "cycles": 6}
    assert observed["last_label_query_at"] < CUTOFF
    assert phase_kinds(document) == {
        "before_window": {"discovery": 1},
        "window": {"discovery": 5},
        "label_period": {},
        "after_label_cutoff": {},
    }
    labels = document["labels"]
    assert labels["primary_samples"] > 0
    assert sum(labels["states"].values()) == labels["primary_samples"]


def test_failed_cycles_stay_counted_and_missing_labels_stay_missing(tmp_path: Path) -> None:
    run = tmp_path / "run"
    # Every tick from cycle 4 raises: rolled back, then the loop halts on repeated errors.
    assert research_run(run, cli=Flaky(raises=frozenset(range(4, TICKS + 1))))["code"] == 3
    document = accept(run)
    assert document["blocking"] == ["label_cutoff_not_reached"]
    observed = document["observation"]
    assert observed["termination"]["reason"] == "repeated_errors"
    assert phase_kinds(document)["window"] == {"discovery": 2, "failed": 5}
    # Due before the cutoff yet never queried: unobserved, never a zero return.
    labels = document["labels"]
    assert labels["primary_samples"] > 0
    assert labels["states"]["unobserved"] == labels["primary_samples"]


def test_no_successful_discovery_in_the_window_blocks(tmp_path: Path) -> None:
    run = tmp_path / "run"
    # Discovery fails from cycle 2 through 8: every cycle in [start, end).
    assert research_run(run, cli=Flaky(discovery_fails=frozenset(range(2, 9))))["code"] == 0
    document = accept(run)
    assert document["blocking"] == ["no_discovery_cycle_in_window"]
    assert phase_kinds(document)["window"] == {"discovery_failed": 7}
    assert document["labels"]["primary_samples"] == 0


def test_deferred_labels_block_even_when_cycles_pass_the_cutoff(tmp_path: Path) -> None:
    run = tmp_path / "run"
    # Enough for a few full cycles; then only the held position is managed.
    budget = runner.tick_budget(AgentConfig()) + 46
    assert research_run(run, budget=budget)["code"] == 0
    document = accept(run)
    assert document["blocking"] == ["label_cutoff_not_reached"]
    observed = document["observation"]
    assert observed["last_cycle_at"] >= CUTOFF > observed["last_label_query_at"]
    assert phase_kinds(document)["after_label_cutoff"] == {"manage_only": 1}


def test_isolated_failures_are_visible_in_an_accepted_record(tmp_path: Path) -> None:
    run = tmp_path / "run"
    cli = Flaky(discovery_fails=frozenset({3}), raises=frozenset({5}))
    assert research_run(run, cli=cli)["code"] == 0
    document = accept(run)
    # The plan sets no failure threshold: coverage holds, and every failure is counted.
    assert document["status"] == "accepted"
    assert phase_kinds(document)["window"] == {"discovery": 5, "discovery_failed": 1, "failed": 1}


def test_a_plan_bound_after_its_window_opened_is_blocked(tmp_path: Path) -> None:
    late = validate_plan(plan_data(start=NOW - 600))
    run = tmp_path / "run"
    # main() refuses to start this run; acceptance blocks it too if it exists.
    assert research_run(run, plan=late)["code"] == 0
    document = accept(run, late)
    assert document["blocking"] == ["plan_bound_after_window_start", "window_start_not_observed"]


def test_a_plan_is_one_trial_and_never_moves_to_another_session(tmp_path: Path) -> None:
    run = tmp_path / "run"
    assert research_run(run)["code"] == 0
    # A second research run under the same plan never starts and queries nothing.
    again = Flaky()
    second = research_run(run, cli=again)
    assert second["code"] == 2 and second["refusal"] == "plan_already_bound_in_this_archive"
    assert again.calls == []
    # Another plan gets its own session in the same archive, as does a plain run.
    assert research_run(run, plan=OTHER)["code"] == 0
    assert research_run(run, plan=None, ticks=2)["code"] == 0
    first, other, unplanned = [
        r["session_id"] for r in archive_records(run) if r["kind"] == "session_start"
    ]
    assert accept(run)["session"]["session_id"] == first
    assert accept(run, OTHER)["session"]["session_id"] == other
    assert refused(run, PLAN, other) == "plan_mismatch"
    assert refused(run, OTHER, first) == "plan_mismatch"
    assert refused(run, PLAN, unplanned) == "session_has_no_plan"
    assert refused(run, PLAN, "0" * 32) == "unknown_session"
    # The reviewer's probe as a plan: the same archive, its window a day earlier.
    assert refused(run, validate_plan(plan_data(start=NOW - 86_400))) == "plan_mismatch"

    # Copying one session's binding into another makes the plan name two trials.
    def move(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            r | binding(PLAN) if r["kind"] == "session_start" and r["session_id"] == other else r
            for r in records
        ]

    rewrite(run, move)
    assert refused(run) == "plan_bound_to_several_sessions"
    assert refused(run, PLAN, first) == "plan_bound_to_several_sessions"


def test_an_edited_binding_refuses_instead_of_reading_as_unplanned(
    planned: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shifted = plan_data(start=NOW - 86_400)
    edits = [
        # The window moved and the digest stayed: the binding contradicts itself.
        ("bound_plan_digest_mismatch", lambda r: r | {"plan": shifted}),
        (
            "bound_plan_digest_mismatch",
            lambda r: {k: v for k, v in r.items() if k != "plan_sha256"},
        ),
        ("bound_plan_malformed", lambda r: {k: v for k, v in r.items() if k != "plan"}),
        ("bound_plan_malformed", lambda r: r | {"plan": shifted | {"note": 1}}),
    ]
    window = ["--start", str(START), "--end", str(END), "--label-cutoff", str(CUTOFF)]
    for n, (category, change) in enumerate(edits):
        run = copied(planned, tmp_path / f"edit{n}")
        rewrite(run, edit_start(change))
        assert refused(run) == category
        # Exploration cannot read it as a session started without a plan either.
        out = tmp_path / f"explore{n}.json"
        argv = ["--run", str(run), *window, "--evidence", "synthetic", "--out", str(out)]
        assert experiment(monkeypatch, *argv) == 2 and not out.exists()
    # A consistent re-digest is another plan: the original no longer matches, and the
    # forged earlier window was bound after it had opened.
    run = copied(planned, tmp_path / "redigest")
    forged = validate_plan(shifted)
    rewrite(run, edit_start(lambda r: r | binding(forged)))
    assert refused(run) == "plan_mismatch"
    blocked = accept(run, forged)["blocking"]
    assert blocked == ["plan_bound_after_window_start", "window_start_not_observed"]


def test_a_prefix_is_blocked_and_never_upgraded_to_complete(
    planned: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = copied(planned, tmp_path / "prefix")
    rewrite(run, lambda records: records[:-1])  # every cycle closed, the run's end gone
    document = accept(run)
    assert document["status"] == "blocked" and document["blocking"] == ["session_incomplete"]
    assert document["session"]["session_complete"] is False
    assert document["observation"]["termination"]["status"] == "unknown_prefix_only"
    plan = write_plan(tmp_path / "plan.json", plan_data())
    # Exit 3: blocked, and the record is still written with every gap.
    assert accept_cli(monkeypatch, run, plan, tmp_path / "prefix.json") == 3
    assert json.loads((tmp_path / "prefix.json").read_bytes()) == document
    with pytest.raises(SystemExit) as stop:
        accept_cli(monkeypatch, run, plan, tmp_path / "allowed.json", "--allow-prefix")
    assert stop.value.code == 2 and not (tmp_path / "allowed.json").exists()


def test_a_lost_capture_write_leaves_a_blocked_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Cycle 10's end write fails: the shadow run goes on, its archive stops there.
    fail_capture(monkeypatch, lambda kind, body: kind == "cycle_end" and body["cycle"] == 10)
    run = tmp_path / "run"
    assert research_run(run)["code"] == 0
    document = accept(run)
    assert document["blocking"] == ["session_incomplete", "label_cutoff_not_reached"]
    observed = document["observation"]
    assert observed["incomplete_cycles"] == [10] and observed["cycles_total"] == 9


def test_cli_reads_the_window_only_from_the_bound_plan(
    planned: Path,
    plain: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    before = evidence_digests(planned)
    plan = write_plan(tmp_path / "plan.json", plan_data())
    out = tmp_path / "acceptance.json"
    capsys.readouterr()
    assert accept_cli(monkeypatch, planned, plan, out) == 0
    document = json.loads(out.read_bytes())
    assert document == accept(planned)
    assert json.loads(capsys.readouterr().out) == {
        "acceptance_sha256": document["acceptance_sha256"],
        "blocking": [],
        "plan_sha256": plan_digest(PLAN),
        "session_id": document["session"]["session_id"],
        "status": "accepted",
    }
    again = tmp_path / "again.json"
    assert accept_cli(monkeypatch, planned, plan, again) == 0
    assert again.read_bytes() == out.read_bytes()
    with pytest.raises(FileExistsError):  # never overwrites an earlier record
        accept_cli(monkeypatch, planned, plan, out)
    # With a plan, analysis-time window arguments cannot be given at all.
    for extra in (["--start", str(START)], ["--end", str(END)], ["--label-cutoff", str(CUTOFF)]):
        with pytest.raises(SystemExit) as stop:
            accept_cli(monkeypatch, planned, plan, tmp_path / "windowed.json", *extra)
        assert stop.value.code == 2
    with pytest.raises(SystemExit) as stop:
        accept_cli(monkeypatch, planned, plan, planned / "inside.json")
    assert stop.value.code == 2 and not (planned / "inside.json").exists()
    # The plan file shifted after capture is another plan: refused, nothing written.
    shifted = write_plan(tmp_path / "shifted.json", plan_data(start=NOW - 86_400))
    capsys.readouterr()
    assert accept_cli(monkeypatch, planned, shifted, tmp_path / "shifted-out.json") == 2
    assert "experiment refused: plan_mismatch" in capsys.readouterr().err
    assert not (tmp_path / "shifted-out.json").exists()
    window = ["--start", str(START), "--end", str(END), "--label-cutoff", str(CUTOFF)]
    explore = ["--evidence", "synthetic", *window]
    # Nor can exploration re-window a session that has a plan.
    assert (
        experiment(monkeypatch, "--run", str(planned), *explore, "--out", str(tmp_path / "e")) == 2
    )
    assert "experiment refused: session_has_bound_plan" in capsys.readouterr().err
    assert evidence_digests(planned) == before
    # A session started without a plan: exploration as before, labelled as such.
    report_path = tmp_path / "plain.json"
    assert experiment(monkeypatch, "--run", str(plain), *explore, "--out", str(report_path)) == 0
    report = json.loads(report_path.read_bytes())
    assert report["schema"] == EXPERIMENT_SCHEMA and "status" not in report
    assert report["manifest"]["window_basis"] == "analysis_time_arguments"
    assert report["manifest"]["plan_sha256"] is None and report["execution_eligible"] is False
    capsys.readouterr()
    assert accept_cli(monkeypatch, plain, plan, tmp_path / "unplanned.json") == 2
    assert "experiment refused: no_planned_session" in capsys.readouterr().err


def test_a_start_record_that_does_not_read_back_its_plan_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The archive accepted a start record, but not one that binds this plan.
    monkeypatch.setattr(runner, "binding", lambda plan: binding(OTHER))
    cli = Flaky()
    result = research_run(tmp_path / "run", cli=cli)
    assert result["code"] == 2 and result["refusal"] == "plan_not_bound"
    assert cli.calls == []


def main_args(run: Path, plan: Path, *extra: str) -> list[str]:
    binary = ["--binary", sys.executable, "--no-gmgn"]
    return ["run", "--out", str(run), *binary, "--plan", str(plan), *extra]


def run_main(monkeypatch: pytest.MonkeyPatch, cli: Market3, argv: list[str]) -> int:
    """The real entry point with only the process runner scripted and pacing unslept."""
    monkeypatch.setattr(runner, "run_cli", lambda *a, **k: cli)
    monkeypatch.setattr(runner.signal, "signal", lambda *a, **k: None)
    monkeypatch.setattr(runner, "paced", lambda r: paced(r, min_interval=0, sleep=lambda s: None))
    monkeypatch.setattr(sys, "argv", argv)
    return runner.main()


def future_plan(t0: int) -> dict[str, Any]:
    """A plan whose window opens an hour after `t0` (the host clock main() reads)."""
    return plan_data(start=t0 + 3_600, end=t0 + 4_200, label_cutoff=t0 + 8_400)


def test_real_main_binds_the_plan_before_any_provider_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    data = future_plan(int(time.time()))
    plan, path = validate_plan(data), write_plan(tmp_path / "plan.json", data)
    cli, run = Market3(), tmp_path / "run"
    argv = main_args(run, path, "--ticks", "1", "--interval", "30")
    assert run_main(monkeypatch, cli, argv) == 0
    records = archive_records(run)
    start, calls = records[0], [r for r in records if r["kind"] == "call"]
    assert start["kind"] == "session_start"
    assert {k: start[k] for k in ("plan", "plan_sha256")} == binding(plan)
    policy = read_journal(run / "journal.db")[0]
    assert start["run_policy_sha256"] == sha256_hex(canonical_json(policy))
    assert cli.calls and len(calls) == len(cli.calls)
    assert all(c["seq"] > start["seq"] for c in calls)
    # One cycle before the window: bound in time, but blocked on coverage.
    blocking = accept(run, plan)["blocking"]
    assert blocking == ["no_discovery_cycle_in_window", "label_cutoff_not_reached"]
    # One plan is one trial: the same plan never starts a second session here.
    queried = len(cli.calls)
    capsys.readouterr()
    assert run_main(monkeypatch, cli, argv) == 2
    assert "refused: plan_already_bound_in_this_archive" in capsys.readouterr().err
    assert len(cli.calls) == queried and archive_records(run) == records


def test_real_main_refuses_a_research_run_before_any_provider_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    t0 = int(time.time())
    opened = write_plan(
        tmp_path / "open.json", plan_data(start=t0 - 1, end=t0 + 600, label_cutoff=t0 + 4_800)
    )
    malformed = write_plan(tmp_path / "bad.json", plan_data() | {"note": 1})
    cli = Market3()
    for path, category in ((opened, "plan_window_already_open"), (malformed, "plan_malformed")):
        run = tmp_path / category
        capsys.readouterr()
        assert run_main(monkeypatch, cli, main_args(run, path)) == 2
        assert f"spot research run refused: {category}" in capsys.readouterr().err
        assert not run.exists()
    # A research run is never a live run.
    with pytest.raises(SystemExit) as stop:
        run_main(monkeypatch, cli, [*main_args(tmp_path / "live", opened), "--live"])
    assert stop.value.code == 2 and not (tmp_path / "live").exists()
    # Without its archive the plan could never be shown bound: refused, not started.
    run = tmp_path / "no-archive"
    run.mkdir()
    (run / "capture.db").write_bytes(b"not a database")
    future = write_plan(tmp_path / "future.json", future_plan(t0))
    capsys.readouterr()
    assert run_main(monkeypatch, cli, main_args(run, future)) == 2
    assert "spot research run refused: capture_unavailable" in capsys.readouterr().err
    assert cli.calls == [] and not (run / "journal.db").exists()
