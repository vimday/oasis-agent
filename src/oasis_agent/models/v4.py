from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .common import normalize_address


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
        if not 0 <= self.fee <= 0xFFFFFF:
            raise ValueError("fee must fit uint24")
        if not -32768 <= self.tick_spacing <= 32767:
            raise ValueError("tick_spacing must fit int16")


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


DYNAMIC_FEE_FLAG = 0x800000
OVERRIDE_FEE_FLAG = 0x400000
