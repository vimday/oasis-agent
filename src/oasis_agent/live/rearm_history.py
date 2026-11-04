"""Offline replay of every journaled settlement before an operator rearm."""

import json
from typing import Any

from ..evidence.exact_capture import decode_header
from ..evidence.rpc_validation import decode_exchange, restore_exchanges
from ..evidence.store import EvidenceStore
from ..farming.transaction_costs import replay_transaction_cost
from ..models.common import evidence_ref_from_json
from .action_cash import ActionCashCapture, replay_action_cash
from .approval_cash import ApprovalCashCapture, replay_approval_cash
from .config import LiveConfig
from .config_epochs import retained_configurations
from .intent import SubmittedTransaction
from .revert_cash import RevertCashCapture, replay_revert_cash
from .state import LoopState

ACTIONS = frozenset({"approve_usdc", "approve_permit2", "swap", "mint", "burn", "reverted"})


def replay_history(
    rows: list[tuple[str, bytes]],
    state: LoopState,
    config: LiveConfig,
    evidence: EvidenceStore,
    sources: tuple[str, str],
    *,
    current_nonce: int | None = None,
) -> tuple[dict[str, object], ...]:
    """Rows come only from a fully validated original journal in one read transaction.

    Scan all snapshots, since the state projection's event history is bounded.
    A repeated event must be byte-identical; no transaction may be charged twice.
    Cash records are replayed under their first journaled configuration epoch.
    """
    configurations = retained_configurations(config)
    events: dict[str, tuple[dict[str, Any], LiveConfig]] = {}
    for binding, payload in rows:
        if binding not in configurations:
            raise ValueError("unsupported settlement configuration epoch")
        for event in json.loads(payload)["history"]:
            if not isinstance(event, dict):
                raise ValueError("invalid journaled action history")
            if event.get("event") not in ACTIONS:
                continue
            tx = event.get("tx")
            if not isinstance(tx, str):
                raise ValueError("settlement transaction identity missing")
            if tx in events:
                if events[tx][0] != event:
                    raise ValueError("ambiguous journaled settlement")
            else:
                events[tx] = (event, configurations[binding])
    anchors = state.settled_blocks
    transactions = [a["transaction_hash"] for a in anchors]
    if len(set(transactions)) != len(anchors) or set(events) != set(transactions):
        raise ValueError("settlement history does not exactly cover original anchors")
    total = 0
    next_nonce = None
    receipts: list[dict[str, object]] = []
    for anchor in anchors:
        event, epoch = events[anchor["transaction_hash"]]
        tx = SubmittedTransaction(**event["transaction"])
        if next_nonce is not None and tx.nonce != next_nonce:
            raise ValueError("settled account nonce history is discontinuous")
        if tx.transaction_hash != anchor["transaction_hash"]:
            raise ValueError("settlement transaction commitment differs")
        cost_refs = tuple(evidence_ref_from_json(r) for r in event["cost_evidence"])
        cash_refs = tuple(evidence_ref_from_json(r) for r in event["cash_evidence"])
        if (
            tuple(r.source for r in cost_refs) != sources
            or tuple(r.source for r in cash_refs) != sources
        ):
            raise ValueError("original cost and cash need the declared provider pair")
        pair = []
        wallets = []
        for cost_ref, cash_ref in zip(cost_refs, cash_refs, strict=True):
            cost_record = evidence.get_record(cost_ref)
            if cost_record.request_payload is None:
                raise ValueError("original gas wire proof missing")
            exchanges = restore_exchanges(
                cost_record.request_payload, cost_record.raw_response_payload
            )
            header = decode_header(
                decode_exchange(exchanges[1])[1]["result"], chain_id=epoch.chain_id
            )
            if header.ref.number != anchor["number"] or header.ref.block_hash != anchor["hash"]:
                raise ValueError("cost proof differs from original settlement anchor")
            capture = replay_transaction_cost(
                evidence, cost_ref, header=header, transaction_hash=tx.transaction_hash
            )
            cost = capture.reconstruct()
            receipt = decode_exchange(capture.exchanges[2])[1]["result"]
            if not isinstance(receipt, dict):
                raise ValueError("original receipt object required")
            kind = event["event"]
            proof: RevertCashCapture | ApprovalCashCapture | ActionCashCapture
            if kind == "reverted":
                proof = replay_revert_cash(evidence, cash_ref, epoch)
                wallet = proof.reconstruct(evidence, epoch)
            elif kind in {"approve_usdc", "approve_permit2"}:
                approval = replay_approval_cash(evidence, cash_ref, epoch)
                if approval.kind != kind:
                    raise ValueError("approval action differs")
                wallet = approval.reconstruct(evidence, epoch)[0]
                proof = approval
            else:
                action = replay_action_cash(evidence, cash_ref, epoch)
                if action.kind != kind:
                    raise ValueError("settled action differs")
                wallet = action.reconstruct(evidence, epoch)
                proof = action
            if (
                proof.transaction != tx
                or cost.total_native != event["native_gas_units"]
                or cost.payer != config.account
                or cost.status != int(kind != "reverted")
                or wallet.header != header
            ):
                raise ValueError("cost or cash differs from original transaction")
            pair.append(receipt)
            wallets.append(wallet)
        if pair[0] != pair[1] or wallets[0] != wallets[1]:
            raise ValueError("settlement providers disagree")
        next_nonce = wallets[0].nonce
        total += event["native_gas_units"]
        receipts.append(pair[0])
    if current_nonce is not None and next_nonce is not None and current_nonce != next_nonce:
        raise ValueError("account nonce advanced without a reconciled settlement")
    if total != state.gas_spent_wei:
        raise ValueError("whole native gas history is incomplete")
    return tuple(receipts)
