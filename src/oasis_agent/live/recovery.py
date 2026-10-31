"""Recover a lost authority reply using public metadata; never re-sign or broadcast."""

from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ..evidence.store import ReadOnlyEvidenceStore
from ..models.common import evidence_ref_from_json
from .config import LiveConfig
from .intent import SubmittedTransaction, pending_plan_hash
from .state import LoopState, Pending, StateStore
from .wallet import WalletScope, replay_wallet


def require_recovery_wallets(path: Path, state: LoopState, c: LiveConfig) -> None:
    """Replay local prerequisites before recovery spends RPC quota or creates evidence.

    This is not settlement validation: receipt/cost/cash checks still run later.
    """
    if state.mode != "live" or path.absolute() != path.resolve() or not path.is_file():
        raise ValueError("existing recovery wallet evidence required")
    nonce: object
    if state.pending is not None and state.signing_attempt is None:
        refs = state.pending.wallet_evidence
        nonce = state.pending.nonce
    elif state.pending is None and isinstance(state.signing_attempt, dict):
        attempt = state.signing_attempt
        refs = tuple(evidence_ref_from_json(r) for r in attempt.get("wallet_evidence", []))
        nonce = attempt.get("nonce")
        if any(
            r.block is None
            or r.block.number != attempt.get("block")
            or r.block.block_hash != attempt.get("block_hash")
            for r in refs
        ):
            raise ValueError("recovery wallet differs from signing reservation")
    else:
        raise ValueError("one pending or uncertain transaction required")
    if (
        len(refs) != 2
        or refs[0].source == refs[1].source
        or refs[0].block is None
        or refs[0].block != refs[1].block
        or type(nonce) is not int
        or not 0 <= nonce < 2**64
    ):
        raise ValueError("original recovery wallet quorum required")
    with closing(ReadOnlyEvidenceStore(path)) as evidence:
        captures = tuple(replay_wallet(evidence, ref) for ref in refs)
        if any(capture.scope != WalletScope.from_config(c) for capture in captures):
            raise ValueError("recovery wallet scope differs from configuration")
        balances = tuple(capture.reconstruct() for capture in captures)
        if balances[0] != balances[1] or balances[0].nonce != nonce:
            raise ValueError("recovery wallet balance or nonce disagrees")


def authority_status(store: StateStore, c: LiveConfig, nonce: int | None = None) -> dict[str, Any]:
    """Public commitment only; historical lookup does not assert settlement success."""
    state = store.load("live")
    pending = state.pending if nonce is None else store.historical_pending(nonce)
    return {
        "account": c.account,
        "chain_id": c.chain_id,
        "pending": asdict(pending) if pending else None,
        "signing_unresolved": state.signing_attempt is not None,
    }


def adopt_authority_pending(state: LoopState, c: LiveConfig, status: dict[str, Any]) -> None:
    attempt = state.signing_attempt
    if state.mode != "live" or state.pending is not None or not isinstance(attempt, dict):
        raise ValueError("recovery requires exactly one unresolved signing attempt")
    public = status.get("pending")
    if (
        status.get("account") != c.account
        or status.get("signing_unresolved") is not False
        or status.get("chain_id") != c.chain_id
        or not isinstance(public, dict)
        or not isinstance(public.get("transaction"), dict)
    ):
        raise ValueError("authority has no public signed commitment for this account")
    tx = SubmittedTransaction(**public["transaction"])
    if (
        tx.chain_id != c.chain_id
        or tx.account != c.account
        or public.get("kind") != attempt.get("kind")
        or public.get("intent") != attempt.get("intent")
        or public.get("tx_hash") != tx.transaction_hash
        or public.get("nonce") != tx.nonce
        or tx.nonce != attempt.get("nonce")
        or tx.target != attempt.get("target")
        or tx.value != attempt.get("value")
        or tx.calldata_hash != attempt.get("calldata_hash")
        or tx.max_fee_per_gas > c.max_fee_per_gas_wei
        or tx.max_priority_fee_per_gas > c.priority_fee_wei
        or tx.gas_limit > 1_500_000
    ):
        raise ValueError("authority commitment differs from reserved proposal")
    refs = tuple(evidence_ref_from_json(r) for r in attempt.get("wallet_evidence", []))
    if len(refs) != 2 or refs[0].source == refs[1].source or refs[0].block != refs[1].block:
        raise ValueError("lost reply requires original retained wallet quorum")
    if (
        refs[0].block is None
        or refs[0].block.number != attempt["block"]
        or refs[0].block.block_hash != attempt["block_hash"]
    ):
        raise ValueError("original wallet scope differs from signing reservation")
    at = public.get("signed_at")
    if type(at) is not int or at < 0:
        raise ValueError("authority signature timestamp missing")
    state.pending = Pending(
        public["kind"],
        tx.transaction_hash,
        tx.nonce,
        at,
        dict(public["intent"]),
        tx.target,
        tx,
        pending_plan_hash(
            asdict(c),
            public["kind"],
            public["intent"],
            tx,
            asdict(state.position) if state.position else None,
            tuple(asdict(r) for r in refs),
        ),
        refs,
    )
    state.signing_attempt = None
    if state.halted == UNKNOWN_SIGNING_HALT:
        state.halted = None
    state.record("authority_reply_recovered", transaction=asdict(tx))


UNKNOWN_SIGNING_HALT = "signing outcome unknown; durable nonce reservation requires reconciliation"


def release_unsigned_attempt(
    state: LoopState, c: LiveConfig, status: dict[str, Any], chain_nonces: tuple[int, ...]
) -> None:
    """Release a reservation the authority provably never signed; never signs or sends.

    The authority journals its own signing attempt before key use and a commitment after,
    so "no unresolved attempt and no commitment at this nonce" plus an unconsumed nonce on
    every provider (latest and pending) means no signature for the nonce exists.
    """
    attempt = state.signing_attempt
    if state.mode != "live" or state.pending is not None or not isinstance(attempt, dict):
        raise ValueError("release requires exactly one unresolved signing attempt")
    nonce = attempt.get("nonce")
    if (
        type(nonce) is not int
        or status.get("account") != c.account
        or status.get("chain_id") != c.chain_id
        or status.get("signing_unresolved") is not False
        or "pending" not in status
        or status["pending"] is not None
        or len(chain_nonces) < 4
        or any(type(n) is not int or n != nonce for n in chain_nonces)
    ):
        raise ValueError("authority or chain does not prove the attempt unsigned")
    state.signing_attempt = None
    if state.halted == UNKNOWN_SIGNING_HALT:
        state.halted = None
    state.record("signing_attempt_released", nonce=nonce, kind=attempt.get("kind"))
