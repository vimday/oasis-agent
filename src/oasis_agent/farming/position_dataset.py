"""Build a position event ledger from retained core and ERC-721 datasets."""

import re
from dataclasses import dataclass

from ..evidence.ingestion import IngestionTrust, VerifiedBlockRange
from ..models.chain import BlockSnapshot
from ..models.common import BlockRef, EvidenceRef, normalize_address
from ..rpc.collector import ProviderAgreement
from ..uniswap_v4.events import MODIFY_LIQUIDITY_TOPIC, decode_modify_liquidity
from ..uniswap_v4.pool_id import pool_id_from_key
from .position_ledger import PositionLedgerEntry, PositionTokenLedger, build_position_token_ledger
from .position_ownership import TRANSFER_TOPIC, decode_position_transfer, replay_position_ownership
from .positions import PositionSnapshot, position_id


def _validate_sha256_hex_digest(value: str, name: str) -> None:
    """Validate that a value is exactly 64 ASCII hex digits [0-9a-fA-F]{64}.

    Strictly validates using ASCII-only character matching, rejecting:
    - Non-ASCII Unicode digits (e.g., Eastern Arabic U+0660-U+0669)
    - Non-hex characters (g, z, spaces, etc.)
    - Wrong length (63 chars, 65 chars, etc.)
    """
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError(f"{name} must be a 64-char SHA-256 hex digest")
    if not re.match(r"^[0-9a-fA-F]{64}$", value):
        raise ValueError(f"{name} must be a valid ASCII hex digest")


def _validate_canonical_evidence(
    evidence: EvidenceRef, evidence_name: str, closing_ref: BlockRef | None
) -> None:
    """Validate that evidence has canonical kind, v2 schema, and complete valid hashes.

    Shared validation for both primary and peer evidence at replay boundary.
    Strictly validates request_hash, raw_response_hash, and interpretation_hash
    as 64-char hex SHA-256 digests.
    """
    if evidence.kind != "canonical_block_range":
        raise ValueError(f"{evidence_name} has wrong kind")
    if evidence.schema_version != 2:
        raise ValueError(f"{evidence_name} is not v2 schema")
    # Validate all three hashes are present and are valid 64-char hex SHA-256 digests
    if not evidence.request_hash:
        raise ValueError(f"{evidence_name} is missing request_hash")
    _validate_sha256_hex_digest(evidence.request_hash, f"{evidence_name} request_hash")
    if not evidence.raw_response_hash:
        raise ValueError(f"{evidence_name} is missing raw_response_hash")
    _validate_sha256_hex_digest(evidence.raw_response_hash, f"{evidence_name} raw_response_hash")
    if not evidence.interpretation_hash:
        raise ValueError(f"{evidence_name} is missing interpretation_hash")
    _validate_sha256_hex_digest(
        evidence.interpretation_hash, f"{evidence_name} interpretation_hash"
    )
    if evidence.block != closing_ref:
        raise ValueError(f"{evidence_name} is not pinned to dataset closing block")


def _validate_source_set_binding(
    agreement: ProviderAgreement, verified: VerifiedBlockRange
) -> None:
    """Validate that exact sorted source tuple matches agreement sources.

    Ensures the primary evidence source and all peer evidence sources form
    exactly the agreement sources, catching omitted providers and source mismatches.
    """
    # Build sorted tuple of all sources: primary evidence source + peer evidence sources
    all_sources = [verified.evidence.source] + [peer.source for peer in verified.peer_evidence]
    expected_sources = sorted(agreement.sources)
    actual_sources = sorted(all_sources)

    if actual_sources != expected_sources:
        raise ValueError(
            f"evidence source set does not match agreement sources: "
            f"expected {expected_sources}, got {actual_sources}"
        )


