"""Join raw snapshot captures with the complete pool/NFT event replay."""

from dataclasses import asdict, dataclass, replace

from ..evidence.ingestion import IngestionTrust, VerifiedBlockRange
from ..evidence.range_validation import validate_block_range_exchanges
from ..evidence.rpc_validation import restore_exchanges
from ..evidence.store import EvidenceStore
from ..models.common import EvidenceRef, canonical_json, normalize_address
from .owner_payments import (
    OwnerBalanceCapture,
    persist_owner_payment,
    resolve_exclusive_owner_payment,
)
from .position_capture import PositionSnapshotCapture, persist_position_snapshot_evidence
from .position_dataset import PositionDatasetReplay, replay_position_datasets
from .position_ledger import build_position_token_ledger
from .position_settlement import PositionSettlementCapture, persist_position_settlement_evidence


@dataclass(frozen=True, slots=True)
class EvidencedPositionReplay:
    opening_evidence: EvidenceRef
    closing_evidence: EvidenceRef
    position: PositionDatasetReplay
    core_settlement_evidence: tuple[EvidenceRef, ...] = ()

    @property
    def trust(self) -> IngestionTrust:
        # Event quorum cannot upgrade the single-provider snapshot captures.
        return IngestionTrust.SINGLE_PROVIDER_RESEARCH

    @property
    def payload(self) -> bytes:
        ledger = self.position.ledger
        return canonical_json(
            {
                "schema": "oasis.evidenced-position-replay.v2",
                "core_settlement_evidence": [asdict(ref) for ref in self.core_settlement_evidence],
                "trust": self.trust.value,
                "execution_eligible": False,
                "chain_id": ledger.opening.block.chain_id,
                "position_manager": ledger.opening.position_manager,
                "token_id": ledger.opening.token_id,
                "pool_id": ledger.opening.pool_id,
                "opening_snapshot_evidence": asdict(self.opening_evidence),
                "closing_snapshot_evidence": asdict(self.closing_evidence),
                "core_event_evidence": [
                    asdict(ref)
                    for ref in (
                        self.position.core_evidence.evidence,
                        *self.position.core_evidence.peer_evidence,
                    )
                ],
                "ownership_event_evidence": [
                    asdict(ref)
                    for ref in (
                        self.position.ownership_evidence.evidence,
                        *self.position.ownership_evidence.peer_evidence,
                    )
                ],
                "opening_block_hash": ledger.opening.block.block_hash,
                "closing_block_hash": ledger.closing.block.block_hash,
                "opening_liquidity": ledger.opening.liquidity,
                "closing_liquidity": ledger.closing.liquidity,
                "settlement_complete": ledger.settlement_complete,
                "unresolved_settlements": ledger.unresolved_settlements,
                "cumulative_owner_amount0": ledger.cumulative_owner_amount0,
                "cumulative_owner_amount1": ledger.cumulative_owner_amount1,
                "net_pnl": None,
            }
        )


def replay_captured_position_datasets(
    store: EvidenceStore,
    *,
    opening: PositionSnapshotCapture,
    closing: PositionSnapshotCapture,
    pool_manager: str,
    core: VerifiedBlockRange,
    ownership: VerifiedBlockRange,
    settlements: tuple[PositionSettlementCapture, ...] = (),
    payments: tuple[OwnerBalanceCapture, ...] = (),
) -> EvidencedPositionReplay:
    """Validate the entire input before retaining any snapshot interpretation.

    Event evidence must already have been persisted by the ingestion layer. Runtime
    identity, provider independence and settlement/valuation attestation remain
    separate gates. This result never makes a signing or profitability claim.
    """
    opening.validate()
    closing.validate()
    if opening.state_view != closing.state_view:
        raise ValueError("StateView changed across the position replay interval")
    position = replay_position_datasets(
        opening.snapshot,
        closing.snapshot,
        pool_manager=pool_manager,
        core=core,
        ownership=ownership,
    )
    seen_settlements: set[tuple[int, int, int]] = set()
    for capture in settlements:
        capture.reconstruct()
        if (
            capture.pool_manager != normalize_address(pool_manager)
            or capture.pool_key != opening.snapshot.pool_key
            or capture.entry not in position.ledger.entries
        ):
            raise ValueError("settlement evidence does not match a replayed position entry")
        if capture.entry.position in seen_settlements:
            raise ValueError("duplicate settlement evidence for position entry")
        seen_settlements.add(capture.entry.position)
    seen_payments: set[tuple[int, int, int]] = set()
    for payment in payments:
        entry = payment.core.entry
        if (
            payment.core.pool_manager != normalize_address(pool_manager)
            or payment.core.pool_key != opening.snapshot.pool_key
            or entry not in position.ledger.entries
            or entry.position in seen_payments
        ):
            raise ValueError("payment does not uniquely match replayed position")
        seen_payments.add(entry.position)
        ownership_replay = position.ledger.ownership_replay
        if ownership_replay is None:
            raise ValueError("payment needs complete ownership replay")
        owner = opening.snapshot.nft_owner
        for transfer in ownership_replay.transfers:
            if transfer.position < entry.position:
                owner = transfer.to_owner
        if owner != payment.owner:
            raise ValueError("payer does not match NFT owner at modification")
        resolve_exclusive_owner_payment(payment)
    # Resolve references from storage and recheck the actual wire evidence, not
    # only matching labels/hashes in caller-constructed VerifiedBlockRange objects.
    for verified in (core, ownership):
        for reference in (verified.evidence, *verified.peer_evidence):
            record = store.get_record(reference)
            if record.payload != verified.dataset.payload or record.request_payload is None:
                raise ValueError("stored position event evidence does not match dataset")
            validate_block_range_exchanges(
                replace(verified.dataset, source=reference.source),
                restore_exchanges(record.request_payload, record.raw_response_payload),
            )
    if payments:
        resolved = {p.core.entry.position: persist_owner_payment(store, p) for p in payments}
        position = replace(
            position,
            ledger=build_position_token_ledger(
                opening.snapshot,
                closing.snapshot,
                tuple(resolved.get(e.position, e) for e in position.ledger.entries),
                canonical_blocks=core.dataset.blocks,
                ownership_replay=position.ledger.ownership_replay,
            ),
        )
    return EvidencedPositionReplay(
        persist_position_snapshot_evidence(store, opening),
        persist_position_snapshot_evidence(store, closing),
        position,
        tuple(persist_position_settlement_evidence(store, capture) for capture in settlements),
    )


__all__ = ["EvidencedPositionReplay", "replay_captured_position_datasets"]
