"""Long-running shadow reliability: pacing/retry, crash-consistent resume, error rollback."""

from pathlib import Path
from typing import Any

import pytest

from oasis_agent.launch.onchainos_source import OnchainosSource, SourceFailure, paced
from oasis_agent.launch.spot_agent import (
    AgentConfig,
    AgentState,
    SpotAgent,
    reconcile_with_journal,
)
from oasis_agent.launch.spot_executor import (
    APPROVED_WALLET,
    Fill,
    LiveExecutor,
    LiveRefused,
    LiveUnresolved,
)
from oasis_agent.launch.spot_lifecycle import LIVE_SCHEMA
from oasis_agent.launch.spot_report import build_report, read_journal
from oasis_agent.sniping.journal import ExecutionJournal
from scripts.run_spot_agent import (
    manage_budget,
    plan_cycle,
    resume,
    run_loop,
    save_state,
    tick_budget,
)

from .test_spot_agent import NOW, TOKEN, FakeCli
from .test_spot_executor import FakeWallet, gate


class Clock:
    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


def test_paced_spaces_calls_and_retries_only_transient_failures() -> None:
    clock, outcomes = Clock(), [SourceFailure("timeout"), SourceFailure("cli_exit"), b"ok"]

    def flaky(argv: Any) -> bytes:
        item = outcomes.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    run = paced(flaky, sleep=clock.sleep, monotonic=clock.monotonic, jitter=lambda: 0.0)
    assert run(["x"]) == b"ok"
    # backoff 2s then 4s; spacing already satisfied by the backoff sleeps
    assert clock.sleeps == [2.0, 4.0]
    for category in ("invalid_json", "response_too_large", "command_not_read_only"):
        calls: list[Any] = []

        def hard(argv: Any, category: str = category, calls: list[Any] = calls) -> bytes:
            calls.append(argv)
            raise SourceFailure(category)

        with pytest.raises(SourceFailure, match=category):
            paced(hard, sleep=clock.sleep, monotonic=clock.monotonic)(["x"])
        assert len(calls) == 1


def test_paced_gives_up_after_bounded_attempts() -> None:
    clock, calls = Clock(), []

    def down(argv: Any) -> bytes:
        calls.append(argv)
        raise SourceFailure("timeout")

    with pytest.raises(SourceFailure, match="timeout"):
        paced(down, attempts=3, sleep=clock.sleep, monotonic=clock.monotonic)(["x"])
    assert len(calls) == 3


def test_paced_enforces_minimum_interval_between_calls() -> None:
    clock = Clock()
    run = paced(lambda argv: b"", min_interval=1.5, sleep=clock.sleep, monotonic=clock.monotonic)
    run(["a"])
    run(["b"])
    assert clock.sleeps == [1.5]


def policy() -> dict[str, Any]:
    return {"schema": "oasis.spot-agent-shadow.v1", "config": AgentConfig().model_dump()}


def loop(run: Path, cli: FakeCli, ticks: int, **kwargs: Any) -> tuple[SpotAgent, int]:
    run.mkdir(exist_ok=True)
    state, spent = resume(run, AgentConfig())
    agent = SpotAgent(OnchainosSource(cli, clock=lambda: NOW), AgentConfig(), state)
    times = iter(range(NOW, NOW + 100_000, 60))
    with ExecutionJournal(run / "journal.db", policy()) as journal:
        code = run_loop(
            agent,
            journal,
            run / "state.json",
            interval=60,
            max_ticks=ticks,
            call_budget=kwargs.get("call_budget", 100_000),
            spent=spent,
            should_stop=lambda: False,
            clock=lambda: float(next(times)),
            sleep=lambda s: None,
        )
    return agent, code


