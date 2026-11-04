"""Append-only migration, crash/CAS boundaries and conservative halted recovery."""

import fcntl
import json
import sqlite3
import sys
import time
from contextlib import ExitStack, closing
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.live import feed_transition
from oasis_agent.live.feed_epoch import HALT
from oasis_agent.live.feed_transition import apply_pair, apply_service
from oasis_agent.live.journal import LiveJournal
from oasis_agent.live.state import StateStore
from oasis_agent.models.common import canonical_json, sha256_hex
from scripts import migrate_live_feed_transition as cli
from tests.unit.test_live_feed_transition import CONFIG, OLD_HASH, SOURCES, capture, setup
from tests.unit.test_live_valuation import synthetic_feed_pin  # noqa: F401

TARGET_HASH = sha256_hex(canonical_json(asdict(CONFIG)))


def events(store):
    with closing(sqlite3.connect(store.journal_path)) as db:
        return db.execute("SELECT * FROM events ORDER BY seq").fetchall()


def prepare(tmp_path, evidence, monkeypatch):
    state, new, refs = setup(evidence)
    store = StateStore(tmp_path / "state.json")
    store.bind(OLD_HASH)
    state.journal_required = True
    store.save(state)
    stop = tmp_path / "STOP"
    stop.touch()
    (tmp_path / "authority.lock").touch()
    monkeypatch.setattr("oasis_agent.live.feed_transition.time.time", lambda: new.timestamp)
    return store, state, refs, stop


def apply(store, refs, stop, evidence, **kwargs):
    return apply_service(
        store,
        CONFIG,
        evidence,
        sources=SOURCES,
        fresh=refs,
        expected_tip=kwargs.pop("tip", store.journal.revision),
        stop_file=stop,
        role="authority",
        **kwargs,
    )


@pytest.mark.parametrize("halted", [False, True])
def test_transition_preserves_old_events_and_closes_both_bindings(tmp_path, monkeypatch, halted):
    with closing(EvidenceStore(tmp_path / "evidence.db")) as evidence:
        store, state, refs, stop = prepare(tmp_path, evidence, monkeypatch)
        state.halted = "original private halt" if halted else None
        store.save(state)
        before = events(store)
        old = LiveJournal(store.journal_path, OLD_HASH, require_existing=True)
        old.load()
        result = apply(store, refs, stop, evidence)
        after = events(store)
        assert after[: len(before)] == before and len(after) == len(before) + 1
        assert result["migration_applied"] and not result["execution_eligible"]
        migrated = StateStore(store.path).load("live")
        assert migrated.halted == (state.halted or HALT)
        assert migrated.exact_risk.basis == state.exact_risk.basis
        assert migrated.exact_risk.external == state.exact_risk.external
        assert migrated.history == state.history and migrated.position == state.position
        assert migrated.gas_spent_wei == state.gas_spent_wei
        assert migrated.last_units == state.last_units
        assert migrated.last_observation is None and migrated.last_light_check is None
        LiveJournal(store.journal_path, TARGET_HASH, require_existing=True).load()
        with pytest.raises(ValueError, match="configuration mismatch"):
            old.load()
        assert old.config_hash == OLD_HASH  # a failed read cannot silently rebind a writer
        with pytest.raises(ValueError):
            old.append(asdict(state))
        with pytest.raises(ValueError):
            apply(store, refs, stop, evidence)
        assert events(store) == after


@pytest.mark.parametrize("fault", ["no_stop", "stale_tip", "missing_proof", "changed_risk"])
def test_refusal_never_creates_transition_metadata(tmp_path, monkeypatch, fault):
    with closing(EvidenceStore(tmp_path / "evidence.db")) as evidence:
        store, state, refs, stop = prepare(tmp_path, evidence, monkeypatch)
        tip = store.journal.revision
        if fault == "no_stop":
            stop.unlink()
        elif fault == "stale_tip":
            tip = "f" * 64
        elif fault == "missing_proof":
            refs = ()
        else:
            state.exact_risk.last_nav += 1
            store.save(state)
            tip = store.journal.revision
        before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
        with pytest.raises(ValueError):
            apply(store, refs, stop, evidence, tip=tip)
        assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before


