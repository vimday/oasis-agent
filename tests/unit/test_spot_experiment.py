"""Frozen-decision experiment: real assess vs the fixed no-smart-wallet-bonus baseline.

Synthetic fixtures only. They validate the tool and its data contract through the
production path (run_loop -> capture -> replay -> experiment); they say nothing about
real markets.
"""

import json
import sys
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

import scripts.run_spot_experiment as experiment_cli
from oasis_agent.launch.opportunity import (
    Market,
    assess,
    market_from_json,
    market_to_json,
)
from oasis_agent.launch.outcomes import HORIZONS, MAX_LATENESS_SECONDS
from oasis_agent.launch.spot_agent import AgentConfig, decision_policy
from oasis_agent.launch.spot_capture import replay_session
from oasis_agent.launch.spot_experiment import (
    HORIZON,
    ExperimentRefused,
    baseline,
    run_experiment,
)

from .test_spot_agent import NOW, TOKEN, FakeCli, hot_row
from .test_spot_capture_replay import capture_run, rewrite

POLICY = decision_policy(AgentConfig())
QUIET = "0x" + "cd" * 20  # never named by a signal: 0 smart wallets
EXITING = "0x" + "ce" * 20  # smart money selling: a refusal the baseline must keep
END = NOW + 7_200
CUTOFF = END + HORIZON + MAX_LATENESS_SECONDS


class Market3(FakeCli):
    """Three tokens: TOKEN (2 smart wallets, flips), QUIET (none), EXITING (sold out)."""

    def __init__(self) -> None:
        super().__init__()
        self.hot = [
            hot_row(),
            hot_row(tokenContractAddress=QUIET),
            hot_row(tokenContractAddress=EXITING),
        ]
        sold = {
            "token": {"tokenAddress": EXITING},
            "timestamp": str((NOW - 120) * 1000),
            "walletType": "1",
            "triggerWalletAddress": "0x01,0x02,0x03,0x04",
            "soldRatioPercent": "90",
        }
        self.signals = [*self.signals, sold]


def advance(cli: FakeCli) -> Any:
    def step(cycle: int, now: int) -> None:
        cli.now = now  # batch price rows carry the provider's own time

    return step


def frozen(tmp_path: Path, ticks: int = 8, interval: int = 900) -> dict[str, Any]:
    cli = Market3()
    capture_run(tmp_path / "run", cli, ticks=ticks, interval=interval, each_cycle=advance(cli))
    return replay_session(tmp_path / "run" / "capture.db", tmp_path / "run" / "raw")


def experiment(records: list[dict[str, Any]], **overrides: Any) -> dict[str, Any]:
    window = {"start": NOW, "end": END, "label_cutoff": CUTOFF} | overrides
    return run_experiment(
        records, AgentConfig(), evidence="synthetic", inputs={"fixture": True}, **window
    )


def test_market_snapshot_is_lossless_including_unknowns_and_exact_prices() -> None:
    m = Market(
        token=TOKEN,
        symbol="T",
        observed_at=NOW,
        liquidity_usd_micros=None,
        market_cap_usd_micros=5,
        holders=None,
        top10_bps=1,
        unique_traders=None,
        buys=None,
        sells=0,
        first_trade_at=None,
        smart_wallets=3,
        risk_flags=None,
        price=Fraction(1, 3),
        high_24h=Fraction(7, 2),
        provider_disagreement=("liquidity",),
    )
    assert market_from_json(json.loads(json.dumps(market_to_json(m)))) == m
    for bad in (
        {"price": "0.33"},
        {"holders": 1.0},
        {"holders": True},
        {"extra": 1},
        {"schema": "x"},
    ):
        with pytest.raises(ValueError):
            market_from_json(market_to_json(m) | bad)


def test_original_rule_is_rebuilt_and_baseline_only_removes_the_bonus(tmp_path: Path) -> None:
    replay = frozen(tmp_path)
    report = experiment(replay["records"])
    assert report["population"]["original_rule_reproduced"] is True
    assert report["population"]["assessments_rebuilt"] > 0
    rows = {d["token"]: d for d in report["disagreements"]}
    # The smart-wallet control: flip with the bonus, watch without it.
    assert rows[TOKEN]["original"]["action"] == "flip"
    assert rows[TOKEN]["baseline"]["action"] == "watch"
    assert rows[TOKEN]["original"]["score_bps"] - rows[TOKEN]["baseline"]["score_bps"] == 1_250
    # The no-difference control: no smart wallets, no disagreement.
    assert QUIET not in rows
    matrix = report["agreement_matrix"]
    assert (
        matrix["flip->watch"] >= 1
        and sum(matrix.values()) == report["population"]["primary_samples"]
    )
    # Deterministic: the same frozen input yields the same report.
    assert experiment(replay["records"]) == report


