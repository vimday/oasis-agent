"""Offline: the wallet's USDC and token Transfer nets in one bound spot fill's receipts.

Input is a binding that `bind_order_cost` has already replayed (same journal snapshot,
same retained two-provider receipt proofs, same tx, wallet, block pin and status 1).
From each provider's retained `eth_getTransactionReceipt` result this reads every log
and keeps only canonical ERC-20 `Transfer` events emitted by Base USDC or by this
order's token that move value from or to this wallet. Each kept log must name this
transaction and the pinned block, be not removed, and have a unique log index; the
two providers must agree on the exact set of kept logs (identity and amount), not only
on the final net. The signed per-asset nets in integer base units are then compared
with the journal fill's `usdc_delta` / `token_delta`.

Scope is what this one receipt shows. It is not a whole-block or account balance
proof, not proof of funds settlement, not fees, USD or PnL, and a non-standard token
(fee-on-transfer, rebasing, missing events) can make a matching receipt incomplete.
Nothing here reads RPC, a wallet or a key; refusals are statuses with fixed reasons.
"""

import re
from typing import Any

from eth_abi.exceptions import DecodingError

from ..evidence.rpc_validation import decode_exchange
from ..evidence.store import EvidenceStore
from ..farming.owner_payments import TRANSFER, _log, _transfer
from ..farming.transaction_costs import replay_transaction_cost
from ..models.chain import BlockSnapshot
from ..models.common import BlockRef, evidence_ref_from_json, normalize_address
from .onchainos_source import BASE_USDC
from .spot_cost_binding import bind_order_cost

VIEW_SCHEMA = "oasis.spot-transfer-check.v1"
Record = dict[str, Any]
Key = tuple[str, int]  # (emitting token, log index)


class _Refused(Exception):
    def __init__(self, status: str, reason: str) -> None:
        super().__init__(reason)
        self.status, self.reason = status, reason


def _scoped_logs(
    receipt: object, *, tx: str, header: BlockSnapshot, wallet: str, assets: frozenset[str]
) -> dict[Key, tuple[str, str, int]]:
    """In-scope Transfer logs of one receipt, keyed by (token, log index)."""
    if not isinstance(receipt, dict) or not isinstance(receipt.get("logs"), list):
        raise _Refused("unverified", "receipt_logs_missing")
    out: dict[Key, tuple[str, str, int]] = {}
    indices: set[int] = set()
    for raw in receipt["logs"]:
        try:
            log = _log(raw)  # canonical quantities, hashes; refuses removed=true
        except (TypeError, ValueError) as error:
            raise _Refused("unverified", "log_malformed") from error
        if log.log_index in indices:
            raise _Refused("unverified", "duplicate_log_index")
        indices.add(log.log_index)
        if (
            log.transaction_hash != tx
            or log.block_number != header.ref.number
            or log.block_hash != header.ref.block_hash
        ):
            raise _Refused("unverified", "log_identity_mismatch")
        if log.address not in assets or not log.topics or log.topics[0] != TRANSFER:
            continue  # other contracts or events: never counted
        try:
            token, sender, recipient, amount = _transfer(log)
        except (TypeError, ValueError, DecodingError) as error:
            # eth_abi's DecodingError (short/garbled data) is not a ValueError.
            raise _Refused("unverified", "transfer_abi_invalid") from error
        sender, recipient = normalize_address(sender), normalize_address(recipient)
        if wallet not in (sender, recipient):
            continue  # moves value between other parties: never counted
        out[(token, log.log_index)] = (sender, recipient, amount)
    return out


def _receipt_result(store: EvidenceStore, ref: object, header: BlockSnapshot, tx: str) -> object:
    capture = replay_transaction_cost(
        store, evidence_ref_from_json(ref), header=header, transaction_hash=tx
    )
    return decode_exchange(capture.exchanges[2])[1].get("result")


def _nets(logs: dict[Key, tuple[str, str, int]], wallet: str, asset: str) -> dict[str, int]:
    received = sum(a for (t, _), (_, r, a) in logs.items() if t == asset and r == wallet)
    sent = sum(a for (t, _), (s, _, a) in logs.items() if t == asset and s == wallet)
    return {"received": received, "sent": sent, "net": received - sent}


