from dataclasses import replace
from pathlib import Path

import pytest
from eth_abi.abi import encode

from oasis_agent.evidence.ingestion import IngestionTrust, persist_and_verify_block_range
from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.farming.position_dataset import replay_position_datasets
from oasis_agent.farming.position_ownership import TRANSFER_TOPIC
from oasis_agent.farming.positions import PositionSnapshot, position_id
from oasis_agent.models.chain import BlockSnapshot, LogRecord
from oasis_agent.models.common import BlockRef, EvidenceRef
from oasis_agent.models.v4 import DYNAMIC_FEE_FLAG, PoolKey
from oasis_agent.rpc.collector import BlockRangeDataset, ProviderAgreement
from oasis_agent.uniswap_v4.events import MODIFY_LIQUIDITY_TOPIC
from oasis_agent.uniswap_v4.pool_id import pool_id_from_key
from tests.rpc_evidence_helpers import range_exchanges

CORE = "0x" + "11" * 20
POSM = "0x" + "22" * 20
OWNER = "0x" + "33" * 20
NEXT_OWNER = "0x" + "44" * 20


def address_topic(address: str) -> str:
    return "0x" + "0" * 24 + address[2:]


def scenario() -> tuple[PositionSnapshot, PositionSnapshot, BlockRangeDataset, BlockRangeDataset]:
    b0 = BlockRef(4663, 10, "0x" + "aa" * 32, 1_000)
    b1 = BlockRef(4663, 11, "0x" + "bb" * 32, 1_001)
    blocks = (BlockSnapshot(b0, "0x" + "99" * 32), BlockSnapshot(b1, b0.block_hash))
    key = PoolKey("0x" + "55" * 20, "0x" + "66" * 20, DYNAMIC_FEE_FLAG, 60, "0x" + "77" * 20)
    pool_id = pool_id_from_key(key)
    opening = PositionSnapshot(
        b0,
        POSM,
        7,
        OWNER,
        POSM,
        key,
        pool_id,
        position_id(POSM, -120, 120, 7),
        -120,
        120,
        False,
        100,
        0,
        0,
        0,
        0,
        0,
        0,
    )
    closing = replace(opening, block=b1, liquidity=120, nft_owner=NEXT_OWNER)
    tx = "0x" + "88" * 32
    core_log = LogRecord(
        CORE,
        (MODIFY_LIQUIDITY_TOPIC, pool_id, address_topic(POSM)),
        "0x"
        + encode(
            ["int24", "int24", "int256", "bytes32"], [-120, 120, 20, (7).to_bytes(32, "big")]
        ).hex(),
        11,
        tx,
        0,
        b1.block_hash,
        0,
    )
    transfer = LogRecord(
        POSM,
        (
            TRANSFER_TOPIC,
            address_topic(OWNER),
            address_topic(NEXT_OWNER),
            "0x" + (7).to_bytes(32, "big").hex(),
        ),
        "0x",
        11,
        tx,
        1,
        b1.block_hash,
        0,
    )
    return (
        opening,
        closing,
        BlockRangeDataset("rpc-a", blocks, (core_log,), address=CORE),
        BlockRangeDataset("rpc-a", blocks, (transfer,), address=POSM),
    )


def run_replay(
    tmp_path: Path,
    opening: PositionSnapshot,
    closing: PositionSnapshot,
    core: BlockRangeDataset,
    nft: BlockRangeDataset,
):
    store = EvidenceStore(tmp_path / "evidence.db")
    try:
        verified = tuple(
            persist_and_verify_block_range(
                store, dataset, range_exchanges(dataset), research_single_provider=True
            )
            for dataset in (core, nft)
        )
        return replay_position_datasets(
            opening, closing, pool_manager=CORE, core=verified[0], ownership=verified[1]
        )
    finally:
        store.close()


def test_builds_ledger_from_raw_core_and_nft_events_without_mirror(tmp_path: Path) -> None:
    result = run_replay(tmp_path, *scenario())
    assert result.ledger.closing.liquidity == 120
    assert result.ledger.ownership_replay.final_owner == NEXT_OWNER
    assert result.ledger.entries[0].observation.liquidity_delta == 20
    assert result.ledger.unresolved_settlements == 1
    assert result.ledger.cumulative_owner_amount0 is None
    assert result.ledger.cumulative_owner_amount1 is None


@pytest.mark.parametrize("token_id", [7, 8])
def test_complete_nft_stream_accepts_target_and_other_self_transfers(
    tmp_path: Path, token_id: int
) -> None:
    opening, closing, core, nft = scenario()
    transfer = nft.logs[0]
    self_transfer = replace(
        transfer,
        topics=(
            TRANSFER_TOPIC,
            address_topic(OWNER),
            address_topic(OWNER),
            "0x" + token_id.to_bytes(32, "big").hex(),
        ),
    )
    result = run_replay(
        tmp_path,
        opening,
        replace(closing, nft_owner=OWNER),
        core,
        replace(nft, logs=(self_transfer,)),
    )
    assert result.ledger.ownership_replay.final_owner == OWNER
    assert len(result.ledger.ownership_replay.transfers) == (1 if token_id == 7 else 0)
    assert result.ledger.cumulative_owner_amount0 is None


