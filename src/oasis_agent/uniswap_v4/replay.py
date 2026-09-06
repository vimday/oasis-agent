from __future__ import annotations

from dataclasses import dataclass, replace

from ..models.chain import BlockSnapshot, LogRecord
from ..models.common import BlockRef, normalize_hash32
from ..models.v4 import (
    DYNAMIC_FEE_FLAG,
    ModifyLiquidityObservation,
    PoolInitialized,
    SwapObservation,
)
from .fees import validate_lp_fee, validate_pool_fee
from .liquidity_math import MAX_SQRT_PRICE, MIN_SQRT_PRICE, sqrt_price_at_tick
from .pool_id import pool_id_from_key

MIN_TICK = -887_272
MAX_TICK = 887_272
MAX_SQRT_PRICE_X96 = (1 << 160) - 1
MAX_LIQUIDITY = (1 << 128) - 1


def _validate_event_position(transaction_index: int, log_index: int) -> None:
    for name, value in (("transaction_index", transaction_index), ("log_index", log_index)):
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"{name} must be an integer")
        if value < 0:
            raise ValueError(f"{name} must be nonnegative")


def _validate_pool_state_values(sqrt_price_x96: int, tick: int, liquidity: int) -> None:
    if not 0 < sqrt_price_x96 <= MAX_SQRT_PRICE_X96:
        raise ValueError("sqrt_price_x96 is outside uint160 state bounds")
    if not MIN_TICK <= tick <= MAX_TICK:
        raise ValueError("tick is outside canonical TickMath bounds")
    if not 0 <= liquidity <= MAX_LIQUIDITY:
        raise ValueError("liquidity is outside uint128 bounds")


@dataclass(frozen=True, slots=True)
class PoolInitializationEvent:
    block: BlockRef
    transaction_hash: str
    transaction_index: int
    log_index: int
    observation: PoolInitialized

    def __post_init__(self) -> None:
        if self.block.timestamp is None:
            raise ValueError("initialization event requires a timestamped block")
        object.__setattr__(
            self, "transaction_hash", normalize_hash32(self.transaction_hash, "transaction hash")
        )
        _validate_event_position(self.transaction_index, self.log_index)
        if self.observation.block_number != self.block.number:
            raise ValueError("initialization event block does not match its observation")
        pool_id = normalize_hash32(self.observation.pool_id, "pool id")
        if pool_id != pool_id_from_key(self.observation.key):
            raise ValueError("initialization PoolId does not match its PoolKey")
        validate_pool_fee(self.observation.key.fee)
        _validate_pool_state_values(
            self.observation.sqrt_price_x96,
            self.observation.tick,
            0,
        )
        if not MIN_SQRT_PRICE <= self.observation.sqrt_price_x96 <= MAX_SQRT_PRICE:
            raise ValueError("initial sqrt price is outside canonical TickMath bounds")
        lower = sqrt_price_at_tick(self.observation.tick)
        if self.observation.tick == MAX_TICK:
            if self.observation.sqrt_price_x96 != lower:
                raise ValueError("initial sqrt price is inconsistent with the emitted tick")
        elif (
            not lower
            <= self.observation.sqrt_price_x96
            < sqrt_price_at_tick(self.observation.tick + 1)
        ):
            raise ValueError("initial sqrt price is inconsistent with the emitted tick")


@dataclass(frozen=True, slots=True)
class SwapReplayEvent:
    block: BlockRef
    transaction_hash: str
    transaction_index: int
    log_index: int
    observation: SwapObservation

    def __post_init__(self) -> None:
        if self.block.timestamp is None:
            raise ValueError("swap event requires a timestamped block")
        object.__setattr__(
            self, "transaction_hash", normalize_hash32(self.transaction_hash, "transaction hash")
        )
        _validate_event_position(self.transaction_index, self.log_index)
        normalize_hash32(self.observation.pool_id, "pool id")
        if self.observation.block_number != self.block.number:
            raise ValueError("swap event block does not match its observation")
        if (
            self.observation.amount0 != 0
            and self.observation.amount1 != 0
            and (self.observation.amount0 > 0) == (self.observation.amount1 > 0)
        ):
            raise ValueError("swap deltas must have opposite signs")
        _validate_pool_state_values(
            self.observation.sqrt_price_x96,
            self.observation.tick,
            self.observation.liquidity,
        )
        if self.observation.fee is None:
            raise ValueError("swap replay requires the effective fee")
        validate_lp_fee(self.observation.fee)

    @property
    def position(self) -> tuple[int, int, int]:
        return self.block.number, self.transaction_index, self.log_index


