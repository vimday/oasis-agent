"""Raw constructed evidence into persistent risk; no real market or execution claims."""

import json
import sqlite3
from dataclasses import replace

import pytest

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.execution.account_risk_observation import (
    RiskObservationRequest,
    observe_account_risk,
)
from oasis_agent.execution.account_risk_reader import EvidenceReplayAccountRiskReader
from oasis_agent.risk.account_risk import Commitment, GlobalRiskJournal, PortfolioFrame
from scripts.observe_account_risk import run
from tests.unit.test_account_lp_cash import build
from tests.unit.test_account_risk import (
    MIDNIGHT,
    asset,
    commitment,
    make_frame,
    make_policy,
    repriced,
)
from tests.unit.test_account_risk_gas_binding import scenario


def setup(tmp_path, *, unchanged=False):
    store = EvidenceStore(tmp_path / "evidence.db")
    manifest, opening, closing, gas = scenario(store, unchanged=unchanged)
    request = RiskObservationRequest(
        schema="oasis.risk-observation.v1", manifest=manifest, opening=opening, closing=closing
    )
    first = EvidenceReplayAccountRiskReader(store, manifest, opening).reconcile()
    journal = GlobalRiskJournal(tmp_path / "risk.db", manifest.policy)
    journal.admit(
        first,
        Commitment(request_hash="1" * 64, account_key=first.accounts[0].key, usd_micros=1),
        now=first.accounts[0].timestamp,
    )
    return store, journal, request, gas.closing.ref.timestamp


def events(journal):
    return journal.db.execute("SELECT * FROM global_events ORDER BY seq").fetchall()


def test_raw_gas_loss_persists_without_new_commitment_and_retry_is_noop(tmp_path):
    store, journal, request, now = setup(tmp_path)
    try:
        before = journal.current_state()
        state, digest = observe_account_risk(store, journal, request, now=now)
        assert state.cumulative_loss == 1  # floor NAV values; never debit rounded gas twice
        assert state.reservations == before.reservations
        assert state.settlements == before.settlements
        saved = events(journal)
        journal.close()
        journal = GlobalRiskJournal(tmp_path / "risk.db", request.manifest.policy)
        assert observe_account_risk(store, journal, request, now=now) == (state, digest)
        assert events(journal) == saved
        with pytest.raises(ValueError, match="stale"):
            observe_account_risk(store, journal, request, now=now + 100_000)
        assert events(journal) == saved
    finally:
        journal.close()
        store.close()


@pytest.mark.parametrize("bad", ["missing_gas", "scalar", "scope", "policy", "opening"])
def test_bad_raw_join_cannot_mutate_persistent_risk(tmp_path, bad):
    store, journal, request, now = setup(tmp_path)
    try:
        saved = events(journal)
        key = next(iter(request.closing))
        obs = request.closing[key]
        if bad == "missing_gas":
            obs = replace(obs, native_gas_evidence=None)
        elif bad == "scalar":
            obs = replace(obs, native_gas_evidence=None, native_gas_units=400)
        elif bad == "scope":
            obs = replace(
                obs, native_gas_evidence=replace(obs.native_gas_evidence, account="0x" + "ff" * 20)
            )
        elif bad == "policy":
            request = request.model_copy(
                update={
                    "manifest": request.manifest.model_copy(
                        update={
                            "policy": request.manifest.policy.model_copy(
                                update={"max_age_seconds": 999}
                            )
                        }
                    )
                }
            )
        elif bad == "opening":
            request = request.model_copy(update={"opening": {key: obs}})
        request = request.model_copy(update={"closing": {key: obs}})
        with pytest.raises(ValueError):
            observe_account_risk(store, journal, request, now=now)
        assert events(journal) == saved
    finally:
        journal.close()
        store.close()


def test_unexplained_cash_refuses_before_journal_event(tmp_path):
    store, journal, request, now = setup(tmp_path, unchanged=True)
    try:
        saved = events(journal)
        with pytest.raises(ValueError, match="unresolved"):
            observe_account_risk(store, journal, request, now=now)
        assert events(journal) == saved
    finally:
        journal.close()
        store.close()


def test_competing_connection_reservation_is_not_overwritten(tmp_path):
    store, journal, request, now = setup(tmp_path)
    peer = GlobalRiskJournal(tmp_path / "risk.db", request.manifest.policy)
    try:
        old = journal.current_state()
        peer.admit(
            old.frame,
            Commitment(request_hash="2" * 64, account_key=old.frame.accounts[0].key, usd_micros=1),
            now=now,
        )
        saved = events(peer)
        with pytest.raises(ValueError, match="baseline changed"):
            journal.observe(old.frame, expected=old, now=now, evidence_hash="e" * 64)
        assert events(peer) == saved
    finally:
        peer.close()
        journal.close()
        store.close()


