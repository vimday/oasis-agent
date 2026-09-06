import unittest
from decimal import Decimal

from oasis_agent.farming.strategy import Band, BandTableFeeStrategy
from oasis_agent.models.finance import PoolState
from oasis_agent.models.v4 import SwapDirection


class BandTableFeeStrategyTests(unittest.TestCase):
    @staticmethod
    def state(
        volatility: Decimal = Decimal(0),
        inventory: Decimal = Decimal(0),
        adverse: Decimal = Decimal(0),
        incentive: Decimal = Decimal(0),
        **kwargs: object,
    ) -> PoolState:
        return PoolState(
            volatility,
            inventory,
            adverse,
            incentive,
            observed_at_timestamp=1_000,
            oracle_timestamp=995,
            **kwargs,  # type: ignore[arg-type]
        )

    @staticmethod
    def strategy(**overrides: object) -> BandTableFeeStrategy:
        base = {
            "min_fee": 100,
            "max_fee": 10_000,
            "base_fee": 500,
            "volatility_bands": (Band(100, 100), Band(1_000, 400)),
            "impact_bands0": (Band(1_000_000, 200), Band(10_000_000, 800)),
            "impact_bands1": (Band(1_000, 200), Band(10_000, 800)),
            "toxicity_bands": (Band(100, 100), Band(500, 300)),
            "inventory_bands": (Band(100, 100), Band(500, 500)),
        }
        base.update(overrides)
        return BandTableFeeStrategy(**base)  # type: ignore[arg-type]

    def test_band_tables_must_be_monotone(self) -> None:
        with self.assertRaisesRegex(ValueError, "monotone"):
            self.strategy(volatility_bands=(Band(100, 400), Band(1_000, 100)))
        with self.assertRaisesRegex(ValueError, "strictly ascending"):
            self.strategy(impact_bands0=(Band(100, 100), Band(100, 200)))

    def test_premium_cannot_exceed_per_term_cap(self) -> None:
        with self.assertRaisesRegex(ValueError, "per-term cap"):
            self.strategy(inventory_bands=(Band(100, 10_000),))

    def test_empty_tables_yield_base_fee(self) -> None:
        strategy = self.strategy(
            volatility_bands=(),
            impact_bands0=(),
            impact_bands1=(),
            toxicity_bands=(),
            inventory_bands=(),
        )
        quote = strategy.quote(self.state(), SwapDirection.ZERO_FOR_ONE, 1_000, True)
        self.assertEqual(quote.effective_fee, 500)

    def test_band_selection_boundaries(self) -> None:
        strategy = self.strategy()
        # 0 <= magnitude < first bound -> first premium; >= last bound -> last premium.
        # Volatility and toxicity are zero here, so both pick their LOW band premium;
        # impact_bands0 is token-denominated so amount=1 stays below the first bound.
        low = strategy.quote(self.state(), SwapDirection.ZERO_FOR_ONE, 1, True)
        high = strategy.quote(self.state(), SwapDirection.ZERO_FOR_ONE, 1_000_000_000, True)
        # low: base 500 + volatility 100 + impact0 200 + toxicity 100
        self.assertEqual(low.effective_fee, 500 + 100 + 200 + 100)
        # high: base 500 + volatility 100 + impact0 800 + toxicity 100
        self.assertEqual(high.effective_fee, 500 + 100 + 800 + 100)

    def test_impact_bands_are_per_token_and_path_aware(self) -> None:
        strategy = self.strategy()
        # 500_000 sits below impact_bands0's first bound (200) but above
        # impact_bands1's last bound (800), so the same raw integer must
        # select different per-token premiums.
        quote0 = strategy.quote(self.state(), SwapDirection.ZERO_FOR_ONE, 500_000, True)
        quote1 = strategy.quote(self.state(), SwapDirection.ONE_FOR_ZERO, 500_000, True)
        self.assertNotEqual(quote0.effective_fee, quote1.effective_fee)
        # Exact-output zeroForOne names the output asset, which is token1.
        quote_out = strategy.quote(self.state(), SwapDirection.ZERO_FOR_ONE, 500_000, False)
        self.assertEqual(quote_out.effective_fee, quote1.effective_fee)

    def test_inventory_direction_sign_is_preserved(self) -> None:
        strategy = self.strategy()
        excess0 = self.state(inventory=Decimal("0.04"))  # I > 0: excess token0
        excess1 = self.state(inventory=Decimal("-0.04"))  # I < 0: excess token1
        # zeroForOne sends token0 into the pool and worsens positive I.
        worsening = strategy.quote(excess0, SwapDirection.ZERO_FOR_ONE, 1_000, True)
        # zeroForOne improves negative I, so no inventory premium.
        improving = strategy.quote(excess1, SwapDirection.ZERO_FOR_ONE, 1_000, True)
        self.assertGreater(worsening.effective_fee, improving.effective_fee)
        # oneForZero worsens negative I.
        worsening_reverse = strategy.quote(excess1, SwapDirection.ONE_FOR_ZERO, 1_000, True)
        improving_reverse = strategy.quote(excess0, SwapDirection.ONE_FOR_ZERO, 1_000, True)
        self.assertGreater(worsening_reverse.effective_fee, improving_reverse.effective_fee)
        # Absent skew: no inventory premium in either direction. 500 stays below
        # the first bound of both per-token impact tables, so the non-inventory
        # terms cancel and any difference would have to come from inventory.
        neutral = self.state(inventory=Decimal(0))
        self.assertEqual(
            strategy.quote(neutral, SwapDirection.ZERO_FOR_ONE, 500, True).effective_fee,
            strategy.quote(neutral, SwapDirection.ONE_FOR_ZERO, 500, True).effective_fee,
        )

    def test_benign_flow_discount_is_applied_and_capped(self) -> None:
        strategy = self.strategy(benign_flow_discount=200)
        state = self.state()
        base = strategy.quote(state, SwapDirection.ZERO_FOR_ONE, 1_000, True, benign_flow=False)
        discounted = strategy.quote(
            state, SwapDirection.ZERO_FOR_ONE, 1_000, True, benign_flow=True
        )
        self.assertEqual(discounted.effective_fee, base.effective_fee - 200)
        self.assertGreaterEqual(discounted.effective_fee, discounted.min_fee)

    def test_fee_is_clamped_to_bounds(self) -> None:
        # The per-term cap is max_fee - min_fee = 9_900; the sum of the two capped
        # terms already exceeds max_fee, so the final quote must clamp to max_fee.
        strategy = self.strategy(
            volatility_bands=(Band(1, 9_900),), toxicity_bands=(Band(1, 9_900),)
        )
        quote = strategy.quote(self.state(), SwapDirection.ZERO_FOR_ONE, 1_000, True)
        self.assertEqual(quote.effective_fee, 10_000)

    def test_stale_oracle_is_rejected(self) -> None:
        strategy = self.strategy(max_oracle_age_seconds=4)
        with self.assertRaisesRegex(ValueError, "stale"):
            strategy.quote(self.state(), SwapDirection.ZERO_FOR_ONE, 1_000, True)

    def test_rate_limit_binds_change_from_previous_fee(self) -> None:
        state = self.state(
            Decimal(0),
            adverse=Decimal(0),
            previous_fee=500,
            previous_fee_timestamp=998,
        )
        strategy = self.strategy(max_fee_change_per_second=25)
        quote = strategy.quote(state, SwapDirection.ZERO_FOR_ONE, 1_000, True)
        # 2 elapsed seconds * 25 pips/second = 50 pips of allowed movement.
        self.assertLessEqual(abs(quote.effective_fee - 500), 50)

    def test_zero_amount_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "nonzero"):
            self.strategy().quote(self.state(), SwapDirection.ZERO_FOR_ONE, 0, True)

    def test_bool_amount_is_rejected(self) -> None:
        with self.assertRaises(TypeError):
            self.strategy().quote(self.state(), SwapDirection.ZERO_FOR_ONE, True, True)  # type: ignore[arg-type]

    def test_quote_is_reproducible(self) -> None:
        strategy = self.strategy()
        first = strategy.quote(self.state(), SwapDirection.ZERO_FOR_ONE, 5_000_000, True)
        second = strategy.quote(self.state(), SwapDirection.ZERO_FOR_ONE, 5_000_000, True)
        self.assertEqual(first, second)
        self.assertTrue(first.state_hash)


if __name__ == "__main__":
    unittest.main()
