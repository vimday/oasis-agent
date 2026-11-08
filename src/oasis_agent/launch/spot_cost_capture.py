"""Produce one replayable cost bundle for one journal-matched spot fill (offline).

From one hash-verified journal snapshot, an operator names ONE intent. This module:

1. selects that intent only if `reconcile_orders` settles it and its fill carries a
   receipt block hash that `read_fee_record` accepts (the same reading report and
   replay use), so a legacy height-only fill or an unprovable record never binds;
2. runs the existing `collect_receipt_cost_quorum` over two declared provider sessions
   (raw request sequence, pinned header read twice, Base OP L1 and operator fee);
3. refuses before retaining anything unless the collector's pinned block is exactly the
   fill's own chain 8453 height and hash, and the cost names this tx, this wallet and
   status 1 (the collector's own probe is never trusted as the fill's block);
4. persists both proofs into a fresh store inside a private staging directory, builds
   the v1 binding, re-verifies it with `bind_order_cost` against a read-only reopen of
   that store, and only then publishes the directory with one atomic rename.

A failure at any step publishes nothing: the staging directory (created here, holding
only this run's own outputs) is removed, so no half bundle or orphan proof survives to
be counted. The destination must not exist, so a repeated run can never add a second
copy of the same fee. Sessions are injected; `FrozenRpc` answers from retained
responses only, so nothing here opens a network connection, a wallet or a key.
"""

import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ..errors import RpcError
from ..evidence.exact_capture import RecordedSession
from ..evidence.rpc_capture import BoundedExchangeRecorder
from ..evidence.store import EvidenceStore, ReadOnlyEvidenceStore
from ..execution.account_capture import SessionFactory
from ..execution.lp_receipts import collect_receipt_cost_quorum
from ..farming.block_settlement import (
    BlockSettlementCapture,
    collect_block_settlement_quorum,
    persist_block_settlement,
)
from ..farming.transaction_costs import TransactionCostCapture, persist_transaction_cost
from ..models.chain import BlockSnapshot
from ..models.common import canonical_json
from ..rpc.transport import JsonRpcTransport
from .receipt_fee_contract import read_fee_record
from .spot_cost_binding import BASE_CHAIN_ID, SCHEMA, bind_order_cost
from .spot_lifecycle import reconcile_orders
from .spot_settlement_check import SCHEMA as SETTLEMENT_SCHEMA
from .spot_settlement_check import check_block_settlement, settlement_scope

FROZEN_SCHEMA = "oasis.frozen-rpc.v1"
Record = dict[str, Any]


class CostCaptureRefused(Exception):
    """Nothing was published; `category` is a fixed reason, never provider text."""

    def __init__(self, category: str) -> None:
        super().__init__(f"spot cost capture refused: {category}")
        self.category = category


class FrozenRpc:
    """An HTTP poster that answers JSON-RPC from retained (method, params) -> result.

    Every request must match one retained entry exactly; anything else raises, so a
    frozen file can never be silently topped up by a live call.
    """

    def __init__(self, frozen: object, *, source: str, chain_id: int) -> None:
        if (
            not isinstance(frozen, dict)
            or set(frozen) != {"schema", "source", "chain_id", "responses"}
            or frozen["schema"] != FROZEN_SCHEMA
            or frozen["source"] != source
            or frozen["chain_id"] != chain_id
            or type(frozen["chain_id"]) is not int
            or not isinstance(frozen["responses"], list)
        ):
            raise CostCaptureRefused("frozen_responses_invalid")
        self._answers: dict[bytes, object] = {}
        for entry in frozen["responses"]:
            if (
                not isinstance(entry, dict)
                or set(entry) != {"method", "params", "result"}
                or not isinstance(entry["method"], str)
                or not isinstance(entry["params"], list)
            ):
                raise CostCaptureRefused("frozen_responses_invalid")
            key = canonical_json([entry["method"], entry["params"]])
            if key in self._answers:
                raise CostCaptureRefused("frozen_responses_invalid")
            self._answers[key] = entry["result"]

    def post(self, payload: bytes) -> bytes:
        request = json.loads(payload)
        key = canonical_json([request.get("method"), request.get("params")])
        if key not in self._answers:
            raise ValueError("request not in the frozen responses")
        return canonical_json({"jsonrpc": "2.0", "id": request["id"], "result": self._answers[key]})


FROZEN_ORIGIN_KIND = "frozen_input_reconstruction"
DIGEST_FILE_BYTES = "sha256_of_exact_file_bytes_parsed"
DIGEST_CANONICAL = "sha256_of_canonical_json_of_parsed_object"


def frozen_origin(
    sources: tuple[str, str], digests: tuple[str, str], digest_basis: str
) -> dict[str, object]:
    """What a cost proof built from frozen responses was built from (not an observation).

    Kept inside each proof's hash-bound interpretation: the same object on both proofs,
    inputs listed in declared provider order.
    """
    return {
        "kind": FROZEN_ORIGIN_KIND,
        "schema": FROZEN_SCHEMA,
        "digest_basis": digest_basis,
        "inputs": [
            {"source": source, "sha256": digest}
            for source, digest in zip(sources, digests, strict=True)
        ],
    }


