from fractions import Fraction
from itertools import pairwise
from random import Random

import pytest

from oasis_agent.fastpath.v2_sizing import V2Leg, V2Pool, V2Route


def _two_pool_route() -> V2Route:
    return V2Route(
        (
            V2Leg(V2Pool("first", "A", "B", 615, 1497, 997, 1000), "A"),
            V2Leg(V2Pool("second", "A", "B", 1730, 1521, 997, 1000), "B"),
        )
    )


def test_integer_optimum_beats_float_peak_neighborhood_with_exact_cost() -> None:
    route = _two_pool_route()
    bounds = route.all_input_bounds(total_cost_in_anchor=134)
    assert bounds.gross_upper == 135 and bounds.net_upper == 1
    result = route.size(1, 250, total_cost_in_anchor=134)
    assert result.complete and result.best is not None
    assert result.best.amount_in in (204, 207)
    assert result.best.gross_profit == result.gross_upper == 135
    assert result.best_net == result.net_upper == 1

    loss = route.size(1, 250, total_cost_in_anchor=136)
    assert loss.complete and loss.best_net == -1


def test_budget_reports_containing_residuals_until_the_best_value_is_proven() -> None:
    route = _two_pool_route()
    gap = route.size(1, 250, total_cost_in_anchor=134, max_quotes=1, max_steps=1)
    assert gap.best is not None and gap.best_net is not None
    assert gap.quotes_evaluated == gap.steps == 1
    assert not gap.complete
    assert gap.net_upper == 1
    for amount in range(1, 251):
        true_net = route.quote(amount).gross_profit - 134
        if true_net > gap.best_net:
            covering = [r for r in gap.residuals if r.min_amount_in <= amount <= r.max_amount_in]
            assert len(covering) == 1
            assert true_net <= covering[0].net_upper


def test_many_small_routes_against_exhaustive_integer_amounts() -> None:
    random = Random(20261123)
    for _ in range(100):
        count = random.randrange(2, 7)
        legs = []
        for index in range(count):
            token_in, token_out = str(index), str((index + 1) % count)
            left, right = sorted((token_in, token_out), reverse=bool(index % 2))
            multiplier, denominator = random.choice([(997, 1000), (9975, 10000), (10**18, 10**18)])
            pool = V2Pool(
                f"pool-{index}",
                left,
                right,
                random.randrange(1, 350),
                random.randrange(1, 350),
                multiplier,
                denominator,
            )
            legs.append(V2Leg(pool, token_in))
        route = V2Route(tuple(legs))
        cost = random.randrange(25)
        low = random.randrange(1, 50)
        high = random.randrange(low, 180)
        quote_map = {}
        for size in range(low, high + 1):
            output = size
            for leg in legs:
                pool = leg.pool
                x, y = (
                    (pool.reserve0, pool.reserve1)
                    if leg.token_in == pool.token0
                    else (pool.reserve1, pool.reserve0)
                )
                output = (
                    output
                    * pool.fee_multiplier
                    * y
                    // (x * pool.fee_denominator + output * pool.fee_multiplier)
                )
            assert route.quote(size).amount_out == output
            if output:
                quote_map[size] = output - size - cost
        expected = max(quote_map.values(), default=None)
        result = route.size(low, high, total_cost_in_anchor=cost)
        assert result.complete
        assert result.best_net == result.net_upper == expected
        if expected is not None:
            lower, upper = (
                result.best_net,
                route.all_input_bounds(total_cost_in_anchor=cost).net_upper,
            )
            assert lower is not None and lower <= upper

        capped = route.size(low, high, total_cost_in_anchor=cost, max_quotes=4, max_steps=2)
        assert capped.quotes_evaluated <= 4 and capped.steps <= 2
        if expected is None:
            assert capped.best is None and capped.complete
            continue
        assert capped.best_net is not None and capped.net_upper is not None
        assert capped.best_net <= expected <= capped.net_upper
        intervals = sorted((item.min_amount_in, item.max_amount_in) for item in capped.residuals)
        for first, second in pairwise(intervals):
            assert first[1] < second[0]
        for size, profit in quote_map.items():
            if profit > capped.best_net:
                assert any(
                    item.min_amount_in <= size <= item.max_amount_in and item.net_upper >= profit
                    for item in capped.residuals
                )


