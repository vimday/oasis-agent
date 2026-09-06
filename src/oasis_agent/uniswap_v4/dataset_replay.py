from __future__ import annotations

from dataclasses import dataclass

from ..models.chain import LogRecord
from ..models.common import normalize_address, normalize_hash32
from ..models.v4 import PoolInitialized
from ..rpc.collector import BlockRangeDataset
from .events import (
    MODIFY_LIQUIDITY_TOPIC,
    POOL_INITIALIZED_TOPIC,
    SWAP_TOPIC,
    decode_modify_liquidity,
    decode_pool_initialized,
    decode_swap,
)
from .replay import (
    LiquidityReplayEvent,
    PoolAccountingReplay,
    PoolInitializationEvent,
    SwapReplayEvent,
    bind_liquidity_observation,
    bind_pool_initialization,
    bind_swap_observation,
    replay_pool,
)


@dataclass(frozen=True, slots=True)
class DecodedPoolDataset:
    dataset_hash: str
    pool_manager: str
    pool_id: str
    initialization: PoolInitializationEvent
    swaps: tuple[SwapReplayEvent, ...]
    liquidity_events: tuple[LiquidityReplayEvent, ...]
    ignored_log_count: int


def _event_topic(log_topics: tuple[str, ...]) -> str | None:
    return log_topics[0] if log_topics else None


def decode_pool_dataset(
    dataset: BlockRangeDataset,
    *,
    pool_manager: str,
    pool_id: str | None = None,
) -> DecodedPoolDataset:
    """Decode one PoolManager stream into a single pool's replay inputs.

    The dataset must include that pool's Initialize event. Other official PoolManager
    event types and events belonging to other pools are counted but ignored.
    """

    manager = normalize_address(pool_manager)
    requested_pool = normalize_hash32(pool_id, "pool id") if pool_id is not None else None
    block_by_number = {snapshot.ref.number: snapshot.ref for snapshot in dataset.blocks}
    initializations: list[tuple[LogRecord, PoolInitialized]] = []
    ignored = 0

    for log in dataset.logs:
        if log.address != manager:
            raise ValueError("pool dataset contains a log from another contract")
        topic = _event_topic(log.topics)
        if topic == POOL_INITIALIZED_TOPIC:
            observation = decode_pool_initialized(log)
            initializations.append((log, observation))
        elif topic not in (SWAP_TOPIC, MODIFY_LIQUIDITY_TOPIC):
            ignored += 1

    if requested_pool is None:
        if len(initializations) != 1:
            raise ValueError("pool_id is required unless the dataset has exactly one Initialize")
        requested_pool = initializations[0][1].pool_id

    matching_initializations = [
        item for item in initializations if item[1].pool_id == requested_pool
    ]
    if len(matching_initializations) != 1:
        raise ValueError("dataset must contain exactly one Initialize for the requested pool")

    initialization_log, initialization_observation = matching_initializations[0]
    initialization_block = block_by_number[initialization_log.block_number]
    initialization = bind_pool_initialization(
        initialization_block,
        initialization_log,
        initialization_observation,
    )

    swaps: list[SwapReplayEvent] = []
    liquidity_events: list[LiquidityReplayEvent] = []
    for log in dataset.logs:
        topic = _event_topic(log.topics)
        block = block_by_number[log.block_number]
        if topic == SWAP_TOPIC:
            swap_observation = decode_swap(log)
            if swap_observation.pool_id == requested_pool:
                swaps.append(bind_swap_observation(block, log, swap_observation))
            else:
                ignored += 1
        elif topic == MODIFY_LIQUIDITY_TOPIC:
            liquidity_observation = decode_modify_liquidity(log)
            if liquidity_observation.pool_id == requested_pool:
                liquidity_events.append(
                    bind_liquidity_observation(block, log, liquidity_observation)
                )
            else:
                ignored += 1
        elif (
            topic == POOL_INITIALIZED_TOPIC
            and decode_pool_initialized(log).pool_id != requested_pool
        ):
            ignored += 1

    return DecodedPoolDataset(
        dataset.content_hash,
        manager,
        requested_pool,
        initialization,
        tuple(swaps),
        tuple(liquidity_events),
        ignored,
    )


def replay_pool_dataset(
    decoded: DecodedPoolDataset, dataset: BlockRangeDataset
) -> PoolAccountingReplay:
    if decoded.dataset_hash != dataset.content_hash:
        raise ValueError("decoded pool inputs do not belong to this dataset")
    canonical = decode_pool_dataset(
        dataset, pool_manager=decoded.pool_manager, pool_id=decoded.pool_id
    )
    if decoded != canonical:
        raise ValueError("decoded pool inputs do not match retained dataset logs")
    return replay_pool(
        decoded.initialization,
        dataset.blocks,
        decoded.swaps,
        decoded.liquidity_events,
    )


__all__ = ["DecodedPoolDataset", "decode_pool_dataset", "replay_pool_dataset"]
