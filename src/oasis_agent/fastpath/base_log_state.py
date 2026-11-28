"""Advance a frozen Base pool roster from streamed block logs without per-head RPC.

The latest V3/Slipstream ``Swap`` in a block carries the pool's post-block ``sqrtPriceX96``,
``tick`` and active ``liquidity``. The latest Aerodrome ``Sync`` carries post-block reserves.
A tracked pool is therefore exact at the end of every block whose logs are complete for it,
and a missed log can only leave that pool stale until its next log, never accumulate drift.

Concentrated pools are modelled inside the current tick-spacing interval only. No initialized
tick can lie strictly inside one spacing interval, so that liquidity is exact and the model
understates depth rather than inventing it. ``Mint`` and ``Burn`` change active liquidity
without a price log; this module marks the pool unpriced until a later ``Swap`` or a pinned
full read restores it.

Completeness for a block is decided from the head's ``logsBloom``. A tracked pool whose three
Bloom bits are present but which has delivered no log in that block is ``pending``. A head is
``provisional`` once no tracked pool is pending. A block is ``confirmed`` when the head
``CONFIRM_LAG`` blocks above it arrives: live PublicNode delivery has placed tracked logs for N
up to 25 ms after head N+1, so the direct child alone does not close a block.
This module does no I/O, signs nothing and has no execution authority.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Final

from eth_hash.auto import keccak

from ..evidence.log_bloom import log_emitter_mask
from ..models.common import normalize_address, normalize_hash32, normalize_hex
from ..uniswap_v4.liquidity_math import (
    MAX_SQRT_PRICE,
    MAX_TICK,
    MIN_SQRT_PRICE,
    MIN_TICK,
    sqrt_price_at_tick,
)
from .pool_graph import PoolKind, PoolNode, TickLiquidity
from .state_sync import (
    AERO_SYNC_TOPIC,
    V2_SYNC_TOPIC,
    V3_BURN_TOPIC,
    V3_MINT_TOPIC,
    V3_SWAP_TOPIC,
)

BASE_CHAIN_ID: Final[int] = 8453
PRICE_TOPICS: Final[frozenset[str]] = frozenset({V3_SWAP_TOPIC, V2_SYNC_TOPIC, AERO_SYNC_TOPIC})
LIQUIDITY_TOPICS: Final[frozenset[str]] = frozenset({V3_MINT_TOPIC, V3_BURN_TOPIC})
STREAM_TOPICS: Final[tuple[str, ...]] = (
    V3_SWAP_TOPIC,
    V3_MINT_TOPIC,
    V3_BURN_TOPIC,
    V2_SYNC_TOPIC,
    AERO_SYNC_TOPIC,
)
_UINT112: Final[int] = (1 << 112) - 1
_UINT128: Final[int] = (1 << 128) - 1
_UINT160: Final[int] = (1 << 160) - 1
_UINT256: Final[int] = (1 << 256) - 1
_ZERO_HASH: Final[str] = "0x" + "00" * 32
MAX_PENDING_BLOCKS: Final[int] = 64
CONFIRM_LAG: Final[int] = 2


class LogStateError(ValueError):
    """The stream broke an ordering, identity or encoding invariant. Stop and reseed."""


@dataclass(frozen=True, slots=True)
class StreamHead:
    height: int
    block_hash: str
    parent_hash: str
    timestamp_ms: int
    base_fee_per_gas: int
    logs_bloom: int
    received_ns: int


@dataclass(frozen=True, slots=True)
class StreamLog:
    height: int
    block_hash: str
    tx_index: int
    log_index: int
    address: str
    topics: tuple[str, ...]
    data: bytes
    received_ns: int


def _quantity(value: object, name: str, *, bits: int = 64) -> int:
    if not isinstance(value, str) or not value.startswith("0x") or len(value) < 3:
        raise LogStateError(f"{name} must be a hex quantity")
    if len(value) > 3 and value[2] == "0":
        raise LogStateError(f"{name} has a leading zero")
    try:
        parsed = int(value, 16)
    except ValueError as exc:
        raise LogStateError(f"{name} is not hex") from exc
    if parsed >= 1 << bits:
        raise LogStateError(f"{name} exceeds {bits} bits")
    return parsed


def _hash(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise LogStateError(f"{name} must be a 32-byte hex string")
    try:
        return normalize_hash32(value, name)
    except ValueError as exc:
        raise LogStateError(str(exc)) from exc


def decode_stream_head(result: object, received_ns: int) -> StreamHead:
    """Decode one ``newHeads`` result. Base Denim heads carry ``timestampMs``."""
    if not isinstance(result, Mapping):
        raise LogStateError("head result must be an object")
    bloom_hex = result.get("logsBloom")
    if not isinstance(bloom_hex, str) or len(bloom_hex) != 2 + 512:
        raise LogStateError("head logsBloom must be 256 bytes")
    try:
        bloom = int(bloom_hex, 16)
    except ValueError as exc:
        raise LogStateError("head logsBloom is not hex") from exc
    height = _quantity(result.get("number"), "number")
    timestamp_s = _quantity(result.get("timestamp"), "timestamp")
    timestamp_ms = _quantity(result.get("timestampMs"), "timestampMs")
    if timestamp_ms // 1000 != timestamp_s:
        raise LogStateError("head timestampMs disagrees with timestamp")
    head = StreamHead(
        height=height,
        block_hash=_hash(result.get("hash"), "hash"),
        parent_hash=_hash(result.get("parentHash"), "parentHash"),
        timestamp_ms=timestamp_ms,
        base_fee_per_gas=_quantity(result.get("baseFeePerGas"), "baseFeePerGas", bits=128),
        logs_bloom=bloom,
        received_ns=received_ns,
    )
    if head.block_hash in (_ZERO_HASH, head.parent_hash):
        raise LogStateError("head hash is zero or equals its parent")
    return head


def decode_stream_log(result: object, received_ns: int) -> StreamLog:
    """Decode one ``logs`` subscription result. Removed logs are rejected, not applied."""
    if not isinstance(result, Mapping):
        raise LogStateError("log result must be an object")
    if result.get("removed") is not False:
        raise LogStateError("log is removed or lacks the removed flag")
    topics_raw = result.get("topics")
    if not isinstance(topics_raw, list) or not 1 <= len(topics_raw) <= 4:
        raise LogStateError("log topics must be a list of 1..4 hashes")
    data_raw = result.get("data")
    if not isinstance(data_raw, str):
        raise LogStateError("log data must be hex")
    try:
        data = bytes.fromhex(normalize_hex(data_raw)[2:])
        address = normalize_address(str(result.get("address")))
    except ValueError as exc:
        raise LogStateError(str(exc)) from exc
    return StreamLog(
        height=_quantity(result.get("blockNumber"), "blockNumber"),
        block_hash=_hash(result.get("blockHash"), "blockHash"),
        tx_index=_quantity(result.get("transactionIndex"), "transactionIndex", bits=32),
        log_index=_quantity(result.get("logIndex"), "logIndex", bits=32),
        address=address,
        topics=tuple(_hash(t, "topic") for t in topics_raw),
        data=data,
        received_ns=received_ns,
    )


def _word(data: bytes, index: int) -> int:
    return int.from_bytes(data[32 * index : 32 * index + 32], "big")


def _signed(word: int, bits: int, name: str) -> int:
    if bits == 256:
        return word - (1 << 256) if word >> 255 else word
    low = word & ((1 << bits) - 1)
    value = low - (1 << bits) if low >> (bits - 1) else low
    if value % (1 << 256) != word:
        raise LogStateError(f"{name} is not a canonical int{bits} word")
    return value


def _bounded(word: int, limit: int, name: str) -> int:
    if word > limit:
        raise LogStateError(f"{name} exceeds its ABI width")
    return word


def _topic_address(topic: str, name: str) -> None:
    if int(topic, 16) >> 160:
        raise LogStateError(f"{name} topic is not a left-padded address")


def spacing_interval(tick: int, spacing: int) -> tuple[int, int]:
    """Return the initialized-tick-free interval ``[lower, upper)`` that contains ``tick``."""
    if type(spacing) is not int or not 1 <= spacing <= 16_384:
        raise LogStateError("tick spacing must be 1..16384")
    lower = (tick // spacing) * spacing
    upper = lower + spacing
    return max(lower, MIN_TICK), min(upper, MAX_TICK)


def _require_cl_state(sqrt_price_x96: int, tick: int, liquidity: int) -> None:
    if not MIN_SQRT_PRICE <= sqrt_price_x96 <= MAX_SQRT_PRICE or not MIN_TICK <= tick <= MAX_TICK:
        raise LogStateError("CL price or tick outside TickMath bounds")
    if not 0 <= liquidity <= _UINT128:
        raise LogStateError("CL liquidity exceeds uint128")
    # A zeroForOne swap ending exactly on a boundary leaves tick = boundary - 1 with the price
    # equal to the boundary, so equality with the upper tick price is canonical.
    if sqrt_price_x96 < sqrt_price_at_tick(tick) or (
        tick < MAX_TICK and sqrt_price_x96 > sqrt_price_at_tick(tick + 1)
    ):
        raise LogStateError("CL sqrtPriceX96 is outside its tick")


def cl_interval_node(
    template: PoolNode, *, sqrt_price_x96: int, tick: int, liquidity: int, spacing: int, height: int
) -> PoolNode:
    """Exact-in-interval CL node. Swaps leaving the interval fail closed in the native model."""
    _require_cl_state(sqrt_price_x96, tick, liquidity)
    lower, upper = spacing_interval(tick, spacing)
    ticks: tuple[TickLiquidity, ...] = ()
    if liquidity:
        ticks = (TickLiquidity(lower, liquidity), TickLiquidity(upper, -liquidity))
    return replace(
        template,
        sqrt_price_x96=sqrt_price_x96,
        tick=tick,
        liquidity=liquidity,
        ticks=ticks,
        block_number=height,
    )


def apply_pool_log(node: PoolNode, spacing: int | None, log: StreamLog) -> PoolNode | None:
    """Return the post-log pool, or ``None`` for a log that does not change modelled state."""
    topic0, data = log.topics[0], log.data
    if len(data) % 32:
        raise LogStateError("log data is not word aligned")
    words = len(data) // 32
    cl = node.kind in (PoolKind.V3, PoolKind.SLIPSTREAM)
    if topic0 == V3_SWAP_TOPIC:
        if not cl or spacing is None or len(log.topics) != 3 or words != 5:
            raise LogStateError("V3 Swap shape or pool kind mismatch")
        _topic_address(log.topics[1], "sender")
        _topic_address(log.topics[2], "recipient")
        sqrt_price = _bounded(_word(data, 2), _UINT160, "sqrtPriceX96")
        liquidity = _bounded(_word(data, 3), _UINT128, "liquidity")
        tick = _signed(_word(data, 4), 24, "tick")
        return cl_interval_node(
            node,
            sqrt_price_x96=sqrt_price,
            tick=tick,
            liquidity=liquidity,
            spacing=spacing,
            height=log.height,
        )
    if topic0 in (V3_MINT_TOPIC, V3_BURN_TOPIC):
        mint = topic0 == V3_MINT_TOPIC
        if not cl or spacing is None or len(log.topics) != 4 or words != (4 if mint else 3):
            raise LogStateError("V3 Mint/Burn shape or pool kind mismatch")
        _topic_address(log.topics[1], "owner")
        lower = _signed(int(log.topics[2], 16), 24, "tickLower")
        upper = _signed(int(log.topics[3], 16), 24, "tickUpper")
        if not MIN_TICK <= lower < upper <= MAX_TICK or lower % spacing or upper % spacing:
            raise LogStateError("position range is not spacing aligned")
        amount = _bounded(_word(data, 1 if mint else 0), _UINT128, "amount")
        assert node.tick is not None and node.liquidity is not None
        assert node.sqrt_price_x96 is not None
        if not lower <= node.tick < upper or amount == 0:
            return None
        liquidity = node.liquidity + amount if mint else node.liquidity - amount
        if not 0 <= liquidity <= _UINT128:
            raise LogStateError("active liquidity left uint128")
        return cl_interval_node(
            node,
            sqrt_price_x96=node.sqrt_price_x96,
            tick=node.tick,
            liquidity=liquidity,
            spacing=spacing,
            height=log.height,
        )
    if topic0 in (V2_SYNC_TOPIC, AERO_SYNC_TOPIC):
        if node.kind != PoolKind.V2 or len(log.topics) != 1 or words != 2:
            raise LogStateError("Sync shape or pool kind mismatch")
        limit = _UINT112 if topic0 == V2_SYNC_TOPIC else _UINT256
        reserve0 = _bounded(_word(data, 0), limit, "reserve0")
        reserve1 = _bounded(_word(data, 1), limit, "reserve1")
        return replace(node, reserve0=reserve0, reserve1=reserve1, block_number=log.height)
    return None


@dataclass(frozen=True, slots=True)
class BlockReport:
    """Per-block account of what the stream delivered for the tracked roster."""

    height: int
    block_hash: str
    head_received_ns: int
    last_log_received_ns: int | None
    tracked_logs: int
    logs_before_head: int
    logs_out_of_order: int
    changed_pools: tuple[str, ...]
    bloom_flagged: tuple[str, ...]
    bloom_only: tuple[str, ...]


@dataclass(slots=True)
class _Block:
    head: StreamHead | None = None
    logs: list[StreamLog] = field(default_factory=list)
    keys: set[tuple[int, int]] = field(default_factory=set)
    early: int = 0
    out_of_order: int = 0


class BaseLogState:
    """Block-ordered, fail-closed log state for a frozen Base roster seeded at one pinned block.

    Any gap, parent mismatch, hash conflict, late log or decode error raises ``LogStateError``.
    The caller must then discard the state and reseed from a new pinned full read.
    """

    def __init__(
        self,
        seed_height: int,
        seed_hash: str,
        pools: Sequence[PoolNode],
        spacings: Mapping[str, int | None],
        *,
        history: int = 32,
        depths: Mapping[str, ClDepth] | None = None,
    ) -> None:
        if not 1 <= history <= 256:
            raise LogStateError("history must be 1..256 blocks")
        self._nodes: dict[str, PoolNode] = {}
        self._spacing: dict[str, int | None] = {}
        for node in pools:
            key = normalize_address(node.pool_id)
            if key in self._nodes or node.chain_id != BASE_CHAIN_ID:
                raise LogStateError("duplicate or foreign pool in seed")
            spacing = spacings.get(key)
            if (node.kind in (PoolKind.V3, PoolKind.SLIPSTREAM)) != (spacing is not None):
                raise LogStateError(f"pool {key} spacing does not match its kind")
            self._nodes[key] = node
            self._spacing[key] = spacing
        if not self._nodes:
            raise LogStateError("seed roster is empty")
        self._depths: dict[str, ClDepth] = {}
        for raw_key, depth in (depths or {}).items():
            pool = normalize_address(raw_key)
            if self._spacing.get(pool) != depth.spacing:
                raise LogStateError(f"pool {pool} depth spacing disagrees with the roster")
            self._depths[pool] = depth
        self.downgrades: list[tuple[int, str, str]] = []
        self._masks = {key: log_emitter_mask(key) for key in self._nodes}
        self.height = self._seed_height = seed_height
        self.block_hash = _hash(seed_hash, "seed hash")
        self._blocks: dict[int, _Block] = {}
        self._history_limit = history
        self._history: dict[int, tuple[str, dict[str, PoolNode]]] = {
            seed_height: (self.block_hash, dict(self._nodes))
        }

    @property
    def pool_ids(self) -> tuple[str, ...]:
        return tuple(self._nodes)

    def _block(self, height: int) -> _Block:
        if height > self.height + MAX_PENDING_BLOCKS:
            raise LogStateError("stream ran too far ahead of confirmed state")
        return self._blocks.setdefault(height, _Block())

    def on_log(self, log: StreamLog) -> bool:
        """Buffer a tracked pool log. Returns ``False`` for untracked or pre-seed logs."""
        if log.address not in self._nodes:
            return False
        if log.height <= self.height:
            # The pinned seed already reflects its own block and earlier ones. A log for a block
            # this state confirmed from the stream arrived late, so the confirmation was wrong.
            if log.height > self._seed_height:
                raise LogStateError("log arrived after its block was confirmed")
            return False
        block = self._block(log.height)
        if block.head is not None and block.head.block_hash != log.block_hash:
            raise LogStateError("log block hash conflicts with its head")
        if block.logs and block.logs[0].block_hash != log.block_hash:
            raise LogStateError("two block hashes delivered logs at one height")
        key = (log.tx_index, log.log_index)
        if key in block.keys:
            raise LogStateError("duplicate log position")
        if any(item.log_index == log.log_index for item in block.logs):
            raise LogStateError("two logs share one block log index")
        if block.head is None:
            block.early += 1
        block.keys.add(key)
        if block.logs and log.log_index < block.logs[-1].log_index:
            # Delivery order is not consensus order. Apply in log-index order; a caller that
            # already scanned this height must rescan, which it detects from buffered_logs().
            block.out_of_order += 1
            block.logs.append(log)
            block.logs.sort(key=lambda item: item.log_index)
        else:
            block.logs.append(log)
        return True

    def on_head(self, head: StreamHead) -> BlockReport | None:
        """Record a head; confirm and return the parent block once its child is known."""
        if head.height <= self.height:
            if head.height == self.height and head.block_hash != self.block_hash:
                raise LogStateError("head conflicts with the confirmed block")
            retained = self._history.get(head.height)
            if head.height < self.height and retained and retained[0] != head.block_hash:
                raise LogStateError("head conflicts with confirmed history")
            return None
        block = self._block(head.height)
        if block.head is not None:
            if block.head != head:
                raise LogStateError("two heads delivered at one height")
            return None
        if any(log.block_hash != head.block_hash for log in block.logs):
            raise LogStateError("buffered logs conflict with their head")
        if head.height == self.height + 1:
            if head.parent_hash != self.block_hash:
                raise LogStateError("first head after confirmed state has a different parent")
        else:
            parent = self._blocks.get(head.height - 1)
            if parent is None or parent.head is None:
                raise LogStateError("head stream skipped a block")
            if parent.head.block_hash != head.parent_hash:
                raise LogStateError("head parent hash mismatch")
        block.head = head
        if head.height - self.height <= CONFIRM_LAG:
            return None
        return self._confirm(self.height + 1)

    def buffered_logs(self, height: int) -> int:
        """Tracked logs buffered so far for an unconfirmed height."""
        block = self._blocks.get(height)
        return 0 if block is None else len(block.logs)

    def pending(self, height: int) -> tuple[str, ...]:
        """Bloom-flagged tracked pools that have not delivered a log for ``height`` yet."""
        block = self._blocks.get(height)
        if block is None or block.head is None:
            raise LogStateError("no head for that height")
        seen = {log.address for log in block.logs}
        bloom = block.head.logs_bloom
        return tuple(k for k, m in self._masks.items() if bloom & m == m and k not in seen)

    def depth_snapshot(self) -> dict[str, ClDepth]:
        """Confirmed depth windows, for reconciliation against a pinned read."""
        return dict(self._depths)

    @property
    def depth_pools(self) -> tuple[str, ...]:
        """Pools currently modelled on a pinned tick window rather than one spacing interval."""
        return tuple(self._depths)

    def view(self, height: int) -> tuple[dict[str, PoolNode], tuple[str, ...]]:
        """Provisional post-block state for the first unconfirmed head and the pools it changed."""
        if not self.height < height <= self.height + CONFIRM_LAG:
            raise LogStateError("provisional views cover only unconfirmed heads")
        logs: list[StreamLog] = []
        for level in range(self.height + 1, height + 1):
            block = self._blocks.get(level)
            if block is None or block.head is None:
                raise LogStateError("no head for that height")
            logs.extend(block.logs)
        nodes, _, changed, _ = self._apply(dict(self._nodes), dict(self._depths), logs)
        return nodes, changed

    def _apply(
        self, nodes: dict[str, PoolNode], depths: dict[str, ClDepth], logs: Iterable[StreamLog]
    ) -> tuple[dict[str, PoolNode], dict[str, ClDepth], tuple[str, ...], list[tuple[str, str]]]:
        changed: dict[str, None] = {}
        dropped: list[tuple[str, str]] = []
        for log in logs:
            key = log.address
            depth = depths.get(key)
            updated: PoolNode | None
            if depth is not None and log.topics[0] not in (V2_SYNC_TOPIC, AERO_SYNC_TOPIC):
                updated, new_depth, reason = apply_depth_log(nodes[key], depth, log)
                if new_depth is None:
                    del depths[key]
                    dropped.append((key, reason or "unknown"))
                else:
                    depths[key] = new_depth
            else:
                updated = apply_pool_log(nodes[key], self._spacing[key], log)
            if updated is not None and updated != nodes[key]:
                nodes[key] = updated
                changed[key] = None
        return nodes, depths, tuple(changed), dropped

    def _confirm(self, height: int) -> BlockReport:
        block = self._blocks.pop(height)
        assert block.head is not None
        nodes, depths, changed, dropped = self._apply(
            dict(self._nodes), dict(self._depths), block.logs
        )
        bloom = block.head.logs_bloom
        flagged = tuple(k for k, m in self._masks.items() if bloom & m == m)
        seen = {log.address for log in block.logs}
        if not seen <= set(flagged):
            raise LogStateError("a tracked pool logged without its Bloom bits")
        self._nodes, self._depths = nodes, depths
        self.downgrades.extend((height, key, reason) for key, reason in dropped)
        self.height, self.block_hash = height, block.head.block_hash
        self._history[height] = (self.block_hash, dict(nodes))
        while len(self._history) > self._history_limit:
            del self._history[min(self._history)]
        return BlockReport(
            height=height,
            block_hash=self.block_hash,
            head_received_ns=block.head.received_ns,
            last_log_received_ns=max((log.received_ns for log in block.logs), default=None),
            tracked_logs=len(block.logs),
            logs_before_head=block.early,
            logs_out_of_order=block.out_of_order,
            changed_pools=changed,
            bloom_flagged=flagged,
            bloom_only=tuple(k for k in flagged if k not in seen),
        )

    def confirmed(self, height: int) -> tuple[str, dict[str, PoolNode]]:
        """Return the retained confirmed state for reconciliation against a pinned full read."""
        if height not in self._history:
            raise LogStateError("height is outside retained history")
        block_hash, nodes = self._history[height]
        return block_hash, dict(nodes)

    def reseed_pool(self, height: int, node: PoolNode) -> None:
        """Replace one pool's state from a pinned read at the current confirmed height."""
        key = normalize_address(node.pool_id)
        if height != self.height or key not in self._nodes:
            raise LogStateError("reseed must target a tracked pool at the confirmed height")
        if any(log.address == key for block in self._blocks.values() for log in block.logs):
            raise LogStateError("cannot reseed a pool with buffered logs ahead of the pin")
        self._nodes[key] = node
        self._history[height][1][key] = node