def load_frozen(raw: bytes) -> tuple[object, str]:
    """Parse one frozen file and return it with the SHA-256 of exactly those bytes."""
    try:
        return json.loads(raw), hashlib.sha256(raw).hexdigest()
    except (ValueError, RecursionError) as error:
        raise CostCaptureRefused("frozen_responses_invalid") from error


def canonical_digest(frozen: object) -> str:
    return hashlib.sha256(canonical_json(frozen)).hexdigest()


def frozen_sessions(frozen: Mapping[str, object], *, chain_id: int) -> SessionFactory:
    """One fresh transport and recorder per call, answered only from `frozen[source]`."""
    posters = {
        source: FrozenRpc(f, source=source, chain_id=chain_id) for source, f in frozen.items()
    }

    def build(source: str) -> RecordedSession:
        if source not in posters:
            raise CostCaptureRefused("provider_not_declared")
        recorder = BoundedExchangeRecorder()
        transport = JsonRpcTransport(f"frozen://{source}", http=posters[source], recorder=recorder)
        return transport, recorder, source

    return build


def _matched_fill(records: Sequence[Record], intent_id: str) -> tuple[Record, str]:
    if not isinstance(intent_id, str) or intent_id not in set(
        reconcile_orders(list(records)).settled
    ):
        raise CostCaptureRefused("order_not_matched")
    (fill,) = [
        r for r in records if r.get("kind") == "live_fill" and r.get("intent_id") == intent_id
    ]
    block_hash = read_fee_record(fill.get("receipt_fees")).block_hash
    if block_hash is None:
        raise CostCaptureRefused("receipt_block_unknown")
    if type(fill.get("block_number")) is not int:
        raise CostCaptureRefused("receipt_block_unknown")
    return fill, block_hash


def collect_order_cost(
    records: Sequence[Record],
    intent_id: str,
    *,
    sources: tuple[str, str],
    sessions: SessionFactory,
) -> tuple[BlockSnapshot, tuple[TransactionCostCapture, TransactionCostCapture]]:
    """Two agreeing proofs of this fill's own receipt, checked against its journal pin."""
    fill, block_hash = _matched_fill(records, intent_id)
    tx, wallet = fill.get("tx_hash"), fill.get("wallet")
    if not isinstance(tx, str) or not isinstance(wallet, str):
        raise CostCaptureRefused("order_not_matched")
    try:
        captures = collect_receipt_cost_quorum(
            chain_id=BASE_CHAIN_ID, transaction_hash=tx, sources=sources, sessions=sessions
        )
    except CostCaptureRefused:
        raise
    except (RpcError, ValueError, TypeError, KeyError, OSError) as error:
        # Includes provider disagreement, a moved header and a request the frozen file
        # cannot answer; the detail is provider text and is not echoed.
        raise CostCaptureRefused("quorum_refused") from error
    header = captures[0].header
    if (
        header.ref.chain_id != BASE_CHAIN_ID
        or header.ref.number != fill["block_number"]
        or header.ref.block_hash != block_hash
    ):
        # The collector's probe located some block; only the fill's own block counts.
        raise CostCaptureRefused("block_mismatch")
    cost = captures[0].reconstruct()
    if cost.transaction_hash != tx or cost.payer != wallet:
        raise CostCaptureRefused("identity_mismatch")
    if cost.status != 1:
        raise CostCaptureRefused("status_mismatch")
    return header, captures


def collect_order_settlement(
    records: Sequence[Record],
    intent_id: str,
    header: BlockSnapshot,
    *,
    sources: tuple[str, str],
    sessions: SessionFactory,
) -> tuple[BlockSettlementCapture, BlockSettlementCapture]:
    """Two agreeing whole-block settlement proofs of this fill's wallet at its own block.

    The scope (wallet, USDC and the order token) comes from `settlement_scope`, the same
    definition the check uses, and the block is the cost proofs' own pinned header.
    """
    fill, block_hash = _matched_fill(records, intent_id)
    if (
        header.ref.chain_id != BASE_CHAIN_ID
        or header.ref.number != fill["block_number"]
        or header.ref.block_hash != block_hash
    ):
        raise CostCaptureRefused("block_mismatch")
    try:
        wallet, tokens = settlement_scope(fill)
    except (KeyError, TypeError, ValueError) as error:
        raise CostCaptureRefused("settlement_scope_invalid") from error
    try:
        return collect_block_settlement_quorum(
            header, wallet, tokens, sources=sources, sessions=sessions
        )
    except CostCaptureRefused:
        raise
    except (RpcError, ValueError, TypeError, KeyError, OSError) as error:
        # A disagreement, a moved header or a request the frozen file cannot answer.
        raise CostCaptureRefused("settlement_quorum_refused") from error


