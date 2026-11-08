"""Offline: bind ONE journal-matched spot order to two retained full-cost proofs.

Given a spot journal, an operator-declared binding (intent id, the receipt block header,
two declared providers and one retained `transaction_cost` evidence reference each) and
a read-only evidence store, this rebuilds each provider's whole native transaction fee
with `farming.transaction_costs.replay_transaction_cost` (raw wire envelopes, pinned
header re-read, Base OP L1 plus versioned operator fee) and accepts it only if:

- the order is settled by the same `reconcile_orders` result report and resume use;
- the fill names its receipt's own block hash (a height alone is never upgraded) and
  the declared header is that exact chain 8453 block;
- the two references are distinct records from the two distinct declared providers;
- both reconstructions agree on every item, and name this tx, this wallet and status 1;
- any single-receipt item already journaled for this fill agrees (else it is a conflict).

The result is per-order native wei evidence only. It is not booked, not converted to
USD, not a funds-settlement proof and not net PnL; nothing here reads RPC, a wallet or
a key, and the journal and evidence store are opened read-only by the caller.
"""

import json
import re
from dataclasses import asdict
from typing import Any

from ..evidence.store import EvidenceStore
from ..farming.transaction_costs import TransactionCost, replay_transaction_cost
from ..models.chain import BlockSnapshot
from ..models.common import BlockRef, EvidenceRef, evidence_ref_from_json
from .receipt_fee_contract import read_fee_record
from .spot_lifecycle import reconcile_orders

SCHEMA = "oasis.spot-cost-binding.v1"
VIEW_SCHEMA = "oasis.spot-cost-replay.v1"
BASE_CHAIN_ID = 8453
_FIELDS = frozenset({"schema", "intent_id", "header", "providers", "costs"})

Record = dict[str, Any]


class _Refused(Exception):
    def __init__(self, status: str, reason: str) -> None:
        super().__init__(reason)
        self.status, self.reason = status, reason


def _unbound(reason: str) -> _Refused:
    return _Refused("unbound", reason)


def _conflict(reason: str) -> _Refused:
    return _Refused("conflict", reason)


def _parse(binding: object) -> tuple[str, BlockSnapshot, tuple[str, str], tuple[EvidenceRef, ...]]:
    if not isinstance(binding, dict) or set(binding) != _FIELDS or binding["schema"] != SCHEMA:
        raise _unbound("binding_malformed")
    key, header, providers, costs = (
        binding["intent_id"],
        binding["header"],
        binding["providers"],
        binding["costs"],
    )
    if not isinstance(key, str) or not key:
        raise _unbound("binding_malformed")
    try:
        if not isinstance(header, dict) or set(header) != {
            "chain_id",
            "number",
            "block_hash",
            "timestamp",
            "parent_hash",
        }:
            raise TypeError("header fields")
        snapshot = BlockSnapshot(
            BlockRef(
                header["chain_id"], header["number"], header["block_hash"], header["timestamp"]
            ),
            header["parent_hash"],
        )
        if not isinstance(costs, list):
            raise TypeError("costs")
        refs = tuple(evidence_ref_from_json(c) for c in costs)
    except (TypeError, ValueError) as error:
        raise _unbound("binding_malformed") from error
    if (
        not isinstance(providers, list)
        or len(providers) != 2
        or not all(isinstance(p, str) and p.strip() for p in providers)
    ):
        raise _unbound("provider_pair_invalid")
    return key, snapshot, (providers[0], providers[1]), refs


def _fill(records: list[Record], key: str) -> Record:
    if key not in set(reconcile_orders(records).settled):
        raise _unbound("order_not_matched")
    (fill,) = [r for r in records if r.get("kind") == "live_fill" and r.get("intent_id") == key]
    return fill