def _validate_quorum_agreement_binding(
    agreement: ProviderAgreement, verified: VerifiedBlockRange
) -> None:
    """Validate that provider agreement fields bind exactly to the verified block range.

    Ensures agreement chain_id, block range, content_hash, and source invariant match
    the dataset, preventing agreement forging from another dataset/provider/range.

    Source invariant: >=2 non-empty unique sorted sources.
    """
    dataset = verified.dataset
    if agreement.chain_id != dataset.chain_id:
        raise ValueError("provider agreement chain_id does not match dataset")
    if agreement.start_block != dataset.start_block or agreement.end_block != dataset.end_block:
        raise ValueError("provider agreement block range does not match dataset")
    if agreement.content_hash != dataset.content_hash:
        raise ValueError("provider agreement content_hash does not match dataset")

    # Validate source invariant: >=2 non-empty unique sorted sources
    if len(agreement.sources) < 2:
        raise ValueError(
            f"provider agreement sources must have >=2 unique sources, got {len(agreement.sources)}"
        )

    # Validate sources are non-empty and unique
    sources_list = list(agreement.sources)
    for source in sources_list:
        if not source or not isinstance(source, str):
            raise ValueError("provider agreement sources must be non-empty strings")

    # Validate sources are sorted (checked by comparing to sorted copy)
    sorted_sources = sorted(sources_list)
    if sources_list != sorted_sources:
        raise ValueError(
            "provider agreement sources must be sorted; "
            f"got {sources_list}, expected {sorted_sources}"
        )

    # Validate no duplicates
    if len(set(sources_list)) != len(sources_list):
        raise ValueError("provider agreement sources must be unique (no duplicates)")


def _validate_peer_evidence_binding(verified: VerifiedBlockRange) -> None:
    """Validate that all peer evidence references bind exactly to the dataset.

    Ensures each peer evidence has canonical kind, v2 schema, complete hashes,
    exact source, content_hash, and closing block pinning.
    """
    dataset = verified.dataset
    closing_ref = dataset.blocks[-1].ref

    for peer in verified.peer_evidence:
        _validate_canonical_evidence(peer, "peer evidence", closing_ref)
        if peer.content_hash != dataset.content_hash:
            raise ValueError("peer evidence content_hash does not match dataset")
        if verified.agreement and peer.source not in verified.agreement.sources:
            raise ValueError("peer evidence source not in provider agreement sources")


def _validate_verified_position_range(
    verified: VerifiedBlockRange,
    *,
    expected_address: str,
    expected_blocks: tuple[BlockSnapshot, ...],
) -> None:
    """Validate that a verified position range matches expected scope and lineage.

    This replay-boundary check protects against forged dataclass replacements and
    future deserialization paths. VerifiedBlockRange.__post_init__ provides the
    construction-time guard; this function adds defense-in-depth validation.
    """
    dataset = verified.dataset
    if dataset.address not in (None, expected_address):
        raise ValueError("position dataset emitter scope does not match the expected contract")
    if dataset.blocks != expected_blocks:
        raise ValueError("position datasets do not share one canonical block lineage")
    if verified.evidence.content_hash != dataset.content_hash:
        raise ValueError("verified position evidence content hash does not match dataset")
    if verified.evidence.source != dataset.source:
        raise ValueError("verified position evidence source does not match dataset")
    if verified.evidence.block != dataset.blocks[-1].ref:
        raise ValueError("verified position evidence is not pinned to the dataset closing block")

    closing_ref = dataset.blocks[-1].ref

    # Validate primary evidence has canonical kind, schema, and complete hashes
    _validate_canonical_evidence(verified.evidence, "primary evidence", closing_ref)

    # Re-run complete VerifiedBlockRange trust/agreement/peer_evidence invariants
    # to prevent forged PROVIDER_QUORUM metadata from being promoted at replay
    if verified.trust is IngestionTrust.PROVIDER_QUORUM:
        if verified.agreement is None:
            raise ValueError("provider-quorum trust requires an agreement")
        if not verified.peer_evidence:
            raise ValueError("provider-quorum trust requires peer evidence")
        # Validate agreement and peer_evidence bind to this dataset, not another
        _validate_quorum_agreement_binding(verified.agreement, verified)
        _validate_peer_evidence_binding(verified)
        # Validate exact sorted source tuple matches agreement sources
        _validate_source_set_binding(verified.agreement, verified)
    elif verified.trust is IngestionTrust.SINGLE_PROVIDER_RESEARCH:
        if verified.agreement is not None or verified.peer_evidence:
            raise ValueError("single-provider research cannot carry quorum metadata")
    else:
        raise ValueError(f"unknown ingestion trust: {verified.trust}")