def test_observation_latch_survives_recovery_and_blocks_existing_admission(tmp_path):
    journal = GlobalRiskJournal(tmp_path / "risk.db", make_policy())
    first = make_frame(assets=(asset(500_000_000),))
    journal.admit(PortfolioFrame(accounts=(first,)), commitment(1), now=first.timestamp)
    try:
        for i, units in enumerate((350_000_000, 500_000_000, 350_000_000), 1):
            old = journal.current_state()
            current = make_frame(
                block_number=100 + i,
                timestamp=first.timestamp + i,
                assets=(asset(units, price_timestamp=first.timestamp + i),),
                movements={"usd": units - old.frame.accounts[0].assets[0].units},
            )
            state, _ = journal.observe(
                PortfolioFrame(accounts=(current,)),
                expected=old,
                now=current.timestamp,
                evidence_hash="e" * 64,
            )
        assert state.cumulative_loss == 300_000_000 and state.halted
        journal.close()
        journal = GlobalRiskJournal(tmp_path / "risk.db", make_policy())
        with pytest.raises(ValueError, match="loss latch"):
            journal.admit(state.frame, commitment(1, request="2" * 64), now=current.timestamp)
        assert journal.current_state().cumulative_loss == 300_000_000
        assert len(journal.current_state().reservations) == 1
    finally:
        journal.close()


def test_cli_report_loss_recovery_uses_existing_ledger(tmp_path, monkeypatch):
    store, journal, request, now = setup(tmp_path)
    journal.close()
    store.close()
    input_path = tmp_path / "request.json"
    input_path.write_text(request.model_dump_json(by_alias=True))
    monkeypatch.setattr("scripts.observe_account_risk.time.time", lambda: now)
    missing_output = tmp_path / "missing-directory" / "report.json"
    with pytest.raises(FileNotFoundError):
        run(input_path, tmp_path / "evidence.db", tmp_path / "risk.db", missing_output)
    with_journal = GlobalRiskJournal(tmp_path / "risk.db", request.manifest.policy)
    saved = events(with_journal)
    with_journal.close()
    output = tmp_path / "report.json"
    assert run(input_path, tmp_path / "evidence.db", tmp_path / "risk.db", output) is False
    report = json.loads(output.read_bytes())
    assert report["state"]["cumulative_loss"] == 1
    assert report["execution_eligible"] is False and report["net_pnl"] is None
    with_journal = GlobalRiskJournal(tmp_path / "risk.db", request.manifest.policy)
    assert events(with_journal) == saved
    with_journal.close()


def test_observation_settles_new_units_once_without_withdrawing_reservation(tmp_path):
    first = make_frame(assets=(asset(100_000_000), repriced(0, value_micros=50_000_000)))
    policy = make_policy(inventory={first.key: ("usd", "lp-position")})
    journal = GlobalRiskJournal(tmp_path / "risk.db", policy)
    try:
        journal.admit(
            PortfolioFrame(accounts=(first,)), commitment(50_000_000), now=first.timestamp
        )
        old = journal.current_state()
        current = make_frame(
            block_number=101,
            timestamp=first.timestamp + 1,
            assets=(asset(50_000_000), repriced(1, value_micros=50_000_000)),
            movements={"usd": -50_000_000, "lp-position": 1},
        )
        state, digest = journal.observe(
            PortfolioFrame(accounts=(current,)),
            expected=old,
            now=current.timestamp,
            evidence_hash="e" * 64,
        )
        assert state.reservations == old.reservations
        assert len(state.settlements) == 1 and state.settlements[0].usd_micros == 50_000_000
        assert journal.observe(
            state.frame, expected=state, now=current.timestamp, evidence_hash="e" * 64
        ) == (state, digest)
        with pytest.raises(ValueError, match="settled commitment"):
            journal.release("1" * 64, reason="not submitted")
    finally:
        journal.close()


def test_midnight_gap_observation_cannot_reset_day(tmp_path):
    first = make_frame(
        timestamp=MIDNIGHT - 60, assets=(asset(500_000_000, price_timestamp=MIDNIGHT - 60),)
    )
    journal = GlobalRiskJournal(tmp_path / "risk.db", make_policy())
    try:
        journal.admit(PortfolioFrame(accounts=(first,)), commitment(1), now=first.timestamp)
        old = journal.current_state()
        current = make_frame(
            block_number=101,
            timestamp=MIDNIGHT + 60,
            assets=(asset(250_000_000, price_timestamp=MIDNIGHT + 60),),
            movements={"usd": -250_000_000},
        )
        saved = events(journal)
        with pytest.raises(ValueError, match="day-boundary"):
            journal.observe(
                PortfolioFrame(accounts=(current,)),
                expected=old,
                now=current.timestamp,
                evidence_hash="e" * 64,
            )
        assert events(journal) == saved
    finally:
        journal.close()