def test_resume_takes_money_state_from_journal_not_stale_state_file(tmp_path: Path) -> None:
    run = tmp_path / "run"
    agent, _ = loop(run, FakeCli(), 1)
    assert TOKEN in agent.state.positions
    stale = AgentConfig()
    # Simulate a kill after the journal append but before the state file was rewritten.
    save_state(run / "state.json", AgentState(stale.initial_cash_usd_micros))
    state, spent = resume(run, stale)
    assert state.positions.keys() == {TOKEN}
    assert state.cash_usd_micros == agent.state.cash_usd_micros
    assert spent > 0


def test_resume_refuses_journal_ending_inside_a_tick() -> None:
    summary = {
        "kind": "tick_summary",
        "cash_usd_micros": 1,
        "positions": [],
        "realized_pnl_usd_micros": 0,
        "closed_trades": 0,
        "winning_trades": 0,
    }
    with pytest.raises(ValueError, match="interrupted tick"):
        reconcile_with_journal(AgentState(1), [summary, {"kind": "paper_buy"}])
    with pytest.raises(ValueError, match="no tick summary"):
        reconcile_with_journal(AgentState(1), [{"kind": "paper_sell"}])


def test_call_budget_spans_restarts(tmp_path: Path) -> None:
    run = tmp_path / "run"
    _, _ = loop(run, FakeCli(), 2)
    _, spent = resume(run, AgentConfig())
    cli = FakeCli()
    _, code = loop(run, cli, 5, call_budget=spent + 10)
    # The earlier process's usage still counts: no full cycle (no discovery, no entry),
    # only management of the held position within the 10 remaining calls.
    assert code == 0
    assert {c[:2] for c in cli.calls} <= {("security", "token-scan"), ("swap", "quote")}
    _, spent_after = resume(run, AgentConfig())
    assert spent < spent_after <= spent + 10


class Exploding(FakeCli):
    explode = False

    def __call__(self, argv: Any) -> bytes:
        if tuple(argv)[:2] == ("swap", "quote") and self.explode:
            raise RuntimeError("provider said: secret detail")
        return super().__call__(argv)


def test_tick_error_rolls_back_unjournaled_state_and_halts_after_repeats(tmp_path: Path) -> None:
    run = tmp_path / "run"
    cli = Exploding()
    cli.explode = True
    agent, code = loop(run, cli, 10)
    assert code == 3
    assert (
        agent.state.positions == {}
        and agent.state.cash_usd_micros == AgentConfig().initial_cash_usd_micros
    )
    records = read_journal(run / "journal.db")[1]
    errors = [r for r in records if r["kind"] == "tick_error"]
    assert [e["consecutive"] for e in errors] == [1, 2, 3, 4, 5]
    assert all("secret" not in str(e) for e in errors)
    assert errors[0]["error_type"] == "RuntimeError"


class SubmittingExecutor:
    """Executor double whose buy submits and then raises."""

    def __init__(self, error: Exception) -> None:
        self.calls = 0
        self.submissions = 0
        self.error = error
        self.gate = type("G", (), {"max_trade_usd_micros": 3_000_000_000})()

    def calls_per_swap(self) -> int:
        return 15

    def buy(self, token: str, usdc_amount: int) -> Any:
        self.submissions += 1
        raise self.error

    def sell(self, token: str, units: int) -> Any:
        raise AssertionError("no position to sell")


@pytest.mark.parametrize(
    "error", [LiveUnresolved("receipt_unavailable"), RuntimeError("after submit")]
)
def test_live_loop_halts_after_any_unproven_submission(tmp_path: Path, error: Exception) -> None:
    executor = SubmittingExecutor(error)
    agent = SpotAgent(
        OnchainosSource(FakeCli(), clock=lambda: NOW),
        AgentConfig(),
        executor=executor,  # type: ignore[arg-type]
    )
    times = iter(range(NOW, NOW + 100_000, 60))
    with ExecutionJournal(tmp_path / "journal.db", policy()) as journal:
        code = run_loop(
            agent,
            journal,
            tmp_path / "state.json",
            interval=60,
            max_ticks=5,
            call_budget=100_000,
            spent=0,
            should_stop=lambda: False,
            clock=lambda: float(next(times)),
            sleep=lambda s: None,
        )
    assert code == 4
    assert executor.submissions == 1  # never ticked again
    records = read_journal(tmp_path / "journal.db")[1]
    assert records[-1]["kind"] == "tick_error" and records[-1]["mode"] == "live"
    assert agent.state.positions == {}


