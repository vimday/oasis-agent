"""Lost replies recover public commitments without authorizing any transaction."""

from contextlib import closing
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path

import pytest

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.live.recovery import adopt_authority_pending
from oasis_agent.live.state import LoopState, StateStore
from tests.unit.test_live_authority import setup_authority
from tests.unit.test_live_valuation import synthetic_feed_pin  # noqa: F401


def lost_reply(authority, fields, context):
    authority.sign(fields, context)
    public = authority.status()
    pending = authority.loop.state.pending
    tx = pending.transaction
    state = LoopState("live")
    state.halted = "signing outcome unknown; durable nonce reservation requires reconciliation"
    state.signing_attempt = {
        **context,
        "nonce": tx.nonce,
        "target": tx.target,
        "value": tx.value,
        "calldata_hash": tx.calldata_hash,
        "wallet_evidence": [asdict(r) for r in pending.wallet_evidence],
    }
    return state, public


def test_lost_reply_adopts_same_commitment_and_survives_strategy_restart(tmp_path, monkeypatch):
    with closing(EvidenceStore(tmp_path / "evidence.db")) as evidence:
        authority, key, fields, context = setup_authority(tmp_path, evidence, monkeypatch)
        state, public = lost_reply(authority, fields, context)
        adopt_authority_pending(state, authority.loop.c, public)
        assert state.pending.transaction == authority.loop.state.pending.transaction
        assert state.signing_attempt is None and state.halted is None
        assert key.calls == 1
        store = StateStore(tmp_path / "strategy.json")
        store.bind(authority.loop.store.journal.config_hash)
        store.save(state)
        assert StateStore(store.path).load("live").pending == state.pending
        with pytest.raises(ValueError):
            adopt_authority_pending(state, authority.loop.c, public)
        assert key.calls == 1


@pytest.mark.parametrize(
    "fault",
    [
        "account",
        "chain",
        "nonce",
        "intent",
        "target",
        "value",
        "data",
        "fees",
        "hash",
        "block",
        "refs",
        "missing",
        "uncertain_signature",
        "missing_signature_status",
    ],
)
def test_recovery_rejects_foreign_or_incomplete_commitment_without_mutation(
    tmp_path, monkeypatch, fault
):
    with closing(EvidenceStore(tmp_path / "evidence.db")) as evidence:
        authority, key, fields, context = setup_authority(tmp_path, evidence, monkeypatch)
        state, public = lost_reply(authority, fields, context)
        tx = public["pending"]["transaction"]
        if fault == "account":
            public["account"] = "0x" + "11" * 20
        elif fault == "chain":
            public["chain_id"] = 1
        elif fault == "nonce":
            tx["nonce"] += 1
        elif fault == "intent":
            public["pending"]["intent"]["amount"] -= 1
        elif fault == "target":
            tx["target"] = "0x" + "11" * 20
        elif fault == "value":
            tx["value"] = 1
        elif fault == "data":
            tx["calldata_hash"] = "0x" + "11" * 32
        elif fault == "fees":
            tx["max_fee_per_gas"] += 1
        elif fault == "hash":
            tx["transaction_hash"] = "0x" + "11" * 32
        elif fault == "block":
            state.signing_attempt["block"] += 1
        elif fault == "refs":
            state.signing_attempt["wallet_evidence"] = []
        elif fault == "missing":
            public["pending"] = None
        elif fault == "uncertain_signature":
            public["signing_unresolved"] = True
        elif fault == "missing_signature_status":
            public.pop("signing_unresolved")
        before = deepcopy(state)
        with pytest.raises(ValueError):
            adopt_authority_pending(state, authority.loop.c, public)
        assert state == before and key.calls == 1


def test_recovery_preserves_unrelated_halt(tmp_path: Path, monkeypatch):
    with closing(EvidenceStore(tmp_path / "evidence.db")) as evidence:
        authority, key, fields, context = setup_authority(tmp_path, evidence, monkeypatch)
        state, public = lost_reply(authority, fields, context)
        state.halted = "operator stop"
        adopt_authority_pending(state, authority.loop.c, public)
        assert state.halted == "operator stop" and key.calls == 1
