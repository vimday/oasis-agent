import unittest

from oasis_agent.uniswap_v4.liquidity_math import (
    MAX_SQRT_PRICE,
    MAX_TICK,
    MIN_SQRT_PRICE,
    MIN_TICK,
    amount0_delta,
    amount1_delta,
    amounts_for_liquidity,
    sqrt_price_at_tick,
)


class LiquidityMathTests(unittest.TestCase):
    def test_tick_math_matches_canonical_boundary_and_unit_vectors(self) -> None:
        self.assertEqual(sqrt_price_at_tick(MIN_TICK), MIN_SQRT_PRICE)
        self.assertEqual(sqrt_price_at_tick(MAX_TICK), MAX_SQRT_PRICE)
        self.assertEqual(sqrt_price_at_tick(0), 1 << 96)
        self.assertEqual(sqrt_price_at_tick(-1), 79_224_201_403_219_477_170_569_942_574)
        self.assertEqual(sqrt_price_at_tick(1), 79_232_123_823_359_799_118_286_999_568)

    def test_rejects_tick_outside_canonical_bounds(self) -> None:
        for tick in (MIN_TICK - 1, MAX_TICK + 1):
            with self.subTest(tick=tick), self.assertRaisesRegex(ValueError, "TickMath"):
                sqrt_price_at_tick(tick)

    def test_position_amounts_follow_three_price_regimes(self) -> None:
        lower = sqrt_price_at_tick(-60)
        middle = sqrt_price_at_tick(0)
        upper = sqrt_price_at_tick(60)
        liquidity = 1_000_000
        self.assertEqual(amounts_for_liquidity(lower, lower, upper, liquidity), (5999, 0))
        self.assertEqual(amounts_for_liquidity(middle, lower, upper, liquidity), (2995, 2995))
        self.assertEqual(amounts_for_liquidity(upper, lower, upper, liquidity), (0, 5999))

    def test_mint_rounding_is_conservative_relative_to_removal(self) -> None:
        lower = sqrt_price_at_tick(-60)
        middle = sqrt_price_at_tick(0)
        upper = sqrt_price_at_tick(60)
        down = amounts_for_liquidity(middle, lower, upper, 1_000_000)
        up = amounts_for_liquidity(middle, lower, upper, 1_000_000, round_up=True)
        self.assertEqual(down, (2995, 2995))
        self.assertEqual(up, (2996, 2996))

    def test_amount_delta_is_symmetric_in_price_order(self) -> None:
        lower = sqrt_price_at_tick(-120)
        upper = sqrt_price_at_tick(120)
        self.assertEqual(
            amount0_delta(lower, upper, 123_456, round_up=False),
            amount0_delta(upper, lower, 123_456, round_up=False),
        )
        self.assertEqual(
            amount1_delta(lower, upper, 123_456, round_up=True),
            amount1_delta(upper, lower, 123_456, round_up=True),
        )


if __name__ == "__main__":
    unittest.main()
