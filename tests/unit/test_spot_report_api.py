"""Shadow run -> hash-linked journal -> report/API, with tamper and authority checks."""

import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any

from oasis_agent.launch.onchainos_source import OnchainosSource
from oasis_agent.launch.spot_agent import AgentConfig, SpotAgent
from oasis_agent.launch.spot_report import build_report, signal_quality
from oasis_agent.sniping.journal import ExecutionJournal
from scripts.run_spot_agent import save_state

from .server_helpers import make_server_config
from .test_server_api import client_for
from .test_spot_agent import NOW, FakeCli


def shadow_run(run: Path, ticks: list[tuple[int, tuple[int, int]]]) -> SpotAgent:
    """Drive the agent through scripted sell prices and journal every record."""
    cli = FakeCli()
    config = AgentConfig(initial_cash_usd_micros=1_000_000_000)
    bot = SpotAgent(OnchainosSource(cli, clock=lambda: NOW), config)
    policy = {"schema": "oasis.spot-agent-shadow.v1", "config": config.model_dump(mode="json")}
    run.mkdir(parents=True, exist_ok=True)
    with ExecutionJournal(run / "journal.db", policy) as journal:
        for at, ratio in ticks:
            cli.sell_ratio = ratio
            for record in bot.tick(at):
                journal.append(record["kind"], record)
    save_state(run / "state.json", bot.state)
    return bot


def test_report_closes_the_loop_from_entry_to_take_profit(tmp_path: Path) -> None:
    run = tmp_path / "run"
    shadow_run(run, [(NOW, (99, 100)), (NOW + 60, (130, 100))])
    report = build_report(run)
    assert report["execution_eligible"] is False and report["mode"] == "shadow"
    book = report["paper_book"]
    assert book["closed_trades"] == 1 and book["winning_trades"] == 1
    assert book["by_exit_reason"]["take_profit"]["n"] == 1
    assert book["realized_pnl_usd_micros"] > 0
    assert report["operations"]["ticks"] == 2 and report["operations"]["halts"] == 0


def test_signal_quality_needs_samples_and_compares_medians() -> None:
    def outcome(action: str, value: int) -> dict[str, Any]:
        return {
            "kind": "outcome",
            "status": "observed",
            "action": action,
            "horizon_seconds": 3600,
            "forward_return_bps": value,
        }

    few = [outcome("flip", 500)] * 5 + [outcome("skip", -100)] * 40
    assert signal_quality(few)["verdict_1h"] == "insufficient_samples"
    # One huge outlier must not decide: the flip median is below the baseline median.
    skewed = [outcome("flip", -50)] * 29 + [outcome("flip", 1_000_000)] + [outcome("skip", 0)] * 30
    assert signal_quality(skewed)["verdict_1h"] == "no_selection_edge"
    good = [outcome("flip", 300)] * 30 + [outcome("watch", -20)] * 30
    quality = signal_quality(good)
    assert quality["verdict_1h"] == "flip_outperforms"
    assert quality["by_horizon_seconds"]["3600"]["flip"]["hit_rate_bps"] == 10_000


def configured(tmp_path: Path, run: Path) -> Any:
    config = make_server_config(tmp_path)
    object.__setattr__(config, "databases", replace(config.databases, spot_run=run))
    return client_for(tmp_path, config=config)


def test_api_serves_report_decisions_and_filtered_assessments(tmp_path: Path) -> None:
    run = tmp_path / "run"
    shadow_run(run, [(NOW, (99, 100)), (NOW + 60, (90, 100))])
    with configured(tmp_path, run) as client:
        report = client.get("/api/v1/spot/report").json()
        assert report["paper_book"]["by_exit_reason"]["stop_loss"]["n"] == 1
        decisions = client.get("/api/v1/spot/decisions").json()["items"]
        assert [d["kind"] for d in decisions] == ["paper_sell", "paper_buy"]  # newest first
        flips = client.get("/api/v1/spot/assessments?action=flip").json()["items"]
        assert flips and all(a["action"] == "flip" for a in flips)
        assert client.get("/api/v1/spot/assessments?action=buy").status_code == 422
        assert client.post("/api/v1/spot/report").status_code == 405


def test_api_fails_closed_when_unconfigured_missing_or_tampered(tmp_path: Path) -> None:
    with client_for(tmp_path) as client:
        assert client.get("/api/v1/spot/report").status_code == 404
    run = tmp_path / "run"
    run.mkdir()
    with configured(tmp_path, run) as client:
        assert client.get("/api/v1/spot/report").status_code == 503
    shadow_run(run, [(NOW, (99, 100))])
    db = sqlite3.connect(run / "journal.db")
    raw = db.execute("SELECT payload FROM execution_events WHERE seq=1").fetchone()[0]
    forged = raw.replace(b'"mode":"shadow"', b'"mode":"live!!"')
    db.execute("UPDATE execution_events SET payload=? WHERE seq=1", (forged,))
    db.commit()
    db.close()
    with configured(tmp_path, run) as client:
        for path in ("report", "decisions", "assessments"):
            response = client.get("/api/v1/spot/" + path)
            assert response.status_code == 503
            assert "live" not in response.text


def test_journal_record_claiming_execution_authority_is_refused(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    policy = {"schema": "oasis.spot-agent-shadow.v1", "config": AgentConfig().model_dump()}
    with ExecutionJournal(run / "journal.db", policy) as journal:
        journal.append("paper_buy", {"execution_eligible": True, "at": NOW})
    with configured(tmp_path, run) as client:
        assert client.get("/api/v1/spot/decisions").status_code == 503
