from dataclasses import dataclass
from typing import Protocol

from eth_hash.auto import keccak

from ..models.common import ZERO_ADDRESS, BlockRef, normalize_address, normalize_hash32
from ..models.v4 import PoolKey
from ..uniswap_v4.liquidity_math import MAX_TICK, MIN_TICK
from ..uniswap_v4.pool_id import pool_id_from_key

Q128 = 1 << 128
UINT128_MAX = (1 << 128) - 1
UINT256_MAX = (1 << 256) - 1
INT24_MIN = -(1 << 23)
INT24_MAX = (1 << 23) - 1


def _signed_int24(value: int) -> int:
    value &= 0xFFFFFF
    return value - (1 << 24) if value & (1 << 23) else value


def _validate_tick(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if not INT24_MIN <= value <= INT24_MAX:
        raise ValueError(f"{name} must fit int24")


def _validate_uint(value: int, bits: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if not 0 <= value < 1 << bits:
        raise ValueError(f"{name} must fit uint{bits}")


@dataclass(frozen=True, slots=True)
class DecodedPositionInfo:
    pool_id_prefix: str
    tick_lower: int
    tick_upper: int
    has_subscriber: bool


def decode_position_info(value: int) -> DecodedPositionInfo:
    _validate_uint(value, 256, "position info")
    subscriber = value & 0xFF
    if subscriber not in (0, 1):
        raise ValueError("position subscriber flag is not canonical")
    tick_lower = _signed_int24(value >> 8)
    tick_upper = _signed_int24(value >> 32)
    if tick_lower >= tick_upper:
        raise ValueError("position tick range is invalid")
    pool_prefix = (value >> 56).to_bytes(25, "big")
    return DecodedPositionInfo(
        "0x" + pool_prefix.hex(),
        tick_lower,
        tick_upper,
        subscriber == 1,
    )


def position_id(position_manager: str, tick_lower: int, tick_upper: int, token_id: int) -> str:
    manager = normalize_address(position_manager)
    _validate_tick(tick_lower, "tick_lower")
    _validate_tick(tick_upper, "tick_upper")
    if tick_lower >= tick_upper:
        raise ValueError("position tick range is invalid")
    _validate_uint(token_id, 256, "token_id")
    if token_id == 0:
        raise ValueError("PositionManager token_id zero is not mintable")
    packed = b"".join(
        (
            bytes.fromhex(manager[2:]),
            tick_lower.to_bytes(3, "big", signed=True),
            tick_upper.to_bytes(3, "big", signed=True),
            token_id.to_bytes(32, "big"),
        )
    )
    return "0x" + keccak(packed).hex()


@dataclass(frozen=True, slots=True)
class CorePositionState:
    liquidity: int
    fee_growth_inside0_last_x128: int
    fee_growth_inside1_last_x128: int

    def __post_init__(self) -> None:
        _validate_uint(self.liquidity, 128, "position liquidity")
        _validate_uint(self.fee_growth_inside0_last_x128, 256, "fee growth 0 last")
        _validate_uint(self.fee_growth_inside1_last_x128, 256, "fee growth 1 last")


class PositionStateSource(Protocol):
    def owner_of(self, token_id: int, block: BlockRef) -> str: ...

    def pool_and_position_info(self, token_id: int, block: BlockRef) -> tuple[PoolKey, int]: ...

    def position_liquidity(self, token_id: int, block: BlockRef) -> int: ...

    def core_position(
        self, pool_id: str, core_position_id: str, block: BlockRef
    ) -> CorePositionState: ...

    def fee_growth_inside(
        self, pool_id: str, tick_lower: int, tick_upper: int, block: BlockRef
    ) -> tuple[int, int]: ...


@dataclass(frozen=True, slots=True)
class PositionSnapshot:
    block: BlockRef
    position_manager: str
    token_id: int
    nft_owner: str
    core_position_owner: str
    pool_key: PoolKey
    pool_id: str
    core_position_id: str
    tick_lower: int
    tick_upper: int
    has_subscriber: bool
    liquidity: int
    fee_growth_inside0_last_x128: int
    fee_growth_inside1_last_x128: int
    fee_growth_inside0_x128: int
    fee_growth_inside1_x128: int
    uncollected_fee0: int
    uncollected_fee1: int

    def __post_init__(self) -> None:
        # Python compares 100.0 == 100 and False == 0. Validate before evidence
        # equality checks so hostile snapshots cannot exploit that equivalence.
        _validate_uint(self.token_id, 256, "token_id")
        _validate_uint(self.liquidity, 128, "position liquidity")
        for name in (
            "fee_growth_inside0_last_x128",
            "fee_growth_inside1_last_x128",
            "fee_growth_inside0_x128",
            "fee_growth_inside1_x128",
            "uncollected_fee0",
            "uncollected_fee1",
        ):
            _validate_uint(getattr(self, name), 256, name)
        _validate_tick(self.tick_lower, "tick_lower")
        _validate_tick(self.tick_upper, "tick_upper")
        if type(self.has_subscriber) is not bool:
            raise TypeError("position subscriber flag must be a boolean")
        if self.token_id == 0 or self.tick_lower >= self.tick_upper:
            raise ValueError("invalid live NFT identity or tick range")
        for name in ("position_manager", "nft_owner", "core_position_owner"):
            object.__setattr__(self, name, normalize_address(getattr(self, name)))
        for name in ("pool_id", "core_position_id"):
            object.__setattr__(self, name, normalize_hash32(getattr(self, name), name))


def _fees_accrued(current: int, previous: int, liquidity: int) -> int:
    growth_delta = (current - previous) & UINT256_MAX
    return growth_delta * liquidity // Q128


def read_position_snapshot(
    position_manager: str,
    token_id: int,
    block: BlockRef,
    source: PositionStateSource,
) -> PositionSnapshot:
    if block.timestamp is None:
        raise ValueError("position snapshot requires a timestamped block")
    manager = normalize_address(position_manager)
    _validate_uint(token_id, 256, "token_id")
    if token_id == 0:
        raise ValueError("PositionManager token_id zero is not mintable")

    nft_owner = normalize_address(source.owner_of(token_id, block))
    if nft_owner == ZERO_ADDRESS:
        raise ValueError("live position snapshot cannot have the zero NFT owner")
    pool_key, packed_info = source.pool_and_position_info(token_id, block)
    decoded = decode_position_info(packed_info)
    if decoded.tick_lower < MIN_TICK or decoded.tick_upper > MAX_TICK:
        raise ValueError("position ticks exceed the protocol TickMath range")
    pool_id = normalize_hash32(pool_id_from_key(pool_key), "pool id")
    if decoded.pool_id_prefix != pool_id[:52]:
        raise ValueError("packed position pool ID prefix does not match PoolKey")
    if decoded.tick_lower % pool_key.tick_spacing or decoded.tick_upper % pool_key.tick_spacing:
        raise ValueError("position ticks are not aligned to pool tick spacing")

    core_id = position_id(manager, decoded.tick_lower, decoded.tick_upper, token_id)
    core = source.core_position(pool_id, core_id, block)
    manager_liquidity = source.position_liquidity(token_id, block)
    _validate_uint(manager_liquidity, 128, "PositionManager liquidity")
    if manager_liquidity != core.liquidity:
        raise ValueError("PositionManager and core position liquidity disagreement")
    current_growth0, current_growth1 = source.fee_growth_inside(
        pool_id,
        decoded.tick_lower,
        decoded.tick_upper,
        block,
    )
    _validate_uint(current_growth0, 256, "current fee growth 0")
    _validate_uint(current_growth1, 256, "current fee growth 1")

    return PositionSnapshot(
        block=block,
        position_manager=manager,
        token_id=token_id,
        nft_owner=nft_owner,
        core_position_owner=manager,
        pool_key=pool_key,
        pool_id=pool_id,
        core_position_id=core_id,
        tick_lower=decoded.tick_lower,
        tick_upper=decoded.tick_upper,
        has_subscriber=decoded.has_subscriber,
        liquidity=core.liquidity,
        fee_growth_inside0_last_x128=core.fee_growth_inside0_last_x128,
        fee_growth_inside1_last_x128=core.fee_growth_inside1_last_x128,
        fee_growth_inside0_x128=current_growth0,
        fee_growth_inside1_x128=current_growth1,
        uncollected_fee0=_fees_accrued(
            current_growth0,
            core.fee_growth_inside0_last_x128,
            core.liquidity,
        ),
        uncollected_fee1=_fees_accrued(
            current_growth1,
            core.fee_growth_inside1_last_x128,
            core.liquidity,
        ),
    )


__all__ = [
    "Q128",
    "CorePositionState",
    "DecodedPositionInfo",
    "PositionSnapshot",
    "PositionStateSource",
    "decode_position_info",
    "position_id",
    "read_position_snapshot",
]