def _bind(records: list[Record], binding: object, store: EvidenceStore) -> Record:
    key, header, providers, refs = _parse(binding)
    fill = _fill(records, key)
    # The same reading the fee report uses: a block pin and a single-receipt item exist
    # only for a record capture could have written (source, status, widths, product).
    reading = read_fee_record(fill.get("receipt_fees"))
    block_hash = reading.block_hash
    if block_hash is None:
        # Legacy height-only fills, unknown sources, not_captured records carrying a
        # hash, and receipts that stated no usable hash.
        raise _unbound("receipt_block_unknown")
    number = fill.get("block_number")
    if (
        header.ref.chain_id != BASE_CHAIN_ID
        or type(number) is not int
        or header.ref.number != number
        or header.ref.block_hash != block_hash
    ):
        # Same height with another hash is another block, never the fill's evidence.
        raise _unbound("block_mismatch")
    if providers[0] == providers[1]:
        raise _unbound("provider_pair_invalid")
    if (
        len(refs) != 2
        or tuple(r.source for r in refs) != providers
        or refs[0].content_hash == refs[1].content_hash
    ):
        raise _unbound("provider_pair_invalid")
    tx = fill.get("tx_hash")
    wallet = fill.get("wallet")
    if not isinstance(tx, str) or not isinstance(wallet, str):
        raise _unbound("order_not_matched")
    costs: list[TransactionCost] = []
    origins: list[object] = []
    for index, ref in enumerate(refs):
        try:
            capture = replay_transaction_cost(store, ref, header=header, transaction_hash=tx)
            costs.append(capture.reconstruct())
            # get_record re-checks the interpretation bytes against the reference's own
            # interpretation_hash, so this origin is exactly what the producer retained.
            origins.append(_retained_origin(store.get_record(ref).interpretation_payload))
        except (KeyError, TypeError, ValueError) as error:
            raise _unbound(f"evidence_invalid_{index}") from error
    origin = _origin(origins, providers)
    if costs[0] != costs[1]:
        raise _conflict("providers_disagree")
    cost = costs[0]
    if (
        cost.chain_id != BASE_CHAIN_ID
        or cost.transaction_hash != tx
        or cost.payer != wallet
        or cost.native_decimals != 18
    ):
        raise _conflict("identity_mismatch")
    if cost.status != 1:
        raise _conflict("status_mismatch")
    # Only a valid observed item is evidence to compare; a record_invalid, missing or
    # invalid item can neither confirm nor contradict the replay.
    for item, replayed in (
        (reading.execution, cost.execution_native),
        (reading.l1, cost.l1_native),
    ):
        if item.state == "observed" and item.wei != replayed:
            raise _conflict("single_receipt_conflict")
    return {
        "status": "replayed",
        "intent_id": key,
        "tx_hash": tx,
        "payer": wallet,
        "block": asdict(header.ref),
        "providers": list(providers),
        # How these proofs were made. Never derived from aliases, distinct reference
        # hashes or equal response bodies: only from the retained, hash-bound origin.
        "origin": origin,
        "evidence": [r.content_hash for r in refs],
        "native_wei": {
            "execution": str(cost.execution_native),
            "l1": str(cost.l1_native),
            "blob": str(cost.blob_native),
            "operator": str(cost.operator_native),
            "total": str(cost.total_native),
        },
    }


FROZEN_ORIGIN = "frozen_input_reconstruction"
FROZEN_INPUT_SCHEMA = "oasis.frozen-rpc.v1"
DIGEST_BASES = frozenset(
    {"sha256_of_exact_file_bytes_parsed", "sha256_of_canonical_json_of_parsed_object"}
)
UNKNOWN_ORIGIN: dict[str, Any] = {"kind": "unknown"}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ABSENT = object()


def _retained_origin(interpretation: bytes | None) -> object:
    if interpretation is None:
        return _ABSENT
    value = json.loads(interpretation)
    if not isinstance(value, dict):
        raise ValueError("cost interpretation is not an object")
    return value.get("origin", _ABSENT)


def _origin(origins: list[object], providers: tuple[str, str]) -> dict[str, Any]:
    """The bundle's stated origin, or `unknown` for historical proofs; never inferred.

    Both proofs must state the same origin, or neither may. A frozen origin is accepted
    only with its exact shape: kind, input schema, a known digest basis and one SHA-256
    per declared provider in declared order. Anything else is refused, never upgraded.
    """
    if all(o is _ABSENT for o in origins):
        return dict(UNKNOWN_ORIGIN)
    first = origins[0]
    if any(o != first for o in origins) or not isinstance(first, dict):
        raise _unbound("origin_invalid")
    inputs = first.get("inputs")
    if (
        set(first) != {"kind", "schema", "digest_basis", "inputs"}
        or first["kind"] != FROZEN_ORIGIN
        or first["schema"] != FROZEN_INPUT_SCHEMA
        or first["digest_basis"] not in DIGEST_BASES
        or not isinstance(inputs, list)
        or len(inputs) != 2
        or any(not isinstance(i, dict) or set(i) != {"source", "sha256"} for i in inputs)
        or [i["source"] for i in inputs] != list(providers)
        or any(
            not isinstance(i["sha256"], str) or not _SHA256.fullmatch(i["sha256"]) for i in inputs
        )
    ):
        raise _unbound("origin_invalid")
    return dict(first)


def bind_order_cost(records: list[Record], binding: object, store: EvidenceStore) -> Record:
    """The per-order replay view; a refusal is a status with a fixed reason, never raised."""
    try:
        result = _bind(records, binding, store)
    except _Refused as refused:
        key = binding.get("intent_id") if isinstance(binding, dict) else None
        result = {
            "status": refused.status,
            "reason": refused.reason,
            "intent_id": key if isinstance(key, str) else None,
            "native_wei": None,
        }
    origin = result.get("origin")
    kind = origin.get("kind") if isinstance(origin, dict) else None
    # The basis says what the replayed bytes ARE. Frozen input is a reconstruction from
    # retained files, never two observed providers, whatever the aliases are called;
    # an unmarked historical proof is replayable but its origin is unknown.
    basis = (
        "frozen_input_reconstruction_not_provider_observation"
        if kind == FROZEN_ORIGIN
        else "two_declared_providers_retained_raw_receipt_origin_unknown"
        if kind == "unknown"
        else "not_replayed"
    )
    return {
        "schema": VIEW_SCHEMA,
        "scope": "this_order_only",
        "basis": basis,
        "provider_independence": "not_established",
        "booked_in_strategy_pnl": False,
        "usd_value": "not_converted",
        "fund_settlement": "unverified_balance_delta_plus_sender_receipt",
        "execution_eligible": False,
        **result,
    }
