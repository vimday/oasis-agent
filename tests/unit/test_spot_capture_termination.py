"""A complete capture needs the run's own end record; closed cycles prove only a prefix."""

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from oasis_agent.launch.spot_capture import ReplayRefused, replay_session
from oasis_agent.launch.spot_coverage import coverage_report
from oasis_agent.sniping.journal import ExecutionJournal

from .test_spot_agent import FakeCli
from .test_spot_capture_replay import (
    Scripted,
    archive_records,
    capture_run,
    rewrite,
    tick_records,
)


def fail_capture(monkeypatch: pytest.MonkeyPatch, should_fail: Any) -> None:
    """Make capture.db appends raise when `should_fail(kind, payload)`; run journal untouched."""
    original = ExecutionJournal.append

    def append(self: ExecutionJournal, kind: str, payload: dict[str, Any]) -> Any:
        if self.path.name == "capture.db" and should_fail(kind, payload):
            raise sqlite3.OperationalError("disk I/O error")
        return original(self, kind, payload)

    monkeypatch.setattr(ExecutionJournal, "append", append)


def replay(run: Path) -> dict[str, Any]:
    return replay_session(run / "capture.db", run / "raw")


def test_normal_run_is_complete_with_its_end_reason(tmp_path: Path) -> None:
    result = capture_run(tmp_path / "run", FakeCli(), ticks=3)
    out = replay(tmp_path / "run")
    assert out["session_complete"] is True and out["cycles_replayed"] == 3
    assert out["termination"] == {"status": "ended", "reason": "max_ticks", "cycles": 3}
    assert out["records"] == tick_records(result["run_records"])
    assert archive_records(tmp_path / "run")[-1]["kind"] == "session_end"


def test_zero_cycle_run_is_a_complete_empty_session(tmp_path: Path) -> None:
    # Budget too small for any cycle and nothing held: the loop stops before cycle 1.
    capture_run(tmp_path / "run", FakeCli(), ticks=3, budget=1)
    out = replay(tmp_path / "run")
    assert out["session_complete"] is True and out["cycles_replayed"] == 0
    assert out["termination"]["reason"] == "budget_stop"


def test_lost_cycle_start_write_is_a_prefix_not_a_complete_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The reviewer's probe: cycle 2's start write fails; production finishes 3 cycles.
    baseline = capture_run(tmp_path / "base", FakeCli(), ticks=3)
    fail_capture(monkeypatch, lambda kind, p: kind == "cycle_start" and p["cycle"] == 2)
    broken = capture_run(tmp_path / "run", FakeCli(), ticks=3)
    assert broken["code"] == baseline["code"] == 0
    assert tick_records(broken["run_records"]) == tick_records(baseline["run_records"])
    assert broken["recorder"].healthy is False
    out = replay(tmp_path / "run")
    assert out["cycles_replayed"] == 1
    assert out["session_complete"] is False
    assert out["termination"]["status"] == "unknown_prefix_only"
    # The surviving prefix is still exactly the production prefix.
    first = tick_records(baseline["run_records"])
    assert out["records"] == first[: len(out["records"])]


def test_lost_final_end_write_is_a_prefix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fail_capture(monkeypatch, lambda kind, p: kind == "session_end")
    capture_run(tmp_path / "run", FakeCli(), ticks=2)
    out = replay(tmp_path / "run")
    assert out["cycles_replayed"] == 2 and out["session_complete"] is False


def test_lost_write_inside_a_cycle_leaves_an_open_cycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"n": 0}

    def third_call(kind: str, payload: dict[str, Any]) -> bool:
        calls["n"] += kind == "call"
        return kind == "call" and calls["n"] == 3

    fail_capture(monkeypatch, third_call)
    capture_run(tmp_path / "run", FakeCli(), ticks=2)
    out = replay(tmp_path / "run")
    assert out["cycles_replayed"] == 0 and out["incomplete_cycles"] == [1]
    assert out["session_complete"] is False


def test_truncation_between_closed_cycles_is_a_prefix(tmp_path: Path) -> None:
    capture_run(tmp_path / "run", FakeCli(), ticks=3)

    def cut(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        ends = [i for i, r in enumerate(records) if r["kind"] == "cycle_end"]
        return records[: ends[1] + 1]  # cycles 1-2 closed, no session_end

    rewrite(tmp_path / "run", cut)
    out = replay(tmp_path / "run")
    assert out["cycles_replayed"] == 2 and out["session_complete"] is False
    assert out["incomplete_cycles"] == []  # closed cycles alone are not completeness


def forged_end(**change: Any) -> Any:
    def edit(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        index = next(i for i, r in enumerate(records) if r["kind"] == "session_end")
        records[index] = records[index] | change
        return records

    return edit


def duplicate_end(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [*records, records[-1]]


def end_inside_cycle(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    end = records.pop()
    last_end = max(i for i, r in enumerate(records) if r["kind"] == "cycle_end")
    return [*records[:last_end], end, records[last_end]]


def input_after_end(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    call = next(r for r in records if r["kind"] == "call")
    return [*records, call]


@pytest.mark.parametrize(
    ("edit", "category"),
    [
        (forged_end(cycles=2), "session_end_count_mismatch"),
        (forged_end(calls=1), "session_end_count_mismatch"),
        (forged_end(reason="looked_fine"), "session_end_malformed"),
        (duplicate_end, "record_after_session_end"),
        (end_inside_cycle, "session_end_inside_cycle"),
        (input_after_end, "record_after_session_end"),
    ],
)
def test_forged_or_misplaced_end_records_are_refused(
    tmp_path: Path, edit: Any, category: str
) -> None:
    capture_run(tmp_path / "run", FakeCli(), ticks=3)
    rewrite(tmp_path / "run", edit)
    with pytest.raises(ReplayRefused) as refused:
        replay(tmp_path / "run")
    assert refused.value.category == category


def test_supported_provider_error_tick_replays_its_error_and_rollback(tmp_path: Path) -> None:
    # OSError on the 6th onchainos call (a quote) raises out of tick; the runner rolls
    # the state back and journals tick_error. Replay must reproduce that exact cycle.
    cli = Scripted(FakeCli(), {6: OSError("pipe closed")})
    result = capture_run(tmp_path / "run", cli, ticks=2)
    kinds = [r["kind"] for r in result["run_records"]]
    assert kinds.count("tick_error") == 1
    out = replay(tmp_path / "run")
    assert out["session_complete"] is True
    failed = [c for c in out["cycles"] if c["error_type"]]
    assert [c["error_type"] for c in failed] == ["OSError"]
    markers = [r for r in out["records"] if r["kind"] == "cycle_failed"]
    assert len(markers) == 1 and markers[0]["state_rolled_back"] is True
    # The failed cycle stays in the coverage denominator, as unknown coverage.
    report = coverage_report(out["records"])
    assert report["cycles"]["failed"] == 1 and report["cycles"]["total"] == 2
    assert report["unknown_cycles"] == 1
    # The run journal's own view agrees.
    journal_view = coverage_report(result["run_records"])
    assert journal_view["cycles"] == report["cycles"]
