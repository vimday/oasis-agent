"""Replayable contiguous market chunks, retaining original RPC proofs per chunk.

Archive chunks are historical research inputs, never fresh execution snapshots.
No merged RPC request or response is fabricated to bridge adjacent chunks.
"""

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Annotated

from pydantic import Field, model_validator

from ..evidence.exact_capture import RecordedSession, validate_session_pair
from ..evidence.ingestion import replay_block_range_evidence
from ..evidence.rpc_capture import persist_block_range_evidence
from ..evidence.store import EvidenceStore
from ..launch.models import StrictModel
from ..models.chain import BlockSnapshot, LogRecord
from ..models.common import EvidenceRef, canonical_json
from ..rpc.client import RpcClient
from ..rpc.collector import BlockRangeDataset, collect_block_range, require_provider_agreement
from ..uniswap_v4.events import SWAP_TOPIC
from ..uniswap_v4.pool_id import pool_id_from_key
from ..uniswap_v4.state_rpc import Slot0State
from .market_window import MarketWindow, _derive_market_window
from .observer_config import Config
from .pool_state_capture import (
    Slot0WindowCapture,
    Slot0WindowQuorum,
    collect_slot0_window,
    persist_slot0_capture,
    replay_slot0_window_evidence,
)


class ArchiveConfig(StrictModel):
    observer: Config
    start_block: Annotated[int, Field(ge=0, lt=2**64)]
    end_block: Annotated[int, Field(ge=0, lt=2**64)]
    max_requests_per_provider: Annotated[int, Field(ge=1, le=100_000)]

    @model_validator(mode="after")
    def bounded(self) -> "ArchiveConfig":
        if self.observer.market is None:
            raise ValueError("archive requires a pool and chunk size")
        if not 1 <= self.end_block - self.start_block + 1 <= 1000:
            raise ValueError("archive range must be 1..1000 blocks")
        if pool_id_from_key(self.observer.market.pool) != self.observer.pool_id:
            raise ValueError("archive PoolKey mismatch")
        return self

    @property
    def ranges(self) -> tuple[tuple[int, int], ...]:
        assert self.observer.market is not None
        size = self.observer.market.blocks
        return tuple(
            (start, min(self.end_block, start + size - 1))
            for start in range(self.start_block, self.end_block + 1, size)
        )

    @staticmethod
    def chunk_requests(start: int, end: int) -> int:
        """Per-provider requests one successful ``capture_chunk`` spends on a range."""
        return 7 * (end - start + 1) + 4

    def requests_to_finish(self, completed: int) -> int:
        """Per-provider minimum for the chunks after ``completed``, assuming no failures."""
        return sum(self.chunk_requests(start, end) for start, end in self.ranges[completed:])


@dataclass(frozen=True, slots=True)
class MarketChunk:
    start_block: int
    end_block: int
    swaps: tuple[EvidenceRef, EvidenceRef]
    prices: tuple[EvidenceRef, EvidenceRef]

    @property
    def payload(self) -> bytes:
        return canonical_json(asdict(self))


def replay_chunk(
    store: EvidenceStore, chunk: MarketChunk, config: ArchiveConfig
) -> tuple[BlockRangeDataset, tuple[Slot0WindowCapture, Slot0WindowCapture]]:
    observer = config.observer
    datasets = tuple(replay_block_range_evidence(store, ref) for ref in chunk.swaps)
    require_provider_agreement(datasets)
    captures = tuple(replay_slot0_window_evidence(store, ref) for ref in chunk.prices)
    Slot0WindowQuorum(*captures)
    data = datasets[0]
    if {item.source for item in datasets} != {item.source for item in captures}:
        raise ValueError("archive price and activity providers differ")
    if {item.source for item in datasets} != {"public", "paid"}:
        raise ValueError("archive requires its declared provider pair")
    if (data.chain_id, data.start_block, data.end_block, data.address, data.topics) != (
        observer.chain_id,
        chunk.start_block,
        chunk.end_block,
        observer.pool_manager,
        (SWAP_TOPIC, observer.pool_id),
    ):
        raise ValueError("archive activity scope mismatch")
    for capture in captures:
        if (
            capture.headers != data.blocks
            or capture.pool_id != observer.pool_id
            or capture.state_view != observer.state_view
            or capture.pool_manager != observer.pool_manager
            or capture.state_view_runtime_keccak != observer.state_view_runtime_keccak
            or capture.pool_manager_runtime_keccak != observer.pool_manager_runtime_keccak
        ):
            raise ValueError("archive state scope mismatch")
    return data, (captures[0], captures[1])