@dataclass(frozen=True, slots=True)
class PositionDatasetReplay:
    core_evidence: VerifiedBlockRange
    ownership_evidence: VerifiedBlockRange
    ledger: PositionTokenLedger

    @property
    def trust(self) -> IngestionTrust:
        if all(
            item.trust is IngestionTrust.PROVIDER_QUORUM
            for item in (self.core_evidence, self.ownership_evidence)
        ):
            return IngestionTrust.PROVIDER_QUORUM
        return IngestionTrust.SINGLE_PROVIDER_RESEARCH


def replay_position_datasets(
    opening: PositionSnapshot,
    closing: PositionSnapshot,
    *,
    pool_manager: str,
    core: VerifiedBlockRange,
    ownership: VerifiedBlockRange,
) -> PositionDatasetReplay:
    """Reconcile liquidity and NFT ownership, leaving token settlements unresolved.

    Requires the full contract-event streams across [opening, closing]. End-of-block
    opening events are excluded. No nonstandard ModifyPosition mirror is required.
    Snapshots still need independent RPC/runtime attestation before promotion.
    """
    manager = normalize_address(pool_manager)
    if core.dataset.blocks != ownership.dataset.blocks:
        raise ValueError("position datasets do not share one canonical block lineage")
    blocks = core.dataset.blocks
    if blocks[0].ref != opening.block or blocks[-1].ref != closing.block:
        raise ValueError("position snapshots do not match dataset boundary blocks")

    # Validate provenance at replay boundary before event decoding
    _validate_verified_position_range(core, expected_address=manager, expected_blocks=blocks)
    _validate_verified_position_range(
        ownership, expected_address=opening.position_manager, expected_blocks=blocks
    )

    for verified, emitter in ((core, manager), (ownership, opening.position_manager)):
        if verified.dataset.address not in (None, emitter) or verified.dataset.topics:
            raise ValueError("position replay requires complete contract event scope")
    for snapshot in (opening, closing):
        if snapshot.pool_id != pool_id_from_key(snapshot.pool_key):
            raise ValueError("position snapshot PoolId does not match PoolKey")
        if snapshot.core_position_owner != snapshot.position_manager:
            raise ValueError("position core owner must be the PositionManager")
        if snapshot.core_position_id != position_id(
            snapshot.position_manager, snapshot.tick_lower, snapshot.tick_upper, snapshot.token_id
        ):
            raise ValueError("position snapshot core ID does not match NFT/range")

    block_by_number = {item.ref.number: item.ref for item in blocks}
    salt = "0x" + opening.token_id.to_bytes(32, "big").hex()
    entries: list[PositionLedgerEntry] = []
    for log in core.dataset.logs:
        if (
            log.block_number <= opening.block.number
            or log.address != manager
            or not log.topics
            or log.topics[0] != MODIFY_LIQUIDITY_TOPIC
        ):
            continue
        observation = decode_modify_liquidity(log)
        if observation.sender != opening.position_manager or observation.salt != salt:
            continue
        # Same NFT salt through PosM must never appear under another pool/range.
        if (observation.pool_id, observation.tick_lower, observation.tick_upper) != (
            opening.pool_id,
            opening.tick_lower,
            opening.tick_upper,
        ):
            raise ValueError("core event conflicts with the target NFT pool/range")
        assert log.transaction_index is not None
        entries.append(
            PositionLedgerEntry(
                block_by_number[log.block_number],
                log.transaction_hash,
                log.transaction_index,
                log.log_index,
                observation,
            )
        )

    transfers = []
    for log in ownership.dataset.logs:
        if (
            log.block_number <= opening.block.number
            or log.address != opening.position_manager
            or not log.topics
            or log.topics[0] != TRANSFER_TOPIC
        ):
            continue
        event = decode_position_transfer(
            block_by_number[log.block_number], opening.position_manager, log
        )
        if event.token_id == opening.token_id:
            transfers.append(event)
    owner_replay = replay_position_ownership(opening, closing, tuple(transfers))
    ledger = build_position_token_ledger(
        opening,
        closing,
        tuple(entries),
        canonical_blocks=tuple(blocks),
        ownership_replay=owner_replay,
    )
    return PositionDatasetReplay(core, ownership, ledger)


__all__ = ["PositionDatasetReplay", "replay_position_datasets"]
