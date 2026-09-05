from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ..models.chain import BlockSnapshot
from ..models.common import BlockRef, EvidenceRef, normalize_hash32
from ..models.v4 import ModifyLiquidityObservation
from .position_ownership import PositionOwnershipReplay
from .positions import UINT128_MAX, PositionSnapshot


class SettlementAttribution(str, Enum):
    POSITION_TRACE = "position_trace"
    EXCLUSIVE_TRANSACTION = "exclusive_transaction"


@dataclass(frozen=True, slots=True)
class PositionLedgerEntry:
    block: BlockRef
    transaction_hash: str
    transaction_index: int
    log_index: int
    observation: ModifyLiquidityObservation
    owner_amount0: int | None = None
    owner_amount1: int | None = None
    settlement_attribution: SettlementAttribution | None = None
    settlement_evidence: EvidenceRef | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "transaction_hash", normalize_hash32(self.transaction_hash, "transaction hash")
        )
        for name, value in (
            ("transaction_index", self.transaction_index),
            ("log_index", self.log_index),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
            if value < 0:
                raise ValueError(f"{name} must be nonnegative")
        if self.block.timestamp is None:
            raise ValueError("position ledger entry requires a timestamped block")
        if self.observation.block_number != self.block.number:
            raise ValueError("position observation does not match its pinned block")

        amounts = self.owner_amount0, self.owner_amount1
        if (amounts[0] is None) != (amounts[1] is None):
            raise ValueError("settlement amounts must be both present or both absent")
        for amount in amounts:
            if amount is not None and (isinstance(amount, bool) or not isinstance(amount, int)):
                raise TypeError("settlement amounts must be integer base units")
        if amounts[0] is None:
            if self.settlement_attribution is not None or self.settlement_evidence is not None:
                raise ValueError("unresolved settlement cannot claim attribution or evidence")
        else:
            if self.settlement_attribution is None or self.settlement_evidence is None:
                raise ValueError("resolved settlement requires attribution and evidence")
            # Verify settlement evidence is pinned to the exact block by chain ID, hash, number, and timestamp
            evidence_block = self.settlement_evidence.block
            if evidence_block is None:
                raise ValueError("settlement evidence block cannot be None")
            if (
                evidence_block.chain_id != self.block.chain_id
                or evidence_block.block_hash != self.block.block_hash
                or evidence_block.number != self.block.number
                or evidence_block.timestamp != self.block.timestamp
            ):
                raise ValueError("settlement evidence block must match entry block exactly")

    @property
    def position(self) -> tuple[int, int, int]:
        return self.block.number, self.transaction_index, self.log_index

    @property
    def settlement_resolved(self) -> bool:
        return self.owner_amount0 is not None


@dataclass(frozen=True, slots=True)
class PositionTokenLedger:
    opening: PositionSnapshot
    closing: PositionSnapshot
    entries: tuple[PositionLedgerEntry, ...]
    settlement_complete: bool
    unresolved_settlements: int
    cumulative_owner_amount0: int | None
    cumulative_owner_amount1: int | None
    ownership_replay: PositionOwnershipReplay | None = None


def _validate_snapshot_identity(opening: PositionSnapshot, closing: PositionSnapshot) -> None:
    for name in (
        "position_manager",
        "token_id",
        "pool_key",
        "pool_id",
        "core_position_id",
        "tick_lower",
        "tick_upper",
    ):
        if getattr(opening, name) != getattr(closing, name):
            raise ValueError(f"position identity changed across ledger window: {name}")
    if opening.block.chain_id != closing.block.chain_id:
        raise ValueError("position ledger cannot cross chains")
    if opening.block.number > closing.block.number:
        raise ValueError("position ledger block window is reversed")


def _validate_entry_against_canonical_lineage(
    entry: PositionLedgerEntry,
    canonical_blocks: tuple[BlockSnapshot, ...],
) -> None:
    """Validate entry block against canonical block lineage.

    Ensures entry block number and hash match the canonical chain exactly.
    Rejects same-height different-hash entries (fork/reorg detection).
    """
    if not canonical_blocks:
        raise ValueError("canonical block lineage cannot be empty")

    entry_block_number = entry.block.number
    canonical_by_number: dict[int, BlockSnapshot] = {}
    for block_snapshot in canonical_blocks:
        block_num = block_snapshot.ref.number
        if block_num in canonical_by_number:
            other = canonical_by_number[block_num]
            if other.ref.block_hash != block_snapshot.ref.block_hash:
                raise ValueError(
                    f"canonical lineage contains conflicting blocks at height {block_num}"
                )
        canonical_by_number[block_num] = block_snapshot

    if entry_block_number not in canonical_by_number:
        raise ValueError(f"entry block number {entry_block_number} not in canonical lineage")

    canonical_snapshot = canonical_by_number[entry_block_number]
    canonical_block = canonical_snapshot.ref
    if entry.block.block_hash != canonical_block.block_hash:
        raise ValueError(
            f"entry block hash mismatch at height {entry_block_number}: "
            f"entry={entry.block.block_hash}, canonical={canonical_block.block_hash}"
        )
    if entry.block.chain_id != canonical_block.chain_id:
        raise ValueError(
            f"entry chain_id mismatch at height {entry_block_number}: "
            f"entry={entry.block.chain_id}, canonical={canonical_block.chain_id}"
        )
    if entry.block.timestamp != canonical_block.timestamp:
        raise ValueError(
            f"entry block timestamp mismatch at height {entry_block_number}: "
            f"entry={entry.block.timestamp}, canonical={canonical_block.timestamp}"
        )


def _validate_complete_canonical_lineage(
    opening: PositionSnapshot,
    closing: PositionSnapshot,
    entries: tuple[PositionLedgerEntry, ...],
    canonical_blocks: tuple[BlockSnapshot, ...],
) -> None:
    """Validate complete canonical block lineage at ledger builder boundary.

    Validates in input order first, then checks coverage and linkage:
    - non-empty tuple
    - first ref exactly equals opening.block
    - last ref exactly equals closing.block
    - exact expected length (closing - opening + 1)
    - each adjacent height +1 (continuous)
    - reject any duplicate height (even identical refs)
    - all same chain_id
    - adjacent parent_hash matches previous block_hash (linked chain)
    - opening/closing/entry refs match canonical snapshots exactly
    """
    if not canonical_blocks:
        raise ValueError("canonical block lineage cannot be empty")

    opening_height = opening.block.number
    closing_height = closing.block.number

    if opening_height >= closing_height:
        raise ValueError("opening block must be before closing block")

    # Validate input order: first ref must be opening.block
    if canonical_blocks[0].ref != opening.block:
        raise ValueError(f"first canonical block must be opening.block at height {opening_height}")

    # Validate input order: last ref must be closing.block
    if canonical_blocks[-1].ref != closing.block:
        raise ValueError(f"last canonical block must be closing.block at height {closing_height}")

    # Validate exact expected length
    expected_length = closing_height - opening_height + 1
    if len(canonical_blocks) != expected_length:
        raise ValueError(
            f"canonical_blocks length {len(canonical_blocks)} does not match "
            f"expected {expected_length} (heights {opening_height} to {closing_height})"
        )

    # Validate in input order: heights must be continuous (+1 each) and chain_id consistent
    canonical_chain_id = canonical_blocks[0].ref.chain_id
    seen_heights: set[int] = set()

    for i, block_snapshot in enumerate(canonical_blocks):
        curr_ref = block_snapshot.ref
        expected_height = opening_height + i

        # Check height matches position in tuple
        if curr_ref.number != expected_height:
            raise ValueError(
                f"canonical_blocks[{i}] has height {curr_ref.number}, "
                f"expected {expected_height} (opening at {opening_height})"
            )

        # Reject duplicate heights (even identical refs)
        if curr_ref.number in seen_heights:
            raise ValueError(
                f"canonical_blocks contains duplicate height {curr_ref.number} at position {i}"
            )
        seen_heights.add(curr_ref.number)

        # Check chain_id consistency
        if curr_ref.chain_id != canonical_chain_id:
            raise ValueError(
                f"canonical_blocks[{i}] has chain_id {curr_ref.chain_id}, "
                f"expected {canonical_chain_id}"
            )

        # Check parent_hash linkage (except first block)
        if i > 0:
            prev_snapshot = canonical_blocks[i - 1]
            if block_snapshot.parent_hash != prev_snapshot.ref.block_hash:
                raise ValueError(
                    f"canonical_blocks[{i}] parent_hash mismatch at height {curr_ref.number}: "
                    f"parent_hash={block_snapshot.parent_hash}, "
                    f"previous block_hash={prev_snapshot.ref.block_hash}"
                )

    # Validate all entry blocks match canonical snapshots by height (will be checked per-entry in loop)
    for entry in entries:
        entry_height = entry.block.number
        # Entry height check happens in the main loop after opening/closing validation
        if opening_height <= entry_height < len(canonical_blocks) + opening_height:
            # Find canonical block at this height
            index_in_canonical = entry_height - opening_height
            entry_canonical = canonical_blocks[index_in_canonical]
            if entry.block != entry_canonical.ref:
                raise ValueError(
                    f"entry block does not match canonical lineage at height {entry_height}"
                )


def build_position_token_ledger(
    opening: PositionSnapshot,
    closing: PositionSnapshot,
    entries: tuple[PositionLedgerEntry, ...],
    *,
    canonical_blocks: tuple[BlockSnapshot, ...],
    ownership_replay: PositionOwnershipReplay | None = None,
) -> PositionTokenLedger:
    _validate_snapshot_identity(opening, closing)
    _validate_complete_canonical_lineage(opening, closing, entries, canonical_blocks)
    if ownership_replay is None:
        if opening.nft_owner != closing.nft_owner:
            raise ValueError("NFT owner changed without an ownership replay")
    elif ownership_replay.opening != opening or ownership_replay.closing != closing:
        raise ValueError("ownership replay does not match the ledger snapshots")
    positions = tuple(entry.position for entry in entries)
    if positions != tuple(sorted(positions)) or len(set(positions)) != len(positions):
        raise ValueError("position ledger entries must be in strict event order")

    expected_salt = "0x" + opening.token_id.to_bytes(32, "big").hex()
    liquidity = opening.liquidity
    unresolved = 0
    amount0 = 0
    amount1 = 0
    for entry in entries:
        observation = entry.observation
        _validate_entry_against_canonical_lineage(entry, canonical_blocks)
        if entry.block.chain_id != opening.block.chain_id:
            raise ValueError("position ledger entry belongs to a different chain")
        if not opening.block.number < entry.block.number <= closing.block.number:
            raise ValueError("position ledger entry is outside the snapshot interval")
        if observation.pool_id != opening.pool_id:
            raise ValueError("position ledger entry belongs to a different pool")
        if observation.sender != opening.position_manager:
            raise ValueError("position ledger sender is not the pinned PositionManager")
        if (observation.tick_lower, observation.tick_upper) != (
            opening.tick_lower,
            opening.tick_upper,
        ):
            raise ValueError("position ledger entry belongs to a different tick range")
        if observation.salt != expected_salt:
            raise ValueError("position ledger salt does not match token ID")
        liquidity += observation.liquidity_delta
        if not 0 <= liquidity <= UINT128_MAX:
            raise ValueError("position liquidity underflow or overflow during replay")
        if entry.settlement_resolved:
            assert entry.owner_amount0 is not None
            assert entry.owner_amount1 is not None
            amount0 += entry.owner_amount0
            amount1 += entry.owner_amount1
        else:
            unresolved += 1

    if liquidity != closing.liquidity:
        raise ValueError("replayed liquidity does not match closing snapshot")
    complete = unresolved == 0
    return PositionTokenLedger(
        opening=opening,
        closing=closing,
        entries=entries,
        settlement_complete=complete,
        unresolved_settlements=unresolved,
        cumulative_owner_amount0=amount0 if complete else None,
        cumulative_owner_amount1=amount1 if complete else None,
        ownership_replay=ownership_replay,
    )


__all__ = [
    "PositionLedgerEntry",
    "PositionTokenLedger",
    "SettlementAttribution",
    "build_position_token_ledger",
]