V3_FEE_SPACING: Final[Mapping[int, int]] = {100: 1, 500: 10, 3000: 60, 10_000: 200}


def stream_topic_filter() -> dict[str, object]:
    """Topic-only ``logs`` filter. Untracked emitters feed the venue census, not the state."""
    return {"topics": [list(STREAM_TOPICS)]}


def topic_name(topic0: str) -> str:
    names = {
        V3_SWAP_TOPIC: "cl_swap",
        V3_MINT_TOPIC: "cl_mint",
        V3_BURN_TOPIC: "cl_burn",
        V2_SYNC_TOPIC: "v2_sync112",
        AERO_SYNC_TOPIC: "aero_sync256",
    }
    return names.get(topic0, "other")


def _selfcheck() -> None:
    # Topic constants must match the canonical event signatures.
    swap = "Swap(address,address,int256,int256,uint160,uint128,int24)"
    if "0x" + keccak(swap.encode()).hex() != V3_SWAP_TOPIC:
        raise LogStateError("V3 Swap topic constant is not canonical")


_selfcheck()


@dataclass(frozen=True, slots=True)
class ClDepth:
    """Initialized-tick ``liquidityNet`` inside a pinned bitmap window ``[lower, upper]``.

    ``lower`` and ``upper`` are word-aligned window edges. Ticks at or beyond an edge are not
    modelled: the native swap meets a sentinel there that drains all liquidity.
    """

    spacing: int
    lower: int
    upper: int
    nets: Mapping[int, int]

    def __post_init__(self) -> None:
        if type(self.spacing) is not int or not 1 <= self.spacing <= 16_384:
            raise LogStateError("depth spacing must be 1..16384")
        if not MIN_TICK <= self.lower < self.upper <= MAX_TICK:
            raise LogStateError("depth window is empty or outside TickMath bounds")
        clean: dict[int, int] = {}
        for tick, net in self.nets.items():
            if type(tick) is not int or type(net) is not int:
                raise LogStateError("depth ticks and nets must be integers")
            if tick % self.spacing or not self.lower < tick < self.upper:
                raise LogStateError("depth tick is unaligned or not strictly inside its window")
            if not -(1 << 127) <= net < 1 << 127:
                raise LogStateError("depth liquidityNet exceeds int128")
            if net:
                clean[tick] = net
        object.__setattr__(self, "nets", MappingProxyType(clean))