@dataclass(frozen=True, slots=True)
class LiquidityReplayEvent:
    block: BlockRef
    transaction_hash: str
    transaction_index: int
    log_index: int
    observation: ModifyLiquidityObservation

    def __post_init__(self) -> None:
        if self.block.timestamp is None:
            raise ValueError("liquidity event requires a timestamped block")
        object.__setattr__(
            self, "transaction_hash", normalize_hash32(self.transaction_hash, "transaction hash")
        )
        _validate_event_position(self.transaction_index, self.log_index)
        if self.observation.block_number != self.block.number:
            raise ValueError("liquidity event block does not match its observation")

    @property
    def position(self) -> tuple[int, int, int]:
        return self.block.number, self.transaction_index, self.log_index


@dataclass(frozen=True, slots=True)
class PoolReplayState:
    chain_id: int
    pool_id: str
    block: BlockRef
    sqrt_price_x96: int
    tick: int
    liquidity: int
    configured_pool_fee: int
    dynamic_fee: bool
    last_fee: int | None
    last_transaction_index: int
    last_log_index: int
    swap_count: int = 0
    filled_swap_count: int = 0
    no_fill_swap_count: int = 0
    cumulative_amount0: int = 0
    cumulative_amount1: int = 0
    liquidity_event_count: int = 0


@dataclass(frozen=True, slots=True)
class SwapReplayStep:
    block: BlockRef
    transaction_hash: str
    transaction_index: int
    log_index: int
    amount0: int
    amount1: int
    fee: int
    previous_sqrt_price_x96: int
    sqrt_price_x96: int
    previous_price_x192: int
    price_x192: int
    price_change_x192: int
    previous_tick: int
    tick: int
    tick_change: int
    previous_liquidity: int
    liquidity: int
    liquidity_change: int


@dataclass(frozen=True, slots=True)
class LiquidityReplayStep:
    block: BlockRef
    transaction_hash: str
    transaction_index: int
    log_index: int
    tick_lower: int
    tick_upper: int
    liquidity_delta: int
    active_at_current_tick: bool
    previous_liquidity: int
    liquidity: int


@dataclass(frozen=True, slots=True)
class PoolAccountingReplay:
    initial_state: PoolReplayState
    final_state: PoolReplayState
    steps: tuple[SwapReplayStep, ...]
    processed_blocks: int
    liquidity_steps: tuple[LiquidityReplayStep, ...] = ()


def validate_block_sequence(blocks: tuple[BlockSnapshot, ...]) -> None:
    if not blocks:
        raise ValueError("block replay requires at least one snapshot")
    previous = blocks[0]
    for current in blocks[1:]:
        if current.ref.chain_id != previous.ref.chain_id:
            raise ValueError("block sequence cannot cross chains")
        if current.ref.number != previous.ref.number + 1:
            raise ValueError("block sequence must be contiguous")
        if current.parent_hash != previous.ref.block_hash:
            raise ValueError("block parent hash does not match prior snapshot")
        assert current.ref.timestamp is not None
        assert previous.ref.timestamp is not None
        if current.ref.timestamp < previous.ref.timestamp:
            raise ValueError("block timestamps must be nondecreasing")
        previous = current


def seed_pool_state(event: PoolInitializationEvent) -> PoolReplayState:
    return PoolReplayState(
        chain_id=event.block.chain_id,
        pool_id=normalize_hash32(event.observation.pool_id, "pool id"),
        block=event.block,
        sqrt_price_x96=event.observation.sqrt_price_x96,
        tick=event.observation.tick,
        liquidity=0,
        configured_pool_fee=event.observation.key.fee,
        dynamic_fee=event.observation.key.fee == DYNAMIC_FEE_FLAG,
        last_fee=None,
        last_transaction_index=event.transaction_index,
        last_log_index=event.log_index,
    )