def _header_json(header: BlockSnapshot) -> dict[str, Any]:
    ref = header.ref
    return {
        "chain_id": ref.chain_id,
        "number": ref.number,
        "block_hash": ref.block_hash,
        "timestamp": ref.timestamp,
        "parent_hash": header.parent_hash,
    }


Settlements = tuple[BlockSettlementCapture, BlockSettlementCapture]
# Read-back outcomes that mean the written proofs are not what this run collected: every
# `unbound` reason, an unreadable proof, and a provider disagreement the quorum already
# excluded. A residual, another transaction in the block, or receipts that contradict the
# block's own log list (two different RPC answers) are observations and are published.
_NOT_THIS_BUNDLE = frozenset({"settlement_unreadable", "providers_disagree_on_block_settlement"})


def write_cost_bundle(
    out: Path,
    records: Sequence[Record],
    intent_id: str,
    header: BlockSnapshot,
    captures: tuple[TransactionCostCapture, TransactionCostCapture],
    *,
    origin: dict[str, object],
) -> dict[str, Any]:
    """Publish `out/` = {evidence.db, binding.json} atomically, or nothing at all.

    `origin` (see `frozen_origin`) is retained inside both proofs' interpretations, so
    the bundle states its own origin wherever it is copied; it is never inferred.
    """
    return _publish(out, records, intent_id, header, captures, None, origin)[0]


def write_settled_bundle(
    out: Path,
    records: Sequence[Record],
    intent_id: str,
    header: BlockSnapshot,
    captures: tuple[TransactionCostCapture, TransactionCostCapture],
    settlements: Settlements,
    *,
    origin: dict[str, object],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """As `write_cost_bundle`, plus both block settlement proofs and `settlement.json`.

    Published only when the settlement proofs bind and replay against this bundle's own
    cost proofs (same scope, providers and origin). Any other settlement status is an
    observation of the inputs (`residual`, `unverified`, `journal_mismatch`, or receipts
    that contradict the block's own log list): it is retained and returned, never
    discarded, and only the report's strict flag turns it into a refusal.
    """
    cost, settled = _publish(out, records, intent_id, header, captures, settlements, origin)
    if settled is None:  # unreachable: _publish checks whenever it is given proofs
        raise CostCaptureRefused("verification_settlement_missing")
    return cost, settled


def _publish(
    out: Path,
    records: Sequence[Record],
    intent_id: str,
    header: BlockSnapshot,
    captures: tuple[TransactionCostCapture, TransactionCostCapture],
    settlements: Settlements | None,
    origin: dict[str, object],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    pair = [captures[0].source, captures[1].source]
    inputs = origin.get("inputs")
    if not isinstance(inputs, list) or [i.get("source") for i in inputs] != pair:
        raise CostCaptureRefused("origin_sources_mismatch")
    if settlements is not None and [s.source for s in settlements] != pair:
        raise CostCaptureRefused("settlement_sources_mismatch")
    if out.exists() or out.is_symlink():
        raise CostCaptureRefused("output_exists")
    if not out.parent.is_dir():
        raise CostCaptureRefused("output_parent_missing")
    staging = Path(tempfile.mkdtemp(prefix=".spot-cost-", dir=out.parent))
    try:
        store = EvidenceStore(staging / "evidence.db")
        try:
            refs = [persist_transaction_cost(store, capture, origin=origin) for capture in captures]
            proofs = [
                persist_block_settlement(store, capture, origin=origin)
                for capture in settlements or ()
            ]
        finally:
            store.close()
        binding = {
            "schema": SCHEMA,
            "intent_id": intent_id,
            "header": _header_json(header),
            "providers": pair,
            "costs": [asdict(ref) for ref in refs],
        }
        declared = {
            "schema": SETTLEMENT_SCHEMA,
            "intent_id": intent_id,
            "providers": pair,
            "settlements": [asdict(ref) for ref in proofs],
        }
        reader = ReadOnlyEvidenceStore(staging / "evidence.db")
        try:
            view = bind_order_cost(list(records), binding, reader)
            if view["status"] != "replayed":
                raise CostCaptureRefused("verification_" + str(view.get("reason")))
            if view.get("origin") != origin:
                # The published bundle must read back as exactly what produced it.
                raise CostCaptureRefused("verification_origin")
            settled = (
                None
                if settlements is None
                else check_block_settlement(list(records), binding, declared, reader)
            )
        finally:
            reader.close()
        if settled is not None and (
            settled["status"] == "unbound" or settled["reason"] in _NOT_THIS_BUNDLE
        ):
            # The proofs do not read back as this bundle's own: never publish them.
            raise CostCaptureRefused("verification_settlement_" + str(settled.get("reason")))
        with (staging / "binding.json").open("xb") as handle:
            handle.write(canonical_json(binding) + b"\n")
        if settlements is not None:
            with (staging / "settlement.json").open("xb") as handle:
                handle.write(canonical_json(declared) + b"\n")
        os.rename(staging, out)  # one step: the directory appears complete or not at all
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return view, settled
