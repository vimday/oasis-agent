"""Paired rearm must validate rollback durability under its own write locks."""

import sqlite3
from contextlib import closing

import pytest

from oasis_agent.live import rearm
from tests.unit import test_live_rearm as fixture
from tests.unit.test_live_valuation import synthetic_feed_pin  # noqa: F401


@pytest.mark.parametrize("role", rearm.ROLES)
def test_real_mode_flip_between_probe_and_begin_never_rearms_either_journal(
    tmp_path, monkeypatch, role
):
    data = fixture.pair(tmp_path, monkeypatch)
    digest = fixture.review(data)["review_hash"]
    directories, stores, *_ = data
    before = {name: fixture.rows(store) for name, store in stores.items()}
    protected = {
        path: path.read_bytes()
        for directory in directories.values()
        for path in directory.iterdir()
        if path != stores[directory.name].journal_path
    }
    connect, append = sqlite3.connect, rearm._append
    flipped, staged = [], []

    class Race(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if (
                sql == "BEGIN IMMEDIATE"
                and not flipped
                and len(super().execute("PRAGMA database_list").fetchall()) == 2
            ):
                with closing(connect(stores[role].journal_path)) as peer:
                    mode = peer.execute("PRAGMA journal_mode=WAL").fetchone()
                    assert mode == ("wal",)
                    flipped.append(mode)
            return super().execute(sql, parameters)

    def stage(*args, **kwargs):
        staged.append(args[1])
        return append(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: connect(*a, **k, factory=Race))
    monkeypatch.setattr(rearm, "_append", stage)
    with pytest.raises(ValueError, match=r"DELETE|FULL"):
        fixture.apply(data, digest)
    assert flipped == [("wal",)] and not staged
    assert {name: fixture.rows(store) for name, store in stores.items()} == before
    assert all(path.read_bytes() == raw for path, raw in protected.items())
    # Inspect mode without LiveJournal's default write connection converting it.
    with closing(connect(stores[role].journal_path)) as peer:
        assert peer.execute("PRAGMA journal_mode").fetchone() == ("wal",)
    assert all(store.load("live", read_only=True).halted == rearm.HALT for store in stores.values())


@pytest.mark.parametrize("phase", ["before_proofs", "before_commit"])
def test_durability_is_rechecked_at_both_locked_boundaries(tmp_path, monkeypatch, phase):
    data = fixture.pair(tmp_path, monkeypatch)
    digest = fixture.review(data)["review_hash"]
    before = {name: fixture.rows(store) for name, store in data[1].items()}
    connect, evaluate = sqlite3.connect, rearm._evaluate
    staged = []
    append = rearm._append

    class Weakened(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if sql == "PRAGMA peer.synchronous" and (
                (phase == "before_proofs" and self.in_transaction)
                or (phase == "before_commit" and staged == ["main", "peer"])
            ):
                # SQLite forbids changing synchronous within the transaction;
                # model a failed durability assertion without altering a file.
                return super().execute("SELECT 1")
            return super().execute(sql, parameters)

    def stage(*args, **kwargs):
        result = append(*args, **kwargs)
        staged.append(args[1])
        return result

    calls = []

    def prove(*args, **kwargs):
        calls.append(True)
        return evaluate(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: connect(*a, **k, factory=Weakened))
    monkeypatch.setattr(rearm, "_append", stage)
    monkeypatch.setattr(rearm, "_evaluate", prove)
    with pytest.raises(ValueError, match=r"DELETE|FULL"):
        fixture.apply(data, digest)
    assert len(calls) == (0 if phase == "before_proofs" else 1)
    assert staged == ([] if phase == "before_proofs" else ["main", "peer"])
    assert {name: fixture.rows(store) for name, store in data[1].items()} == before
    assert all(store.load("live", read_only=True).halted for store in data[1].values())
