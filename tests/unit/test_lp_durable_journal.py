"""Crash/restart and competing-writer safety for LP account/nonce reservations."""

from dataclasses import replace

import pytest

from oasis_agent.errors import ExecutionBlockedError
from oasis_agent.execution.lp_journal import (
    HaltRecoveryProof,
    LpExecutionJournal,
    recovery_evidence_hash,
)
from oasis_agent.execution.lp_state_machine import LpState
from oasis_agent.models.common import BlockRef, EvidenceRef
from tests.unit.test_lp_state_machine import make_intent

ACCOUNT = "0x" + "45" * 20
EVIDENCE = "0x" + "67" * 32


def recovery_proof(**overrides):
    block = BlockRef(4663, 100, "0x" + "ab" * 32, 1_000)
    values = {
        "chain_id": 4663,
        "account": ACCOUNT,
        "position_token_id": make_intent().position_token_id,
        "recovery_transaction_hash": "0x" + "98" * 32,
        "finalized_block": block,
        "position_liquidity_after": 0,
        "receipt_success": True,
        "token_balances_reconciled": True,
        "approvals_revoked": True,
    }
    values.update(overrides)
    content_hash = recovery_evidence_hash(**values)
    values["evidence"] = tuple(
        EvidenceRef(
            "recovery-reconciliation",
            content_hash,
            block,
            source,
            "2026-09-07T00:00:00+00:00",
            "2" * 64,
            raw,
            "4" * 64,
        )
        for source, raw in (("rpc-a", "3" * 64), ("rpc-b", "5" * 64))
    )
    return HaltRecoveryProof(**values)


def journal(path):
    return LpExecutionJournal(
        path, chain_id=4663, account=ACCOUNT, config_hash=make_intent().config_hash
    )


def test_reservation_is_idempotent_and_survives_restart(tmp_path):
    path = tmp_path / "execution.db"
    intent = make_intent()
    with journal(path) as db:
        first = db.reserve(intent)
        assert db.reserve(intent) == first
    with journal(path) as db:
        assert db.get(intent.intent_hash) == first
        with pytest.raises(ExecutionBlockedError, match="nonce"):
            db.reserve(replace(intent, intent_id="different-action"))


def test_concurrent_connections_cannot_double_reserve_nonce(tmp_path):
    path = tmp_path / "execution.db"
    with journal(path) as first, journal(path) as second:
        first.reserve(make_intent())
        with pytest.raises(ExecutionBlockedError, match="nonce"):
            second.reserve(make_intent(intent_id="racing-action"))


def test_pending_action_blocks_new_nonce_until_reconciled(tmp_path):
    with journal(tmp_path / "execution.db") as db:
        db.reserve(make_intent())
        with pytest.raises(ExecutionBlockedError, match="unresolved"):
            db.reserve(make_intent(nonce=4))


def test_halt_cannot_clear_pending_nonce_after_restart(tmp_path):
    path = tmp_path / "execution.db"
    intent = make_intent()
    with journal(path) as db:
        record = db.reserve(intent)
        record = db.advance(
            intent.intent_hash,
            expected_digest=record.digest,
            target=LpState.HALTED,
            evidence_hash=EVIDENCE,
        )
    with journal(path) as db:
        assert db.get(intent.intent_hash).state is LpState.HALTED
        with pytest.raises(ExecutionBlockedError, match="unresolved"):
            db.reserve(make_intent(nonce=4))
        with pytest.raises(ExecutionBlockedError):
            db.advance(
                intent.intent_hash,
                expected_digest=record.digest,
                target=LpState.CLOSED,
                evidence_hash=EVIDENCE,
            )


def test_compare_and_swap_rejects_stale_writer(tmp_path):
    with journal(tmp_path / "execution.db") as db:
        old = db.reserve(make_intent())
        current = db.advance(
            old.intent_hash,
            expected_digest=old.digest,
            target=LpState.PINNED_SIMULATED,
            evidence_hash=EVIDENCE,
        )
        with pytest.raises(ExecutionBlockedError, match="stale"):
            db.advance(
                old.intent_hash,
                expected_digest=old.digest,
                target=LpState.POLICY_APPROVED,
                evidence_hash=EVIDENCE,
            )
        assert db.get(old.intent_hash) == current


def test_policy_cannot_change_on_restart(tmp_path):
    path = tmp_path / "execution.db"
    with journal(path):
        pass
    with pytest.raises(ValueError, match="policy"):
        LpExecutionJournal(path, chain_id=1, account=ACCOUNT, config_hash=make_intent().config_hash)


def test_tampering_blocks_reads_and_writes(tmp_path):
    with journal(tmp_path / "execution.db") as db:
        db.reserve(make_intent())
        db.db.execute("UPDATE events SET digest='broken'")
        db.db.commit()
        with pytest.raises(ValueError, match="corrupt"):
            db.get(make_intent().intent_hash)
        with pytest.raises(ValueError, match="corrupt"):
            db.reserve(make_intent(nonce=4))