def bind_swap_observation(
    block: BlockRef, log: LogRecord, observation: SwapObservation
) -> SwapReplayEvent:
    if log.block_hash is None or log.transaction_index is None:
        raise ValueError("swap log lacks block hash or transaction index")
    if log.block_number != block.number or log.block_hash != block.block_hash:
        raise ValueError("swap log does not match the pinned block")
    return SwapReplayEvent(
        block,
        log.transaction_hash,
        log.transaction_index,
        log.log_index,
        observation,
    )


def bind_pool_initialization(
    block: BlockRef,
    log: LogRecord,
    observation: PoolInitialized,
) -> PoolInitializationEvent:
    if log.block_hash is None or log.transaction_index is None:
        raise ValueError("initialization log lacks block hash or transaction index")
    if log.block_number != block.number or log.block_hash != block.block_hash:
        raise ValueError("initialization log does not match the pinned block")
    return PoolInitializationEvent(
        block,
        log.transaction_hash,
        log.transaction_index,
        log.log_index,
        observation,
    )


def bind_liquidity_observation(
    block: BlockRef,
    log: LogRecord,
    observation: ModifyLiquidityObservation,
) -> LiquidityReplayEvent:
    if log.block_hash is None or log.transaction_index is None:
        raise ValueError("liquidity log lacks block hash or transaction index")
    if log.block_number != block.number or log.block_hash != block.block_hash:
        raise ValueError("liquidity log does not match the pinned block")
    return LiquidityReplayEvent(
        block,
        log.transaction_hash,
        log.transaction_index,
        log.log_index,
        observation,
    )


def reduce_swap(
    state: PoolReplayState, event: SwapReplayEvent
) -> tuple[PoolReplayState, SwapReplayStep]:
    if event.block != state.block:
        raise ValueError("swap reducer requires state pinned to the event block")
    if event.block.chain_id != state.chain_id:
        raise ValueError("swap event belongs to a different chain")
    if normalize_hash32(event.observation.pool_id, "pool id") != state.pool_id:
        raise ValueError("swap event belongs to a different pool")
    if (event.transaction_index, event.log_index) <= (
        state.last_transaction_index,
        state.last_log_index,
    ):
        raise ValueError("swap violates strict event order")

    observation = event.observation
    assert observation.fee is not None
    no_fill = observation.amount0 == 0 and observation.amount1 == 0
    if not state.dynamic_fee and observation.fee != state.configured_pool_fee:
        raise ValueError("static-fee pool emitted a different effective LP fee")
    next_state = replace(
        state,
        sqrt_price_x96=observation.sqrt_price_x96,
        tick=observation.tick,
        liquidity=observation.liquidity,
        last_fee=observation.fee,
        last_transaction_index=event.transaction_index,
        last_log_index=event.log_index,
        swap_count=state.swap_count + 1,
        filled_swap_count=state.filled_swap_count + (0 if no_fill else 1),
        no_fill_swap_count=state.no_fill_swap_count + (1 if no_fill else 0),
        cumulative_amount0=state.cumulative_amount0 + observation.amount0,
        cumulative_amount1=state.cumulative_amount1 + observation.amount1,
    )
    step = SwapReplayStep(
        block=event.block,
        transaction_hash=event.transaction_hash,
        transaction_index=event.transaction_index,
        log_index=event.log_index,
        amount0=observation.amount0,
        amount1=observation.amount1,
        fee=observation.fee,
        previous_sqrt_price_x96=state.sqrt_price_x96,
        sqrt_price_x96=observation.sqrt_price_x96,
        previous_price_x192=state.sqrt_price_x96**2,
        price_x192=observation.sqrt_price_x96**2,
        price_change_x192=observation.sqrt_price_x96**2 - state.sqrt_price_x96**2,
        previous_tick=state.tick,
        tick=observation.tick,
        tick_change=observation.tick - state.tick,
        previous_liquidity=state.liquidity,
        liquidity=observation.liquidity,
        liquidity_change=observation.liquidity - state.liquidity,
    )
    return next_state, step