def test_live_tick_budget_reserves_worst_case_swap_calls() -> None:
    config = AgentConfig()
    assert tick_budget(config, 15) - tick_budget(config) == 2 * config.max_positions * 15


def test_live_flag_on_a_shadow_directory_is_refused(tmp_path: Path) -> None:
    run = tmp_path / "run"
    loop(run, FakeCli(), 1)
    live = {"schema": "oasis.spot-agent-live.v2", "config": AgentConfig().model_dump()}
    with pytest.raises(ValueError, match="cannot change"):
        ExecutionJournal(run / "journal.db", live)


class SilentSubmitter(SubmittingExecutor):
    """Submits but lets the tick finish without a booked trade (a swallowed outcome)."""

    def buy(self, token: str, usdc_amount: int) -> Any:
        self.submissions += 1
        raise LiveRefused("pretend_refused")


def test_submission_without_booked_trade_halts(tmp_path: Path) -> None:
    executor = SilentSubmitter(RuntimeError("unused"))
    agent = SpotAgent(
        OnchainosSource(FakeCli(), clock=lambda: NOW),
        AgentConfig(),
        executor=executor,  # type: ignore[arg-type]
    )
    times = iter(range(NOW, NOW + 100_000, 60))
    with ExecutionJournal(tmp_path / "journal.db", policy()) as journal:
        code = run_loop(
            agent,
            journal,
            tmp_path / "state.json",
            interval=60,
            max_ticks=5,
            call_budget=100_000,
            spent=0,
            should_stop=lambda: False,
            clock=lambda: float(next(times)),
            sleep=lambda s: None,
        )
    assert code == 4 and executor.submissions == 1
    last = read_journal(tmp_path / "journal.db")[1][-1]
    assert last["category"] == "submission_without_booked_trade"


class HalfBooked(SubmittingExecutor):
    """First swap books a fill; a second hidden submission is never booked."""

    def buy(self, token: str, usdc_amount: int) -> Any:
        self.submissions += 2
        return Fill("buy", token, "0x" + "cd" * 32, 7, -usdc_amount, usdc_amount * 1_000)


def test_mismatch_keeps_booked_trade_records_and_halts(tmp_path: Path) -> None:
    executor = HalfBooked(RuntimeError("unused"))
    agent = SpotAgent(
        OnchainosSource(FakeCli(), clock=lambda: NOW),
        AgentConfig(),
        executor=executor,  # type: ignore[arg-type]
    )
    times = iter(range(NOW, NOW + 100_000, 60))
    with ExecutionJournal(tmp_path / "journal.db", policy()) as journal:
        code = run_loop(
            agent,
            journal,
            tmp_path / "state.json",
            interval=60,
            max_ticks=5,
            call_budget=100_000,
            spent=0,
            should_stop=lambda: False,
            clock=lambda: float(next(times)),
            sleep=lambda s: None,
        )
    kinds = [r["kind"] for r in read_journal(tmp_path / "journal.db")[1]]
    assert code == 4 and "live_buy" in kinds and kinds[-1] == "tick_error"
    assert TOKEN in agent.state.positions  # the booked trade is not rolled back


def live_policy() -> dict[str, Any]:
    return {"schema": LIVE_SCHEMA, "config": AgentConfig().model_dump()}


def open_live(run: Path) -> ExecutionJournal:
    """A live journal shaped like a real run: live policy plus its live_start record."""
    run.mkdir(exist_ok=True)
    journal = ExecutionJournal(run / "journal.db", live_policy())
    if not any(r["kind"] == "live_start" for r in read_journal(run / "journal.db")[1]):
        start = {"at": NOW, "kind": "live_start", "cash_usd_micros": 1_000_000_000}
        journal.append("live_start", start | {"execution_eligible": False})
    return journal


