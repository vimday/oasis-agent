"""Exact spot-rate bounds that falsify unresolved cycle sizing intervals.

For a constant-product or concentrated-liquidity exact-input swap, the output never exceeds
``amount_in x (1 - fee) x spot``: fees are taken first and the price only moves against the
trader. Chaining hops gives ``out(x) <= x x R`` with ``R`` the product of fee-adjusted spot
rates, so on any interval ``[lo, hi]`` the net profit is at most ``hi x (R - 1) - cost``.
When that is below the required profit, the interval provably holds no qualifying amount.
Rates are exact rationals; nothing is rounded in the trader's favour.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction

from .pool_graph import CycleSizingResidual, PoolKind, PoolNode

_FEE_DENOM = 1_000_000
_Q192 = 1 << 192


def spot_rate(pool: PoolNode, token_in: str) -> Fraction | None:
    """Fee-adjusted best marginal rate out/in, or ``None`` when no exact bound is known."""
    zero_for_one = token_in.lower() == pool.token0.lower()
    if not zero_for_one and token_in.lower() != pool.token1.lower():
        raise ValueError("token is not in the pool")
    fee = Fraction(_FEE_DENOM - pool.fee_pips, _FEE_DENOM)
    if pool.kind in (PoolKind.V3, PoolKind.SLIPSTREAM, PoolKind.V4):
        if not pool.sqrt_price_x96:
            return None
        price = Fraction(pool.sqrt_price_x96 * pool.sqrt_price_x96, _Q192)  # token1 per token0
        return fee * (price if zero_for_one else 1 / price)
    if pool.kind == PoolKind.V2:
        if not pool.reserve0 or not pool.reserve1:
            return None
        rin, rout = (
            (pool.reserve0, pool.reserve1) if zero_for_one else (pool.reserve1, pool.reserve0)
        )
        return fee * Fraction(rout, rin)
    return None  # stable-swap and unknown curves have no linear spot bound here


def cycle_rate(
    pools: Mapping[str, PoolNode], base_token: str, pool_ids: Sequence[str]
) -> Fraction | None:
    """Product of spot rates along the cycle that starts and ends at ``base_token``."""
    token = base_token.lower()
    rate = Fraction(1)
    for pool_id in pool_ids:
        pool = pools[pool_id.lower()]
        hop = spot_rate(pool, token)
        if hop is None:
            return None
        rate *= hop
        token = (pool.token1 if token == pool.token0.lower() else pool.token0).lower()
    if token != base_token.lower():
        raise ValueError("cycle does not return to its base token")
    return rate


@dataclass(frozen=True, slots=True)
class ResidualBound:
    pool_ids: tuple[str, ...]
    base_token: str
    rate: Fraction | None
    profit_upper: int | None  # floor of hi x (R - 1) - cost, in base units
    falsified: bool


def bound_residual(
    pools: Mapping[str, PoolNode], residual: CycleSizingResidual, cost: int, min_profit: int = 1
) -> ResidualBound:
    """Bound one residual; ``cost`` is the cycle's full gas plus L1 cost in base units."""
    rate = cycle_rate(pools, residual.base_token, residual.pool_ids)
    if rate is None:
        return ResidualBound(residual.pool_ids, residual.base_token, None, None, False)
    upper_fraction = residual.max_amount_in * (rate - 1) - cost
    upper = upper_fraction.numerator // upper_fraction.denominator
    return ResidualBound(
        residual.pool_ids,
        residual.base_token,
        rate,
        upper,
        falsified=upper < min_profit,
    )