def test_missing_liquidity_or_transfer_events_cannot_close_ledger(tmp_path: Path) -> None:
    opening, closing, core, nft = scenario()
    with pytest.raises(ValueError, match="closing snapshot"):
        run_replay(tmp_path, opening, closing, replace(core, logs=()), nft)
    with pytest.raises(ValueError, match="replayed owner"):
        run_replay(tmp_path, opening, closing, core, replace(nft, logs=()))


def test_same_height_other_fork_is_rejected(tmp_path: Path) -> None:
    opening, closing, core, nft = scenario()
    alt = replace(nft.blocks[-1], ref=replace(nft.blocks[-1].ref, block_hash="0x" + "cc" * 32))
    nft = replace(
        nft,
        blocks=(nft.blocks[0], alt),
        logs=(replace(nft.logs[0], block_hash=alt.ref.block_hash),),
    )
    with pytest.raises(ValueError, match="lineage"):
        run_replay(tmp_path, opening, closing, core, nft)


def test_narrowed_event_stream_and_wrong_core_id_are_rejected(tmp_path: Path) -> None:
    opening, closing, core, nft = scenario()
    with pytest.raises(ValueError, match="complete contract event scope"):
        run_replay(tmp_path, opening, closing, replace(core, topics=(MODIFY_LIQUIDITY_TOPIC,)), nft)
    with pytest.raises(ValueError, match="core ID"):
        run_replay(
            tmp_path, replace(opening, core_position_id="0x" + "ff" * 32), closing, core, nft
        )


def test_opening_block_events_are_not_counted_twice(tmp_path: Path) -> None:
    opening, closing, core, nft = scenario()
    earlier = replace(
        core.logs[0],
        block_number=opening.block.number,
        block_hash=opening.block.block_hash,
        transaction_hash="0x" + "dd" * 32,
    )
    result = run_replay(tmp_path, opening, closing, replace(core, logs=(earlier, *core.logs)), nft)
    assert len(result.ledger.entries) == 1


def test_rejects_core_evidence_that_does_not_match_dataset_content(tmp_path: Path) -> None:
    opening, closing, core, nft = scenario()
    store = EvidenceStore(tmp_path / "evidence.db")
    try:
        verified_core, verified_nft = tuple(
            persist_and_verify_block_range(
                store, dataset, range_exchanges(dataset), research_single_provider=True
            )
            for dataset in (core, nft)
        )
        # Forge the evidence by directly mutating the frozen object's state
        forged_evidence = replace(verified_core.evidence, content_hash="f" * 64)
        forged = object.__new__(type(verified_core))
        object.__setattr__(forged, "dataset", verified_core.dataset)
        object.__setattr__(forged, "evidence", forged_evidence)
        object.__setattr__(forged, "trust", verified_core.trust)
        object.__setattr__(forged, "agreement", verified_core.agreement)
        object.__setattr__(forged, "peer_evidence", verified_core.peer_evidence)
        with pytest.raises(ValueError, match="content hash"):
            replay_position_datasets(
                opening,
                closing,
                pool_manager=CORE,
                core=forged,
                ownership=verified_nft,
            )
    finally:
        store.close()


def test_allows_single_provider_position_replay_for_research(tmp_path: Path) -> None:
    opening, closing, core, nft = scenario()
    result = run_replay(tmp_path, opening, closing, core, nft)
    assert result.trust is IngestionTrust.SINGLE_PROVIDER_RESEARCH


def test_rejects_closing_block_evidence_from_another_scope(tmp_path: Path) -> None:
    opening, closing, core, nft = scenario()
    store = EvidenceStore(tmp_path / "evidence.db")
    try:
        verified_core, verified_nft = tuple(
            persist_and_verify_block_range(
                store, dataset, range_exchanges(dataset), research_single_provider=True
            )
            for dataset in (core, nft)
        )
        # Forge the dataset by directly mutating the frozen object's state
        forged_dataset = object.__new__(type(verified_nft.dataset))
        object.__setattr__(forged_dataset, "source", verified_nft.dataset.source)
        object.__setattr__(forged_dataset, "blocks", verified_nft.dataset.blocks)
        object.__setattr__(forged_dataset, "logs", verified_nft.dataset.logs)
        object.__setattr__(forged_dataset, "address", CORE)  # Wrong address
        object.__setattr__(forged_dataset, "topics", verified_nft.dataset.topics)

        altered = object.__new__(type(verified_nft))
        object.__setattr__(altered, "dataset", forged_dataset)
        object.__setattr__(altered, "evidence", verified_nft.evidence)
        object.__setattr__(altered, "trust", verified_nft.trust)
        object.__setattr__(altered, "agreement", verified_nft.agreement)
        object.__setattr__(altered, "peer_evidence", verified_nft.peer_evidence)

        with pytest.raises(ValueError, match=r"evidence|dataset"):
            replay_position_datasets(
                opening, closing, pool_manager=CORE, core=verified_core, ownership=altered
            )
    finally:
        store.close()