def reduce_liquidity(
    state: PoolReplayState, event: LiquidityReplayEvent
) -> tuple[PoolReplayState, LiquidityReplayStep]:
    if event.block != state.block:
        raise ValueError("liquidity reducer requires state pinned to the event block")
    if event.block.chain_id != state.chain_id:
        raise ValueError("liquidity event belongs to a different chain")
    observation = event.observation
    if observation.pool_id != state.pool_id:
        raise ValueError("liquidity event belongs to a different pool")
    if (event.transaction_index, event.log_index) <= (
        state.last_transaction_index,
        state.last_log_index,
    ):
        raise ValueError("liquidity event violates strict event order")
    active = observation.tick_lower <= state.tick < observation.tick_upper
    next_liquidity = state.liquidity + observation.liquidity_delta if active else state.liquidity
    if not 0 <= next_liquidity <= MAX_LIQUIDITY:
        raise ValueError("active liquidity underflow or overflow during replay")
    next_state = replace(
        state,
        liquidity=next_liquidity,
        last_transaction_index=event.transaction_index,
        last_log_index=event.log_index,
        liquidity_event_count=state.liquidity_event_count + 1,
    )
    return next_state, LiquidityReplayStep(
        event.block,
        event.transaction_hash,
        event.transaction_index,
        event.log_index,
        observation.tick_lower,
        observation.tick_upper,
        observation.liquidity_delta,
        active,
        state.liquidity,
        next_liquidity,
    )


def replay_swaps(
    initialization: PoolInitializationEvent,
    blocks: tuple[BlockSnapshot, ...],
    events: tuple[SwapReplayEvent, ...],
) -> PoolAccountingReplay:
    return replay_pool(initialization, blocks, events, ())


def replay_pool(
    initialization: PoolInitializationEvent,
    blocks: tuple[BlockSnapshot, ...],
    swap_events: tuple[SwapReplayEvent, ...],
    liquidity_events: tuple[LiquidityReplayEvent, ...],
) -> PoolAccountingReplay:
    validate_block_sequence(blocks)
    if blocks[0].ref != initialization.block:
        raise ValueError("replay must start at the pinned initialization block")

    combined: tuple[SwapReplayEvent | LiquidityReplayEvent, ...] = tuple(
        sorted((*swap_events, *liquidity_events), key=lambda event: event.position)
    )
    positions = tuple(event.position for event in combined)
    if positions != tuple(sorted(positions)) or len(set(positions)) != len(positions):
        raise ValueError("pool events must occupy unique positions in strict event order")
    if tuple(event.position for event in swap_events) != tuple(
        sorted(event.position for event in swap_events)
    ) or tuple(event.position for event in liquidity_events) != tuple(
        sorted(event.position for event in liquidity_events)
    ):
        raise ValueError("event streams must be supplied in strict event order")

    pinned_blocks = {snapshot.ref.number: snapshot.ref for snapshot in blocks}
    events_by_block: dict[int, list[SwapReplayEvent | LiquidityReplayEvent]] = {}
    for event in combined:
        pinned = pinned_blocks.get(event.block.number)
        if pinned is None or pinned != event.block:
            raise ValueError("pool event is not bound to a pinned block snapshot")
        events_by_block.setdefault(event.block.number, []).append(event)

    initial_state = seed_pool_state(initialization)
    state = initial_state
    steps: list[SwapReplayStep] = []
    liquidity_steps: list[LiquidityReplayStep] = []
    for index, snapshot in enumerate(blocks):
        if index:
            state = replace(
                state,
                block=snapshot.ref,
                last_transaction_index=-1,
                last_log_index=-1,
            )
        for event in events_by_block.get(snapshot.ref.number, []):
            if isinstance(event, SwapReplayEvent):
                state, step = reduce_swap(state, event)
                steps.append(step)
            else:
                state, liquidity_step = reduce_liquidity(state, event)
                liquidity_steps.append(liquidity_step)

    return PoolAccountingReplay(
        initial_state,
        state,
        tuple(steps),
        len(blocks),
        tuple(liquidity_steps),
    )


__all__ = [
    "LiquidityReplayEvent",
    "LiquidityReplayStep",
    "PoolAccountingReplay",
    "PoolInitializationEvent",
    "PoolReplayState",
    "SwapReplayEvent",
    "SwapReplayStep",
    "bind_liquidity_observation",
    "bind_pool_initialization",
    "bind_swap_observation",
    "reduce_liquidity",
    "reduce_swap",
    "replay_pool",
    "replay_swaps",
    "seed_pool_state",
    "validate_block_sequence",
]