def validate_archive(
    store: EvidenceStore, chunks: tuple[MarketChunk, ...], config: ArchiveConfig
) -> None:
    if len(chunks) > len(config.ranges):
        raise ValueError("archive has extra chunks")
    previous = None
    for chunk, bounds in zip(chunks, config.ranges, strict=False):
        if (chunk.start_block, chunk.end_block) != bounds:
            raise ValueError("archive chunks have gaps or were reordered")
        dataset, _ = replay_chunk(store, chunk, config)
        first, last = dataset.blocks[0], dataset.blocks[-1]
        if previous is not None and (
            first.parent_hash != previous.ref.block_hash
            or first.ref.timestamp is None
            or previous.ref.timestamp is None
            or first.ref.timestamp < previous.ref.timestamp
        ):
            raise ValueError("archive chunk ancestry changed")
        previous = last


def capture_chunk(
    store: EvidenceStore,
    config: ArchiveConfig,
    start: int,
    end: int,
    state_sessions: tuple[RecordedSession, RecordedSession],
    activity_sessions: tuple[RecordedSession, RecordedSession],
) -> MarketChunk:
    validate_session_pair(*state_sessions)
    validate_session_pair(*activity_sessions)
    sessions = (*state_sessions, *activity_sessions)
    if len({id(item[0]) for item in sessions}) != 4 or len({id(item[1]) for item in sessions}) != 4:
        raise ValueError("archive requires four fresh sessions")
    if tuple(item[2] for item in state_sessions) != tuple(item[2] for item in activity_sessions):
        raise ValueError("archive session provider order differs")
    if (start, end) not in config.ranges:
        raise ValueError("chunk outside declared archive")
    observer = config.observer
    prices = []
    swaps = []
    for state_session, activity_session in zip(state_sessions, activity_sessions, strict=True):
        capture = collect_slot0_window(
            state_session,
            chain_id=observer.chain_id,
            state_view=observer.state_view,
            pool_manager=observer.pool_manager,
            pool_id=observer.pool_id,
            block_numbers=tuple(range(start, end + 1)),
            state_view_runtime_keccak=observer.state_view_runtime_keccak,
            pool_manager_runtime_keccak=observer.pool_manager_runtime_keccak,
        )
        prices.append(
            persist_slot0_capture(store, capture, retrieved_at=datetime.now(UTC).isoformat())
        )
        data = collect_block_range(
            RpcClient(activity_session[0], expected_chain_id=observer.chain_id),
            source=activity_session[2],
            start_block=start,
            end_block=end,
            address=observer.pool_manager,
            topics=(SWAP_TOPIC, observer.pool_id),
            max_blocks=end - start + 1,
        )
        swaps.append(persist_block_range_evidence(store, data, activity_session[1].exchanges))
    result = MarketChunk(start, end, (swaps[0], swaps[1]), (prices[0], prices[1]))
    replay_chunk(store, result, config)
    return result


def build_archive_window(
    store: EvidenceStore, chunks: tuple[MarketChunk, ...], config: ArchiveConfig
) -> MarketWindow:
    """Join verified contiguous chunks without manufacturing a whole-range RPC reply."""
    validate_archive(store, chunks, config)
    if len(chunks) != len(config.ranges):
        raise ValueError("archive range incomplete")
    blocks: list[BlockSnapshot] = []
    logs: list[LogRecord] = []
    observations: list[tuple[Slot0State, tuple[EvidenceRef, EvidenceRef]]] = []
    references: list[EvidenceRef] = []
    for chunk in chunks:
        dataset, captures = replay_chunk(store, chunk, config)
        blocks.extend(dataset.blocks)
        logs.extend(dataset.logs)
        observations.extend((state, chunk.prices) for state in captures[0].slot0)
        references.extend((*chunk.swaps, *chunk.prices))
    observer = config.observer
    assert observer.market is not None
    dataset = BlockRangeDataset(
        "verified-archive-composite",
        tuple(blocks),
        tuple(logs),
        observer.pool_manager,
        (SWAP_TOPIC, observer.pool_id),
    )
    return _derive_market_window(
        store,
        dataset=dataset,
        pool_id=observer.pool_id,
        observations=tuple(observations),
        input_references=tuple(references),
        horizon_seconds=observer.market.horizon_seconds,
        max_lateness_seconds=observer.market.max_lateness_seconds,
        mature_prefix=True,
    )