def test_rejects_forged_quorum_trust_without_peer_evidence(tmp_path: Path) -> None:
    """Ensure forged PROVIDER_QUORUM trust cannot bypass agreement/peer_evidence checks."""
    opening, closing, core, nft = scenario()
    store = EvidenceStore(tmp_path / "evidence.db")
    try:
        verified_core, verified_nft = tuple(
            persist_and_verify_block_range(
                store, dataset, range_exchanges(dataset), research_single_provider=True
            )
            for dataset in (core, nft)
        )
        # Verify the original is single-provider
        assert verified_core.trust is IngestionTrust.SINGLE_PROVIDER_RESEARCH

        # Forge a VerifiedBlockRange with PROVIDER_QUORUM trust but no agreement/peer_evidence
        forged = object.__new__(type(verified_core))
        object.__setattr__(forged, "dataset", verified_core.dataset)
        object.__setattr__(forged, "evidence", verified_core.evidence)
        object.__setattr__(forged, "trust", IngestionTrust.PROVIDER_QUORUM)  # Mutate trust
        object.__setattr__(forged, "agreement", None)  # No agreement
        object.__setattr__(forged, "peer_evidence", ())  # No peer evidence

        with pytest.raises(ValueError, match=r"provider-quorum|agreement|peer"):
            replay_position_datasets(
                opening,
                closing,
                pool_manager=CORE,
                core=forged,
                ownership=verified_nft,
            )
    finally:
        store.close()


def test_rejects_agreement_from_another_dataset(tmp_path: Path) -> None:
    """Ensure agreement from another dataset/range/content cannot be promoted at replay."""
    opening, closing, core, nft = scenario()
    store = EvidenceStore(tmp_path / "evidence.db")
    try:
        verified_core, verified_nft = tuple(
            persist_and_verify_block_range(
                store, dataset, range_exchanges(dataset), research_single_provider=True
            )
            for dataset in (core, nft)
        )

        # Create a forged agreement with different chain_id
        wrong_agreement = ProviderAgreement(
            chain_id=4664,  # Different chain
            start_block=core.start_block,
            end_block=core.end_block,
            sources=("rpc-b", "rpc-c"),
            content_hash=verified_core.evidence.content_hash,
        )

        # Provide peer evidence with matching sources and content_hash
        peer = EvidenceRef(
            "canonical_block_range",
            verified_core.evidence.content_hash,
            verified_core.dataset.blocks[-1].ref,
            "rpc-b",
            schema_version=2,
            request_hash="a" * 64,
            raw_response_hash="b" * 64,
            interpretation_hash="c" * 64,
        )

        forged = object.__new__(type(verified_core))
        object.__setattr__(forged, "dataset", verified_core.dataset)
        object.__setattr__(forged, "evidence", verified_core.evidence)
        object.__setattr__(forged, "trust", IngestionTrust.PROVIDER_QUORUM)
        object.__setattr__(forged, "agreement", wrong_agreement)
        object.__setattr__(forged, "peer_evidence", (peer,))

        with pytest.raises(ValueError, match=r"agreement|chain_id"):
            replay_position_datasets(
                opening, closing, pool_manager=CORE, core=forged, ownership=verified_nft
            )
    finally:
        store.close()


def test_rejects_peer_evidence_from_another_source(tmp_path: Path) -> None:
    """Ensure peer evidence from source absent in agreement.sources is rejected.

    Tests that source-set binding validation catches peer evidence with a source
    that is not in the agreement sources list, exercising the exact sorted tuple
    matching validation.
    """
    opening, closing, core, nft = scenario()
    store = EvidenceStore(tmp_path / "evidence.db")
    try:
        verified_core, verified_nft = tuple(
            persist_and_verify_block_range(
                store, dataset, range_exchanges(dataset), research_single_provider=True
            )
            for dataset in (core, nft)
        )

        # Create a forged peer evidence with a source NOT in agreement.sources
        wrong_peer = EvidenceRef(
            "canonical_block_range",
            verified_core.evidence.content_hash,  # Correct content_hash
            verified_core.dataset.blocks[-1].ref,
            "rpc-absent",  # Source NOT in agreement.sources
            schema_version=2,
            request_hash="a" * 64,
            raw_response_hash="b" * 64,
            interpretation_hash="c" * 64,
        )

        # Agreement only includes rpc-a and rpc-b
        agreement = ProviderAgreement(
            chain_id=core.chain_id,
            start_block=core.start_block,
            end_block=core.end_block,
            sources=("rpc-a", "rpc-b"),  # rpc-absent is NOT here
            content_hash=verified_core.evidence.content_hash,
        )

        forged = object.__new__(type(verified_core))
        object.__setattr__(forged, "dataset", verified_core.dataset)
        object.__setattr__(forged, "evidence", verified_core.evidence)
        object.__setattr__(forged, "trust", IngestionTrust.PROVIDER_QUORUM)
        object.__setattr__(forged, "agreement", agreement)
        object.__setattr__(forged, "peer_evidence", (wrong_peer,))

        with pytest.raises(ValueError, match="source not in provider agreement"):
            replay_position_datasets(
                opening, closing, pool_manager=CORE, core=forged, ownership=verified_nft
            )
    finally:
        store.close()