def depth_window(tick: int, spacing: int, half_words: int) -> tuple[int, int, tuple[int, ...]]:
    """Return word-aligned tick edges and the bitmap word indexes covering ``tick``."""
    if not 0 <= half_words <= 8:
        raise LogStateError("half_words must be 0..8")
    word = (tick // spacing) >> 8
    words = tuple(range(word - half_words, word + half_words + 1))
    lower = max(words[0] * 256 * spacing, MIN_TICK)
    upper = min((words[-1] + 1) * 256 * spacing, MAX_TICK)
    return lower, upper, words


def _segment_liquidity(depth: ClDepth, tick: int, liquidity: int, target: int) -> int:
    """Active liquidity at ``target`` implied by liquidity at ``tick`` and the window nets."""
    if target >= tick:
        return liquidity + sum(n for t, n in depth.nets.items() if tick < t <= target)
    return liquidity - sum(n for t, n in depth.nets.items() if target < t <= tick)


def depth_node(
    template: PoolNode,
    *,
    sqrt_price_x96: int,
    tick: int,
    liquidity: int,
    depth: ClDepth,
    height: int,
) -> PoolNode:
    """CL node with every initialized tick in the window and draining sentinels at its edges."""
    _require_cl_state(sqrt_price_x96, tick, liquidity)
    if not depth.lower <= tick < depth.upper:
        raise LogStateError("price left the depth window")
    inner = sorted((t, n) for t, n in depth.nets.items() if depth.lower < t < depth.upper)
    running = _segment_liquidity(depth, tick, liquidity, depth.lower)
    if running < 0:
        raise LogStateError("depth implies negative liquidity at the window floor")
    ticks = [TickLiquidity(depth.lower, running)]
    for t, n in inner:
        running += n
        if not 0 <= running <= _UINT128:
            raise LogStateError("depth implies liquidity outside uint128")
        ticks.append(TickLiquidity(t, n))
    ticks.append(TickLiquidity(depth.upper, -running))
    if _segment_liquidity(depth, tick, liquidity, depth.upper - 1) != running:
        raise LogStateError("depth prefix sums disagree")
    return replace(
        template,
        sqrt_price_x96=sqrt_price_x96,
        tick=tick,
        liquidity=liquidity,
        ticks=tuple(ticks),
        block_number=height,
    )


def apply_depth_log(
    node: PoolNode, depth: ClDepth, log: StreamLog
) -> tuple[PoolNode, ClDepth | None, str | None]:
    """Apply a CL log on the depth model.

    Returns the new node and depth. A ``None`` depth with a reason means the window can no
    longer be trusted for this pool; the node is then rebuilt on the interval model.
    """
    assert node.tick is not None and node.liquidity is not None and node.sqrt_price_x96 is not None
    topic0 = log.topics[0]
    if topic0 == V3_SWAP_TOPIC:
        interval = apply_pool_log(node, depth.spacing, log)
        assert interval is not None and interval.tick is not None and interval.liquidity is not None
        assert interval.sqrt_price_x96 is not None
        if not depth.lower <= interval.tick < depth.upper:
            return interval, None, "price_left_window"
        implied = _segment_liquidity(depth, node.tick, node.liquidity, interval.tick)
        if implied != interval.liquidity:
            return interval, None, "swap_liquidity_disagrees_with_ticks"
        rebuilt = depth_node(
            node,
            sqrt_price_x96=interval.sqrt_price_x96,
            tick=interval.tick,
            liquidity=interval.liquidity,
            depth=depth,
            height=log.height,
        )
        return rebuilt, depth, None
    if topic0 in (V3_MINT_TOPIC, V3_BURN_TOPIC):
        updated = apply_pool_log(node, depth.spacing, log)  # validates shape and alignment
        lower = _signed(int(log.topics[2], 16), 24, "tickLower")
        upper = _signed(int(log.topics[3], 16), 24, "tickUpper")
        sign = 1 if topic0 == V3_MINT_TOPIC else -1
        amount = sign * _word(log.data, 1 if topic0 == V3_MINT_TOPIC else 0)
        if amount == 0:
            return node, depth, None
        nets = dict(depth.nets)
        for t, delta in ((lower, amount), (upper, -amount)):
            # Edge ticks are draining sentinels whose full net was never read.
            if depth.lower < t < depth.upper:
                nets[t] = nets.get(t, 0) + delta
        new_depth = ClDepth(depth.spacing, depth.lower, depth.upper, nets)
        liquidity = node.liquidity if updated is None else updated.liquidity
        assert liquidity is not None
        rebuilt = depth_node(
            node,
            sqrt_price_x96=node.sqrt_price_x96,
            tick=node.tick,
            liquidity=liquidity,
            depth=new_depth,
            height=log.height,
        )
        return rebuilt, new_depth, None
    raise LogStateError("depth model received a non-CL log")
