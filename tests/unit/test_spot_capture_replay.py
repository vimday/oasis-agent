"""Ordered input capture of the real shadow loop, and strict offline replay of SpotAgent."""

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

import scripts.replay_spot_capture as replay_cli
import scripts.run_spot_agent as runner
from oasis_agent.launch.gmgn_source import GmgnSource
from oasis_agent.launch.onchainos_source import OnchainosSource, SourceFailure, paced
from oasis_agent.launch.spot_agent import AgentConfig, AgentState, SpotAgent
from oasis_agent.launch.spot_capture import (
    CAPTURE_SCHEMA,
    CaptureRecorder,
    ReplayRefused,
    replay_session,
)
from oasis_agent.launch.spot_coverage import coverage_report
from oasis_agent.launch.spot_report import read_journal
from oasis_agent.models.common import canonical_json, sha256_hex
from oasis_agent.sniping.journal import ExecutionJournal
from scripts.run_spot_agent import manage_budget, run_loop, start_capture, tick_budget

from .test_spot_agent import NOW, FakeCli, hot_row
from .test_spot_gmgn import FakeGmgn, info
from .test_spot_runner import SubmittingExecutor

SHADOW = {"schema": "oasis.spot-agent-shadow.v1", "config": AgentConfig().model_dump(mode="json")}


class Clock:
    """One wall clock for loop, recorder and sources: advances 250 ms per read."""

    def __init__(self, start: float) -> None:
        self.t = start

    def __call__(self) -> float:
        self.t += 0.25
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


class Scripted:
    """Wraps a fake CLI; per-call hooks change prices or fail calls by call number."""

    def __init__(self, inner: Any, hooks: dict[int, Any] | None = None) -> None:
        self.inner, self.hooks, self.n = inner, hooks or {}, 0

    def __call__(self, argv: Any) -> bytes:
        self.n += 1
        hook = self.hooks.get(self.n)
        if isinstance(hook, Exception):
            raise hook
        if callable(hook):
            hook(self.inner)
        result: bytes = self.inner(argv)
        return result