def test_thin_pool_skips_output_plateaus_across_huge_input_domain() -> None:
    route = V2Route(
        (
            V2Leg(V2Pool("thin", "A", "B", 10**18, 80, 997, 1000), "A"),
            V2Leg(V2Pool("return", "A", "B", 10**21, 400, 9975, 10000), "B"),
        )
    )
    result = route.size(1, 10**22, total_cost_in_anchor=10**16, max_quotes=200, max_steps=200)
    assert result.complete and result.best is not None
    assert len(result.best.amounts) == 3
    assert result.best.amount_out == route.quote(result.best.amount_in).amount_out
    assert result.quotes_evaluated <= 158
    assert result.best_net == result.net_upper
    assert result.best_net is not None and result.best_net > 0


def test_400_distinct_pool_cycle_and_repeated_asset() -> None:
    legs = tuple(
        V2Leg(V2Pool(str(i), "A", "B", 10**25, 10**25, 999, 1000), "A" if i % 2 == 0 else "B")
        for i in range(400)
    )
    route = V2Route(legs)
    result = route.size(10**12, 10**12 + 20, total_cost_in_anchor=50)
    expected = max(route.quote(10**12 + offset).gross_profit - 50 for offset in range(21))
    assert result.complete and result.best_net == result.net_upper == expected
    assert result.best is not None and len(result.best.amounts) == 401
    with pytest.raises(ValueError, match="2 to 400"):
        V2Route((*legs, legs[0]))


def test_balanced_curve_preserves_order_and_exact_bound_with_odd_long_route() -> None:
    rng = Random(301)
    length = 301
    legs = []
    for index in range(length):
        token_in, token_out = str(index), str((index + 1) % length)
        tokens = (token_out, token_in) if index % 2 else (token_in, token_out)
        reserve0, reserve1 = rng.randrange(10**20, 2 * 10**20), rng.randrange(10**20, 2 * 10**20)
        fee, denominator = (997, 1000) if index % 2 else (9975, 10000)
        legs.append(
            V2Leg(V2Pool(str(index), *tokens, reserve0, reserve1, fee, denominator), token_in)
        )

    route = V2Route(tuple(legs))
    for amount_in in (1, 10**18, (1 << 256) - 1):
        continuous_output = Fraction(amount_in)
        for leg in legs:
            pool = leg.pool
            reserves = (pool.reserve0, pool.reserve1)
            reserve_in, reserve_out = reserves if leg.token_in == pool.token0 else reserves[::-1]
            effective = continuous_output * pool.fee_multiplier
            continuous_output = (
                reserve_out * effective / (reserve_in * pool.fee_denominator + effective)
            )
        exact_integer_upper = continuous_output.numerator // continuous_output.denominator
        expected_gross_upper = exact_integer_upper - amount_in
        assert route._continuous_integer_upper(amount_in) == expected_gross_upper
        assert route.quote(amount_in).amount_out <= exact_integer_upper


def test_validates_anchor_direction_pool_identity_and_nontrade_sizes() -> None:
    pool = V2Pool("0x" + "aB" * 20, "A", "B", 100, 1, 1, 1)
    reverse = V2Pool("return", "A", "B", 100, 100, 1, 1)
    with pytest.raises(ValueError, match="outside the pool"):
        V2Leg(pool, "other")
    with pytest.raises(ValueError, match="contiguous"):
        V2Route((V2Leg(pool, "A"), V2Leg(reverse, "A")))
    with pytest.raises(ValueError, match="return to its anchor"):
        V2Route((V2Leg(pool, "A"), V2Leg(V2Pool("other", "B", "C", 20, 40, 1, 1), "B")))
    with pytest.raises(ValueError, match="reuse a pool"):
        upper_address = V2Pool(pool.pool_id.upper(), "A", "B", 100, 100, 997, 1000)
        V2Route((V2Leg(pool, "A"), V2Leg(upper_address, "B")))
    with pytest.raises(ValueError, match="fee multiplier"):
        V2Pool("a", "X", "Y", 1, 2, 101, 100)
    route = V2Route((V2Leg(pool, "A"), V2Leg(reverse, "B")))
    assert route.quote(0).amounts == (0, 0, 0)
    assert route.quote(10**20).amount_out == 0
    result = route.size(1, 10**20, total_cost_in_anchor=100)
    assert result.complete and result.best_net is None and result.net_upper is None
    with pytest.raises(ValueError, match="minimum input"):
        route.size(0, 5, total_cost_in_anchor=2)