def test_missing_persistent_baseline_is_not_reinitialized(tmp_path):
    store = EvidenceStore(tmp_path / "evidence.db")
    m, opening, closing, gas = scenario(store)
    journal = GlobalRiskJournal(tmp_path / "risk.db", m.policy)
    try:
        request = RiskObservationRequest(
            schema="oasis.risk-observation.v1", manifest=m, opening=opening, closing=closing
        )
        with pytest.raises(ValueError, match="established"):
            observe_account_risk(store, journal, request, now=gas.closing.ref.timestamp)
        assert events(journal) == []
    finally:
        journal.close()
        store.close()


def test_raw_lp_cash_bridge_loss_reaches_persistent_journal(tmp_path):
    store = EvidenceStore(tmp_path / "evidence.db")
    m, opening, closing = build(store)
    first = EvidenceReplayAccountRiskReader(store, m, opening).reconcile()
    journal = GlobalRiskJournal(tmp_path / "risk.db", m.policy)
    try:
        journal.admit(
            first,
            Commitment(request_hash="1" * 64, account_key=first.accounts[0].key, usd_micros=1),
            now=1000,
        )
        before = journal.current_state()
        request = RiskObservationRequest(
            schema="oasis.risk-observation.v1", manifest=m, opening=opening, closing=closing
        )
        state, _ = observe_account_risk(store, journal, request, now=1001)
        assert state.cumulative_loss == first.accounts[0].nav - state.frame.accounts[0].nav > 0
        assert state.frame.accounts[0].non_trading_flow_usd_micros == 0
        assert state.reservations == before.reservations
    finally:
        journal.close()
        store.close()


def test_existing_only_journal_never_creates_missing_or_blank_history(tmp_path):
    path = tmp_path / "missing.db"
    with pytest.raises(sqlite3.OperationalError):
        GlobalRiskJournal(path, make_policy(), require_existing=True)
    assert not path.exists()
    path.write_bytes(b"")
    with pytest.raises(sqlite3.OperationalError):
        GlobalRiskJournal(path, make_policy(), require_existing=True)
    assert path.read_bytes() == b""


def test_different_raw_opening_cannot_replace_persistent_nav(tmp_path):
    store, journal, request, now = setup(tmp_path)
    try:
        old = journal.current_state()
        # Advance the durable account to another individually valid frame while
        # preserving its NAV; the old request cannot silently install its opening.
        account = old.frame.accounts[0].model_copy(
            update={
                "block_number": old.frame.accounts[0].block_number + 1,
                "block_hash": "0x" + "ff" * 32,
                "timestamp": old.frame.accounts[0].timestamp + 1,
            }
        )
        journal.observe(
            PortfolioFrame(accounts=(account,)), expected=old, now=now, evidence_hash="e" * 64
        )
        saved = events(journal)
        with pytest.raises(ValueError, match="opening differs"):
            observe_account_risk(store, journal, request, now=now)
        assert events(journal) == saved
    finally:
        journal.close()
        store.close()


def test_raw_observer_preserves_prior_losses_and_prior_interval_movements(tmp_path):
    store = EvidenceStore(tmp_path / "evidence.db")
    m, opening, closing, gas = scenario(store)
    first = EvidenceReplayAccountRiskReader(store, m, opening).reconcile()
    a = first.accounts[0]
    prior_asset = a.assets[0].model_copy(update={"units": a.assets[0].units + 10**15})
    prior = a.model_copy(
        update={
            "assets": (prior_asset,),
            "block_number": a.block_number - 1,
            "timestamp": a.timestamp - 1,
        }
    )
    journal = GlobalRiskJournal(tmp_path / "risk.db", m.policy)
    try:
        journal.admit(
            PortfolioFrame(accounts=(prior,)),
            Commitment(request_hash="1" * 64, account_key=a.key, usd_micros=1),
            now=a.timestamp,
        )
        boundary = a.model_copy(update={"movements": {"native": -(10**15)}})
        old, _ = journal.observe(
            PortfolioFrame(accounts=(boundary,)),
            expected=journal.current_state(),
            now=a.timestamp,
            evidence_hash="e" * 64,
        )
        assert old.cumulative_loss == 2_000_000
        request = RiskObservationRequest(
            schema="oasis.risk-observation.v1", manifest=m, opening=opening, closing=closing
        )
        state, _ = observe_account_risk(store, journal, request, now=gas.closing.ref.timestamp)
        assert state.cumulative_loss == old.cumulative_loss + 1
    finally:
        journal.close()
        store.close()