def test_expiry_during_write_lock_rolls_back_ddl_and_state(tmp_path, monkeypatch):
    with closing(EvidenceStore(tmp_path / "evidence.db")) as evidence:
        store, _, refs, stop = prepare(tmp_path, evidence, monkeypatch)
        now = time.time()
        clock = iter([now, now + 10000])
        monkeypatch.setattr("oasis_agent.live.feed_transition.time.time", lambda: next(clock))
        before = events(store)
        with pytest.raises(ValueError, match="expired"):
            apply(store, refs, stop, evidence)
        assert events(store) == before
        with closing(sqlite3.connect(store.journal_path)) as db:
            assert not db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='config_transitions'"
            ).fetchone()


def test_projection_crash_recovers_new_halted_epoch_without_retry(tmp_path, monkeypatch):
    with closing(EvidenceStore(tmp_path / "evidence.db")) as evidence:
        store, state, refs, stop = prepare(tmp_path, evidence, monkeypatch)
        projection = store.path.read_bytes()

        def crash(*_):
            raise OSError("synthetic interruption")

        monkeypatch.setattr(Path, "replace", crash)
        with pytest.raises(OSError):
            apply(store, refs, stop, evidence)
        assert store.path.read_bytes() == projection
        recovered = StateStore(store.path)
        loaded = recovered.load("live", read_only=True)
        assert recovered.journal.config_hash == TARGET_HASH
        assert loaded.halted == state.halted
        assert loaded.exact_risk.last_at > state.exact_risk.last_at


@pytest.mark.parametrize("fault", ["metadata", "orphan", "snapshot"])
def test_transition_corruption_cannot_be_read_as_valid_epoch(tmp_path, monkeypatch, fault):
    with closing(EvidenceStore(tmp_path / "evidence.db")) as evidence:
        store, _, refs, stop = prepare(tmp_path, evidence, monkeypatch)
        apply(store, refs, stop, evidence)
        with closing(sqlite3.connect(store.journal_path)) as db:
            if fault == "metadata":
                db.execute("DROP TRIGGER transition_no_update")
                record = json.loads(
                    db.execute("SELECT payload FROM config_transitions").fetchone()[0]
                )
                record["review"]["risk_preview"]["basis"] = 0
                db.execute("UPDATE config_transitions SET payload=?", (canonical_json(record),))
            elif fault == "orphan":
                db.execute("INSERT INTO config_transitions VALUES (9999, ?)", (b"{}",))
            else:
                db.execute("DROP TRIGGER no_update")
                seq, payload = db.execute(
                    "SELECT seq,payload FROM events ORDER BY seq DESC LIMIT 1"
                ).fetchone()
                state = json.loads(payload)
                state["halted"] = None
                db.execute("UPDATE events SET payload=? WHERE seq=?", (canonical_json(state), seq))
            db.commit()
        with pytest.raises(ValueError):
            StateStore(store.path).load("live", read_only=True)


@pytest.mark.parametrize("new_day", [False, True])
def test_migration_rolls_action_counters_only_on_new_risk_day(tmp_path, monkeypatch, new_day):
    with closing(EvidenceStore(tmp_path / "evidence.db")) as evidence:
        store, state, refs, stop = prepare(tmp_path, evidence, monkeypatch)
        _, fixture, _ = setup(evidence)
        if new_day:
            fixture = replace(fixture, timestamp=fixture.timestamp + 86400)
            _, refs = capture(evidence, CONFIG, fixture)
        state.actions_today = 12
        state.day = state.exact_risk.day
        store.save(state)
        monkeypatch.setattr("oasis_agent.live.feed_transition.time.time", lambda: fixture.timestamp)
        apply(store, refs, stop, evidence)
        result = StateStore(store.path).load("live")
        assert result.actions_today == (0 if new_day else 12)
        assert result.day == result.exact_risk.day
        assert result.exact_risk.basis == state.exact_risk.basis
        assert result.halted == state.halted


def test_active_service_refuses_before_proof_review(tmp_path, monkeypatch):
    with closing(EvidenceStore(tmp_path / "evidence.db")) as evidence:
        store, _, refs, stop = prepare(tmp_path, evidence, monkeypatch)

        def forbidden(*args, **kwargs):
            pytest.fail("active service must refuse before evidence access")

        monkeypatch.setattr("oasis_agent.live.feed_transition.review_service", forbidden)
        with (tmp_path / "authority.lock").open("rb") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            before = events(store)
            with pytest.raises(BlockingIOError):
                apply(store, refs, stop, evidence)
            assert events(store) == before


