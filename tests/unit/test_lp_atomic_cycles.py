"""A cycle is either durable in full or absent; retries cannot duplicate it."""

import sqlite3
from decimal import Decimal

import pytest

from oasis_agent.shadow.journal import ShadowJournal
from oasis_agent.shadow.service import LpShadowService

from .test_lp_shadow_service import setup
from .test_shadow_lp_runner import band_strategy


def test_duplicate_cycle_survives_service_restart(tmp_path):
    store, journal, service, cycle = setup(tmp_path)
    try:
        first = service.consume_once(cycle, now=100)
        assert first.duplicate is False
        journal.close()
        journal = ShadowJournal(tmp_path / "shadow.db", chain_id=1, max_age_seconds=10)
        restarted = LpShadowService(
            store, journal, band_strategy(), min_benefit=Decimal(0), canary_limits=None
        )
        retry = restarted.consume_once(cycle, now=101)
        assert retry.duplicate is True
        assert retry.record == first.record
        assert len(journal.decision_records()) == 1
        assert journal.db.execute("SELECT count(*) FROM observations").fetchone()[0] == 1
    finally:
        journal.close()
        store.close()


def test_crash_between_health_and_decision_rolls_back(tmp_path, monkeypatch):
    store, journal, service, cycle = setup(tmp_path)
    original = journal.record_decision

    def crash(payload):
        raise RuntimeError("simulated process interruption")

    monkeypatch.setattr(journal, "record_decision", crash)
    try:
        with pytest.raises(RuntimeError):
            service.consume_once(cycle, now=100)
        assert journal.db.execute("SELECT count(*) FROM observations").fetchone()[0] == 0
        assert journal.db.execute("SELECT count(*) FROM decisions").fetchone()[0] == 0
        monkeypatch.setattr(journal, "record_decision", original)
        assert service.consume_once(cycle, now=100).record["decision"] == "recenter"
    finally:
        journal.close()
        store.close()


def test_duplicate_does_not_hide_later_halt(tmp_path):
    store, journal, service, cycle = setup(tmp_path)
    try:
        service.consume_once(cycle, now=100)
        journal.fault(now=101)
        retry = service.consume_once(cycle, now=102)
        assert retry.duplicate is True
        assert retry.halted is True
    finally:
        journal.close()
        store.close()


def test_other_connection_cannot_observe_half_cycle(tmp_path, monkeypatch):
    store, journal, service, cycle = setup(tmp_path)
    observer = sqlite3.connect(tmp_path / "shadow.db")
    original = journal.record_decision

    def inspect(payload):
        assert observer.execute("SELECT count(*) FROM observations").fetchone()[0] == 0
        return original(payload)

    monkeypatch.setattr(journal, "record_decision", inspect)
    try:
        service.consume_once(cycle, now=100)
        assert observer.execute("SELECT count(*) FROM observations").fetchone()[0] == 1
        assert observer.execute("SELECT count(*) FROM decisions").fetchone()[0] == 1
    finally:
        observer.close()
        journal.close()
        store.close()
