"""Coverage denominators from capture -> SpotAgent -> replay: sources, fates, label units."""

from pathlib import Path
from typing import Any

from oasis_agent.launch.outcomes import HORIZONS, MAX_LATENESS_SECONDS
from oasis_agent.launch.spot_agent import AgentConfig
from oasis_agent.launch.spot_capture import replay_session
from oasis_agent.launch.spot_coverage import coverage_report
from scripts.run_spot_agent import manage_budget, tick_budget

from .test_spot_agent import NOW, TOKEN, FakeCli, hot_row
from .test_spot_capture_replay import capture_run, tick_records

OTHER = [
    "0x" + f"{i:02x}" * 20 for i in (0xC1, 0xC2, 0xC3, 0xC4, 0xC5, 0xC6, 0xC7, 0xC8, 0xC9, 0xCA)
]


def run_and_replay(
    tmp_path: Path, cli: Any, **kwargs: Any
) -> tuple[dict[str, Any], dict[str, Any]]:
    result = capture_run(tmp_path / "run", cli, **kwargs)
    replay = replay_session(tmp_path / "run" / "capture.db", tmp_path / "run" / "raw")
    assert replay["records"] == tick_records(result["run_records"])  # same decisions
    return result, replay


def summaries(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in records if r["kind"] == "tick_summary"]


def test_source_counts_keep_invalid_duplicate_and_overlapping_nominations(tmp_path: Path) -> None:
    cli = FakeCli()
    cli.hot = [
        hot_row(),
        hot_row(),  # duplicate of TOKEN within hot-tokens
        hot_row(tokenContractAddress="not-an-address"),  # invalid row
        hot_row(tokenContractAddress=OTHER[0]),
        "garbage",  # not an object
    ]
    cli.signals = [
        *cli.signals,  # names TOKEN again: overlaps hot-tokens
        {
            "token": {"tokenAddress": OTHER[1]},
            "timestamp": str((NOW - 60) * 1000),
            "walletType": "1",
            "triggerWalletAddress": "0x01",
            "soldRatioPercent": "0",
        },
        {"token": {"tokenAddress": "bad"}, "walletType": "1"},  # invalid
    ]
    _, replay = run_and_replay(tmp_path, cli, ticks=1)
    coverage = summaries(replay["records"])[0]["coverage"]
    assert coverage["discovery_sources"] == {
        "hot_tokens": {"status": "ok", "rows": 5, "valid": 3, "unique": 2, "new": 2},
        "signals": {"status": "ok", "rows": 3, "valid": 2, "unique": 2, "new": 1},
    }
    # `new` sums to the unique candidates, which the fates then partition.
    assert coverage["discovered"] == 3
    report = coverage_report(replay["records"])
    assert report["fates_reconcile_where_observed"] is True
    assert report["discovery_sources"]["by_source"]["hot_tokens"]["rows"] == 5


def test_failed_signal_source_is_visible_not_an_empty_source(tmp_path: Path) -> None:
    cli = FakeCli()
    cli.fail = {("signal", "list")}
    _, replay = run_and_replay(tmp_path, cli, ticks=1)
    sources = summaries(replay["records"])[0]["coverage"]["discovery_sources"]
    assert sources["signals"] == {"status": "failed", "rows": 0, "valid": 0, "unique": 0, "new": 0}
    report = coverage_report(replay["records"])
    assert report["discovery_sources"]["by_source"]["signals"]["cycles_failed"] == 1


class FailAdvancedFor(FakeCli):
    """advanced-info fails (provider_error) for exactly one token; all else as FakeCli."""

    def __init__(self, token: str) -> None:
        super().__init__()
        self.bad = token

    def __call__(self, argv: Any) -> bytes:
        argv = tuple(argv)
        if argv[:2] == ("token", "advanced-info") and self.bad in argv:
            return b'{"ok":false}'
        return super().__call__(argv)