def test_concurrent_journal_append_after_review_cannot_be_overwritten(tmp_path, monkeypatch):
    with closing(EvidenceStore(tmp_path / "evidence.db")) as evidence:
        store, _, refs, stop = prepare(tmp_path, evidence, monkeypatch)
        transition = store.journal.transition

        def concurrent(record):
            writer = StateStore(store.path)
            state = writer.load("live")
            state.halted = "concurrent operator stop"
            writer.save(state)
            return transition(record)

        monkeypatch.setattr(store.journal, "transition", concurrent)
        with pytest.raises(ValueError, match="source changed"):
            apply(store, refs, stop, evidence)
        state = StateStore(store.path).load("live")
        assert state.halted == "concurrent operator stop"
        assert LiveJournal(store.journal_path, OLD_HASH, require_existing=True).load() is not None


@pytest.mark.parametrize("fault", [None, "nav_disagreement", "second_commit_interrupted"])
def test_pair_reviews_both_and_rolls_back_both_on_second_staging_failure(
    tmp_path, monkeypatch, fault
):
    stores, evidence, fresh = {}, {}, {}
    with ExitStack() as resources:
        for role in ("strategy", "authority"):
            root = tmp_path / role
            root.mkdir()
            evidence[role] = resources.enter_context(closing(EvidenceStore(root / "proof.db")))
            store, _, refs, _ = prepare(root, evidence[role], monkeypatch)
            (root / "live.lock").touch()
            if fault == "nav_disagreement" and role == "authority":
                _, fixture, _ = setup(evidence[role])
                _, refs = capture(evidence[role], CONFIG, replace(fixture, usdc_price=98_000_000))
            stores[role], fresh[role] = store, refs
        before = {role: events(s) for role, s in stores.items()}
        if fault == "second_commit_interrupted":
            transition = LiveJournal._transition_in_transaction
            calls = []

            def interrupted(self, db, record, *, schema="main"):
                calls.append(1)
                if len(calls) == 2:
                    raise OSError("synthetic second-service interruption")
                return transition(self, db, record, schema=schema)

            monkeypatch.setattr(LiveJournal, "_transition_in_transaction", interrupted)
        if fault:
            with pytest.raises((ValueError, OSError)):
                apply_pair(
                    stores["strategy"],
                    stores["authority"],
                    CONFIG,
                    evidence,
                    fresh,
                    sources=SOURCES,
                )
        else:
            result = apply_pair(
                stores["strategy"], stores["authority"], CONFIG, evidence, fresh, sources=SOURCES
            )
            assert result["migration_applied"] and not result["execution_eligible"]
        for role, store in stores.items():
            state = store.load("live", read_only=True)
            assert state.halted
            changed = fault is None
            assert store.journal.config_hash == (TARGET_HASH if changed else OLD_HASH)
            assert events(store)[: len(before[role])] == before[role]
            if not changed:
                assert events(store) == before[role]


@pytest.mark.parametrize(
    "do_apply,projection_failure", [(False, False), (True, False), (True, True)]
)
def test_operator_command_defaults_to_readonly(
    tmp_path, monkeypatch, capsys, do_apply, projection_failure
):
    argv = ["migrate_live_feed_transition"]
    fresh, stores = {}, {}
    for role in ("strategy", "authority"):
        root = tmp_path / role
        root.mkdir()
        path = root / "proof.db"
        with closing(EvidenceStore(path)) as evidence:
            store, _, refs, _ = prepare(root, evidence, monkeypatch)
        (root / "live.lock").touch()
        stores[role] = store
        fresh[role] = [asdict(ref) for ref in refs]
        argv += [f"--{role}-state", str(store.path), f"--{role}-evidence", str(path)]
    config, refs = tmp_path / "config.json", tmp_path / "refs.json"
    config.write_bytes(canonical_json(asdict(CONFIG)))
    refs.write_bytes(canonical_json(fresh))
    argv += ["--config", str(config), "--fresh-nav", str(refs), "--sources", *SOURCES]
    before = {role: events(store) for role, store in stores.items()}
    if do_apply:
        argv += ["--apply", "--out", str(tmp_path / "attempt")]
    if projection_failure:

        def failed_projection(*_):
            raise OSError("synthetic projection failure")

        monkeypatch.setattr(StateStore, "save", failed_projection)
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as finished:
        cli.main()
    assert finished.value.code == (3 if projection_failure else 0)
    result = json.loads(capsys.readouterr().out)
    assert result["execution_eligible"] is False
    assert result["migration_applied"] is do_apply
    if projection_failure:
        assert result["projection_complete"] is False
        assert result["status"] == "both_migrated_halted_projection_incomplete"
    for role, store in stores.items():
        reopened = StateStore(store.path)
        assert reopened.load("live").halted
        assert reopened.journal.config_hash == (TARGET_HASH if do_apply else OLD_HASH)
        assert events(store)[: len(before[role])] == before[role]
        if not do_apply:
            assert events(store) == before[role]


