"""Incremental EVM pool log state synchronizer with reorg rollback and fail-closed validation."""

from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Final

from eth_hash.auto import keccak

from ..models.common import normalize_hash32, normalize_hex
from ..uniswap_v4.liquidity_math import (
    MAX_SQRT_PRICE,
    MAX_TICK,
    MIN_SQRT_PRICE,
    MIN_TICK,
    sqrt_price_at_tick,
)
from .bridge import FastpathBridge, require_int
from .pool_graph import FastpathPoolGraph, PoolKind, PoolNode, TickLiquidity

_UINT112_MAX: Final[int] = (1 << 112) - 1
_UINT128_MAX: Final[int] = (1 << 128) - 1
_UINT160_MAX: Final[int] = (1 << 160) - 1
_UINT256_MAX: Final[int] = (1 << 256) - 1
_INT128_MIN: Final[int] = -(1 << 127)
_INT128_MAX: Final[int] = (1 << 127) - 1
_FEE_PIPS_DENOM: Final[int] = 1_000_000


def _topic0(signature: str) -> str:
    return "0x" + keccak(signature.encode("ascii")).hex()


V2_SYNC_TOPIC: Final[str] = _topic0("Sync(uint112,uint112)")
AERO_SYNC_TOPIC: Final[str] = _topic0("Sync(uint256,uint256)")
V3_SWAP_TOPIC: Final[str] = _topic0("Swap(address,address,int256,int256,uint160,uint128,int24)")
V3_MINT_TOPIC: Final[str] = _topic0("Mint(address,address,int24,int24,uint128,uint256,uint256)")
V3_BURN_TOPIC: Final[str] = _topic0("Burn(address,int24,int24,uint128,uint256,uint256)")
V4_SWAP_TOPIC: Final[str] = _topic0(
    "Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)"
)
V4_MODIFY_LIQUIDITY_TOPIC: Final[str] = _topic0(
    "ModifyLiquidity(bytes32,address,int24,int24,int256,bytes32)"
)


class StateSyncError(ValueError):
    """Raised when EVM pool log synchronization encounters invalid or out-of-window state."""


class ReorgDetectedError(StateSyncError):
    """Raised when incoming block metadata breaks chain continuity (reorg or gap)."""


@dataclass(frozen=True, slots=True)
class EvmPoolLogEvent:
    chain_id: int
    block_number: int
    block_hash: str
    parent_hash: str
    tx_index: int
    log_index: int
    pool_id: str
    topic0: str
    topics: tuple[str, ...]
    data_hex: str

    def __post_init__(self) -> None:
        try:
            chain_id = require_int(self.chain_id, "chain_id", non_negative=True)
            if chain_id <= 0:
                raise ValueError("chain_id must be positive")
            require_int(self.block_number, "block_number", non_negative=True)
            require_int(self.tx_index, "tx_index", non_negative=True)
            require_int(self.log_index, "log_index", non_negative=True)
        except (TypeError, ValueError) as exc:
            raise StateSyncError(str(exc)) from exc


@dataclass(frozen=True, slots=True)
class _BlockCheckpoint:
    block_number: int
    block_hash: str
    pools: dict[tuple[int, str], PoolNode]
    quarantined_pools: dict[str, str]