def test_baseline_never_relaxes_a_refusal(tmp_path: Path) -> None:
    replay = frozen(tmp_path)
    for r in replay["records"]:
        if r["kind"] != "assessment":
            continue
        m = market_from_json(r["market"])
        original, base = assess(m, POLICY), baseline(m, POLICY)
        # Removing points can only lower the score; it can never create a flip.
        assert base.score_bps <= original.score_bps
        assert not (base.action == "flip" and original.action != "flip")
        if original.action in ("skip", "lp_candidate"):
            assert base == original  # hard blockers and LP routing ignore the bonus
        if "smart_money_exiting" in original.reasons:
            assert "smart_money_exiting" in base.reasons
    # While the sell signal is inside its window the refusal holds in both rules; after
    # the window it ages out of the market itself (the fixture's signal is not renewed).
    exiting = [r for r in replay["records"] if r["kind"] == "assessment" and r["token"] == EXITING]
    active = [r for r in exiting if r["market"]["smart_sold_bps"] is not None]
    assert active and all("smart_money_exiting" in r["reasons"] for r in active)
    for r in active:
        assert "smart_money_exiting" in baseline(market_from_json(r["market"]), POLICY).reasons


def test_sample_rule_keeps_the_earliest_per_token_and_full_denominator(tmp_path: Path) -> None:
    replay = frozen(tmp_path)
    report = experiment(replay["records"])
    population = report["population"]
    assert population["unique_tokens"] == population["primary_samples"] == 3
    assessed = [r for r in replay["records"] if r["kind"] == "assessment"]
    assert population["in_window"] == len([r for r in assessed if NOW <= r["at"] < END])
    assert (
        population["primary_samples"]
        + population["excluded_overlapping_same_token"]
        + population["not_registered"]
        == population["in_window"]
    )
    # Primary samples are the earliest assessment of each token, not its best label.
    earliest = {}
    for r in sorted(assessed, key=lambda r: r["at"]):
        if r["label_registration"] == "registered":
            earliest.setdefault(r["token"], r["at"])
    primary_times = {d["token"]: d["assessed_at"] for d in report["disagreements"]}
    assert primary_times[TOKEN] == earliest[TOKEN]
    states = report["groups"]["all_primary"]["labels"]
    assert sum(states.values()) == population["primary_samples"]