def test_rejects_omitted_provider_from_quorum_sources(tmp_path: Path) -> None:
    """Ensure omitted provider from agreement sources is caught at replay."""
    opening, closing, core, nft = scenario()
    store = EvidenceStore(tmp_path / "evidence.db")
    try:
        verified_core, verified_nft = tuple(
            persist_and_verify_block_range(
                store, dataset, range_exchanges(dataset), research_single_provider=True
            )
            for dataset in (core, nft)
        )

        # Create agreement that claims 3 sources but we only have 2
        # Agreement claims rpc-a, rpc-b, rpc-c as sources
        agreement = ProviderAgreement(
            chain_id=core.chain_id,
            start_block=core.start_block,
            end_block=core.end_block,
            sources=("rpc-a", "rpc-b", "rpc-c"),  # 3 sources claimed
            content_hash=verified_core.evidence.content_hash,
        )

        # But we only have evidence from rpc-a and rpc-b
        peer_b = EvidenceRef(
            "canonical_block_range",
            verified_core.evidence.content_hash,
            verified_core.dataset.blocks[-1].ref,
            "rpc-b",
            schema_version=2,
            request_hash="a" * 64,
            raw_response_hash="b" * 64,
            interpretation_hash="c" * 64,
        )

        forged = object.__new__(type(verified_core))
        object.__setattr__(forged, "dataset", verified_core.dataset)
        object.__setattr__(forged, "evidence", verified_core.evidence)  # source=rpc-a
        object.__setattr__(forged, "trust", IngestionTrust.PROVIDER_QUORUM)
        object.__setattr__(forged, "agreement", agreement)
        object.__setattr__(forged, "peer_evidence", (peer_b,))  # only peer from rpc-b

        with pytest.raises(ValueError, match=r"source set|expected"):
            replay_position_datasets(
                opening, closing, pool_manager=CORE, core=forged, ownership=verified_nft
            )
    finally:
        store.close()


def test_rejects_malformed_primary_evidence(tmp_path: Path) -> None:
    """Ensure malformed primary evidence is rejected at replay."""
    opening, closing, core, nft = scenario()
    store = EvidenceStore(tmp_path / "evidence.db")
    try:
        verified_core, verified_nft = tuple(
            persist_and_verify_block_range(
                store, dataset, range_exchanges(dataset), research_single_provider=True
            )
            for dataset in (core, nft)
        )

        # Create agreement with valid sources
        agreement = ProviderAgreement(
            chain_id=core.chain_id,
            start_block=core.start_block,
            end_block=core.end_block,
            sources=("rpc-a", "rpc-b"),
            content_hash=verified_core.evidence.content_hash,
        )

        # Create valid peer
        peer = EvidenceRef(
            "canonical_block_range",
            verified_core.evidence.content_hash,
            verified_core.dataset.blocks[-1].ref,
            "rpc-b",
            schema_version=2,
            request_hash="a" * 64,
            raw_response_hash="b" * 64,
            interpretation_hash="c" * 64,
        )

        # Create forged primary evidence with wrong schema_version
        wrong_primary = EvidenceRef(
            "canonical_block_range",
            verified_core.evidence.content_hash,
            verified_core.dataset.blocks[-1].ref,
            "rpc-a",
            schema_version=1,  # Wrong: should be 2
            request_hash="a" * 64,
            raw_response_hash="b" * 64,
            interpretation_hash="c" * 64,
        )

        forged = object.__new__(type(verified_core))
        object.__setattr__(forged, "dataset", verified_core.dataset)
        object.__setattr__(forged, "evidence", wrong_primary)
        object.__setattr__(forged, "trust", IngestionTrust.PROVIDER_QUORUM)
        object.__setattr__(forged, "agreement", agreement)
        object.__setattr__(forged, "peer_evidence", (peer,))

        with pytest.raises(ValueError, match=r"primary evidence|v2 schema"):
            replay_position_datasets(
                opening, closing, pool_manager=CORE, core=forged, ownership=verified_nft
            )
    finally:
        store.close()


