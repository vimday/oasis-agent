"""Reconstruct retained native/ERC-20 owner cash independently from two RPC sources."""

import json
from dataclasses import asdict, dataclass

from pydantic import TypeAdapter

from ..evidence.rpc_validation import restore_exchanges
from ..evidence.store import EvidenceStore
from ..models.chain import BlockSnapshot
from ..models.common import ZERO_ADDRESS, EvidenceRef, canonical_json, normalize_address
from ..models.finance import AssetPair
from ..models.v4 import PoolKey
from .native_payments import (
    NativeOwnerPaymentCapture,
    native_owner_payment_payload,
    resolve_exclusive_native_payment,
)
from .owner_payments import (
    OwnerBalanceCapture,
    owner_payment_payload,
    resolve_exclusive_owner_payment,
)
from .position_ledger import PositionLedgerEntry
from .position_settlement import replay_position_settlement_evidence


@dataclass(frozen=True, slots=True)
class OwnerCashQuorum:
    owner: str
    entry: PositionLedgerEntry
    amounts: AssetPair
    evidence: tuple[EvidenceRef, EvidenceRef]

    @property
    def payload(self) -> bytes:
        return canonical_json(
            {
                "schema": "oasis.owner-cash-quorum.v1",
                **asdict(self),
                "execution_eligible": False,
                "unresolved": ["token_runtime_attestation", "finality", "all_cost_pnl"],
            }
        )


def replay_owner_cash_quorum(
    store: EvidenceStore,
    references: tuple[EvidenceRef, EvidenceRef],
    *,
    header: BlockSnapshot,
    pool_manager: str,
    pool_key: PoolKey,
    entry: PositionLedgerEntry,
    owner: str,
) -> OwnerCashQuorum:
    """Require the same explicit unresolved event scope; replay both raw witnesses.

    Uses existing exclusive native/ERC-20 resolvers, including native gas separation.
    Batched, ambiguous and nonstandard transfers remain unsupported, never netted.
    """
    if len(references) != 2 or len({ref.source for ref in references}) != 2:
        raise ValueError("owner cash requires two distinct providers")
    native = ZERO_ADDRESS in (pool_key.currency0, pool_key.currency1)
    expected_kind = "native_owner_settlement" if native else "position_owner_settlement"
    values: list[AssetPair] = []
    core_values = []
    for ref in references:
        if ref.kind != expected_kind or ref.schema_version != 1 or ref.block != header.ref:
            raise ValueError("owner payment evidence kind or snapshot mismatch")
        record = store.get_record(ref)
        if record.request_payload is None:
            raise ValueError("owner payment raw requests missing")
        data = json.loads(record.payload)
        core_ref = TypeAdapter(EvidenceRef).validate_python(data["core_evidence"])
        if core_ref.source != ref.source:
            raise ValueError("owner and core provider mismatch")
        core = replay_position_settlement_evidence(
            store,
            core_ref,
            header=header,
            pool_manager=pool_manager,
            pool_key=pool_key,
            entry=entry,
        )
        exchanges = restore_exchanges(record.request_payload, record.raw_response_payload)
        if native:
            native_capture = NativeOwnerPaymentCapture(core, owner, ref.source, exchanges)
            amounts = resolve_exclusive_native_payment(native_capture)
            expected = native_owner_payment_payload(native_capture, amounts, core_ref)
        else:
            capture = OwnerBalanceCapture(core, owner, ref.source, exchanges)
            amounts = resolve_exclusive_owner_payment(capture)
            expected = owner_payment_payload(capture, amounts, core_ref)
        if expected != record.payload:
            raise ValueError("owner cash interpretation differs from raw evidence")
        values.append(amounts)
        core_values.append(core.reconstruct())
    if values[0] != values[1] or core_values[0] != core_values[1]:
        raise ValueError("owner cash or core settlement providers disagree")
    return OwnerCashQuorum(normalize_address(owner), entry, values[0], references)