def test_future_labels_cannot_change_the_sample_or_the_actions(tmp_path: Path) -> None:
    replay = frozen(tmp_path)
    report = experiment(replay["records"])

    def flipped_labels(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out = []
        for r in records:
            if r["kind"] == "outcome" and r.get("status") == "observed":
                out.append(r | {"forward_return_bps": -r["forward_return_bps"] - 999})
            else:
                out.append(r)
        return out

    other = experiment(flipped_labels(replay["records"]))
    assert other["population"] == report["population"]
    assert other["agreement_matrix"] == report["agreement_matrix"]
    assert [d["token"] for d in other["disagreements"]] == [
        d["token"] for d in report["disagreements"]
    ]
    assert (
        other["groups"]["original_flip"]["forward_return_bps"]
        != report["groups"]["original_flip"]["forward_return_bps"]
    )


def test_label_states_cover_not_due_and_unobserved(tmp_path: Path) -> None:
    replay = frozen(tmp_path, ticks=2, interval=900)
    # The run ended before any 1 h label could be queried: every sample is not observed,
    # none is dropped or counted as a zero return.
    report = experiment(replay["records"])
    states = report["groups"]["all_primary"]["labels"]
    assert (
        states["observed"] == 0 and sum(states.values()) == report["population"]["primary_samples"]
    )
    assert report["groups"]["all_primary"]["forward_return_bps"] == {"n": 0}
    late_cutoff = experiment(replay["records"], label_cutoff=CUTOFF + 10**6)
    assert (
        late_cutoff["groups"]["all_primary"]["labels"]["unobserved"]
        == states["unobserved"] + states["not_due"]
    )


def test_refusals(tmp_path: Path) -> None:
    replay = frozen(tmp_path)
    records = replay["records"]
    with pytest.raises(ExperimentRefused, match="label_cutoff_before_window_horizon"):
        experiment(records, label_cutoff=END + HORIZON)
    with pytest.raises(ExperimentRefused, match="empty_window"):
        experiment(records, end=NOW)
    with pytest.raises(ExperimentRefused, match="policy_mismatch"):
        run_experiment(
            records,
            AgentConfig(min_score_bps=5_000),
            start=NOW,
            end=END,
            label_cutoff=CUTOFF,
            evidence="synthetic",
            inputs={},
        )

    def edit(kind: str, change: Any) -> list[dict[str, Any]]:
        out, done = [], False
        for r in records:
            if r["kind"] == kind and not done:
                out.append(change(r))
                done = True
            else:
                out.append(r)
        return out

    # A recorded decision the real rule does not reproduce.
    tampered = edit("assessment", lambda r: r | {"score_bps": r["score_bps"] + 1})
    with pytest.raises(ExperimentRefused, match="original_rule_not_reproduced"):
        experiment(tampered)

    # A snapshot whose inputs were edited (more smart wallets) no longer reproduces.
    def first_of(token: str, change: Any) -> list[dict[str, Any]]:
        out, done = [], False
        for r in records:
            if r["kind"] == "assessment" and r["token"] == token and not done:
                out.append(change(r))
                done = True
            else:
                out.append(r)
        return out

    # The flip's own input, edited to remove its smart wallets: the recorded flip can no
    # longer be reproduced. (An edit the rule ignores, e.g. above saturation, would be.)
    inflated = first_of(TOKEN, lambda r: r | {"market": r["market"] | {"smart_wallets": 0}})
    with pytest.raises(ExperimentRefused, match="original_rule_not_reproduced"):
        experiment(inflated)
    # Older records without a market snapshot are unsupported, not guessed.
    old = edit("assessment", lambda r: {k: v for k, v in r.items() if k != "market"})
    with pytest.raises(ExperimentRefused, match="assessment_without_market_snapshot"):
        experiment(old)
    # A label that does not belong to its sample, or a duplicate label.
    observed = next(
        r for r in records if r["kind"] == "outcome" and r["horizon_seconds"] == HORIZON
    )
    # Edit that 1 h label itself (the first outcome record may be a 15 min label).
    hour = records.index(observed)
    changed = observed | {"score_bps": observed["score_bps"] + 1}
    wrong = [*records[:hour], changed, *records[hour + 1 :]]
    with pytest.raises(ExperimentRefused, match="label_identity_mismatch"):
        experiment(wrong)
    with pytest.raises(ExperimentRefused, match="duplicate_label"):
        experiment([*records, observed])


def test_negative_control_a_broken_ablation_is_caught() -> None:
    """If the comparator forgot to remove the bonus, the flip->watch control vanishes."""
    m = Market(
        token=TOKEN,
        symbol="T",
        observed_at=NOW,
        liquidity_usd_micros=120_000_500_000,
        market_cap_usd_micros=900_000_000_000,
        holders=2_000,
        top10_bps=3_050,
        unique_traders=80,
        buys=120,
        sells=60,
        first_trade_at=NOW - 86_400,
        change_5m_bps=-80,
        change_1h_bps=1_250,
        change_24h_bps=4_000,
        volume_24h_usd_micros=50_000_000_000,
        smart_wallets=2,
        smart_sold_bps=0,
        risk_flags=(),
        tax_bps=0,
        dev_rug_count=0,
    )
    m = replace(m, price=Fraction(95, 100), high_24h=Fraction(1))
    assert assess(m, POLICY).action == "flip"
    assert baseline(m, POLICY).action == "watch"
    broken = assess(m, POLICY)  # an "ablation" that changed nothing
    assert broken.action != baseline(m, POLICY).action


def test_cli_runs_offline_and_refuses_a_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cli = Market3()
    run = tmp_path / "run"
    capture_run(run, cli, ticks=8, interval=900, each_cycle=advance(cli))
    out = tmp_path / "experiment.json"
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
        str(out),
    ]
    monkeypatch.setattr(sys, "argv", argv)
    assert experiment_cli.main() == 0
    report = json.loads(out.read_bytes())
    assert report["manifest"]["evidence_source"] == "synthetic"
    assert report["manifest"]["inputs"]["session_complete"] is True
    assert report["execution_eligible"] is False
    assert report["limits"]["net_return"] == "not_measured"
    # A capture that is only a prefix is refused unless explicitly allowed.
    rewrite(run, lambda rs: rs[:-1])  # drop session_end
    monkeypatch.setattr(sys, "argv", [*argv[:-1], str(tmp_path / "prefix.json")])
    assert experiment_cli.main() == 2
    assert "capture_is_prefix_only" in capsys.readouterr().err
    monkeypatch.setattr(sys, "argv", [*argv[:-1], str(tmp_path / "prefix.json"), "--allow-prefix"])
    assert experiment_cli.main() == 0
    prefix = json.loads((tmp_path / "prefix.json").read_bytes())
    assert prefix["manifest"]["inputs"]["session_complete"] is False
    assert HORIZONS[1] == HORIZON
