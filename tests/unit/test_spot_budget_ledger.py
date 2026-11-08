"""Budget ledger strictness through a real journal, and live-start admission before any call."""

import argparse
import contextlib
from pathlib import Path
from typing import Any

import pytest

import scripts.run_spot_agent as runner
from oasis_agent.launch.onchainos_source import OnchainosSource
from oasis_agent.launch.spot_agent import AgentConfig, AgentState
from oasis_agent.launch.spot_budget import (
    BudgetLedgerError,
    budget_ledger,
    full_cycle_calls,
    legacy_error_charge,
)
from oasis_agent.launch.spot_executor import APPROVED_WALLET, LiveExecutor, swap_call_bound
from oasis_agent.launch.spot_lifecycle import LIVE_SCHEMA, SHADOW_SCHEMA
from oasis_agent.launch.spot_report import build_report, read_journal
from oasis_agent.sniping.journal import ExecutionJournal
from scripts.run_spot_agent import resume

from .test_spot_agent import NOW, FakeCli
from .test_spot_executor import FakeWallet, gate
from .test_spot_lifecycle import SUMMARY


def reserve(key: str, amount: object) -> dict[str, Any]:
    return {"kind": "budget_reserve", "reserve_id": key, "reserved": amount}


def settle(key: str, used: object) -> dict[str, Any]:
    return {"kind": "budget_settle", "reserve_id": key, "used": used}


def journal_records(tmp_path: Path, rows: list[dict[str, Any]], schema: str = SHADOW_SCHEMA):
    run = tmp_path / "run"
    run.mkdir()
    policy = {"schema": schema, "config": AgentConfig().model_dump(mode="json")}
    with ExecutionJournal(run / "journal.db", policy) as journal:
        for r in rows:
            journal.append(r["kind"], r | {"execution_eligible": False})
    return run, *read_journal(run / "journal.db")


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        ([reserve("a", 5), reserve("a", 6)], "conflicting reservations"),
        ([reserve("a", 5), settle("a", -1)], "not a non-negative integer"),
        ([reserve("a", 5), settle("a", True)], "not a non-negative integer"),
        ([reserve("a", 5), settle("a", 1.0)], "not a non-negative integer"),
        ([reserve("a", 5), settle("a", "1")], "not a non-negative integer"),
        ([reserve("a", -5)], "reservation is not"),
        ([reserve("a", False)], "reservation is not"),
        ([settle("z", 0)], "without a prior reservation"),
        ([reserve("a", 5), settle("a", 6)], "exceeds its reservation"),
        ([reserve("a", 5), settle("a", 1), settle("a", 2)], "conflicting settlements"),
        ([reserve("", 5)], "without a reserve_id"),
        ([{"kind": "budget_settle", "used": 0}], "without a reserve_id"),
    ],
)
def test_malformed_budget_events_refuse_resume_and_report(
    tmp_path: Path, rows: list[dict[str, Any]], message: str
) -> None:
    run, policy, records = journal_records(tmp_path, rows)
    with pytest.raises(BudgetLedgerError, match=message):
        budget_ledger(records, legacy_error_charge(policy))
    # The runner's resume refuses (main maps ValueError to exit 2); the report raises.
    with pytest.raises(ValueError, match=message):
        resume(run, AgentConfig())
    with pytest.raises(ValueError, match=message):
        build_report(run)


def test_identical_repeats_are_idempotent_and_unsettled_is_charged_in_full(
    tmp_path: Path,
) -> None:
    rows = [reserve("a", 5), reserve("a", 5), settle("a", 2), settle("a", 2), reserve("b", 7)]
    _, policy, records = journal_records(tmp_path, rows)
    ledger = budget_ledger(records, legacy_error_charge(policy))
    assert ledger["charged"] == 2 + 7 and ledger["confirmed"] == 2
    assert ledger["unsettled_reserved"] == 7 and ledger["unsettled_reservations"] == 1


def test_legacy_failure_bound_comes_from_the_run_schema() -> None:
    config = AgentConfig().model_dump(mode="json")
    shadow = legacy_error_charge({"schema": SHADOW_SCHEMA, "config": config})
    assert shadow == full_cycle_calls(8, 3)
    for schema in (LIVE_SCHEMA, "oasis.spot-agent-live.v1"):
        live = legacy_error_charge({"schema": schema, "config": config})
        assert live == full_cycle_calls(8, 3, swap_call_bound()) > shadow
    with pytest.raises(BudgetLedgerError, match="unknown run schema"):
        legacy_error_charge({"schema": "oasis.other", "config": config})
    with pytest.raises(BudgetLedgerError, match="no valid cycle bound"):
        legacy_error_charge({"schema": SHADOW_SCHEMA, "config": {"enrich_top": -1}})


class CountingWallet(FakeWallet):
    """FakeWallet counting every CLI read; optionally failing each one."""

    def __init__(self, fail: bool = False) -> None:
        super().__init__(usdc=1_000_000_000)
        self.read_count, self.fail = 0, fail

    def read(self, args: list[str]) -> Any:
        self.read_count += 1
        if self.fail:
            raise OSError("provider down")
        return super().read(args)


