"""Spot-rate bounds must never undercut a real swap and must falsify only provable intervals."""

from fractions import Fraction

import pytest

from oasis_agent.fastpath.cycle_bounds import bound_residual, cycle_rate, spot_rate
from oasis_agent.fastpath.pool_graph import (
    CycleSizingResidual,
    PoolKind,
    PoolNode,
    TickLiquidity,
    quote_pool,
)
from oasis_agent.uniswap_v4.liquidity_math import sqrt_price_at_tick

A = "0x" + "0a" * 20
B = "0x" + "0b" * 20
P1 = "0x" + "11" * 20
P2 = "0x" + "22" * 20


def _v2(pool_id: str, r0: int, r1: int, fee: int = 3000) -> PoolNode:
    return PoolNode(8453, pool_id, PoolKind.V2, A, B, 18, 18, fee, 1, 0, r0, r1)


def _cl(pool_id: str, tick: int, liquidity: int, fee: int = 500) -> PoolNode:
    return PoolNode(
        8453,
        pool_id,
        PoolKind.V3,
        A,
        B,
        18,
        18,
        fee,
        1,
        0,
        sqrt_price_x96=sqrt_price_at_tick(tick),
        tick=tick,
        liquidity=liquidity,
        ticks=(TickLiquidity(tick - 600, liquidity), TickLiquidity(tick + 600, -liquidity)),
    )


def test_spot_rate_bounds_native_quotes_from_above() -> None:
    for pool in (_v2(P1, 10**21, 3 * 10**21), _cl(P2, 1200, 10**22)):
        for token in (A, B):
            rate = spot_rate(pool, token)
            assert rate is not None
            for amount in (10**9, 10**15, 10**18, 10**20):
                try:
                    out = quote_pool(pool, token, amount).amount_out
                except Exception:  # noqa: BLE001 - leaving the modelled range is fine here
                    continue
                assert out <= amount * rate


def test_cycle_rate_and_falsification() -> None:
    cheap = _v2(P1, 10**21, 2 * 10**21, fee=0)  # 1 A -> 2 B
    rich = _v2(P2, 10**21, 2 * 10**21, fee=0)  # 2 B -> 1 A
    pools = {P1: cheap, P2: rich}
    assert cycle_rate(pools, A, (P1, P2)) == 1
    tilted = {P1: cheap, P2: _v2(P2, 101 * 10**19, 2 * 10**21, fee=0)}
    rate = cycle_rate(tilted, A, (P1, P2))
    assert rate == Fraction(101, 100)
    residual = CycleSizingResidual(8453, A, (P1, P2), 1, 10**18, 2**255)
    assert bound_residual(tilted, residual, cost=10**16 - 1).falsified is False
    assert bound_residual(tilted, residual, cost=10**16).falsified is True
    assert bound_residual(tilted, residual, cost=10**16).profit_upper == 0


def test_cycle_must_close_and_tokens_must_belong() -> None:
    with pytest.raises(ValueError, match="return"):
        cycle_rate({P1: _v2(P1, 1, 1)}, A, (P1,))
    with pytest.raises(ValueError, match="not in the pool"):
        spot_rate(_v2(P1, 1, 1), "0x" + "cc" * 20)