def run_live(run: Path, executor: Any, cli: FakeCli | None = None, ticks: int = 5) -> int:
    open_live(run).close()
    state, spent = resume(run, AgentConfig())
    agent = SpotAgent(
        OnchainosSource(cli or FakeCli(), clock=lambda: NOW),
        AgentConfig(),
        state,
        executor=executor,
    )
    times = iter(range(NOW, NOW + 100_000, 60))
    with ExecutionJournal(run / "journal.db", live_policy()) as journal:
        return run_loop(
            agent,
            journal,
            run / "state.json",
            interval=60,
            max_ticks=ticks,
            call_budget=100_000,
            spent=spent,
            should_stop=lambda: False,
            clock=lambda: float(next(times)),
            sleep=lambda s: None,
        )


class JournalingExecutor(SubmittingExecutor):
    """Writes intent/fill like the real executor; `hidden` extra submissions get no fill."""

    def __init__(self, emit: Any, hidden: int) -> None:
        super().__init__(RuntimeError("unused"))
        self.emit, self.hidden = emit, hidden

    def buy(self, token: str, usdc_amount: int) -> Any:
        body = {"side": "buy", "token": token, "execution_eligible": False}
        for n in range(self.hidden):
            self.submissions += 1
            self.emit("live_intent", body | {"kind": "live_intent", "intent_id": f"hidden{n}"})
        self.submissions += 1
        tx = "0x" + "cd" * 32
        self.emit("live_intent", body | {"kind": "live_intent", "intent_id": "seen"})
        self.emit("live_fill", body | {"kind": "live_fill", "intent_id": "seen", "tx_hash": tx})
        return Fill("buy", token, tx, 7, -usdc_amount, usdc_amount * 1_000, "seen")


@pytest.mark.parametrize("booked", [True, False])
def test_unexplained_submission_survives_restart(tmp_path: Path, booked: bool) -> None:
    run = tmp_path / "run"
    db = run / "journal.db"
    if booked:
        # One proven, booked buy plus one hidden submission in the same tick.
        with open_live(run) as side:
            executor: Any = JournalingExecutor(side.append, hidden=1)
            assert run_live(run, executor, ticks=1) == 4
    else:
        # A submission that books nothing (outcome swallowed).
        assert run_live(run, SilentSubmitter(RuntimeError("unused"))) == 4
    kinds = [r["kind"] for r in read_journal(db)[1]]
    # The halt is persisted, and it precedes the tick's own summary/error records.
    assert "live_halt" in kinds
    assert kinds.index("live_halt") < len(kinds) - 1
    with pytest.raises(ValueError, match="halted for human review"):
        resume(run, AgentConfig())
    assert build_report(run)["run_status"]["resumable"] is False


def test_refused_live_sell_halts_and_stays_halted_across_restart(tmp_path: Path) -> None:
    run = tmp_path / "run"
    cli = FakeCli()

    # The real executor over a scripted wallet: the journal holds production-shaped
    # intent/fill records, so the lifecycle checks see exactly what a live run writes.
    wallet = FakeWallet(usdc=1_000_000_000)
    wallet.fill_out = 200_000_000_000  # token units received for the 200 USD buy
    with open_live(run) as side:
        executor = LiveExecutor(
            gate(),
            read=wallet.read,
            submit=wallet.submit,
            receipt=lambda _tx: (1, 123, APPROVED_WALLET),
            emit=side.append,
            clock=lambda: NOW,
            sleep=lambda _s: None,
            settle_attempts=1,
        )
        assert run_live(run, executor, cli, ticks=1) == 0  # entry booked, run continues
        wallet.balances[TOKEN] = 0  # the tokens left the wallet outside the agent
        cli.sell_ratio = (60, 100)  # stop-loss forces a sell the executor refuses
        assert run_live(run, executor, cli, ticks=5) == 4
    assert executor.submissions == 1  # no further entry after the refusal
    with pytest.raises(ValueError, match="human_review_halt"):
        resume(run, AgentConfig())
    # The report derives the same verdict from the same journal: no unknown order, yet
    # not resumable, with the held position still shown.
    report = build_report(run)
    status = report["run_status"]
    assert status["unknown_live_orders"] == 0 and status["settled_live_orders"] == 1
    assert status["resumable"] is False
    assert {"human_review_halt", "live_halt_recorded"} <= set(status["blocking_reasons"])
    assert [p["token"] for p in report["open_positions"]] == [TOKEN]