def capture_run(
    run: Path,
    cli: Any,
    *,
    gmgn: Any = None,
    ticks: int = 4,
    budget: int = 100_000,
    interval: int = 60,
    state: AgentState | None = None,
    each_cycle: Any = None,
    plan: Any = None,
) -> dict[str, Any]:
    """The real shadow run_loop with the recorder wired exactly as main() wires it.

    `plan` makes it a research run: the session starts through main()'s own
    `start_capture`, and a refused binding returns code 2 before any provider call.
    """
    run.mkdir(parents=True, exist_ok=True)
    raw = run / "raw"
    raw.mkdir(exist_ok=True)
    clock = Clock(float(NOW))
    archive = ExecutionJournal(run / "capture.db", {"schema": CAPTURE_SCHEMA})
    recorder = CaptureRecorder(archive, raw, clock=clock)

    def wrap(provider: str, runner: Any) -> Any:
        inner = paced(recorder.attempt(runner), min_interval=0, sleep=clock.sleep, monotonic=clock)
        return recorder.logical(provider, inner)

    source = OnchainosSource(wrap("onchainos", cli), clock=recorder.source_clock, retain=raw)
    second = None
    if gmgn is not None:
        second = GmgnSource(wrap("gmgn", gmgn), clock=recorder.source_clock, retain=raw)
    agent = SpotAgent(source, AgentConfig(), state, gmgn=second)

    def between() -> bool:
        # Called before each cycle and while sleeping; the cycle follows from the clock.
        if each_cycle is not None:
            each_cycle(int((clock.t - NOW) // interval) + 1, int(clock.t))
        return False

    with ExecutionJournal(run / "journal.db", SHADOW) as journal:
        digest = sha256_hex(canonical_json(SHADOW))
        refusal = start_capture(recorder, agent, digest, plan, run / "capture.db")
        if refusal is not None:
            archive.close()
            run_records = read_journal(run / "journal.db")[1]
            return {"code": 2, "refusal": refusal, "agent": agent, "run_records": run_records}
        code = run_loop(
            agent,
            journal,
            run / "state.json",
            interval=interval,
            max_ticks=ticks,
            call_budget=budget,
            spent=0,
            should_stop=between,
            clock=clock,
            sleep=clock.sleep,
            capture=recorder,
        )
    archive.close()
    run_records = read_journal(run / "journal.db")[1]
    return {"code": code, "agent": agent, "recorder": recorder, "run_records": run_records}


def tick_records(run_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The run journal's decision records, without journal or budget bookkeeping."""
    drop = {"seq", "previous", "policy_digest", "digest"}
    skip = {"budget_reserve", "budget_settle", "budget_stop", "tick_error"}
    return [
        {k: v for k, v in r.items() if k not in drop} for r in run_records if r["kind"] not in skip
    ]


def archive_records(run: Path) -> list[dict[str, Any]]:
    return read_journal(run / "capture.db")[1]


def tokens(cli: FakeCli, n: int) -> FakeCli:
    cli.hot = [hot_row(tokenContractAddress="0x" + f"{i + 0xA0:02x}" * 20) for i in range(n)]
    return cli


def market_moves(cli: FakeCli) -> Any:
    """Cycle 1 buys; cycle 2 falls into a stop-loss exit; later cycles label outcomes."""

    def step(cycle: int, now: int) -> None:
        cli.now = now  # batch price rows carry the provider's own time
        cli.sell_ratio = {1: (99, 100), 2: (60, 100)}.get(cycle, (99, 100))

    return step


def test_replay_reproduces_every_cycle_of_the_real_loop(tmp_path: Path) -> None:
    run = tmp_path / "run"
    cli = FakeCli()
    result = capture_run(run, cli, ticks=4, interval=600, each_cycle=market_moves(cli))
    kinds = [r["kind"] for r in result["run_records"]]
    assert "paper_buy" in kinds and "paper_sell" in kinds and "outcome" in kinds
    replay = replay_session(run / "capture.db", run / "raw")
    assert replay["cycles_replayed"] == 4 and replay["session_complete"] is True
    assert replay["execution_eligible"] is False
    # The replayed decisions are exactly the run journal's decisions, record by record.
    assert replay["records"] == tick_records(result["run_records"])
    assert replay["calls_served"] == sum(1 for r in archive_records(run) if r["kind"] == "call")
    # Deterministic: a second replay of the same archive is identical.
    assert replay_session(run / "capture.db", run / "raw") == replay


def test_equal_responses_of_different_calls_stay_distinct(tmp_path: Path) -> None:
    run = tmp_path / "run"
    capture_run(run, FakeCli(), ticks=2)
    calls = [r for r in archive_records(run) if r["kind"] == "call"]
    hot = [c for c in calls if c["argv"][:2] == ["token", "hot-tokens"]]
    # Same command, same bytes, two cycles: two call records, one stored blob.
    assert len(hot) == 2 and hot[0]["raw_sha256"] == hot[1]["raw_sha256"]
    assert [c["cycle"] for c in hot] == [1, 2]
    assert [c["call_seq"] for c in calls] == list(range(1, len(calls) + 1))


def test_same_command_with_different_responses_replays_each_in_order(tmp_path: Path) -> None:
    run = tmp_path / "run"
    cli = FakeCli()
    result = capture_run(run, cli, ticks=3, interval=600, each_cycle=market_moves(cli))
    quotes = {}
    for c in archive_records(run):
        if c["kind"] == "call" and c["argv"][:2] == ["swap", "quote"]:
            quotes.setdefault(tuple(c["argv"]), set()).add(c["raw_sha256"])
    assert any(len(hashes) > 1 for hashes in quotes.values())
    replay = replay_session(run / "capture.db", run / "raw")
    assert replay["records"] == tick_records(result["run_records"])


def test_interleaved_gmgn_and_its_failures_replay(tmp_path: Path) -> None:
    run = tmp_path / "run"
    # GMGN answers per attempt: ok, then a retryable failure retried into success, then
    # a permanent failure the agent ignores. FakeGmgn echoes `info()` for any token, so
    # its claims are foreign to the other tokens and change nothing but the evidence.
    gmgn = Scripted(FakeGmgn(info()), {2: SourceFailure("cli_exit"), 4: SourceFailure("x")})
    result = capture_run(run, tokens(FakeCli(), 3), gmgn=gmgn, ticks=2)
    calls = [r for r in archive_records(run) if r["kind"] == "call"]
    providers = [c["provider"] for c in calls]
    assert "gmgn" in providers and providers.index("gmgn") < len(providers) - 1
    gmgn_calls = [c for c in calls if c["provider"] == "gmgn"]
    # The retried call is one logical query with two transport attempts.
    assert [c["attempts"] for c in gmgn_calls][:3] == [1, 2, 1]
    assert gmgn_calls[2]["failure"] == "x"
    replay = replay_session(run / "capture.db", run / "raw")
    assert replay["records"] == tick_records(result["run_records"])


def test_gmgn_disagreement_and_recorded_provider_failure_replay(tmp_path: Path) -> None:
    run = tmp_path / "run"
    cli = FakeCli()
    cli.fail = {("signal", "list")}
    result = capture_run(run, cli, gmgn=FakeGmgn(info(liquidity="1000")), ticks=2)
    # `{"ok":false}` is a successful transport: its bytes are kept, and the source's
    # own parser raises provider_error again from those same bytes on replay.
    signal_calls = [
        r
        for r in archive_records(run)
        if r["kind"] == "call" and r["argv"][:2] == ["signal", "list"]
    ]
    assert signal_calls and all("raw_sha256" in c for c in signal_calls)
    replay = replay_session(run / "capture.db", run / "raw")
    assessed = [r for r in replay["records"] if r["kind"] == "assessment"]
    assert any("provider_disagree:liquidity" in a["reasons"] for a in assessed)
    assert replay["records"] == tick_records(result["run_records"])


def test_failed_tick_replays_as_the_same_error_and_rollback(tmp_path: Path) -> None:
    run = tmp_path / "run"
    cli = Scripted(FakeCli(), {6: RuntimeError("provider said: secret")})
    capture_run(run, cli, ticks=2)
    errors = [r for r in archive_records(run) if r["kind"] == "call" and "error_type" in r]
    assert [e["error_type"] for e in errors] == ["RuntimeError"]
    assert "secret" not in json.dumps(archive_records(run))
    # RuntimeError is not replayable as a builtin with a fixed meaning: refused, not guessed.
    with pytest.raises(ReplayRefused, match="unreplayable_error"):
        replay_session(run / "capture.db", run / "raw")


def test_manage_only_cycle_defers_labels_and_replays(tmp_path: Path) -> None:
    run = tmp_path / "run"
    cli = FakeCli()
    budget = tick_budget(AgentConfig()) + manage_budget(1, 0) + 5
    result = capture_run(run, cli, ticks=3, budget=budget)  # stable price: still held
    starts = [r for r in archive_records(run) if r["kind"] == "cycle_start"]
    modes = [s["manage_only"] for s in starts]
    # Full cycles while the budget covers their worst case, then manage-only.
    assert modes[0] is False and True in modes
    replay = replay_session(run / "capture.db", run / "raw")
    deferred = [r for r in replay["records"] if r.get("cycle") == "manage_only"]
    assert deferred and deferred[0]["coverage"]["discovery"] == "deferred_manage_only"
    assert replay["records"] == tick_records(result["run_records"])


def rewrite(run: Path, edit: Any) -> None:
    """Re-chain the archive after `edit(records)`: a hash-valid forgery, not corruption."""
    records = archive_records(run)
    edited = edit(
        [
            {k: v for k, v in r.items() if k not in ("seq", "previous", "policy_digest", "digest")}
            for r in records
        ]
    )
    (run / "capture.db").unlink()
    with ExecutionJournal(run / "capture.db", {"schema": CAPTURE_SCHEMA}) as journal:
        for r in edited:
            journal.append(r["kind"], r)


def captured(tmp_path: Path, **kwargs: Any) -> Path:
    run = tmp_path / "run"
    cli = FakeCli()
    capture_run(run, cli, ticks=3, interval=600, each_cycle=market_moves(cli), **kwargs)
    replay_session(run / "capture.db", run / "raw")  # the untouched archive replays
    return run


def calls_of(records: list[dict[str, Any]]) -> list[int]:
    return [i for i, r in enumerate(records) if r["kind"] == "call"]


def swap_two_calls(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    i, j = calls_of(records)[:2]
    a, b = records[i], records[j]
    records[i] = a | {k: b[k] for k in ("provider", "argv", "raw_sha256")}
    records[j] = b | {k: a[k] for k in ("provider", "argv", "raw_sha256")}
    return records


def future_price(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Give cycle 1's first call the bytes a later cycle received (a future response)."""
    later = [r for r in records if r["kind"] == "call" and r["cycle"] == 3 and "raw_sha256" in r]
    first = calls_of(records)[0]
    records[first] = records[first] | {
        "argv": later[0]["argv"],
        "raw_sha256": later[0]["raw_sha256"],
    }
    return records


def drop_call(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    del records[calls_of(records)[3]]
    return records


def extra_call(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    i = calls_of(records)[-1]
    return [
        *records[: i + 1],
        records[i] | {"call_seq": records[i]["call_seq"] + 1},
        *records[i + 1 :],
    ]


def move_call_to_next_cycle(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    first_end = next(i for i, r in enumerate(records) if r["kind"] == "cycle_end")
    records[first_end - 1] = records[first_end - 1] | {"cycle": 2}
    return records


def late_response(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # The cycle's last call answered only after the cycle had already ended.
    first_end = next(i for i, r in enumerate(records) if r["kind"] == "cycle_end")
    i = first_end - 1
    records[i] = records[i] | {"available_ms": records[first_end]["wall_ms"] + 1}
    return records


def early_request(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    i = calls_of(records)[0]
    records[i] = records[i] | {"started_ms": NOW * 1000 - 1}
    return records


def changed_decision(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    end = next(i for i, r in enumerate(records) if r["kind"] == "cycle_end")
    records[end] = records[end] | {"records_sha256": "0" * 64}
    return records


def truncated(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    last_end = max(i for i, r in enumerate(records) if r["kind"] == "cycle_end")
    return records[:last_end]  # the last cycle never finished


@pytest.mark.parametrize(
    ("edit", "category"),
    [
        (swap_two_calls, "call_mismatch"),
        (future_price, "call_mismatch"),
        (drop_call, "call_sequence_broken"),
        (extra_call, "cycle_call_count_mismatch"),
        (move_call_to_next_cycle, "call_outside_its_cycle"),
        (late_response, "call_after_its_cycle"),
        (early_request, "call_before_its_cycle"),
        (changed_decision, "decision_mismatch"),
    ],
)
def test_forged_inputs_are_refused(tmp_path: Path, edit: Any, category: str) -> None:
    run = captured(tmp_path)
    rewrite(run, edit)
    with pytest.raises(ReplayRefused) as refused:
        replay_session(run / "capture.db", run / "raw")
    assert refused.value.category == category


def test_future_price_swap_with_matching_argv_is_caught_by_the_decision_digest(
    tmp_path: Path,
) -> None:
    # The negative control that matters for leakage: same command, a later cycle's
    # bytes. Command matching alone would accept it; the recorded decision digest must not.
    run = captured(tmp_path)

    def later_bytes_same_command(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        # The held position's sell quote: same argv in cycles 1 and 2, different bytes
        # (cycle 2 is the stop-loss price). Serve cycle 2's bytes to cycle 1.
        calls = [r for r in records if r["kind"] == "call" and r["argv"][:2] == ["swap", "quote"]]
        early = next(
            c
            for c in calls
            if c["cycle"] == 1 and any(d["cycle"] == 2 and d["argv"] == c["argv"] for d in calls)
        )
        late = next(d for d in calls if d["cycle"] == 2 and d["argv"] == early["argv"])
        assert early["raw_sha256"] != late["raw_sha256"]
        index = records.index(early)
        records[index] = early | {"raw_sha256": late["raw_sha256"]}
        return records

    rewrite(run, later_bytes_same_command)
    with pytest.raises(ReplayRefused, match="decision_mismatch"):
        replay_session(run / "capture.db", run / "raw")


def test_incomplete_session_replays_only_completed_cycles_and_says_so(tmp_path: Path) -> None:
    run = captured(tmp_path)
    rewrite(run, truncated)
    replay = replay_session(run / "capture.db", run / "raw")
    assert replay["session_complete"] is False and replay["incomplete_cycles"] == [3]
    assert replay["cycles_replayed"] == 2


def test_tampered_blob_missing_blob_and_corrupt_chain_are_refused(tmp_path: Path) -> None:
    run = captured(tmp_path)
    blob = min((run / "raw").glob("*.json"))
    original = blob.read_bytes()
    blob.write_bytes(original + b" ")
    with pytest.raises(ReplayRefused, match="raw_hash_mismatch"):
        replay_session(run / "capture.db", run / "raw")
    blob.unlink()
    with pytest.raises(ReplayRefused, match="raw_missing"):
        replay_session(run / "capture.db", run / "raw")
    blob.write_bytes(original)
    with sqlite3.connect(run / "capture.db") as db:
        db.execute("UPDATE execution_events SET digest = ? WHERE seq = 2", ("0" * 64,))
    with pytest.raises(ReplayRefused, match="archive_corrupt"):
        replay_session(run / "capture.db", run / "raw")


def test_unsupported_sessions_are_marked_and_refused(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    (run / "raw").mkdir()
    archive = ExecutionJournal(run / "capture.db", {"schema": CAPTURE_SCHEMA})

    class Nominator:
        calls = 0

        def nominate(self, chain: int) -> list[str]:
            return []

    agent = SpotAgent(OnchainosSource(FakeCli()), AgentConfig(), nominators=[Nominator()])
    recorder = CaptureRecorder(archive, run / "raw")
    recorder.start_session(agent, "0" * 64)
    archive.close()
    start = archive_records(run)[0]
    assert start["replayable"] is False and start["unreplayable_reasons"] == [
        "nominators_not_supported"
    ]
    with pytest.raises(ReplayRefused, match="session_not_replayable"):
        replay_session(run / "capture.db", run / "raw")


def test_live_loop_refuses_a_recorder(tmp_path: Path) -> None:
    agent = SpotAgent(
        OnchainosSource(FakeCli()), AgentConfig(), executor=SubmittingExecutor(RuntimeError("x"))
    )
    recorder = CaptureRecorder(
        ExecutionJournal(tmp_path / "c.db", {"schema": CAPTURE_SCHEMA}), tmp_path
    )
    with (
        ExecutionJournal(tmp_path / "j.db", SHADOW) as journal,
        pytest.raises(ValueError, match="never captured"),
    ):
        run_loop(
            agent,
            journal,
            tmp_path / "s.json",
            interval=60,
            max_ticks=1,
            call_budget=1000,
            spent=0,
            should_stop=lambda: False,
            capture=recorder,
        )


def test_archive_failure_never_changes_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline = capture_run(tmp_path / "a", FakeCli(), ticks=2)
    original = ExecutionJournal.append

    def failing(self: ExecutionJournal, kind: str, payload: dict[str, Any]) -> Any:
        if self.path.name == "capture.db" and kind == "call":
            raise sqlite3.OperationalError("disk full")
        return original(self, kind, payload)

    monkeypatch.setattr(ExecutionJournal, "append", failing)
    broken = capture_run(tmp_path / "b", FakeCli(), ticks=2)
    monkeypatch.setattr(ExecutionJournal, "append", original)
    assert broken["code"] == baseline["code"] == 0
    assert tick_records(broken["run_records"]) == tick_records(baseline["run_records"])
    assert broken["recorder"].healthy is False
    replay = replay_session(tmp_path / "b" / "capture.db", tmp_path / "b" / "raw")
    assert replay["session_complete"] is False and replay["cycles_replayed"] == 0


def test_initial_state_is_part_of_the_capture(tmp_path: Path) -> None:
    state = AgentState(500_000_000)
    run = tmp_path / "run"
    result = capture_run(run, FakeCli(), ticks=2, state=state)
    replay = replay_session(run / "capture.db", run / "raw")
    assert replay["records"] == tick_records(result["run_records"])
    assert archive_records(run)[0]["state"]["cash_usd_micros"] == 500_000_000


def test_coverage_reconciles_and_keeps_failures_and_deferrals(tmp_path: Path) -> None:
    run = tmp_path / "run"
    cli = tokens(FakeCli(), 12)  # more than enrich_top=8: some are deferred by the quota
    cli.fail = {("token", "advanced-info")}
    capture_run(run, cli, ticks=1)
    replay = replay_session(run / "capture.db", run / "raw")
    report = coverage_report(replay["records"])
    fates = report["candidate_fates"]
    assert report["fates_reconcile_where_observed"] is True
    assert sum(fates.values()) == report["discovered_candidate_cycles"] == 12
    # The quota counts enrichment attempts: 8 tried (all failed on advanced-info, kept in
    # the denominator as enrich_failed), 4 never reached (deferred, not skipped).
    assert fates["not_reached_enrich_quota"] == 4 and fates["enrich_failed"] == 8
    assert fates["assessed_after_enrich"] == 0
    # Every assessment is accounted for by a registration outcome; none vanish.
    assessed = sum(v["cycle_opportunities"] for v in report["assessments_by_action"].values())
    assert sum(report["label_registration"].values()) == assessed
    assert report["label_basis"] == "provider_spot_mid_forward_return_not_tradable_net_return"


def test_real_main_captures_and_the_replay_command_reproduces_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """main() with only the process runner scripted, then the offline replay CLI."""
    cli = FakeCli()
    run, out = tmp_path / "run", tmp_path / "replay.json"
    monkeypatch.setattr(runner, "run_cli", lambda *a, **k: cli)
    monkeypatch.setattr(runner.signal, "signal", lambda *a, **k: None)
    monkeypatch.setattr(runner.time, "sleep", lambda s: None)
    argv = ["run", "--out", str(run), "--binary", sys.executable, "--no-gmgn"]
    monkeypatch.setattr(sys, "argv", [*argv, "--ticks", "2", "--interval", "30"])
    assert runner.main() == 0
    run_records = read_journal(run / "journal.db")[1]
    session = archive_records(run)[0]
    assert session["replayable"] is True
    assert session["run_policy_sha256"] == sha256_hex(
        canonical_json(read_journal(run / "journal.db")[0])
    )
    # The CLI never touches the provider: a runner that explodes proves it.
    monkeypatch.setattr(runner, "run_cli", lambda *a, **k: None)
    monkeypatch.setattr(sys, "argv", ["replay", "--run", str(run), "--out", str(out)])
    assert replay_cli.main() == 0
    report = json.loads(out.read_bytes())
    assert report["records"] == tick_records(run_records)
    assert (
        report["cycles_replayed"] == 2
        and report["coverage"]["fates_reconcile_where_observed"] is True
    )
    # Never overwrites an earlier result, and never writes into the run's evidence.
    with pytest.raises(FileExistsError):
        replay_cli.main()
    monkeypatch.setattr(sys, "argv", ["replay", "--run", str(run), "--out", str(run / "r.json")])
    with pytest.raises(SystemExit):
        replay_cli.main()
    capsys.readouterr()


def test_replay_command_reports_a_fixed_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:

    run = captured(tmp_path)
    for blob in (run / "raw").glob("*.json"):
        blob.unlink()
    monkeypatch.setattr(
        sys, "argv", ["replay", "--run", str(run), "--out", str(tmp_path / "o.json")]
    )
    assert replay_cli.main() == 2
    assert "replay refused: raw_missing" in capsys.readouterr().err
    assert not (tmp_path / "o.json").exists()
