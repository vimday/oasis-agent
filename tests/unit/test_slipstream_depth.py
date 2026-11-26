import pytest

from oasis_agent.fastpath.slipstream_depth import (
    DepthCoverageError,
    DepthQuote,
    SlipstreamDepth,
    initialized_ticks,
)
from oasis_agent.uniswap_v4.liquidity_math import amount0_delta, amount1_delta, sqrt_price_at_tick


def test_bitmap_gaps_and_negative_tick_compression() -> None:
    assert initialized_ticks({-2: 1 << 255, -1: 5, 0: 1}, 10) == {-2570, -2560, -2540, 0}
    with pytest.raises(ValueError, match="noncontiguous"):
        initialized_ticks({-2: 0, 0: 1}, 10)
    with pytest.raises(ValueError, match="disagree"):
        SlipstreamDepth(sqrt_price_at_tick(0), 0, 1, 0, 1, {0: 1}, {})


@pytest.mark.parametrize("zero_for_one", [False, True])
def test_exact_tick_boundary_and_out_of_window(zero_for_one: bool) -> None:
    liquidity = 10**12
    tick_end = -1 if zero_for_one else 1
    page, offset = divmod(tick_end, 256)
    words = {-1: 0, 0: 0}
    words[page] = 1 << offset
    start, end = sqrt_price_at_tick(0), sqrt_price_at_tick(tick_end)
    into, out = (amount0_delta, amount1_delta) if zero_for_one else (amount1_delta, amount0_delta)
    amount = into(start, end, liquidity, round_up=True)
    # Liquidity goes to zero at the initialized boundary in the tested direction.
    net = liquidity if zero_for_one else -liquidity
    depth = SlipstreamDepth(start, 0, liquidity, 0, 1, words, {tick_end: net})
    assert depth.quote(amount, zero_for_one=zero_for_one) == DepthQuote(
        out(start, end, liquidity, round_up=False), end, 1
    )
    with pytest.raises(DepthCoverageError, match="observed tick bitmap"):
        depth.quote(amount + 1, zero_for_one=zero_for_one)


def test_same_tick_quoter_count_matches_inclusive_bitmap() -> None:
    depth = SlipstreamDepth(sqrt_price_at_tick(0), 0, 10**12, 100, 1, {0: 1}, {0: 10**12})
    assert depth.quote(1, zero_for_one=False) == DepthQuote(0, sqrt_price_at_tick(0), 1)


def test_bound_state_rejects_boolean_and_detaches_from_caller_maps() -> None:
    words, nets = {0: 1}, {0: 10**12}
    with pytest.raises(ValueError, match="integers"):
        SlipstreamDepth(sqrt_price_at_tick(0), 0, 10**12, False, 1, words, nets)
    depth = SlipstreamDepth(sqrt_price_at_tick(0), 0, 10**12, 100, 1, words, nets)
    words.clear()
    nets.clear()
    assert depth.quote(1, zero_for_one=False) == DepthQuote(0, sqrt_price_at_tick(0), 1)


def test_base_historical_depth_matches_quoter_integers() -> None:
    # Base 61,476,050 control documented in BASE_SLIPSTREAM_DEPTH_20261127.md.
    nets = {
        -201830: 8060263945145,
        -201639: 8561469204,
        -201613: -20744676816276,
        -201280: 29129671777,
        -201253: 136581152298260,
    }
    words = dict.fromkeys(range(-789, -785), 0)
    for tick in nets:
        page, bit = divmod(tick, 256)
        words[page] |= 1 << bit
    depth = SlipstreamDepth(3268834752717952977156813, -201924, 15201239936812, 80, 1, words, nets)
    assert depth.quote(54_834_356, zero_for_one=False) == DepthQuote(
        30_378_959_162_042_405,
        3_402_687_413_640_448_204_929_318,
        5,
    )
