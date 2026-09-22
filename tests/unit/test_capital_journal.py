"""Durable loss latching with synthetic reconciled values; no valuation attestation."""

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from oasis_agent.models.common import BlockRef
from oasis_agent.risk.canary_instructions import parse_canary_instructions
from oasis_agent.risk.capital_journal import CapitalJournal, CapitalObservation

ACCOUNT = "0x" + "12" * 20


def limits():
    return parse_canary_instructions(
        (Path(__file__).parents[2] / "configs/canary.robinhood.pending.json").read_bytes()
    )


def observation(n=1, *, daily=0, cumulative=0, capital=500_000_000, timestamp=1000):
    return CapitalObservation(
        BlockRef(4663, n, "0x" + f"{n:064x}", timestamp),
        f"{n:064x}",
        capital,
        daily,
        cumulative,
    )


def test_limit_latches_across_restart_day_and_lower_capital(tmp_path):
    path = tmp_path / "risk.db"
    journal = CapitalJournal(path, account=ACCOUNT, limits=limits())
    assert journal.status().reasons == ("valuation_missing",)
    assert not journal.record(observation()).halted
    breached = journal.record(observation(2, daily=200_000_000, cumulative=200_000_000))
    assert breached.halted and breached.reasons == ("daily_loss_limit_reached",)
    assert not breached.execution_eligible
    assert journal.record(observation(2, daily=200_000_000, cumulative=200_000_000)) == breached
    journal.close()
    journal = CapitalJournal(path, account=ACCOUNT, limits=limits())
    try:
        assert journal.status() == breached
        later = journal.record(observation(3, capital=0, cumulative=200_000_000, timestamp=90000))
        assert later.halted and later.reasons == breached.reasons
    finally:
        journal.close()


@pytest.mark.parametrize(
    "mutation", ["policy", "account", "chain", "hash", "loss", "day_loss", "time"]
)
def test_conflicting_or_regressing_observations_and_policy_fail_closed(tmp_path, mutation):
    path = tmp_path / "risk.db"
    journal = CapitalJournal(path, account=ACCOUNT, limits=limits())
    journal.record(observation(daily=100, cumulative=100))
    try:
        if mutation in {"policy", "account"}:
            with pytest.raises(ValueError, match="policy changed"):
                CapitalJournal(
                    path,
                    account="0x" + "34" * 20 if mutation == "account" else ACCOUNT,
                    limits=replace(limits(), maximum_daily_loss_usd_micros=10)
                    if mutation == "policy"
                    else limits(),
                )
        else:
            item = observation(2, daily=100, cumulative=100)
            if mutation == "chain":
                item = replace(item, block=replace(item.block, chain_id=1))
            elif mutation == "hash":
                item = replace(item, valuation_hash=observation().valuation_hash)
            elif mutation == "loss":
                item = observation(2)
            elif mutation == "day_loss":
                item = observation(2, daily=99, cumulative=100)
            else:
                item = observation(2, daily=100, cumulative=100, timestamp=999)
            with pytest.raises(ValueError):
                journal.record(item)
        assert journal.status().observations == 1
    finally:
        journal.close()


def test_second_writer_sees_halt_and_tampering_blocks_existing_reader(tmp_path):
    path = tmp_path / "risk.db"
    first = CapitalJournal(path, account=ACCOUNT, limits=limits())
    second = CapitalJournal(path, account=ACCOUNT, limits=limits())
    try:
        first.record(observation(capital=500_000_001))
        assert second.status().reasons == ("total_capital_exceeded",)
        second.record(observation(2, capital=0))
        assert first.status().halted
        with sqlite3.connect(path) as db:
            db.execute("UPDATE capital_observations SET digest=? WHERE seq=1", ("f" * 64,))
        with pytest.raises(ValueError, match="integrity"):
            first.status()
        with pytest.raises(ValueError, match="integrity"):
            second.record(observation(3))
    finally:
        first.close()
        second.close()


def test_cumulative_loss_halts_even_after_day_change(tmp_path):
    journal = CapitalJournal(tmp_path / "risk.db", account=ACCOUNT, limits=limits())
    try:
        journal.record(observation(daily=199_000_000, cumulative=499_000_000))
        state = journal.record(
            observation(2, daily=1_000_000, cumulative=500_000_000, timestamp=90000)
        )
        assert state.reasons == ("cumulative_loss_limit_reached",)
    finally:
        journal.close()
