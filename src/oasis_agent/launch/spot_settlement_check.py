"""Offline: does the wallet's whole-block balance change equal this one spot fill?

Inputs are the order's cost binding (see `spot_cost_binding`), a settlement binding
naming two retained `account_block_settlement` proofs from the same declared provider
pair, and a read-only evidence store. The scope is derived, never declared: the fill's
wallet, its pinned block (number, hash, parent) and the tokens {Base USDC, order token}.

`reconciled` requires, in order:

1. the cost proofs replay and both settlement proofs replay for exactly that scope, from
   the declared providers in order, with the same stated origin as the cost proofs;
2. both providers' settlements are identical (balances, runtimes, supply, every log);
3. the receipt-level Transfer check is `matched` (two receipts, same position, journal);
4. each receipt's Transfer logs of the scoped tokens equal the block's logs of this
   transaction, row for row (a contradiction between proofs is a `conflict`);
5. each token's balance and supply change is fully explained by the block's Transfer
   logs (else `residual`), and no other transaction in the block moved them for this
   wallet (else `unverified`);
6. the wallet's native change is exactly minus this receipt's replayed total fee.

It is not account NAV (other assets are not read), not finality and not a release
condition. Nothing here reads RPC, a wallet or a key; refusals are fixed reasons.
"""

import json
from typing import Any

from ..evidence.store import EvidenceStore
from ..farming.block_settlement import (
    KIND,
    TokenSettlement,
    replay_block_settlement,
    transaction_settlement,
)
from ..farming.owner_payments import TRANSFER, _log
from ..models.chain import BlockSnapshot, LogRecord
from ..models.common import BlockRef, EvidenceRef, evidence_ref_from_json, normalize_address
from .onchainos_source import BASE_USDC
from .spot_cost_binding import UNKNOWN_ORIGIN, bind_order_cost
from .spot_transfer_check import _journal_int, _receipt_result, check_order_transfers

SCHEMA = "oasis.spot-settlement-binding.v1"
VIEW_SCHEMA = "oasis.spot-block-settlement.v1"
_FIELDS = frozenset({"schema", "intent_id", "providers", "settlements"})
Record = dict[str, Any]


class _Refused(Exception):
    def __init__(self, status: str, reason: str) -> None:
        super().__init__(reason)
        self.status, self.reason = status, reason


def settlement_scope(fill: Record) -> tuple[str, tuple[str, ...]]:
    """The wallet and sorted token set one spot fill's block settlement must cover.

    The producer and the check share this one definition, so a bundle can never be
    captured for a scope the check would not ask for.
    """
    wallet, token = normalize_address(fill["wallet"]), normalize_address(fill["token"])
    if token == BASE_USDC:
        raise ValueError("order token is the settlement asset")
    return wallet, tuple(sorted({BASE_USDC, token}))


def _parse(binding: object, *, intent_id: str, providers: list[str]) -> tuple[EvidenceRef, ...]:
    """Two settlement references from the cost proofs' own declared providers, in order."""
    if not isinstance(binding, dict) or set(binding) != _FIELDS or binding["schema"] != SCHEMA:
        raise _Refused("unbound", "settlement_binding_malformed")
    if binding["intent_id"] != intent_id or binding["providers"] != providers:
        raise _Refused("unbound", "settlement_binding_scope_mismatch")
    refs = binding["settlements"]
    if not isinstance(refs, list) or len(refs) != 2:
        raise _Refused("unbound", "settlement_binding_malformed")
    try:
        parsed = tuple(evidence_ref_from_json(ref) for ref in refs)
    except (TypeError, ValueError) as error:
        raise _Refused("unbound", "settlement_binding_malformed") from error
    if (
        [ref.source for ref in parsed] != providers
        or any(ref.kind != KIND for ref in parsed)
        or parsed[0].content_hash == parsed[1].content_hash
    ):
        raise _Refused("unbound", "settlement_provider_pair_invalid")
    return parsed


_ABSENT = object()


def _origin(store: EvidenceStore, ref: EvidenceRef) -> object:
    """The origin a settlement proof retained in its hash-bound interpretation, or _ABSENT."""
    interpretation = store.get_record(ref).interpretation_payload
    if interpretation is None:
        raise ValueError("settlement interpretation missing")
    value = json.loads(interpretation)
    if not isinstance(value, dict):
        raise ValueError("settlement interpretation is not an object")
    return value.get("origin", _ABSENT)


def _same_origin(settlement: object, cost: object) -> bool:
    """One bundle, one origin. Cost proofs that state none accept only settlements that
    state none; an explicit `unknown` or any other value is never treated as absent."""
    if cost == UNKNOWN_ORIGIN:
        return settlement is _ABSENT
    return settlement is not _ABSENT and settlement == cost


def _receipt_logs(receipt: object, tokens: frozenset[str]) -> tuple[LogRecord, ...]:
    """This receipt's Transfer-topic logs of the scoped tokens, in receipt order."""
    if not isinstance(receipt, dict) or not isinstance(receipt.get("logs"), list):
        raise ValueError("receipt logs missing")
    logs = tuple(_log(raw) for raw in receipt["logs"])
    scoped = [log for log in logs if log.address in tokens and log.topics[:1] == (TRANSFER,)]
    # Rows, not listing order: the receipt check already refused duplicate log indices.
    return tuple(sorted(scoped, key=lambda log: log.log_index))


def _token_json(row: TokenSettlement, role: str) -> Record:
    return {
        "role": role,
        "token": row.token,
        "runtime_hash": row.runtime_hash,
        "decimals": row.decimals,
        **{
            name: str(getattr(row, name))
            for name in (
                "balance_before",
                "balance_after",
                "balance_delta",
                "block_transfer_net",
                "residual",
                "supply_delta",
                "block_mint_minus_burn",
                "supply_residual",
                "transaction_net",
                "other_transaction_net",
            )
        },
        "transaction_logs": row.transaction_logs,
        "other_transaction_logs": row.other_transaction_logs,
    }