def test_rejects_primary_evidence_with_missing_hashes(tmp_path: Path) -> None:
    """Ensure primary evidence with missing hashes is rejected at replay."""
    opening, closing, core, nft = scenario()
    store = EvidenceStore(tmp_path / "evidence.db")
    try:
        verified_core, verified_nft = tuple(
            persist_and_verify_block_range(
                store, dataset, range_exchanges(dataset), research_single_provider=True
            )
            for dataset in (core, nft)
        )

        # Create agreement with valid sources
        agreement = ProviderAgreement(
            chain_id=core.chain_id,
            start_block=core.start_block,
            end_block=core.end_block,
            sources=("rpc-a", "rpc-b"),
            content_hash=verified_core.evidence.content_hash,
        )

        # Create valid peer
        peer = EvidenceRef(
            "canonical_block_range",
            verified_core.evidence.content_hash,
            verified_core.dataset.blocks[-1].ref,
            "rpc-b",
            schema_version=2,
            request_hash="a" * 64,
            raw_response_hash="b" * 64,
            interpretation_hash="c" * 64,
        )

        # Create forged primary evidence with missing interpretation_hash
        wrong_primary = EvidenceRef(
            "canonical_block_range",
            verified_core.evidence.content_hash,
            verified_core.dataset.blocks[-1].ref,
            "rpc-a",
            schema_version=2,
            request_hash="a" * 64,
            raw_response_hash="b" * 64,
            interpretation_hash="",  # Missing/empty
        )

        forged = object.__new__(type(verified_core))
        object.__setattr__(forged, "dataset", verified_core.dataset)
        object.__setattr__(forged, "evidence", wrong_primary)
        object.__setattr__(forged, "trust", IngestionTrust.PROVIDER_QUORUM)
        object.__setattr__(forged, "agreement", agreement)
        object.__setattr__(forged, "peer_evidence", (peer,))

        with pytest.raises(ValueError, match=r"primary evidence|complete request"):
            replay_position_datasets(
                opening, closing, pool_manager=CORE, core=forged, ownership=verified_nft
            )
    finally:
        store.close()


def test_rejects_primary_evidence_with_malformed_hash_length(tmp_path: Path) -> None:
    """Ensure primary evidence with wrong hash digest length is rejected at replay boundary.

    Uses object.__new__ to bypass EvidenceRef construction-time validation, simulating
    a forged evidence object that escaped deserialization checks.
    """
    opening, closing, core, nft = scenario()
    store = EvidenceStore(tmp_path / "evidence.db")
    try:
        verified_core, verified_nft = tuple(
            persist_and_verify_block_range(
                store, dataset, range_exchanges(dataset), research_single_provider=True
            )
            for dataset in (core, nft)
        )

        agreement = ProviderAgreement(
            chain_id=core.chain_id,
            start_block=core.start_block,
            end_block=core.end_block,
            sources=("rpc-a", "rpc-b"),
            content_hash=verified_core.evidence.content_hash,
        )

        # Create valid peer
        peer = EvidenceRef(
            "canonical_block_range",
            verified_core.evidence.content_hash,
            verified_core.dataset.blocks[-1].ref,
            "rpc-b",
            schema_version=2,
            request_hash="a" * 64,
            raw_response_hash="b" * 64,
            interpretation_hash="c" * 64,
        )

        # Create forged primary evidence with wrong request_hash length (63 chars instead of 64)
        # Use object.__new__ to bypass construction-time validation
        wrong_primary = object.__new__(EvidenceRef)
        object.__setattr__(wrong_primary, "kind", "canonical_block_range")
        object.__setattr__(wrong_primary, "content_hash", verified_core.evidence.content_hash)
        object.__setattr__(wrong_primary, "block", verified_core.dataset.blocks[-1].ref)
        object.__setattr__(wrong_primary, "source", "rpc-a")
        object.__setattr__(wrong_primary, "retrieved_at", verified_core.evidence.retrieved_at)
        object.__setattr__(wrong_primary, "request_hash", "a" * 63)  # Wrong: too short
        object.__setattr__(wrong_primary, "raw_response_hash", "b" * 64)
        object.__setattr__(wrong_primary, "interpretation_hash", "c" * 64)
        object.__setattr__(wrong_primary, "schema_version", 2)

        forged = object.__new__(type(verified_core))
        object.__setattr__(forged, "dataset", verified_core.dataset)
        object.__setattr__(forged, "evidence", wrong_primary)
        object.__setattr__(forged, "trust", IngestionTrust.PROVIDER_QUORUM)
        object.__setattr__(forged, "agreement", agreement)
        object.__setattr__(forged, "peer_evidence", (peer,))

        with pytest.raises(ValueError, match=r"request_hash|64-char"):
            replay_position_datasets(
                opening, closing, pool_manager=CORE, core=forged, ownership=verified_nft
            )
    finally:
        store.close()


