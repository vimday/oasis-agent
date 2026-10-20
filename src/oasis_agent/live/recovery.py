"""Recover a lost authority reply using public metadata; never re-sign or broadcast."""

from dataclasses import asdict
from typing import Any

from ..models.common import evidence_ref_from_json
from .config import LiveConfig
from .intent import SubmittedTransaction, pending_plan_hash
from .state import LoopState, Pending, StateStore


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
    if state.halted == "signing outcome unknown; durable nonce reservation requires reconciliation":
        state.halted = None
    state.record("authority_reply_recovered", transaction=asdict(tx))