_SIGNED = re.compile(r"^-?(0|[1-9][0-9]{0,77})$")


def _journal_int(value: object) -> int | None:
    """The journal's signed decimal string (Fill.record), ASCII canonical only."""
    return int(value) if isinstance(value, str) and _SIGNED.fullmatch(value) else None


def _check(records: list[Record], binding: Any, store: EvidenceStore) -> Record:
    replay = bind_order_cost(records, binding, store)
    if replay["status"] != "replayed":
        raise _Refused("unbound", "cost_binding_" + str(replay.get("reason")))
    (fill,) = [
        r
        for r in records
        if r.get("kind") == "live_fill" and r.get("intent_id") == replay["intent_id"]
    ]
    wallet, token, tx = replay["payer"], normalize_address(fill["token"]), replay["tx_hash"]
    block = replay["block"]
    header = BlockSnapshot(
        BlockRef(block["chain_id"], block["number"], block["block_hash"], block["timestamp"]),
        binding["header"]["parent_hash"],
    )
    if token == BASE_USDC:
        raise _Refused("unverified", "token_is_settlement")
    assets = frozenset({BASE_USDC, token})
    per_source = []
    for ref in binding["costs"]:
        receipt = _receipt_result(store, ref, header, tx)
        per_source.append(_scoped_logs(receipt, tx=tx, header=header, wallet=wallet, assets=assets))
    if per_source[0] != per_source[1]:
        # Same net is not enough: the exact in-scope log set must be identical.
        raise _Refused("conflict", "providers_disagree_on_transfer_logs")
    logs = per_source[0]
    usdc, units = _nets(logs, wallet, BASE_USDC), _nets(logs, wallet, token)
    journal = {
        "usdc": _journal_int(fill.get("usdc_delta")),
        "token": _journal_int(fill.get("token_delta")),
    }
    if journal["usdc"] is None or journal["token"] is None:
        raise _Refused("unverified", "journal_delta_invalid")
    if not logs:
        status, reason = "unverified", "no_in_scope_transfer_logs"
    elif (usdc["net"], units["net"]) == (journal["usdc"], journal["token"]):
        status, reason = "matched", None
    else:
        status, reason = "journal_mismatch", "journal_delta_differs_from_receipt_transfers"
    return {
        "status": status,
        "reason": reason,
        "intent_id": replay["intent_id"],
        "tx_hash": tx,
        "wallet": wallet,
        "block": block,
        "providers": replay["providers"],
        "origin": replay["origin"],
        "logs_in_scope": len(logs),
        "assets": {
            "usdc": {"token": BASE_USDC, **{k: str(v) for k, v in usdc.items()}},
            "token": {"token": token, **{k: str(v) for k, v in units.items()}},
        },
        "journal": {"usdc_delta": str(journal["usdc"]), "token_delta": str(journal["token"])},
    }


def check_order_transfers(records: list[Record], binding: object, store: EvidenceStore) -> Record:
    """Receipt Transfer nets versus the journal fill for one bound order; never raises."""
    try:
        result = _check(records, binding, store)
    except _Refused as refused:
        key = binding.get("intent_id") if isinstance(binding, dict) else None
        result = {
            "status": refused.status,
            "reason": refused.reason,
            "intent_id": key if isinstance(key, str) else None,
            "assets": None,
        }
    except (KeyError, TypeError, ValueError):
        key = binding.get("intent_id") if isinstance(binding, dict) else None
        result = {
            "status": "unverified",
            "reason": "receipt_unreadable",
            "intent_id": key if isinstance(key, str) else None,
            "assets": None,
        }
    return {
        "schema": VIEW_SCHEMA,
        "scope": "this_receipt_erc20_transfers_of_usdc_and_order_token_touching_wallet",
        "unit": "token_base_units",
        "whole_block_balance": "not_proven",
        "fund_settlement": "not_proven",
        "non_standard_token_behaviour": "not_detected_here",
        "booked_in_strategy_pnl": False,
        "execution_eligible": False,
        **result,
    }