def _check(
    records: list[Record], cost_binding: Any, binding: object, store: EvidenceStore
) -> Record:
    cost = bind_order_cost(records, cost_binding, store)
    if cost["status"] != "replayed":
        raise _Refused("unbound", "cost_binding_" + str(cost.get("reason")))
    key, tx, providers = cost["intent_id"], cost["tx_hash"], cost["providers"]
    refs = _parse(binding, intent_id=key, providers=providers)
    (fill,) = [r for r in records if r.get("kind") == "live_fill" and r.get("intent_id") == key]
    try:
        wallet, tokens = settlement_scope(fill)
    except (KeyError, TypeError, ValueError) as error:
        raise _Refused("unbound", "settlement_scope_invalid") from error
    block = cost["block"]
    header = BlockSnapshot(
        BlockRef(block["chain_id"], block["number"], block["block_hash"], block["timestamp"]),
        cost_binding["header"]["parent_hash"],
    )
    try:
        captures = [
            replay_block_settlement(store, ref, header=header, account=wallet, tokens=tokens)
            for ref in refs
        ]
        origins = [_origin(store, ref) for ref in refs]
    except (KeyError, TypeError, ValueError) as error:
        raise _Refused("unbound", "settlement_evidence_invalid") from error
    if not all(_same_origin(origin, cost["origin"]) for origin in origins):
        # One bundle, one origin: a settlement made from other inputs is not this bundle's.
        raise _Refused("unbound", "settlement_origin_differs_from_cost")
    values = [capture.reconstruct() for capture in captures]
    if values[0] != values[1]:
        raise _Refused("conflict", "providers_disagree_on_block_settlement")
    value = values[0]
    transfers = check_order_transfers(records, cost_binding, store)
    if transfers["status"] != "matched":
        raise _Refused(transfers["status"], "receipt_transfers_" + str(transfers["reason"]))
    in_block = tuple(log for log in value.logs if log.transaction_hash == tx)
    for ref in cost_binding["costs"]:
        if _receipt_logs(_receipt_result(store, ref, header, tx), frozenset(tokens)) != in_block:
            # The receipt and the block's own log list must describe the same rows.
            raise _Refused("conflict", "receipt_and_block_transfer_logs_differ")
    view = transaction_settlement(value, tx)
    rows = {row.token: row for row in view.tokens}
    token = normalize_address(fill["token"])
    usdc, units = rows[BASE_USDC], rows[token]
    fee = int(cost["native_wei"]["total"])
    native_residual = view.native_delta + fee
    usdc_journal = _journal_int(fill.get("usdc_delta"))
    token_journal = _journal_int(fill.get("token_delta"))
    if usdc_journal is None or token_journal is None:
        raise _Refused("unverified", "journal_delta_invalid")
    journal = (usdc_journal, token_journal)
    if any(row.residual or row.supply_residual for row in view.tokens):
        status, reason = "residual", "token_balance_or_supply_not_explained_by_transfers"
    elif native_residual:
        status, reason = "residual", "native_balance_not_explained_by_receipt_fee"
    elif any(row.other_transaction_logs for row in view.tokens):
        # By touch, not by net: +5 and -5 from other transactions still leave the block's
        # balance change attributable to more than this fill.
        status, reason = "unverified", "other_transactions_moved_wallet_in_block"
    elif (usdc.balance_delta, units.balance_delta) != journal:
        # Implied by the checks above (matched receipt nets equal the journal, the receipt
        # rows are the block's rows, no residual, no other transaction); kept as a guard.
        status, reason = "journal_mismatch", "block_balance_delta_differs_from_journal"
    else:
        status, reason = "reconciled", None
    return {
        "status": status,
        "reason": reason,
        "intent_id": key,
        "tx_hash": tx,
        "wallet": wallet,
        "block": {**block, "parent_hash": header.parent_hash},
        "providers": providers,
        "origin": cost["origin"],
        "transaction_index": view.transaction_index,
        "block_transfer_logs": len(value.logs),
        "transaction_transfer_logs": len(in_block),
        "native_wei": {
            "before": str(view.native_before),
            "after": str(view.native_after),
            "delta": str(view.native_delta),
            "receipt_fee_total": str(fee),
            "residual": str(native_residual),
        },
        "tokens": {"usdc": _token_json(usdc, "settlement"), "token": _token_json(units, "order")},
        "journal": {"usdc_delta": str(journal[0]), "token_delta": str(journal[1])},
    }


def check_block_settlement(
    records: list[Record], cost_binding: object, binding: object, store: EvidenceStore
) -> Record:
    """The whole-block settlement view for one bound order; never raises."""
    try:
        result = _check(records, cost_binding, binding, store)
    except _Refused as refused:
        result = {"status": refused.status, "reason": refused.reason, "tokens": None}
    except KeyError, TypeError, ValueError:
        result = {"status": "unverified", "reason": "settlement_unreadable", "tokens": None}
    key = cost_binding.get("intent_id") if isinstance(cost_binding, dict) else None
    return {
        "schema": VIEW_SCHEMA,
        "scope": "one_wallet_native_usdc_and_order_token_across_the_fill_block",
        "unit": "base_units",
        "account_nav": "not_proven",
        "finality": "not_proven",
        "provider_independence": "not_established",
        "booked_in_strategy_pnl": False,
        "execution_eligible": False,
        "intent_id": key if isinstance(key, str) else None,
        **result,
    }
