"""Bind successful exclusive LP action gas to replayed action-time owner cash.

This observes only actions already present in one complete cash window. It cannot
explain reverted attempts, unrelated account operations, sponsorship or all PnL.
"""

import json
from dataclasses import dataclass, field

from pydantic import TypeAdapter

from ..evidence.exact_capture import decode_header
from ..evidence.rpc_validation import decode_exchange, restore_exchanges
from ..evidence.store import EvidenceStore
from ..models.common import BlockRef, EvidenceRef
from .cash_window import CashWindowRequest, replay_cash_window
from .transaction_costs import replay_transaction_cost


@dataclass(frozen=True, slots=True)
class ActionCostProof:
    transaction_hash: str
    evidence: tuple[EvidenceRef, EvidenceRef]


@dataclass(frozen=True, slots=True)
class BoundActionCost:
    block: BlockRef
    transaction_hash: str
    owner: str
    native_units: int
    evidence: tuple[EvidenceRef, ...]


@dataclass(frozen=True, slots=True)
class PositionActionCosts:
    token_id: int
    actions: tuple[BoundActionCost, ...]
    scope: str = field(default="successful_exclusive_owner_paid_actions_only", init=False)
    execution_eligible: bool = field(default=False, init=False)


def bind_position_action_costs(
    store: EvidenceStore,
    request: CashWindowRequest,
    proofs: tuple[ActionCostProof, ...],
) -> PositionActionCosts:
    """Replay cash first and require cost receipts to equal settlement receipts.

    Merely sharing a pool, block, payer or transaction hash is insufficient. Both
    providers must retain the same full receipt used in exclusive owner settlement,
    including its logs. The payer must be the owner at the action, not the closing
    NFT owner. Require exactly one proof pair per action and charge the transaction
    once. Shared transactions and externally funded actions remain unsupported.
    """
    window = replay_cash_window(store, request)
    payments = window.payments
    txs = tuple(p.entry.transaction_hash for p in payments)
    if len(set(txs)) != len(txs):
        raise ValueError("shared action transactions cannot be charged twice")
    if (
        len(proofs) != len(txs)
        or len({p.transaction_hash for p in proofs}) != len(proofs)
        or {p.transaction_hash for p in proofs} != set(txs)
    ):
        raise ValueError("cost proofs must cover every action exactly once")
    indexed = {p.transaction_hash: p for p in proofs}
    actions = []
    for payment in payments:
        proof = indexed[payment.entry.transaction_hash]
        providers = {r.source for r in payment.evidence}
        if len(proof.evidence) != 2 or {r.source for r in proof.evidence} != providers:
            raise ValueError("action cost provider cohort differs from owner cash")
        owner_refs = {r.source: r for r in payment.evidence}
        costs = []
        for ref in proof.evidence:
            owner_record = store.get_record(owner_refs[ref.source])
            core_ref = TypeAdapter(EvidenceRef).validate_python(
                json.loads(owner_record.payload)["core_evidence"]
            )
            if core_ref.source != ref.source:
                raise ValueError("owner settlement core source differs")
            core_record = store.get_record(core_ref)
            if core_record.request_payload is None:
                raise ValueError("owner settlement is missing raw requests")
            exchanges = restore_exchanges(
                core_record.request_payload, core_record.raw_response_payload
            )
            header = decode_header(
                decode_exchange(exchanges[1])[1]["result"],
                chain_id=payment.entry.block.chain_id,
            )
            if header.ref != payment.entry.block:
                raise ValueError("cost scope differs from the action block")
            capture = replay_transaction_cost(
                store, ref, header=header, transaction_hash=proof.transaction_hash
            )
            receipt = decode_exchange(capture.exchanges[2])[1]["result"]
            if receipt != decode_exchange(exchanges[2])[1]["result"]:
                raise ValueError("cost receipt differs from exclusive settlement receipt")
            cost = capture.reconstruct()
            if cost.payer != payment.owner or cost.status != 1:
                raise ValueError("action gas was not paid by its successful action-time owner")
            costs.append(cost)
        if costs[0] != costs[1]:
            raise ValueError("action cost providers disagree")
        actions.append(
            BoundActionCost(
                payment.entry.block,
                proof.transaction_hash,
                payment.owner,
                costs[0].total_native,
                (*proof.evidence, *payment.evidence),
            )
        )
    return PositionActionCosts(window.ledger.opening.token_id, tuple(actions))