def _parse_hash32(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise StateSyncError(f"{name} must be a 32-byte hex string")
    try:
        return normalize_hash32(value, name)
    except ValueError as exc:
        raise StateSyncError(str(exc)) from exc


def _parse_pool_id(value: object) -> str:
    if not isinstance(value, str):
        raise StateSyncError("pool_id must be a hex string")
    try:
        normalized = normalize_hex(value)
    except ValueError as exc:
        raise StateSyncError(f"invalid pool_id hex: {exc}") from exc
    if len(normalized) not in (42, 66):
        raise StateSyncError(f"pool_id must be 20 or 32 bytes, got {normalized!r}")
    return normalized


def _decode_words(data_hex: object) -> list[int]:
    if not isinstance(data_hex, str):
        raise StateSyncError("data_hex must be a hex string")
    try:
        normalized = normalize_hex(data_hex)
    except ValueError as exc:
        raise StateSyncError(f"malformed data_hex: {exc}") from exc
    raw = bytes.fromhex(normalized[2:])
    if len(raw) % 32 != 0:
        raise StateSyncError("event data_hex is not 32-byte word aligned")
    return [int.from_bytes(raw[i : i + 32], "big") for i in range(0, len(raw), 32)]


def _decode_signed(word: int, bits: int, name: str) -> int:
    if not 0 <= word <= _UINT256_MAX:
        raise StateSyncError(f"{name} ABI word out of uint256 bounds")
    if bits == 256:
        sign_bit = 1 << 255
        return word - (1 << 256) if word & sign_bit else word
    mask = (1 << bits) - 1
    sign_bit = 1 << (bits - 1)
    lower = word & mask
    upper = word >> bits
    expected_upper = (1 << (256 - bits)) - 1 if lower & sign_bit else 0
    if upper != expected_upper:
        raise StateSyncError(f"{name} signed int{bits} word is not canonically sign-extended")
    return lower - (1 << bits) if lower & sign_bit else lower


def _decode_uint_bounded(word: int, max_val: int, name: str) -> int:
    if not 0 <= word <= max_val:
        raise StateSyncError(f"{name} out of bounds: {word}")
    return word


def _validate_topic_address(topic: str, name: str) -> str:
    raw = bytes.fromhex(topic[2:])
    if len(raw) != 32 or raw[:12] != b"\x00" * 12:
        raise StateSyncError(f"{name} indexed address is not canonically ABI-padded")
    return "0x" + raw[-20:].hex()


def _decode_topic_int24(topic: str, name: str) -> int:
    word = int.from_bytes(bytes.fromhex(topic[2:]), "big")
    tick = _decode_signed(word, 24, name)
    if not MIN_TICK <= tick <= MAX_TICK:
        raise StateSyncError(f"{name} tick {tick} out of canonical TickMath bounds")
    return tick


def _validate_cl_invariants(sqrt_price_x96: int, tick: int, liquidity: int) -> None:
    if not MIN_SQRT_PRICE <= sqrt_price_x96 <= MAX_SQRT_PRICE:
        raise StateSyncError("sqrtPriceX96 out of canonical TickMath bounds")
    if not MIN_TICK <= tick <= MAX_TICK:
        raise StateSyncError(f"tick {tick} out of canonical TickMath bounds")
    if not 0 <= liquidity <= _UINT128_MAX:
        raise StateSyncError("liquidity exceeds uint128")
    if sqrt_price_x96 < sqrt_price_at_tick(tick):
        raise StateSyncError("sqrtPriceX96 below sqrt_price_at_tick(tick)")
    if tick < MAX_TICK and sqrt_price_x96 > sqrt_price_at_tick(tick + 1):
        raise StateSyncError("sqrtPriceX96 > sqrt_price_at_tick(tick + 1)")


def _apply_liquidity_delta(
    pool: PoolNode,
    tick_lower: int,
    tick_upper: int,
    liquidity_delta: int,
    block_number: int,
) -> PoolNode:
    if tick_lower >= tick_upper:
        raise StateSyncError(f"invalid tick range [{tick_lower}, {tick_upper})")
    if pool.tick is None or pool.liquidity is None:
        raise StateSyncError(f"pool {pool.pool_id} is missing active tick/liquidity state")

    if liquidity_delta == 0:
        return replace(pool, block_number=block_number)

    new_liquidity = pool.liquidity
    if tick_lower <= pool.tick < tick_upper:
        new_liquidity += liquidity_delta
        if not 0 <= new_liquidity <= _UINT128_MAX:
            raise StateSyncError("active liquidity delta overflows or underflows uint128")

    tick_map = {entry.tick: entry.liquidity_net for entry in pool.ticks}
    lower_net = tick_map.get(tick_lower, 0) + liquidity_delta
    upper_net = tick_map.get(tick_upper, 0) - liquidity_delta
    if not (_INT128_MIN <= lower_net <= _INT128_MAX and _INT128_MIN <= upper_net <= _INT128_MAX):
        raise StateSyncError("tick liquidity_net exceeds int128 bounds")

    if lower_net == 0:
        tick_map.pop(tick_lower, None)
    else:
        tick_map[tick_lower] = lower_net
    if upper_net == 0:
        tick_map.pop(tick_upper, None)
    else:
        tick_map[tick_upper] = upper_net

    new_ticks = tuple(TickLiquidity(tick=k, liquidity_net=v) for k, v in sorted(tick_map.items()))
    return replace(
        pool,
        liquidity=new_liquidity,
        ticks=new_ticks,
        block_number=block_number,
    )


def _apply_event_to_pool(
    pool: PoolNode,
    topic0: str,
    topics: tuple[str, ...],
    data_hex: str,
    block_number: int,
) -> PoolNode | None:
    words = _decode_words(data_hex)

    if topic0 == V2_SYNC_TOPIC:
        if pool.kind != PoolKind.V2:
            raise StateSyncError(f"V2 Sync event incompatible with pool kind {pool.kind}")
        if len(topics) != 1 or len(words) != 2:
            raise StateSyncError("V2 Sync requires 1 topic and 2 data words")
        r0 = _decode_uint_bounded(words[0], _UINT112_MAX, "reserve0")
        r1 = _decode_uint_bounded(words[1], _UINT112_MAX, "reserve1")
        return replace(pool, reserve0=r0, reserve1=r1, block_number=block_number)

    if topic0 == AERO_SYNC_TOPIC:
        if pool.kind not in (PoolKind.V2, PoolKind.AERODROME_STABLE):
            raise StateSyncError(f"Aerodrome Sync event incompatible with pool kind {pool.kind}")
        if len(topics) != 1 or len(words) != 2:
            raise StateSyncError("Aerodrome Sync requires 1 topic and 2 data words")
        r0 = _decode_uint_bounded(words[0], _UINT256_MAX, "reserve0")
        r1 = _decode_uint_bounded(words[1], _UINT256_MAX, "reserve1")
        return replace(pool, reserve0=r0, reserve1=r1, block_number=block_number)

    if topic0 == V3_SWAP_TOPIC:
        if pool.kind not in (PoolKind.V3, PoolKind.SLIPSTREAM):
            raise StateSyncError(f"V3 Swap event incompatible with pool kind {pool.kind}")
        if len(topics) != 3 or len(words) != 5:
            raise StateSyncError("V3 Swap requires 3 topics and 5 data words")
        _validate_topic_address(topics[1], "sender")
        _validate_topic_address(topics[2], "recipient")
        _decode_signed(words[0], 256, "amount0")
        _decode_signed(words[1], 256, "amount1")
        sqrt_price_x96 = _decode_uint_bounded(words[2], _UINT160_MAX, "sqrtPriceX96")
        liquidity = _decode_uint_bounded(words[3], _UINT128_MAX, "liquidity")
        tick = _decode_signed(words[4], 24, "tick")
        _validate_cl_invariants(sqrt_price_x96, tick, liquidity)
        return replace(
            pool,
            sqrt_price_x96=sqrt_price_x96,
            liquidity=liquidity,
            tick=tick,
            block_number=block_number,
        )

    if topic0 == V3_MINT_TOPIC:
        if pool.kind not in (PoolKind.V3, PoolKind.SLIPSTREAM):
            raise StateSyncError(f"V3 Mint event incompatible with pool kind {pool.kind}")
        if len(topics) != 4 or len(words) not in (3, 4):
            raise StateSyncError("V3 Mint requires 4 topics and canonical ABI words")
        _validate_topic_address(topics[1], "owner")
        tick_lower = _decode_topic_int24(topics[2], "tickLower")
        tick_upper = _decode_topic_int24(topics[3], "tickUpper")
        if len(words) == 4:
            _decode_uint_bounded(words[0], _UINT160_MAX, "sender")
            amount = _decode_uint_bounded(words[1], _UINT128_MAX, "amount")
        else:
            amount = _decode_uint_bounded(words[0], _UINT128_MAX, "amount")
        return _apply_liquidity_delta(pool, tick_lower, tick_upper, amount, block_number)

    if topic0 == V3_BURN_TOPIC:
        if pool.kind not in (PoolKind.V3, PoolKind.SLIPSTREAM):
            raise StateSyncError(f"V3 Burn event incompatible with pool kind {pool.kind}")
        if len(topics) != 4 or len(words) != 3:
            raise StateSyncError("V3 Burn requires 4 topics and 3 data words")
        _validate_topic_address(topics[1], "owner")
        tick_lower = _decode_topic_int24(topics[2], "tickLower")
        tick_upper = _decode_topic_int24(topics[3], "tickUpper")
        amount = _decode_uint_bounded(words[0], _UINT128_MAX, "amount")
        return _apply_liquidity_delta(pool, tick_lower, tick_upper, -amount, block_number)

    if topic0 == V4_SWAP_TOPIC:
        if pool.kind != PoolKind.V4:
            raise StateSyncError(f"V4 Swap event incompatible with pool kind {pool.kind}")
        if len(topics) != 3 or len(words) != 6:
            raise StateSyncError("V4 Swap requires 3 topics and 6 data words")
        _validate_topic_address(topics[2], "sender")
        _decode_signed(words[0], 128, "amount0")
        _decode_signed(words[1], 128, "amount1")
        sqrt_price_x96 = _decode_uint_bounded(words[2], _UINT160_MAX, "sqrtPriceX96")
        liquidity = _decode_uint_bounded(words[3], _UINT128_MAX, "liquidity")
        tick = _decode_signed(words[4], 24, "tick")
        fee_pips = _decode_uint_bounded(words[5], _FEE_PIPS_DENOM - 1, "fee")
        _validate_cl_invariants(sqrt_price_x96, tick, liquidity)
        return replace(
            pool,
            sqrt_price_x96=sqrt_price_x96,
            liquidity=liquidity,
            tick=tick,
            fee_pips=fee_pips,
            block_number=block_number,
        )

    if topic0 == V4_MODIFY_LIQUIDITY_TOPIC:
        if pool.kind != PoolKind.V4:
            raise StateSyncError(
                f"V4 ModifyLiquidity event incompatible with pool kind {pool.kind}"
            )
        if len(topics) != 3 or len(words) != 4:
            raise StateSyncError("V4 ModifyLiquidity requires 3 topics and 4 data words")
        _validate_topic_address(topics[2], "sender")
        tick_lower = _decode_signed(words[0], 24, "tickLower")
        tick_upper = _decode_signed(words[1], 24, "tickUpper")
        if not (MIN_TICK <= tick_lower <= MAX_TICK and MIN_TICK <= tick_upper <= MAX_TICK):
            raise StateSyncError("ModifyLiquidity ticks out of canonical TickMath bounds")
        liquidity_delta = _decode_signed(words[2], 256, "liquidityDelta")
        return _apply_liquidity_delta(pool, tick_lower, tick_upper, liquidity_delta, block_number)

    return None


class IncrementalPoolStateSynchronizer:
    """Block-contiguous EVM log synchronizer with bounded copy-on-write reorg rollback."""

    def __init__(
        self,
        initial_pools: Sequence[PoolNode] = (),
        *,
        head_block_number: int | None = None,
        head_block_hash: str | None = None,
        max_rollback_blocks: int = 64,
    ) -> None:
        try:
            checked_max = require_int(
                max_rollback_blocks,
                "max_rollback_blocks",
                non_negative=True,
            )
        except (TypeError, ValueError) as exc:
            raise StateSyncError(str(exc)) from exc
        if checked_max <= 0:
            raise StateSyncError("max_rollback_blocks must be >= 1")
        if (head_block_number is None) != (head_block_hash is None):
            raise StateSyncError("head_block_number and head_block_hash must both be set or None")

        self._max_rollback_blocks = checked_max
        self._pools: dict[tuple[int, str], PoolNode] = {}
        self._quarantined_pools: dict[str, str] = {}
        self._checkpoints: OrderedDict[int, _BlockCheckpoint] = OrderedDict()

        for pool in initial_pools:
            self._pools[(pool.chain_id, _parse_pool_id(pool.pool_id))] = pool

        if head_block_number is not None and head_block_hash is not None:
            try:
                checked_head = require_int(
                    head_block_number, "head_block_number", non_negative=True
                )
            except (TypeError, ValueError) as exc:
                raise StateSyncError(str(exc)) from exc
            norm_head_hash = _parse_hash32(head_block_hash, "head_block_hash")
            self._head_block_number: int | None = checked_head
            self._head_block_hash: str | None = norm_head_hash
            self._checkpoints[checked_head] = _BlockCheckpoint(
                block_number=checked_head,
                block_hash=norm_head_hash,
                pools=dict(self._pools),
                quarantined_pools=dict(self._quarantined_pools),
            )
        else:
            self._head_block_number = None
            self._head_block_hash = None

    @property
    def head_block_number(self) -> int | None:
        return self._head_block_number

    @property
    def head_block_hash(self) -> str | None:
        return self._head_block_hash

    @property
    def max_rollback_blocks(self) -> int:
        return self._max_rollback_blocks

    @property
    def quarantined_pools(self) -> dict[str, str]:
        return dict(self._quarantined_pools)

    def register_pool(self, pool: PoolNode) -> None:
        key = (pool.chain_id, _parse_pool_id(pool.pool_id))
        self._pools[key] = pool
        if (
            self._head_block_number is not None
            and len(self._checkpoints) == 1
            and self._head_block_number in self._checkpoints
        ):
            head_cp = self._checkpoints[self._head_block_number]
            self._checkpoints[self._head_block_number] = _BlockCheckpoint(
                block_number=head_cp.block_number,
                block_hash=head_cp.block_hash,
                pools=dict(self._pools),
                quarantined_pools=dict(self._quarantined_pools),
            )

    def get_pool(self, chain_id: int, pool_id: str) -> PoolNode | None:
        return self._pools.get((chain_id, _parse_pool_id(pool_id)))

    def quarantine_pool(self, pool_id: str, reason: str) -> None:
        if not reason.strip():
            raise StateSyncError("quarantine reason must be non-empty")
        self._quarantined_pools[_parse_pool_id(pool_id)] = reason

    def unquarantine_pool(self, pool_id: str) -> None:
        self._quarantined_pools.pop(_parse_pool_id(pool_id), None)

    def apply_block_logs(
        self,
        block_number: int,
        block_hash: str,
        parent_hash: str,
        events: Sequence[EvmPoolLogEvent],
        *,
        quarantine_on_event_error: bool = False,
    ) -> int:
        try:
            checked_block = require_int(block_number, "block_number", non_negative=True)
        except (TypeError, ValueError) as exc:
            raise StateSyncError(str(exc)) from exc
        norm_block_hash = _parse_hash32(block_hash, "block_hash")
        norm_parent_hash = _parse_hash32(parent_hash, "parent_hash")

        if self._head_block_number is not None:
            if checked_block != self._head_block_number + 1:
                raise ReorgDetectedError(
                    f"non-contiguous block {checked_block}: expected {self._head_block_number + 1}"
                )
            if norm_parent_hash != self._head_block_hash:
                raise ReorgDetectedError(
                    f"parent_hash mismatch at block {checked_block}: "
                    f"expected {self._head_block_hash}, got {norm_parent_hash}"
                )

        seen_log_keys: set[tuple[int, int]] = set()
        for ev in events:
            if ev.block_number != checked_block:
                raise ReorgDetectedError(
                    f"event block_number {ev.block_number} != batch block_number {checked_block}"
                )
            if _parse_hash32(ev.block_hash, "event.block_hash") != norm_block_hash:
                raise ReorgDetectedError("event block_hash mismatch against block header")
            if _parse_hash32(ev.parent_hash, "event.parent_hash") != norm_parent_hash:
                raise ReorgDetectedError("event parent_hash mismatch against block header")
            log_key = (ev.tx_index, ev.log_index)
            if log_key in seen_log_keys:
                raise StateSyncError(f"duplicate (tx_index, log_index) {log_key} in block")
            seen_log_keys.add(log_key)

        staged_pools = dict(self._pools)
        staged_quarantined = dict(self._quarantined_pools)
        pre_checkpoint: _BlockCheckpoint | None = None
        if self._head_block_number is None and checked_block > 0:
            pre_checkpoint = _BlockCheckpoint(
                block_number=checked_block - 1,
                block_hash=norm_parent_hash,
                pools=dict(self._pools),
                quarantined_pools=dict(self._quarantined_pools),
            )

        ordered_events = sorted(events, key=lambda e: (e.tx_index, e.log_index))
        applied_count = 0

        for ev in ordered_events:
            topic0 = _parse_hash32(ev.topic0, "event.topic0")
            if not ev.topics:
                raise StateSyncError("event.topics cannot be empty")
            topics = tuple(_parse_hash32(t, "event.topic") for t in ev.topics)
            if topics[0] != topic0:
                raise StateSyncError("event.topics[0] does not match event.topic0")

            ev_pool_id = _parse_pool_id(ev.pool_id)
            if topic0 in (V4_SWAP_TOPIC, V4_MODIFY_LIQUIDITY_TOPIC):
                if len(topics) != 3:
                    raise StateSyncError("V4 PoolManager event requires exactly 3 topics")
                target_pool_id = topics[1]
                if len(ev_pool_id) == 66 and ev_pool_id != target_pool_id:
                    raise StateSyncError("V4 event.pool_id disagrees with topics[1]")
            else:
                target_pool_id = ev_pool_id

            pool_key = (ev.chain_id, target_pool_id)
            pool = staged_pools.get(pool_key)
            if pool is None or target_pool_id in staged_quarantined:
                continue

            try:
                updated = _apply_event_to_pool(pool, topic0, topics, ev.data_hex, checked_block)
            except StateSyncError as exc:
                if quarantine_on_event_error:
                    staged_quarantined[target_pool_id] = str(exc)
                    continue
                raise

            if updated is not None:
                staged_pools[pool_key] = updated
                applied_count += 1

        if pre_checkpoint is not None:
            self._checkpoints[pre_checkpoint.block_number] = pre_checkpoint

        self._pools = staged_pools
        self._quarantined_pools = staged_quarantined
        self._head_block_number = checked_block
        self._head_block_hash = norm_block_hash
        self._checkpoints[checked_block] = _BlockCheckpoint(
            block_number=checked_block,
            block_hash=norm_block_hash,
            pools=dict(staged_pools),
            quarantined_pools=dict(staged_quarantined),
        )

        max_entries = self._max_rollback_blocks + 1
        while len(self._checkpoints) > max_entries:
            self._checkpoints.popitem(last=False)

        return applied_count

    def rollback_to_block(self, block_number: int, expected_block_hash: str) -> None:
        try:
            checked_block = require_int(block_number, "block_number", non_negative=True)
        except (TypeError, ValueError) as exc:
            raise StateSyncError(str(exc)) from exc
        expected_hash = _parse_hash32(expected_block_hash, "expected_block_hash")

        checkpoint = self._checkpoints.get(checked_block)
        if checkpoint is None:
            raise StateSyncError(
                f"block {checked_block} is not in rollback retention window "
                f"(max_rollback_blocks={self._max_rollback_blocks})"
            )
        if checkpoint.block_hash != expected_hash:
            raise StateSyncError(
                f"rollback hash mismatch at block {checked_block}: "
                f"expected {checkpoint.block_hash}, got {expected_hash}"
            )

        self._pools = dict(checkpoint.pools)
        self._quarantined_pools = dict(checkpoint.quarantined_pools)
        self._head_block_number = checkpoint.block_number
        self._head_block_hash = checkpoint.block_hash

        stale_blocks = [num for num in self._checkpoints if num > checked_block]
        for num in stale_blocks:
            del self._checkpoints[num]

    def to_fastpath_graph(self, bridge: FastpathBridge | None = None) -> FastpathPoolGraph:
        graph = FastpathPoolGraph(bridge=bridge)
        for (_, pool_id), pool in self._pools.items():
            if pool_id in self._quarantined_pools:
                continue
            graph.upsert_pool(pool)
        return graph
