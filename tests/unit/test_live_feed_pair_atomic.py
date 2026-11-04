"""A peer write failure must never commit only one service's strict-feed epoch."""

import os
import sqlite3
import subprocess
import sys
import time
from contextlib import ExitStack, closing
from dataclasses import asdict

import pytest

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.live import feed_transition
from oasis_agent.live.feed_transition import apply_pair
from oasis_agent.live.journal import LiveJournal
from oasis_agent.live.state import StateStore
from oasis_agent.models.common import canonical_json
from tests.unit.test_live_feed_migration import TARGET_HASH, events, prepare
from tests.unit.test_live_feed_transition import CONFIG, OLD_HASH, SOURCES
from tests.unit.test_live_valuation import synthetic_feed_pin  # noqa: F401


def pair(tmp_path, monkeypatch, resources):
    stores, evidence, fresh = {}, {}, {}
    for role in ("strategy", "authority"):
        root = tmp_path / role
        root.mkdir()
        evidence[role] = resources.enter_context(closing(EvidenceStore(root / "proof.db")))
        stores[role], _, fresh[role], _ = prepare(root, evidence[role], monkeypatch)
        (root / "live.lock").touch()
    return stores, evidence, fresh


def test_second_database_rejects_epoch_without_changing_either_binding(tmp_path, monkeypatch):
    with ExitStack() as resources:
        stores, evidence, fresh = pair(tmp_path, monkeypatch, resources)
        with closing(sqlite3.connect(stores["authority"].journal_path)) as db:
            db.execute(
                "CREATE TRIGGER reject_epoch BEFORE INSERT ON events "
                "WHEN NEW.config = '" + TARGET_HASH + "' "
                "BEGIN SELECT RAISE(ABORT, 'synthetic peer refusal'); END"
            )
            db.commit()
        before = {role: events(store) for role, store in stores.items()}
        projections = {role: store.path.read_bytes() for role, store in stores.items()}
        with pytest.raises(sqlite3.IntegrityError, match="synthetic peer refusal"):
            apply_pair(
                stores["strategy"], stores["authority"], CONFIG, evidence, fresh, sources=SOURCES
            )
        for role, store in stores.items():
            assert events(store) == before[role]
            assert store.path.read_bytes() == projections[role]
            assert store.load("live", read_only=True).halted
            assert store.journal.config_hash == OLD_HASH


@pytest.mark.parametrize("failure", ["commit", "expired_after_both_staged"])
def test_final_failure_rolls_back_both_epochs_and_transition_tables(tmp_path, monkeypatch, failure):
    with ExitStack() as resources:
        stores, evidence, fresh = pair(tmp_path, monkeypatch, resources)
        before = {role: events(store) for role, store in stores.items()}
        if failure == "commit":
            connect = sqlite3.connect

            class RefuseCommit(sqlite3.Connection):
                def execute(self, sql, parameters=()):
                    if (
                        sql == "COMMIT"
                        and self.execute("PRAGMA database_list").fetchall()[-1][1] == "peer"
                    ):
                        raise sqlite3.OperationalError("synthetic paired commit refusal")
                    return super().execute(sql, parameters)

            monkeypatch.setattr(
                sqlite3, "connect", lambda *a, **k: connect(*a, **k, factory=RefuseCommit)
            )
        else:
            original = LiveJournal._transition_in_transaction

            def expire(self, db, record, *, schema="main"):
                result = original(self, db, record, schema=schema)
                if schema == "peer":
                    monkeypatch.setattr(
                        feed_transition.time,
                        "time",
                        lambda: record["review"]["fresh_nav"]["valid_until"] + 1,
                    )
                return result

            monkeypatch.setattr(LiveJournal, "_transition_in_transaction", expire)
        with pytest.raises((ValueError, sqlite3.OperationalError)):
            apply_pair(
                stores["strategy"], stores["authority"], CONFIG, evidence, fresh, sources=SOURCES
            )
        for role, store in stores.items():
            assert events(store) == before[role]
            assert store.load("live", read_only=True).halted
            assert store.journal.config_hash == OLD_HASH
            with closing(sqlite3.connect(store.journal_path)) as db:
                assert not db.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='config_transitions'"
                ).fetchone()


