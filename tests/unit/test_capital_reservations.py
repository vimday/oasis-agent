"""Shared cross-chain commitments cannot each spend the full owner budget."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from oasis_agent.risk.capital_reservations import CapitalReservations

ACCOUNT = "0x" + "12" * 20


def journal(path, **overrides):
    return CapitalReservations(
        path, **{"total_usd_micros": 500_000_000, "chains": (8453, 4663), **overrides}
    )


def test_cross_chain_commitments_share_one_cap_and_survive_restart(tmp_path):
    path = tmp_path / "global.db"
    first, second = journal(path), journal(path)
    try:
        reserved = first.reserve("base", chain=8453, account=ACCOUNT, usd_micros=450_000_000)
        assert not reserved.execution_eligible
        second.reserve("robinhood", chain=4663, account=ACCOUNT, usd_micros=50_000_000)
        assert first.used_usd_micros() == 500_000_000
        with pytest.raises(ValueError, match="cross-chain"):
            second.reserve("extra", chain=4663, account=ACCOUNT, usd_micros=1)
        first.mark_submitted("base")
        assert first.used_usd_micros() == 500_000_000
    finally:
        first.close()
        second.close()
    reopened = journal(path)
    try:
        assert reopened.used_usd_micros() == 500_000_000
        assert (
            reopened.reserve("base", chain=8453, account=ACCOUNT, usd_micros=450_000_000).state
            == "submitted"
        )
    finally:
        reopened.close()


def test_settlement_only_releases_one_intent_and_is_idempotent(tmp_path):
    ledger = journal(tmp_path / "global.db")
    try:
        ledger.reserve("one", chain=8453, account=ACCOUNT, usd_micros=400_000_000)
        ledger.reserve("two", chain=4663, account=ACCOUNT, usd_micros=100_000_000)
        ledger.mark_submitted("one")
        ledger.settle("one", reconciliation_digest="a" * 64)
        ledger.settle("one", reconciliation_digest="a" * 64)
        assert ledger.used_usd_micros() == 100_000_000
        assert (
            ledger.reserve("one", chain=8453, account=ACCOUNT, usd_micros=400_000_000).state
            == "settled"
        )
        with pytest.raises(ValueError, match="resubmitted"):
            ledger.mark_submitted("one")
        with pytest.raises(ValueError, match="changed proof"):
            ledger.settle("one", reconciliation_digest="b" * 64)
        ledger.reserve("three", chain=8453, account=ACCOUNT, usd_micros=400_000_000)
        assert ledger.used_usd_micros() == 500_000_000
    finally:
        ledger.close()


@pytest.mark.parametrize("change", ["chain", "account", "amount"])
def test_idempotent_intent_cannot_change_commitment(tmp_path, change):
    ledger = journal(tmp_path / "global.db")
    try:
        kwargs = {"chain": 8453, "account": ACCOUNT, "usd_micros": 100}
        ledger.reserve("one", **kwargs)
        kwargs[{"chain": "chain", "account": "account", "amount": "usd_micros"}[change]] = {
            "chain": 4663,
            "account": "0x" + "34" * 20,
            "amount": 101,
        }[change]
        with pytest.raises(ValueError, match="changed commitment"):
            ledger.reserve("one", **kwargs)
        assert ledger.used_usd_micros() == 100
    finally:
        ledger.close()


def test_policy_changes_and_corrupt_states_fail_closed(tmp_path):
    path = tmp_path / "global.db"
    ledger = journal(path)
    try:
        ledger.reserve("one", chain=8453, account=ACCOUNT, usd_micros=100)
        with pytest.raises(ValueError, match="policy changed"):
            journal(path, total_usd_micros=1_000_000_000)
        with sqlite3.connect(path) as db:
            db.execute("UPDATE reservations SET state='settled' WHERE intent='one'")
        with pytest.raises(ValueError, match="integrity"):
            ledger.used_usd_micros()
    finally:
        ledger.close()


def test_concurrent_chains_cannot_both_reserve_300_dollars(tmp_path):
    path = tmp_path / "shared.db"
    barrier = Barrier(2)

    def reserve(chain):
        ledger = journal(path)
        try:
            barrier.wait(timeout=5)
            try:
                ledger.reserve(str(chain), chain=chain, account=ACCOUNT, usd_micros=300_000_000)
                return True
            except ValueError as exc:
                assert "cross-chain" in str(exc)
                return False
        finally:
            ledger.close()

    with ThreadPoolExecutor(max_workers=2) as workers:
        outcomes = list(workers.map(reserve, (8453, 4663)))
    assert sorted(outcomes) == [False, True]
    ledger = journal(path)
    try:
        assert ledger.used_usd_micros() == 300_000_000
    finally:
        ledger.close()