def test_replaced_lock_during_acquisition_refuses_before_evidence(tmp_path, monkeypatch):
    with closing(EvidenceStore(tmp_path / "evidence.db")) as evidence:
        store, _, refs, stop = prepare(tmp_path, evidence, monkeypatch)
        before = events(store)
        lock_path = tmp_path / "authority.lock"
        flock = fcntl.flock

        def replace_locked_descriptor(fd, flags):
            flock(fd, flags)
            lock_path.rename(tmp_path / "old.lock")
            lock_path.touch()

        def forbidden(*args, **kwargs):
            pytest.fail("replaced service lock must refuse before proof review")

        monkeypatch.setattr(fcntl, "flock", replace_locked_descriptor)
        monkeypatch.setattr(feed_transition, "review_service", forbidden)
        with pytest.raises(ValueError, match="lock changed"):
            apply(store, refs, stop, evidence)
        assert events(store) == before


def replace_control_file(root, fault):
    path = root / ("authority.lock" if fault == "lock_replaced" else "STOP")
    if fault == "stop_modified":
        path.write_text("operator changed STOP during review")
    else:
        path.rename(path.with_suffix(".previous"))
        path.touch()


@pytest.mark.parametrize("fault", ["lock_replaced", "stop_replaced", "stop_modified"])
def test_control_identity_change_after_proof_review_refuses(tmp_path, monkeypatch, fault):
    with closing(EvidenceStore(tmp_path / "evidence.db")) as evidence:
        store, _, refs, stop = prepare(tmp_path, evidence, monkeypatch)
        before = events(store)
        review = feed_transition.review_service

        def interrupted(*args, **kwargs):
            result = review(*args, **kwargs)
            replace_control_file(tmp_path, fault)
            return result

        monkeypatch.setattr(feed_transition, "review_service", interrupted)
        with pytest.raises(ValueError, match="lock or STOP changed"):
            apply(store, refs, stop, evidence)
        assert events(store) == before
        assert StateStore(store.path).load("live", read_only=True).halted


@pytest.mark.parametrize("fault", ["lock_replaced", "stop_replaced", "stop_modified"])
@pytest.mark.parametrize("after_first_staging", [False, True])
def test_pair_rechecks_both_controls_before_its_shared_commit(
    tmp_path, monkeypatch, fault, after_first_staging
):
    stores, evidence, fresh = {}, {}, {}
    with ExitStack() as resources:
        for role in ("strategy", "authority"):
            root = tmp_path / role
            root.mkdir()
            evidence[role] = resources.enter_context(closing(EvidenceStore(root / "proof.db")))
            store, _, refs, _ = prepare(root, evidence[role], monkeypatch)
            (root / "live.lock").touch()
            stores[role], fresh[role] = store, refs
        before = {role: events(store) for role, store in stores.items()}
        if after_first_staging:
            transition = LiveJournal._transition_in_transaction

            def changed_after_staging(self, db, record, *, schema="main"):
                result = transition(self, db, record, schema=schema)
                replace_control_file(tmp_path / "authority", fault)
                return result

            monkeypatch.setattr(LiveJournal, "_transition_in_transaction", changed_after_staging)
        else:
            review = feed_transition.review_service

            def changed_after_review(*args, **kwargs):
                result = review(*args, **kwargs)
                # Mutate the peer during the first service's review.
                if not (tmp_path / "mutated").exists():
                    (tmp_path / "mutated").touch()
                    replace_control_file(tmp_path / "authority", fault)
                return result

            monkeypatch.setattr(feed_transition, "review_service", changed_after_review)
        with pytest.raises(ValueError, match="lock or STOP changed"):
            apply_pair(
                stores["strategy"], stores["authority"], CONFIG, evidence, fresh, sources=SOURCES
            )
        for role, store in stores.items():
            loaded = store.load("live", read_only=True)
            assert loaded.halted
            assert store.journal.config_hash == OLD_HASH
            assert events(store) == before[role]