def test_shadow_transient_errors_keep_resuming(tmp_path: Path) -> None:
    run = tmp_path / "run"
    cli = FakeCli()
    cli.fail = {("token", "hot-tokens")}  # discovery failure: observation-only
    loop(run, cli, 2)
    resume(run, AgentConfig())  # must not raise
    assert build_report(run)["run_status"]["resumable"] is True


def test_held_position_is_managed_before_outcome_labels() -> None:
    cli = FakeCli()
    bot = SpotAgent(OnchainosSource(cli, clock=lambda: NOW), AgentConfig())
    bot.tick(NOW)
    assert TOKEN in bot.state.positions and bot.state.outcomes.due(NOW + 3_600)
    cli.calls.clear()
    cli.sell_ratio = (60, 100)  # stop-loss at the same time labels fall due
    records = bot.tick(NOW + 3_600)
    first = [c[:2] for c in cli.calls[:3]]
    assert first[0] == ("security", "token-scan") and first[1] == ("swap", "quote")
    assert ("market", "prices") in [c[:2] for c in cli.calls]  # labels still run after
    assert any(r["kind"] == "paper_sell" for r in records)


def test_low_budget_runs_manage_only_cycles_and_then_stops_visibly(tmp_path: Path) -> None:
    run = tmp_path / "run"
    cli = FakeCli()
    agent, _ = loop(run, cli, 1)
    assert TOKEN in agent.state.positions
    _, spent = resume(run, AgentConfig())
    full = tick_budget(AgentConfig())
    # Enough for one manage-only cycle (2 calls for one held position), not a full one.
    budget = spent + 3
    agent, code = loop(run, cli, 5, call_budget=budget)
    assert code == 0
    records = read_journal(run / "journal.db")[1]
    after = [r for r in records if r["at"] > NOW or r["kind"] == "budget_stop"]
    summaries = [r for r in records if r["kind"] == "tick_summary"]
    managed = [s for s in summaries if s.get("cycle") == "manage_only"]
    # Each manage-only cycle reserves its worst case (2 per held position) but may use
    # less (a cached security read); cycles continue only while that worst case fits.
    assert managed and all(0 < m["provider_calls"] <= 2 for m in managed)
    assert all(m["deferred"] == ["outcome_labels", "discovery"] for m in managed)
    assert not any(r["kind"] in ("paper_buy", "assessment") for r in after if r["at"] > NOW)
    stop = records[-1]
    assert stop["kind"] == "budget_stop" and stop["open_positions"] == 1
    assert stop["calls_used"] <= budget and stop["reserved_needed"] < full
    assert budget - stop["calls_used"] < stop["reserved_needed"]  # stopped only when needed
    # Restart keeps cumulative usage, so the same budget admits no further cycle.
    _, spent_again = resume(run, AgentConfig())
    assert spent_again == stop["calls_used"] == spent + sum(m["provider_calls"] for m in managed)
    _, code = loop(run, cli, 5, call_budget=budget)
    assert code == 0 and read_journal(run / "journal.db")[1][-1]["kind"] == "budget_stop"
    report = build_report(run)
    assert report["operations"]["manage_only_ticks"] == len(managed)
    assert report["operations"]["budget_stop"]["open_positions"] == 1
    assert report["run_status"]["resumable"] is True  # resource stop is not a trading halt


