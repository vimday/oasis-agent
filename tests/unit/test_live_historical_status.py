"""Historical commitments recover metadata only; each service still proves cash."""

import sqlite3
from contextlib import closing
from copy import deepcopy
from dataclasses import asdict

import pytest

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.live.authority_socket import SCHEMA, TransactionSocketClient, status_nonce
from oasis_agent.live.journal import JournalConflict
from oasis_agent.live.recovery import adopt_authority_pending, authority_status
from oasis_agent.live.state import LoopState, StateStore
from oasis_agent.models.common import canonical_json, sha256_hex
from tests.unit.test_live_approval_cash import ApprovalFixture, approval_loop


def test_settled_authority_recovers_strategy_then_independently_settles_once(tmp_path):
    with closing(EvidenceStore(tmp_path / "evidence.db")) as evidence:
        authority = approval_loop(tmp_path, evidence, ApprovalFixture(), ApprovalFixture())
        store = StateStore(tmp_path / "authority.json")
        store.bind(sha256_hex(canonical_json(asdict(authority.c))))
        store.save(authority.state)
        authority.store = store
        pending = deepcopy(authority.state.pending)
        tx = pending.transaction
        block = pending.wallet_evidence[0].block
        strategy = LoopState(
            "live",
            signing_attempt={
                "kind": pending.kind,
                "intent": pending.intent,
                "nonce": pending.nonce,
                "target": tx.target,
                "value": tx.value,
                "calldata_hash": tx.calldata_hash,
                "block": block.number,
                "block_hash": block.block_hash,
                "wallet_evidence": [asdict(r) for r in pending.wallet_evidence],
            },
        )
        assert "confirmed" in authority._resolve_pending()
        store.save(authority.state)
        assert authority.state.pending is None and authority.state.gas_spent_wei == 218
        before = store.journal_path.read_bytes()
        public = authority_status(store, authority.c, pending.nonce)
        assert authority_status(store, authority.c)["pending"] is None
        assert authority_status(store, authority.c, pending.nonce + 1)["pending"] is None
        adopt_authority_pending(strategy, authority.c, public)
        assert strategy.pending.transaction == tx
        # Historical lookup/adoption does not copy the authority's settled counters.
        assert strategy.gas_spent_wei == 0 and strategy.actions_today == 0
        assert store.journal_path.read_bytes() == before
        own = StateStore(tmp_path / "strategy.json")
        own.bind(store.journal.config_hash)
        own.save(strategy)
        authority.store, authority.state = own, strategy
        assert "confirmed" in authority._resolve_pending()
        own.save(strategy)
        assert strategy.pending is None and strategy.gas_spent_wei == 218
        assert strategy.actions_today == 1
        with pytest.raises(ValueError):
            adopt_authority_pending(strategy, authority.c, public)


@pytest.mark.parametrize("fault", ["conflict", "later_corruption", "uncertain", "config"])
def test_history_refuses_ambiguity_corruption_uncertainty_or_foreign_config(tmp_path, fault):
    with closing(EvidenceStore(tmp_path / "evidence.db")) as evidence:
        loop = approval_loop(tmp_path, evidence, ApprovalFixture(), ApprovalFixture())
        store = StateStore(tmp_path / "authority.json")
        store.bind(sha256_hex(canonical_json(asdict(loop.c))))
        store.save(loop.state)
        nonce = loop.state.pending.nonce
        if fault == "conflict":
            loop.state.pending.signed_at += 1
        else:
            loop.state.pending = None
        if fault == "uncertain":
            loop.state.signing_attempt = {"nonce": nonce + 1}
        store.save(loop.state)
        if fault == "later_corruption":
            with sqlite3.connect(store.journal_path) as db:
                db.execute("DROP TRIGGER no_update")
                db.execute(
                    "UPDATE events SET parent = ? WHERE seq = (SELECT MAX(seq) FROM events)",
                    ("f" * 64,),
                )
        if fault == "uncertain":
            public = authority_status(store, loop.c, nonce)
            assert public["signing_unresolved"] is True
        elif fault == "config":
            with pytest.raises(ValueError, match="configuration"):
                store.bind("f" * 64)
        else:
            with pytest.raises(ValueError):
                authority_status(store, loop.c, nonce)


def test_history_read_does_not_refresh_stale_writer_revision(tmp_path):
    store = StateStore(tmp_path / "authority.json")
    store.bind("a" * 64)
    stale = StateStore(store.path)
    original = stale.load("live")
    current = store.load("live")
    current.halted = "retained halt"
    store.save(current)
    assert stale.historical_pending(0) is None
    with pytest.raises(JournalConflict):
        stale.save(original)


@pytest.mark.parametrize("nonce", [True, -1, 2**64, 1.0, "1", None])
def test_invalid_nonce_refuses_before_socket_access(tmp_path, nonce):
    frame = canonical_json(
        {"schema": SCHEMA, "account": "account", "operation": "status", "nonce": nonce}
    )
    with pytest.raises(ValueError):
        status_nonce(frame, "account")
    if nonce is not None:
        client = TransactionSocketClient(tmp_path / "absent.sock", "0x" + "11" * 20)
        with pytest.raises(ValueError, match="nonce"):
            client.pending_status(nonce)