def test_projection_failure_keeps_both_committed_epochs_recoverable_without_retry(
    tmp_path, monkeypatch
):
    with ExitStack() as resources:
        stores, evidence, fresh = pair(tmp_path, monkeypatch, resources)
        before = {role: store.path.read_bytes() for role, store in stores.items()}
        rows = {role: events(store) for role, store in stores.items()}
        monkeypatch.setattr(
            StateStore,
            "save",
            lambda *_: (_ for _ in ()).throw(OSError("synthetic projection failure")),
        )
        result = apply_pair(
            stores["strategy"], stores["authority"], CONFIG, evidence, fresh, sources=SOURCES
        )
        assert (
            result["atomic_pair"]
            and result["migration_applied"]
            and not result["projection_complete"]
        )
        for role, store in stores.items():
            assert store.path.read_bytes() == before[role]
            assert events(store)[: len(rows[role])] == rows[role]
            reopened = StateStore(store.path)
            assert reopened.load("live", read_only=True).halted
            assert reopened.journal.config_hash == TARGET_HASH
        with pytest.raises(ValueError):
            apply_pair(
                stores["strategy"], stores["authority"], CONFIG, evidence, fresh, sources=SOURCES
            )


def test_success_preserves_risk_and_history_with_append_only_metadata_on_both(
    tmp_path, monkeypatch
):
    with ExitStack() as resources:
        stores, evidence, fresh = pair(tmp_path, monkeypatch, resources)
        prior = {role: store.load("live", read_only=True) for role, store in stores.items()}
        before = {role: events(store) for role, store in stores.items()}
        for store in stores.values():
            (store.path.parent / "live.receipt-budget.db").write_bytes(
                b"synthetic untouched budget"
            )
        result = apply_pair(
            stores["strategy"], stores["authority"], CONFIG, evidence, fresh, sources=SOURCES
        )
        assert (
            result["atomic_pair"]
            and result["projection_complete"]
            and not result["execution_eligible"]
        )
        for role, store in stores.items():
            state = store.load("live", read_only=True)
            assert state.halted == prior[role].halted
            assert state.exact_risk.basis == prior[role].exact_risk.basis
            assert state.exact_risk.external == prior[role].exact_risk.external
            assert state.exact_risk.last_at == fresh[role][0].block.timestamp
            for field in ("position", "history", "gas_spent_wei", "settled_blocks", "last_units"):
                assert getattr(state, field) == getattr(prior[role], field)
            assert events(store)[: len(before[role])] == before[role]
            assert len(events(store)) == len(before[role]) + 1
            assert (
                store.path.parent / "live.receipt-budget.db"
            ).read_bytes() == b"synthetic untouched budget"
            assert (store.path.parent / "STOP").exists()
            with closing(sqlite3.connect(store.journal_path)) as db:
                for sql in (
                    "UPDATE config_transitions SET payload = X'7b7d'",
                    "DELETE FROM config_transitions",
                ):
                    with pytest.raises(sqlite3.IntegrityError, match="append only"):
                        db.execute(sql)


def test_stale_expected_peer_tip_refuses_before_either_transition(tmp_path, monkeypatch):
    with ExitStack() as resources:
        stores, evidence, fresh = pair(tmp_path, monkeypatch, resources)
        before = {role: events(store) for role, store in stores.items()}
        tips = {role: store.journal.revision for role, store in stores.items()}
        tips["authority"] = "f" * 64
        with pytest.raises(ValueError, match="retained attempt scope"):
            apply_pair(
                stores["strategy"],
                stores["authority"],
                CONFIG,
                evidence,
                fresh,
                sources=SOURCES,
                expected_tips=tips,
            )
        assert {role: events(store) for role, store in stores.items()} == before


@pytest.mark.parametrize("suffix", ["-journal", "-wal", "-shm"])
def test_recovery_sidecar_is_preserved_without_opening_either_original(
    tmp_path, monkeypatch, suffix
):
    with ExitStack() as resources:
        stores, evidence, fresh = pair(tmp_path, monkeypatch, resources)
        path = stores["authority"].journal_path.with_name(
            stores["authority"].journal_path.name + suffix
        )
        path.write_bytes(b"synthetic sidecar requiring inspection")
        before = {
            p: p.read_bytes()
            for root in (tmp_path / "strategy", tmp_path / "authority")
            for p in root.iterdir()
        }
        with pytest.raises(ValueError, match="sidecars"):
            apply_pair(
                stores["strategy"], stores["authority"], CONFIG, evidence, fresh, sources=SOURCES
            )
        assert {p: p.read_bytes() for p in before} == before


@pytest.mark.parametrize("role", ["strategy", "authority"])
def test_wal_mode_is_refused_without_conversion(tmp_path, monkeypatch, role):
    with ExitStack() as resources:
        stores, evidence, fresh = pair(tmp_path, monkeypatch, resources)
        with closing(sqlite3.connect(stores[role].journal_path)) as db:
            assert db.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        before = {name: events(store) for name, store in stores.items()}
        with pytest.raises(ValueError, match=r"DELETE|sidecars"):
            apply_pair(
                stores["strategy"], stores["authority"], CONFIG, evidence, fresh, sources=SOURCES
            )
        assert {name: events(store) for name, store in stores.items()} == before
        with closing(sqlite3.connect(stores[role].journal_path)) as db:
            assert db.execute("PRAGMA journal_mode").fetchone() == ("wal",)