def test_manage_only_cycle_still_exits_on_stop_loss(tmp_path: Path) -> None:
    run = tmp_path / "run"
    cli = FakeCli()
    loop(run, cli, 1)
    _, spent = resume(run, AgentConfig())
    cli.sell_ratio = (60, 100)
    agent, code = loop(run, cli, 1, call_budget=spent + 2)
    assert code == 0 and agent.state.positions == {}
    records = read_journal(run / "journal.db")[1]
    assert next(r for r in records if r["kind"] == "paper_sell")["reason"] == "stop_loss"


def test_no_position_and_short_budget_stops_without_a_cycle(tmp_path: Path) -> None:
    run = tmp_path / "run"
    agent, code = loop(run, FakeCli(), 3, call_budget=10)
    assert code == 0 and agent.provider_calls() == 0
    records = read_journal(run / "journal.db")[1]
    assert [r["kind"] for r in records] == ["budget_stop"]
    assert records[0]["open_positions"] == 0


def test_cycle_plan_never_exceeds_the_remaining_budget() -> None:
    config = AgentConfig()
    for remaining in range(0, tick_budget(config, 15) + 2):
        for held in range(config.max_positions + 1):
            for per_swap in (0, 15):
                cycle, reserved = plan_cycle(remaining, held, config, per_swap)
                if cycle != "stop":
                    assert reserved <= remaining
                if cycle == "manage_only":
                    assert held and reserved == manage_budget(held, per_swap)


def live_loop(run: Path, executor: Any, cli: FakeCli, ticks: int, budget: int) -> int:
    state, spent = resume(run, AgentConfig())
    agent = SpotAgent(
        OnchainosSource(cli, clock=lambda: NOW), AgentConfig(), state, executor=executor
    )
    times = iter(range(NOW, NOW + 100_000, 60))
    with ExecutionJournal(run / "journal.db", live_policy()) as journal:
        return run_loop(
            agent,
            journal,
            run / "state.json",
            interval=60,
            max_ticks=ticks,
            call_budget=budget,
            spent=spent,
            should_stop=lambda: False,
            clock=lambda: float(next(times)),
            sleep=lambda s: None,
        )


@pytest.mark.parametrize("short", [0, 1])
def test_live_manage_only_reserves_a_full_sell_or_submits_nothing(
    tmp_path: Path, short: int
) -> None:
    run, cli = tmp_path / "run", FakeCli()
    wallet = FakeWallet(usdc=1_000_000_000)
    wallet.fill_out = 200_000_000_000

    def executor_for(emit: Any) -> LiveExecutor:
        # One executor per process, as in production: its call counter starts at zero.
        return LiveExecutor(
            gate(),
            read=wallet.read,
            submit=wallet.submit,
            receipt=lambda _tx: (1, 123, APPROVED_WALLET),
            emit=emit,
            clock=lambda: NOW,
            sleep=lambda _s: None,
            settle_attempts=1,
        )

    with open_live(run) as side:
        assert live_loop(run, executor_for(side.append), cli, 1, 100_000) == 0  # buy booked
        _, spent = resume(run, AgentConfig())
        second = executor_for(side.append)
        need = manage_budget(1, second.calls_per_swap())
        wallet.fill_out = 150_000_000  # USDC the sell returns
        cli.sell_ratio = (60, 100)  # stop-loss
        submitted = len(wallet.submits)
        code = live_loop(run, second, cli, 1, spent + need - short)
    records = read_journal(run / "journal.db")[1]
    assert code == 0
    if short:
        # Not enough for a worst-case sell: nothing is submitted and the stop is visible.
        assert len(wallet.submits) == submitted
        assert records[-1]["kind"] == "budget_stop" and records[-1]["open_positions"] == 1
    else:
        assert len(wallet.submits) == submitted + 1
        assert any(r["kind"] == "live_sell" for r in records)
        assert resume(run, AgentConfig())[1] <= spent + need  # never above the budget
    assert build_report(run)["run_status"]["resumable"] is True
