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
from oasis_agent.launch.spot_executor import Fill, LiveRefused, LiveUnresolved
from oasis_agent.launch.spot_report import read_journal
from oasis_agent.sniping.journal import ExecutionJournal
from scripts.run_spot_agent import resume, run_loop, save_state, tick_budget

from .test_spot_agent import NOW, TOKEN, FakeCli


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
    assert code == 0 and cli.calls == []  # budget already consumed by the earlier process


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
    live = {"schema": "oasis.spot-agent-live.v1", "config": AgentConfig().model_dump()}
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