def test_submission_hash_cannot_be_replaced(tmp_path):
    with journal(tmp_path / "execution.db") as db:
        record = db.reserve(make_intent())
        for state in (LpState.PINNED_SIMULATED, LpState.POLICY_APPROVED, LpState.SIGNED):
            record = db.advance(
                record.intent_hash,
                expected_digest=record.digest,
                target=state,
                evidence_hash=EVIDENCE,
                transaction_hash=EVIDENCE if state is LpState.SIGNED else None,
            )
        with pytest.raises(ValueError, match="transaction"):
            db.advance(
                record.intent_hash,
                expected_digest=record.digest,
                target=LpState.SUBMITTED,
                evidence_hash=EVIDENCE,
                transaction_hash="0x" + "89" * 32,
            )
        assert db.get(record.intent_hash) == record


def test_signed_transaction_can_halt_without_releasing_nonce(tmp_path):
    with journal(tmp_path / "execution.db") as db:
        record = db.reserve(make_intent())
        for state in (LpState.PINNED_SIMULATED, LpState.POLICY_APPROVED, LpState.SIGNED):
            record = db.advance(
                record.intent_hash,
                expected_digest=record.digest,
                target=state,
                evidence_hash=EVIDENCE,
                transaction_hash=EVIDENCE if state is LpState.SIGNED else None,
            )
        record = db.advance(
            record.intent_hash,
            expected_digest=record.digest,
            target=LpState.HALTED,
            evidence_hash=EVIDENCE,
        )
        assert record.transaction_hash == EVIDENCE
        with pytest.raises(ExecutionBlockedError, match="unresolved"):
            db.reserve(make_intent(nonce=4))


@pytest.mark.parametrize("corruption", ["schema", "intent", "evidence", "transaction"])
def test_semantically_invalid_rehashed_reservation_rejected(tmp_path, corruption):
    import hashlib
    import json

    from oasis_agent.models.common import canonical_json

    with journal(tmp_path / "execution.db") as db:
        record = db.reserve(make_intent())
        data = json.loads(db.db.execute("SELECT payload FROM events").fetchone()[0])
        if corruption == "schema":
            data["schema"] = "unknown"
        elif corruption == "intent":
            data["intent"]["amount0_max"] += 1
        elif corruption == "evidence":
            data["evidence_hash"] = "missing"
        else:
            data["transaction_hash"] = EVIDENCE
        payload = canonical_json(data)
        db.db.execute(
            "UPDATE events SET payload=?, digest=?", (payload, hashlib.sha256(payload).hexdigest())
        )
        db.db.commit()
        with pytest.raises(ValueError):
            db.get(record.intent_hash)


def test_halted_action_closes_only_with_full_recovery_proof_and_survives_restart(tmp_path):
    path = tmp_path / "execution.db"
    intent = make_intent()
    with journal(path) as db:
        record = db.reserve(intent)
        record = db.advance(
            intent.intent_hash,
            expected_digest=record.digest,
            target=LpState.HALTED,
            evidence_hash=EVIDENCE,
        )
        closed = db.recover_halted(
            intent.intent_hash, expected_digest=record.digest, proof=recovery_proof()
        )
        assert closed.state is LpState.CLOSED
        assert closed.position_token_id == intent.position_token_id
        assert closed.recovery_transaction_hash == "0x" + "98" * 32
    with journal(path) as db:
        assert db.get(intent.intent_hash) == closed
        assert db.reserve(make_intent(intent_id="next", nonce=4)).state is LpState.RESERVED


@pytest.mark.parametrize(
    "change",
    [
        {"position_liquidity_after": 1},
        {"receipt_success": False},
        {"token_balances_reconciled": False},
        {"approvals_revoked": False},
        {"account": "0x" + "11" * 20},
        {"position_token_id": 8},
    ],
)
def test_incomplete_or_wrong_recovery_cannot_release_nonce(tmp_path, change):
    with journal(tmp_path / "execution.db") as db:
        record = db.reserve(make_intent())
        record = db.advance(
            record.intent_hash,
            expected_digest=record.digest,
            target=LpState.HALTED,
            evidence_hash=EVIDENCE,
        )
        with pytest.raises(ExecutionBlockedError, match="safe unwind"):
            db.recover_halted(
                record.intent_hash,
                expected_digest=record.digest,
                proof=recovery_proof(**change),
            )
        assert db.get(record.intent_hash).state is LpState.HALTED


def test_recovery_single_provider_or_unrelated_content_fails_closed():
    proof = recovery_proof()
    with pytest.raises(ValueError, match="two distinct"):
        replace(proof, evidence=proof.evidence[:1])
    with pytest.raises(ValueError, match="incomplete"):
        replace(
            proof, evidence=(replace(proof.evidence[0], content_hash="9" * 64), *proof.evidence[1:])
        )