def test_partial_enrich_failure_and_quota_stay_in_the_denominator(tmp_path: Path) -> None:
    cli = FailAdvancedFor(OTHER[1])
    cli.hot = [hot_row(tokenContractAddress=t) for t in OTHER]  # 10 > enrich_top=8
    cli.signals = []
    _, replay = run_and_replay(tmp_path, cli, ticks=1)
    report = coverage_report(replay["records"])
    fates = report["candidate_fates"]
    assert report["discovered_candidate_cycles"] == 10
    assert fates["enrich_failed"] == 1 and fates["not_reached_enrich_quota"] == 2
    assert fates["enrich_failed"] + fates["assessed_after_enrich"] == 8
    assert sum(fates.values()) == 10
    assessed = sum(v["cycle_opportunities"] for v in report["assessments_by_action"].values())
    assert assessed == 10 - 1 - 2  # failed and deferred are never assessments or skips
    assert sum(report["label_registration"].values()) == assessed


def test_label_units_count_samples_times_horizons(tmp_path: Path) -> None:
    """One token, several samples, each with every horizon: labels != tokens."""
    cli = FakeCli()
    cli.hot = [hot_row(tokenContractAddress=OTHER[0])]
    cli.signals = []
    interval = 400  # > 300 s: each cycle registers a new sample of the same token
    _, replay = run_and_replay(
        tmp_path,
        cli,
        ticks=4,
        interval=interval,
        each_cycle=lambda c, now: setattr(cli, "now", now),
    )
    first = summaries(replay["records"])
    registered = sum(s["coverage"]["label_registration"]["registered"] for s in first)
    backlog = first[-1]["coverage"]["label_backlog"]
    assert registered >= 3 and backlog["tokens_due"] <= 1
    # Every registered sample carries every horizon until labelled.
    emitted = sum(sum(s["coverage"]["labels_emitted"].values()) for s in first)
    assert backlog["labels_pending"] + emitted == registered * len(HORIZONS)
    assert backlog["samples_pending"] == registered - sum(
        1
        for r in replay["records"]
        if r["kind"] == "outcome" and r["horizon_seconds"] == HORIZONS[-1]
    )


def test_manage_only_keeps_due_labels_in_the_backlog(tmp_path: Path) -> None:
    """A deferred label is still owed: it stays in the backlog, it is not dropped."""
    cli = FakeCli()
    # Charged calls are actual usage: one full cycle (~7 calls) leaves less than a full
    # cycle's worst case, but enough to keep managing the held position.
    budget = tick_budget(AgentConfig()) + manage_budget(1, 0)
    late = HORIZONS[0] + MAX_LATENESS_SECONDS + 60
    _, replay = run_and_replay(
        tmp_path,
        cli,
        ticks=3,
        budget=budget,
        interval=late,
        each_cycle=lambda c, now: setattr(cli, "now", now),
    )
    rows = summaries(replay["records"])
    deferred = [s for s in rows if s.get("cycle") == "manage_only"]
    assert deferred, "the budget must force manage-only cycles"
    owed = deferred[0]["coverage"]["label_backlog"]
    assert owed["labels_due"] >= 1 and owed["samples_pending"] >= 1
    assert deferred[0]["coverage"]["labels_emitted"] == {"observed": 0, "unobserved": 0}
    report = coverage_report(replay["records"])
    assert report["label_backlog_at_last_covered_cycle"]["labels_pending"] >= 1
    assert report["cycles"]["by_discovery"]["deferred_manage_only"] == len(deferred)


def test_records_without_coverage_are_unknown_not_zero() -> None:
    old = {"kind": "tick_summary", "at": NOW, "provider_calls": 3}
    report = coverage_report([old, {"kind": "tick_error", "at": NOW + 60}])
    assert report["cycles"] == {
        "total": 2,
        "completed": 1,
        "failed": 1,
        "completed_with_coverage": 0,
        "completed_without_coverage_evidence": 1,
        "by_discovery": {},
    }
    assert report["unknown_cycles"] == 2
    assert report["coverage_basis"] == "completed_with_coverage_cycles_only"
    assert report["label_backlog_at_last_covered_cycle"] is None
    assert TOKEN  # fixture import kept for shape parity with the agent tests