@pytest.mark.parametrize("phase", ["first_staged", "both_staged", "committed"])
def test_sigkill_never_leaves_one_authoritative_epoch_committed(tmp_path, monkeypatch, phase):
    with ExitStack() as resources:
        stores, evidence, fresh = pair(tmp_path, monkeypatch, resources)
        before = {role: events(store) for role, store in stores.items()}
        projections = {role: store.path.read_bytes() for role, store in stores.items()}
        marker, request = tmp_path / "pause", tmp_path / "request.json"
        request.write_bytes(
            canonical_json(
                {
                    "phase": phase,
                    "now": fresh["strategy"][0].block.timestamp,
                    "sources": SOURCES,
                    "refs": {role: [asdict(ref) for ref in refs] for role, refs in fresh.items()},
                }
            )
        )
        code = """
import json,sys,time
from pathlib import Path
from contextlib import ExitStack,closing
from eth_hash.auto import keccak
from oasis_agent.evidence.store import ReadOnlyEvidenceStore
from oasis_agent.live import valuation,feed_transition
from oasis_agent.live.journal import LiveJournal
from oasis_agent.live.state import StateStore
from oasis_agent.models.common import evidence_ref_from_json
from tests.unit.test_live_feed_transition import CONFIG
root,request,marker=map(Path,sys.argv[1:4]); data=json.loads(request.read_bytes())
valuation.FEED_RUNTIME='0x'+keccak(bytes.fromhex('6000')).hex()
feed_transition.time.time=lambda:data['now']
def pause():
    marker.write_text('synthetic crash boundary')
    time.sleep(30)
original=LiveJournal._transition_in_transaction
def stage(self,db,record,*,schema='main'):
    result=original(self,db,record,schema=schema)
    if data['phase']==('first_staged' if schema=='main' else 'both_staged'):pause()
    return result
LiveJournal._transition_in_transaction=stage
if data['phase']=='committed':StateStore.save=lambda *_:pause()
with ExitStack() as stack:
    evidence={
        role:stack.enter_context(closing(ReadOnlyEvidenceStore(root/role/'proof.db')))
        for role in ('strategy','authority')
    }
    refs={
        role:tuple(evidence_ref_from_json(ref) for ref in values)
        for role,values in data['refs'].items()
    }
    feed_transition.apply_pair(
        StateStore(root/'strategy/state.json'),StateStore(root/'authority/state.json'),
        CONFIG,evidence,refs,sources=tuple(data['sources']),
    )
"""
        process = subprocess.Popen(
            [sys.executable, "-c", code, str(tmp_path), str(request), str(marker)],
            env={k: os.environ[k] for k in ("PATH", "PYTHONPATH") if k in os.environ},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            deadline = time.monotonic() + 10
            while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            assert marker.exists(), process.communicate(timeout=1)
            process.kill()
            process.communicate(timeout=3)
            assert process.returncode == -9
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=3)
        if phase != "committed":
            # Production migration must refuse hot sidecars. This fixture then
            # explicitly models operator inspection/recovery using both originals;
            # no production retry or sidecar removal is added by the fix.
            with pytest.raises(ValueError, match="sidecars"):
                apply_pair(
                    stores["strategy"],
                    stores["authority"],
                    CONFIG,
                    evidence,
                    fresh,
                    sources=SOURCES,
                )
            main = stores["strategy"].journal_path.resolve().as_uri() + "?mode=rw"
            peer = stores["authority"].journal_path.resolve().as_uri() + "?mode=rw"
            with closing(sqlite3.connect(main, uri=True, isolation_level=None)) as db:
                db.execute("ATTACH DATABASE ? AS peer", (peer,))
                db.execute("BEGIN")
                db.execute("SELECT COUNT(*) FROM main.events").fetchone()
                db.execute("SELECT COUNT(*) FROM peer.events").fetchone()
                db.execute("COMMIT")
        for role, store in stores.items():
            assert store.path.read_bytes() == projections[role]
            reopened = StateStore(store.path)
            assert reopened.load("live", read_only=True).halted
            expected = TARGET_HASH if phase == "committed" else OLD_HASH
            assert reopened.journal.config_hash == expected
            after = events(store)
            assert after[: len(before[role])] == before[role]
            assert len(after) == len(before[role]) + (phase == "committed")