def test_rejects_primary_evidence_with_malformed_hex_digest(tmp_path: Path) -> None:
    """Ensure primary evidence with invalid hex in digest is rejected at replay boundary.

    Uses object.__new__ to bypass EvidenceRef construction-time validation, simulating
    a forged evidence object that escaped deserialization checks.
    """
    opening, closing, core, nft = scenario()
    store = EvidenceStore(tmp_path / "evidence.db")
    try:
        verified_core, verified_nft = tuple(
            persist_and_verify_block_range(
                store, dataset, range_exchanges(dataset), research_single_provider=True
            )
            for dataset in (core, nft)
        )

        agreement = ProviderAgreement(
            chain_id=core.chain_id,
            start_block=core.start_block,
            end_block=core.end_block,
            sources=("rpc-a", "rpc-b"),
            content_hash=verified_core.evidence.content_hash,
        )

        # Create valid peer
        peer = EvidenceRef(
            "canonical_block_range",
            verified_core.evidence.content_hash,
            verified_core.dataset.blocks[-1].ref,
            "rpc-b",
            schema_version=2,
            request_hash="a" * 64,
            raw_response_hash="b" * 64,
            interpretation_hash="c" * 64,
        )

        # Create forged primary evidence with invalid hex characters in raw_response_hash
        # Use object.__new__ to bypass construction-time validation
        wrong_primary = object.__new__(EvidenceRef)
        object.__setattr__(wrong_primary, "kind", "canonical_block_range")
        object.__setattr__(wrong_primary, "content_hash", verified_core.evidence.content_hash)
        object.__setattr__(wrong_primary, "block", verified_core.dataset.blocks[-1].ref)
        object.__setattr__(wrong_primary, "source", "rpc-a")
        object.__setattr__(wrong_primary, "retrieved_at", verified_core.evidence.retrieved_at)
        object.__setattr__(wrong_primary, "request_hash", "a" * 64)
        object.__setattr__(
            wrong_primary, "raw_response_hash", "g" * 64
        )  # Wrong: 'g' is not valid hex
        object.__setattr__(wrong_primary, "interpretation_hash", "c" * 64)
        object.__setattr__(wrong_primary, "schema_version", 2)

        forged = object.__new__(type(verified_core))
        object.__setattr__(forged, "dataset", verified_core.dataset)
        object.__setattr__(forged, "evidence", wrong_primary)
        object.__setattr__(forged, "trust", IngestionTrust.PROVIDER_QUORUM)
        object.__setattr__(forged, "agreement", agreement)
        object.__setattr__(forged, "peer_evidence", (peer,))

        with pytest.raises(ValueError, match=r"raw_response_hash|valid hex"):
            replay_position_datasets(
                opening, closing, pool_manager=CORE, core=forged, ownership=verified_nft
            )
    finally:
        store.close()


def test_rejects_peer_evidence_with_malformed_hash_length(tmp_path: Path) -> None:
    """Ensure peer evidence with wrong hash digest length is rejected at replay boundary.

    Uses object.__new__ to bypass EvidenceRef construction-time validation, simulating
    a forged evidence object that escaped deserialization checks.
    """
    opening, closing, core, nft = scenario()
    store = EvidenceStore(tmp_path / "evidence.db")
    try:
        verified_core, verified_nft = tuple(
            persist_and_verify_block_range(
                store, dataset, range_exchanges(dataset), research_single_provider=True
            )
            for dataset in (core, nft)
        )

        agreement = ProviderAgreement(
            chain_id=core.chain_id,
            start_block=core.start_block,
            end_block=core.end_block,
            sources=("rpc-a", "rpc-b"),
            content_hash=verified_core.evidence.content_hash,
        )

        # Create forged peer with wrong interpretation_hash length (65 chars instead of 64)
        # Use object.__new__ to bypass construction-time validation
        wrong_peer = object.__new__(EvidenceRef)
        object.__setattr__(wrong_peer, "kind", "canonical_block_range")
        object.__setattr__(wrong_peer, "content_hash", verified_core.evidence.content_hash)
        object.__setattr__(wrong_peer, "block", verified_core.dataset.blocks[-1].ref)
        object.__setattr__(wrong_peer, "source", "rpc-b")
        object.__setattr__(wrong_peer, "retrieved_at", verified_core.evidence.retrieved_at)
        object.__setattr__(wrong_peer, "request_hash", "a" * 64)
        object.__setattr__(wrong_peer, "raw_response_hash", "b" * 64)
        object.__setattr__(wrong_peer, "interpretation_hash", "c" * 65)  # Wrong: too long
        object.__setattr__(wrong_peer, "schema_version", 2)

        forged = object.__new__(type(verified_core))
        object.__setattr__(forged, "dataset", verified_core.dataset)
        object.__setattr__(forged, "evidence", verified_core.evidence)
        object.__setattr__(forged, "trust", IngestionTrust.PROVIDER_QUORUM)
        object.__setattr__(forged, "agreement", agreement)
        object.__setattr__(forged, "peer_evidence", (wrong_peer,))

        with pytest.raises(ValueError, match=r"interpretation_hash|64-char"):
            replay_position_datasets(
                opening, closing, pool_manager=CORE, core=forged, ownership=verified_nft
            )
    finally:
        store.close()