def start_live(
    run: Path, monkeypatch: pytest.MonkeyPatch, wallet: CountingWallet, budget: int
) -> Any:
    """The real _build_agent fresh-live path; only network constructors are stubbed."""
    run.mkdir(exist_ok=True)
    policy = {"schema": LIVE_SCHEMA, "config": AgentConfig().model_dump(mode="json")}
    with ExecutionJournal(run / "journal.db", policy) as journal:
        executor = LiveExecutor(
            gate(),
            read=wallet.read,
            submit=wallet.submit,
            receipt=lambda _tx: (1, 123, APPROVED_WALLET),
            emit=journal.append,
            clock=lambda: NOW,
            sleep=lambda _s: None,
        )
        monkeypatch.setattr(runner, "LiveExecutor", lambda *a, **k: executor)
        monkeypatch.setattr(runner, "JsonRpcTransport", lambda url: None)
        monkeypatch.setattr(runner, "RpcClient", lambda *a, **k: None)
        monkeypatch.setenv("SPOT_TEST_RPC", "unused")
        args = argparse.Namespace(rpc_env="SPOT_TEST_RPC", binary=Path("/bin/true"))
        source = OnchainosSource(FakeCli(), clock=lambda: NOW)
        return runner._build_agent(
            args, gate(), AgentConfig(), AgentState(0), source, None, journal, budget
        )


def kinds(run: Path) -> list[str]:
    return [r["kind"] for r in read_journal(run / "journal.db")[1]]


def test_zero_budget_live_start_queries_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wallet = CountingWallet()
    assert start_live(tmp_path / "run", monkeypatch, wallet, 0) is None
    assert wallet.read_count == 0
    assert kinds(tmp_path / "run") == ["budget_stop"]
    stop = read_journal(tmp_path / "run" / "journal.db")[1][0]
    assert stop["stage"] == "live_start_balance" and stop["open_positions"] is None
    assert "live_start" not in kinds(tmp_path / "run")  # no invented cash


def test_budget_of_one_admits_exactly_the_balance_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wallet = CountingWallet()
    agent = start_live(tmp_path / "run", monkeypatch, wallet, 1)
    assert agent is not None and wallet.read_count == 1
    assert kinds(tmp_path / "run") == ["budget_reserve", "budget_settle", "live_start"]


def test_failed_start_restarts_never_exceed_the_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, wallet = tmp_path / "run", CountingWallet(fail=True)
    for _ in range(5):
        with contextlib.suppress(OSError):
            start_live(run, monkeypatch, wallet, 1)
    # The first failed read's reservation stays unsettled and is charged in full, so
    # every later restart is refused before touching the provider.
    assert wallet.read_count == 1
    assert kinds(run) == ["budget_reserve"] + ["budget_stop"] * 4
    _, charged = resume(run, AgentConfig())
    assert charged == 1


def summary(calls: object) -> dict[str, Any]:
    return SUMMARY | {"at": NOW, "provider_calls": calls}


def stop(used: object) -> dict[str, Any]:
    return {"kind": "budget_stop", "at": NOW, "calls_used": used, "open_positions": 0}


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        ([stop(2.9)], "budget_stop.calls_used"),  # was truncated to 2
        ([stop(True)], "budget_stop.calls_used"),
        ([stop("3")], "budget_stop.calls_used"),
        ([stop(-1)], "budget_stop.calls_used"),
        ([{"kind": "budget_stop", "at": NOW}], "budget_stop.calls_used"),
        ([summary(10), summary(-9)], "tick_summary.provider_calls"),  # was summed to 1
        ([summary(1.5)], "tick_summary.provider_calls"),
        ([{k: v for k, v in SUMMARY.items() if k != "provider_calls"}], "provider_calls"),
        ([{"kind": "budget_future", "reserved": 106}], "unsupported budget event"),
        ([reserve("a", 5), {"kind": "budget_refund", "used": 5}], "unsupported budget event"),
    ],
)
def test_legacy_and_unknown_budget_input_is_refused_not_lowered(
    tmp_path: Path, rows: list[dict[str, Any]], message: str
) -> None:
    run, policy, records = journal_records(tmp_path, rows)
    with pytest.raises(BudgetLedgerError, match=message):
        budget_ledger(records, legacy_error_charge(policy))
    with pytest.raises(ValueError, match=message):
        resume(run, AgentConfig())
    with pytest.raises(ValueError, match=message):
        build_report(run)


def test_valid_legacy_journal_keeps_its_conservative_bound(tmp_path: Path) -> None:
    error = {"kind": "tick_error", "at": NOW, "error_type": "OSError"}
    rows = [summary(4), error, summary(3), stop(5), {"kind": "halt_review", "reason": "x"}]
    run, policy, records = journal_records(tmp_path, rows)
    charge = 4 + 3 + legacy_error_charge(policy)
    assert budget_ledger(records, legacy_error_charge(policy))["charged"] == charge
    assert resume(run, AgentConfig())[1] == charge
    assert build_report(run)["operations"]["budget"]["charged"] == charge
    # Then reserve/settle continue on top of the legacy charge.
    with ExecutionJournal(run / "journal.db", policy) as journal:
        journal.append("budget_reserve", reserve("a", 9) | {"execution_eligible": False})
        journal.append("budget_settle", settle("a", 2) | {"execution_eligible": False})
    assert resume(run, AgentConfig())[1] == charge + 2
