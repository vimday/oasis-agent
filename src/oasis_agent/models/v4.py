from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .common import normalize_address, normalize_hash32

DYNAMIC_FEE_FLAG = 0x800000
OVERRIDE_FEE_FLAG = 0x400000
MAX_STATIC_LP_FEE = 1_000_000


@dataclass(frozen=True, slots=True)
class PoolKey:
    currency0: str
    currency1: str
    fee: int
    tick_spacing: int
    hooks: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "currency0", normalize_address(self.currency0))
        object.__setattr__(self, "currency1", normalize_address(self.currency1))
        object.__setattr__(self, "hooks", normalize_address(self.hooks))
        if self.currency0 >= self.currency1:
            raise ValueError("PoolKey requires canonical currency order")
        # v4 wire format fits fee in uint24, but the protocol-valid set is a
        # static fee at most MAX_STATIC_LP_FEE or exactly the dynamic fee flag;
        # values in between (and any other flag combination) are non-canonical.
        if not 0 <= self.fee <= MAX_STATIC_LP_FEE and self.fee != DYNAMIC_FEE_FLAG:
            raise ValueError("fee must be at most 1000000 or exactly the dynamic fee flag")
        if not 1 <= self.tick_spacing <= 32_767:
            raise ValueError("tick_spacing must be between 1 and 32767")


@dataclass(frozen=True, slots=True)
class PoolInitialized:
    pool_id: str
    key: PoolKey
    sqrt_price_x96: int
    tick: int
    block_number: int


@dataclass(frozen=True, slots=True)
class BalanceDelta:
    amount0: int
    amount1: int


class SwapDirection(str, Enum):
    ZERO_FOR_ONE = "zero_for_one"
    ONE_FOR_ZERO = "one_for_zero"


@dataclass(frozen=True, slots=True)
class SwapObservation:
    pool_id: str
    sender: str
    amount0: int
    amount1: int
    sqrt_price_x96: int
    liquidity: int
    tick: int
    fee: int | None
    block_number: int


@dataclass(frozen=True, slots=True)
class ModifyLiquidityObservation:
    pool_id: str
    sender: str
    tick_lower: int
    tick_upper: int
    liquidity_delta: int
    salt: str
    block_number: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "pool_id", normalize_hash32(self.pool_id, "pool id"))
        object.__setattr__(self, "sender", normalize_address(self.sender))
        object.__setattr__(self, "salt", normalize_hash32(self.salt, "position salt"))
        for name, value in (
            ("tick_lower", self.tick_lower),
            ("tick_upper", self.tick_upper),
            ("liquidity_delta", self.liquidity_delta),
            ("block_number", self.block_number),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
        if not -(1 << 23) <= self.tick_lower < self.tick_upper <= (1 << 23) - 1:
            raise ValueError("position tick range must fit ordered int24 values")
        if not -(1 << 255) <= self.liquidity_delta <= (1 << 255) - 1:
            raise ValueError("liquidity_delta must fit int256")
        if self.block_number < 0:
            raise ValueError("block_number must be nonnegative")