def test_rejects_primary_evidence_with_unicode_hex_digits(tmp_path: Path) -> None:
    """Ensure primary evidence with non-ASCII Unicode digits is rejected at replay boundary.

    Tests ASCII-only hex validation by attempting to use Eastern Arabic digits
    (U+0660-U+0669) which are valid Unicode digit characters but must be rejected
    at replay boundary. Uses object.__new__ to bypass EvidenceRef construction-time
    validation.
    """
    opening, closing, core, nft = scenario()
    store = EvidenceStore(tmp_path / "evidence.db")
    try:
        verified_core, verified_nft = tuple(
            persist_and_verify_block_range(
                store, dataset, range_exchanges(dataset), research_single_provider=True
            )
            for dataset in (core, nft)
        )

        agreement = ProviderAgreement(
            chain_id=core.chain_id,
            start_block=core.start_block,
            end_block=core.end_block,
            sources=("rpc-a", "rpc-b"),
            content_hash=verified_core.evidence.content_hash,
        )

        # Create valid peer
        peer = EvidenceRef(
            "canonical_block_range",
            verified_core.evidence.content_hash,
            verified_core.dataset.blocks[-1].ref,
            "rpc-b",
            schema_version=2,
            request_hash="a" * 64,
            raw_response_hash="b" * 64,
            interpretation_hash="c" * 64,
        )

        # Create forged primary evidence with Eastern Arabic digits
        # (U+0660-U+0669) instead of ASCII hex. Six full digit runs plus two
        # digits is 64 Unicode digits, none of which is ASCII hex.
        arabic_digits = "".join(chr(0x0660 + i) for i in range(10))
        unicode_hex_string = arabic_digits * 6 + arabic_digits[:2]  # 64 Eastern Arabic digits
        wrong_primary = object.__new__(EvidenceRef)
        object.__setattr__(wrong_primary, "kind", "canonical_block_range")
        object.__setattr__(wrong_primary, "content_hash", verified_core.evidence.content_hash)
        object.__setattr__(wrong_primary, "block", verified_core.dataset.blocks[-1].ref)
        object.__setattr__(wrong_primary, "source", "rpc-a")
        object.__setattr__(wrong_primary, "retrieved_at", verified_core.evidence.retrieved_at)
        object.__setattr__(wrong_primary, "request_hash", unicode_hex_string)
        object.__setattr__(wrong_primary, "raw_response_hash", "b" * 64)
        object.__setattr__(wrong_primary, "interpretation_hash", "c" * 64)
        object.__setattr__(wrong_primary, "schema_version", 2)

        forged = object.__new__(type(verified_core))
        object.__setattr__(forged, "dataset", verified_core.dataset)
        object.__setattr__(forged, "evidence", wrong_primary)
        object.__setattr__(forged, "trust", IngestionTrust.PROVIDER_QUORUM)
        object.__setattr__(forged, "agreement", agreement)
        object.__setattr__(forged, "peer_evidence", (peer,))

        with pytest.raises(ValueError, match=r"request_hash|ASCII hex"):
            replay_position_datasets(
                opening, closing, pool_manager=CORE, core=forged, ownership=verified_nft
            )
    finally:
        store.close()


