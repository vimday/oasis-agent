"""Lost replies recover public commitments without authorizing any transaction."""

from contextlib import closing
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path

import pytest

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.live.recovery import (
    UNKNOWN_SIGNING_HALT,
    adopt_authority_pending,
    release_unsigned_attempt,
)
from oasis_agent.live.runner import MAX_RELEASED_ATTEMPTS
from oasis_agent.live.state import LoopState, StateStore
from tests.unit.test_live_authority import setup_authority
from tests.unit.test_live_lp import CONFIG, ScriptedRpc, live_loop
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


def unsigned_status(**overrides):
    status = {
        "account": CONFIG.account,
        "chain_id": CONFIG.chain_id,
        "pending": None,
        "signing_unresolved": False,
    }
    return status | overrides


def refused_state(nonce=5):
    state = LoopState("live")
    state.halted = UNKNOWN_SIGNING_HALT
    state.signing_attempt = {"nonce": nonce, "kind": "approve_permit2"}
    return state


def test_refused_proposal_is_released_only_with_authority_and_chain_proof():
    state = refused_state()
    release_unsigned_attempt(state, CONFIG, unsigned_status(), (5, 5, 5, 5))
    assert state.signing_attempt is None and state.halted is None
    assert state.history[-1] == {
        "event": "signing_attempt_released",
        "nonce": 5,
        "kind": "approve_permit2",
    }


@pytest.mark.parametrize(
    ("status", "nonces"),
    [
        (unsigned_status(signing_unresolved=True), (5, 5, 5, 5)),  # authority mid-signature
        (unsigned_status(pending={"nonce": 5}), (5, 5, 5, 5)),  # a commitment exists
        (unsigned_status(account="0x" + "11" * 20), (5, 5, 5, 5)),
        (unsigned_status(chain_id=1), (5, 5, 5, 5)),
        ({k: v for k, v in unsigned_status().items() if k != "pending"}, (5, 5, 5, 5)),
        ({k: v for k, v in unsigned_status().items() if k != "signing_unresolved"}, (5,) * 4),
        (unsigned_status(), (6, 5, 5, 5)),  # nonce consumed on one provider
        (unsigned_status(), (5, 6, 5, 5)),  # signed tx sits in a mempool
        (unsigned_status(), (5, 5, 5, 6)),
        (unsigned_status(), (5, 5)),  # one provider is not proof
        (unsigned_status(), (5, 5, True, 5)),
    ],
)
def test_release_refuses_any_sign_of_a_signature_without_mutation(status, nonces):
    state = refused_state()
    before = deepcopy(state)
    with pytest.raises(ValueError):
        release_unsigned_attempt(state, CONFIG, status, nonces)
    assert state == before


def test_release_preserves_unrelated_halt_and_refuses_pending():
    state = refused_state()
    state.halted = "operator stop"
    release_unsigned_attempt(state, CONFIG, unsigned_status(), (5, 5, 5, 5))
    assert state.halted == "operator stop" and state.signing_attempt is None
    with pytest.raises(ValueError):
        release_unsigned_attempt(state, CONFIG, unsigned_status(), (5, 5, 5, 5))


class StatusOnlySigner:
    account = CONFIG.account

    def __init__(self, status):
        self.status, self.calls = status, []

    def pending_status(self, nonce):
        self.calls.append(nonce)
        return self.status

    def sign_envelope(self, fields, context):
        pytest.fail("release must never sign")


@pytest.mark.parametrize("unsigned", [True, False])
def test_tick_releases_a_refused_proposal_or_halts(tmp_path: Path, unsigned: bool):
    loop = live_loop(tmp_path, ScriptedRpc(latest_nonce=5))
    loop.state.mode = "live"
    loop.signer = StatusOnlySigner(
        unsigned_status() if unsigned else unsigned_status(signing_unresolved=True)
    )
    loop.state.signing_attempt = {"nonce": 5, "kind": "approve_permit2"}
    status = loop.tick()
    assert loop.signer.calls == [5]
    if unsigned:
        assert status.startswith("released unsigned approve_permit2 reservation at nonce 5")
        assert loop.state.signing_attempt is None and loop.state.halted is None
    else:
        assert status == f"HALT {UNKNOWN_SIGNING_HALT}"
        assert loop.state.signing_attempt is not None
        assert loop.state.halted == UNKNOWN_SIGNING_HALT
        assert loop.tick() == f"halted: {UNKNOWN_SIGNING_HALT}"


def test_tick_release_is_bounded_per_process(tmp_path: Path):
    loop = live_loop(tmp_path, ScriptedRpc(latest_nonce=5))
    loop.state.mode = "live"
    loop.signer = StatusOnlySigner(unsigned_status())
    for _ in range(MAX_RELEASED_ATTEMPTS):
        loop.state.signing_attempt = {"nonce": 5, "kind": "approve_permit2"}
        assert loop.tick().startswith("released unsigned")
    loop.state.signing_attempt = {"nonce": 5, "kind": "approve_permit2"}
    assert loop.tick() == f"HALT {UNKNOWN_SIGNING_HALT}"
    assert loop.signer.calls == [5] * MAX_RELEASED_ATTEMPTS
