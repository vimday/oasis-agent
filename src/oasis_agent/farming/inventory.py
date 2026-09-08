from dataclasses import dataclass

from ..uniswap_v4.liquidity_math import amounts_for_liquidity, sqrt_price_at_tick
from .positions import UINT128_MAX, PositionSnapshot


@dataclass(frozen=True, slots=True)
class PositionInventory:
    sqrt_price_x96: int
    sqrt_price_lower_x96: int
    sqrt_price_upper_x96: int
    principal0: int
    principal1: int
    uncollected_fee0: int
    uncollected_fee1: int
    total0: int
    total1: int
    in_range: bool


def position_inventory(snapshot: PositionSnapshot, sqrt_price_x96: int) -> PositionInventory:
    if not 0 <= snapshot.liquidity <= UINT128_MAX:
        raise ValueError("snapshot liquidity must fit uint128")
    lower = sqrt_price_at_tick(snapshot.tick_lower)
    upper = sqrt_price_at_tick(snapshot.tick_upper)
    principal0, principal1 = amounts_for_liquidity(
        sqrt_price_x96,
        lower,
        upper,
        snapshot.liquidity,
        round_up=False,
    )
    return PositionInventory(
        sqrt_price_x96,
        lower,
        upper,
        principal0,
        principal1,
        snapshot.uncollected_fee0,
        snapshot.uncollected_fee1,
        principal0 + snapshot.uncollected_fee0,
        principal1 + snapshot.uncollected_fee1,
        lower <= sqrt_price_x96 < upper,
    )


__all__ = ["PositionInventory", "position_inventory"]