def test_accepts_valid_provider_quorum_evidence(tmp_path: Path) -> None:
    """Ensure replay accepts legitimate PROVIDER_QUORUM evidence with exact binding.

    Tests that valid evidence with correct chain_id, content_hash, sources, and
    schema binds correctly at replay boundary, proving positive acceptance path.
    """
    opening, closing, core, nft = scenario()
    store = EvidenceStore(tmp_path / "evidence.db")
    try:
        verified_core, verified_nft = tuple(
            persist_and_verify_block_range(
                store, dataset, range_exchanges(dataset), research_single_provider=True
            )
            for dataset in (core, nft)
        )

        # Build legitimate PROVIDER_QUORUM agreement for both core and ownership datasets
        core_agreement = ProviderAgreement(
            chain_id=verified_core.dataset.chain_id,
            start_block=verified_core.dataset.start_block,
            end_block=verified_core.dataset.end_block,
            sources=("rpc-a", "rpc-b"),
            content_hash=verified_core.evidence.content_hash,
        )

        nft_agreement = ProviderAgreement(
            chain_id=verified_nft.dataset.chain_id,
            start_block=verified_nft.dataset.start_block,
            end_block=verified_nft.dataset.end_block,
            sources=("rpc-a", "rpc-b"),
            content_hash=verified_nft.evidence.content_hash,
        )

        # Create valid peer evidence for both datasets
        core_peer = EvidenceRef(
            "canonical_block_range",
            verified_core.evidence.content_hash,
            verified_core.dataset.blocks[-1].ref,
            "rpc-b",
            schema_version=2,
            request_hash="a" * 64,
            raw_response_hash="b" * 64,
            interpretation_hash="c" * 64,
        )

        nft_peer = EvidenceRef(
            "canonical_block_range",
            verified_nft.evidence.content_hash,
            verified_nft.dataset.blocks[-1].ref,
            "rpc-b",
            schema_version=2,
            request_hash="d" * 64,
            raw_response_hash="e" * 64,
            interpretation_hash="f" * 64,
        )

        # Create legitimate PROVIDER_QUORUM verified ranges with agreements and peers
        quorum_core = object.__new__(type(verified_core))
        object.__setattr__(quorum_core, "dataset", verified_core.dataset)
        object.__setattr__(quorum_core, "evidence", verified_core.evidence)
        object.__setattr__(quorum_core, "trust", IngestionTrust.PROVIDER_QUORUM)
        object.__setattr__(quorum_core, "agreement", core_agreement)
        object.__setattr__(quorum_core, "peer_evidence", (core_peer,))

        quorum_nft = object.__new__(type(verified_nft))
        object.__setattr__(quorum_nft, "dataset", verified_nft.dataset)
        object.__setattr__(quorum_nft, "evidence", verified_nft.evidence)
        object.__setattr__(quorum_nft, "trust", IngestionTrust.PROVIDER_QUORUM)
        object.__setattr__(quorum_nft, "agreement", nft_agreement)
        object.__setattr__(quorum_nft, "peer_evidence", (nft_peer,))

        # Should accept valid PROVIDER_QUORUM evidence without raising
        result = replay_position_datasets(
            opening, closing, pool_manager=CORE, core=quorum_core, ownership=quorum_nft
        )
        # Verify ledger was built successfully with PROVIDER_QUORUM trust
        assert result.trust is IngestionTrust.PROVIDER_QUORUM
        assert result.ledger.closing.liquidity == 120
        assert result.ledger.entries[0].observation.liquidity_delta == 20
    finally:
        store.close()


def test_rejects_provider_agreement_with_duplicate_sources(tmp_path: Path) -> None:
    """Ensure replay rejects ProviderAgreement with duplicate sources (source invariant).

    Tests that forged duplicate provider sources are rejected at replay boundary
    even if the actual peer evidence tuple comparison might pass.
    """
    opening, closing, core, nft = scenario()
    store = EvidenceStore(tmp_path / "evidence.db")
    try:
        verified_core, verified_nft = tuple(
            persist_and_verify_block_range(
                store, dataset, range_exchanges(dataset), research_single_provider=True
            )
            for dataset in (core, nft)
        )

        # Create forged agreement with duplicate sources using object.__new__
        # to bypass construction-time validation
        forged_agreement = object.__new__(ProviderAgreement)
        object.__setattr__(forged_agreement, "chain_id", verified_core.dataset.chain_id)
        object.__setattr__(forged_agreement, "start_block", verified_core.dataset.start_block)
        object.__setattr__(forged_agreement, "end_block", verified_core.dataset.end_block)
        object.__setattr__(
            forged_agreement, "sources", ("rpc-a", "rpc-a")
        )  # Duplicate (violates unique constraint)
        object.__setattr__(forged_agreement, "content_hash", verified_core.evidence.content_hash)

        # Create valid peer evidence
        peer = EvidenceRef(
            "canonical_block_range",
            verified_core.evidence.content_hash,
            verified_core.dataset.blocks[-1].ref,
            "rpc-b",
            schema_version=2,
            request_hash="a" * 64,
            raw_response_hash="b" * 64,
            interpretation_hash="c" * 64,
        )

        # Create forged PROVIDER_QUORUM verified range with duplicate-source agreement
        forged_core = object.__new__(type(verified_core))
        object.__setattr__(forged_core, "dataset", verified_core.dataset)
        object.__setattr__(forged_core, "evidence", verified_core.evidence)
        object.__setattr__(forged_core, "trust", IngestionTrust.PROVIDER_QUORUM)
        object.__setattr__(forged_core, "agreement", forged_agreement)
        object.__setattr__(forged_core, "peer_evidence", (peer,))

        # Should reject forged duplicate sources at replay boundary
        with pytest.raises(ValueError, match=r"sources must be unique|duplicate"):
            replay_position_datasets(
                opening, closing, pool_manager=CORE, core=forged_core, ownership=verified_nft
            )
    finally:
        store.close()
