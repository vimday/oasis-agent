"""Legacy defaults must not change the hash approved for an original journal."""

import json
import sqlite3
from contextlib import ExitStack, closing
from dataclasses import asdict

import pytest

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.live.feed_transition import review_service
from oasis_agent.live.journal import JournalConflict
from oasis_agent.live.state import LoopState, StateStore
from oasis_agent.models.common import canonical_json, sha256_hex
from tests.unit import test_live_feed_migration as feed
from tests.unit import test_live_feed_pair_atomic as feed_pair
from tests.unit import test_live_runtime_epoch as runtime
from tests.unit.test_live_valuation import synthetic_feed_pin  # noqa: F401


def omit_default(store):
    state = store.load("live", read_only=True)
    raw = asdict(state)
    assert raw.pop("last_light_check") is None
    store.journal.append(raw)
    return canonical_json(raw)


@pytest.mark.parametrize("legacy", [False, True])
def test_runtime_review_and_pair_commit_bind_the_legacy_raw_snapshot(tmp_path, monkeypatch, legacy):
    data, _ = runtime.prepare(tmp_path, monkeypatch, legacy=legacy)
    raw_by_role, old_rows = {}, {}
    for role, root in zip(runtime.cli.ROLES, data[0], strict=True):
        store = StateStore(runtime.cli.paths(root, role)[0], require_existing=True)
        raw_by_role[role] = omit_default(store)
        old_rows[role] = runtime.events(root, role)
    before = runtime.fixtures.snapshot(tmp_path)
    report = runtime.fixtures.run(data)
    assert runtime.fixtures.snapshot(tmp_path) == before
    for role, raw in raw_by_role.items():
        assert report["services"][role]["state_hash"] == sha256_hex(raw)
    result = runtime.apply(data, report)
    assert result["atomic_pair"] and result["migration_applied"]
    assert result["execution_eligible"] is False
    for role, root in zip(runtime.cli.ROLES, data[0], strict=True):
        assert runtime.events(root, role)[:-1] == old_rows[role]
        state = StateStore(runtime.cli.paths(root, role)[0]).load("live", read_only=True)
        assert state.halted and state.last_light_check is None


def test_feed_review_and_commit_bind_the_legacy_raw_snapshot(tmp_path, monkeypatch):
    with closing(EvidenceStore(tmp_path / "proof.db")) as evidence:
        store, _, refs, stop = feed.prepare(tmp_path, evidence, monkeypatch)
        raw = omit_default(store)
        old_rows = feed.events(store)
        result = feed.apply(store, refs, stop, evidence)
        assert result["migration_applied"] and result["execution_eligible"] is False
        rows = feed.events(store)
        assert rows[: len(old_rows)] == old_rows
        with closing(sqlite3.connect(store.journal_path)) as db:
            record = json.loads(db.execute("SELECT payload FROM config_transitions").fetchone()[0])
        assert record["source_state_hash"] == record["review"]["state_hash"] == sha256_hex(raw)


def test_paired_feed_epoch_accepts_omitted_defaults_without_rewriting_history(
    tmp_path, monkeypatch
):
    with ExitStack() as resources:
        stores, evidence, fresh = feed_pair.pair(tmp_path, monkeypatch, resources)
        old_rows = {}
        for role, store in stores.items():
            omit_default(store)
            old_rows[role] = feed.events(store)
        result = feed.apply_pair(
            stores["strategy"],
            stores["authority"],
            feed.CONFIG,
            evidence,
            fresh,
            sources=feed.SOURCES,
        )
        assert result["atomic_pair"] and result["migration_applied"]
        assert result["execution_eligible"] is False
        for role, store in stores.items():
            assert feed.events(store)[: len(old_rows[role])] == old_rows[role]


@pytest.mark.parametrize("fault", ["financial", "pretty", "duplicate"])
def test_feed_review_refuses_mismatched_or_noncanonical_source_bytes(tmp_path, fault):
    with closing(EvidenceStore(tmp_path / "proof.db")) as evidence:
        state, new, refs = feed.setup(evidence)
        raw = asdict(state)
        if fault == "financial":
            raw["gas_spent_wei"] += 1
        payload = canonical_json(raw)
        if fault == "pretty":
            payload = json.dumps(raw, indent=2).encode()
        if fault == "duplicate":
            payload = b'{"mode":"live",' + payload[1:]
        before = runtime.fixtures.snapshot(tmp_path)
        with pytest.raises(ValueError):
            review_service(
                feed.CONFIG,
                state,
                feed.OLD_HASH,
                evidence,
                source_payload=payload,
                sources=feed.SOURCES,
                fresh=refs,
                now=new.timestamp,
            )
        assert runtime.fixtures.snapshot(tmp_path) == before


def bound_store(tmp_path):
    store = StateStore(tmp_path / "state.json")
    store.bind("a" * 64)
    store.save(LoopState("live", journal_required=True))
    raw = omit_default(store)
    return store, store.load("live", read_only=True), raw


def test_retained_payload_is_readonly_and_keeps_omitted_defaults(tmp_path):
    store, state, raw = bound_store(tmp_path)
    before = runtime.fixtures.snapshot(tmp_path)
    identity = (store.journal, store.journal.revision, store.journal.config_hash)
    assert store.retained_payload(state) == raw
    assert (store.journal, store.journal.revision, store.journal.config_hash) == identity
    assert runtime.fixtures.snapshot(tmp_path) == before
    assert "last_light_check" not in json.loads(raw)


def test_semantically_equal_competing_snapshot_does_not_refresh_caller_cas(tmp_path):
    store, state, _ = bound_store(tmp_path)
    tip = store.journal.revision
    peer = StateStore(store.path)
    peer.load("live", read_only=True)
    peer.journal.append(asdict(state))
    before = runtime.fixtures.snapshot(tmp_path)
    with pytest.raises(ValueError, match="changed"):
        store.retained_payload(state)
    assert store.journal.revision == tip
    with pytest.raises(JournalConflict):
        store.save(state)
    assert runtime.fixtures.snapshot(tmp_path) == before


@pytest.mark.parametrize("fault", ["financial_state", "shadow", "unloaded", "missing"])
def test_retained_payload_refuses_an_unbound_or_different_state_without_writes(tmp_path, fault):
    store, state, _ = bound_store(tmp_path)
    tip = store.journal.revision
    if fault == "financial_state":
        state.gas_spent_wei += 1
    elif fault == "shadow":
        state.mode = "shadow"
    elif fault == "unloaded":
        store = StateStore(store.path)
    else:
        store.journal_path.unlink()
    before = runtime.fixtures.snapshot(tmp_path)
    with pytest.raises(ValueError):
        store.retained_payload(state)
    if store.journal is not None:
        assert store.journal.revision == tip
    assert runtime.fixtures.snapshot(tmp_path) == before
