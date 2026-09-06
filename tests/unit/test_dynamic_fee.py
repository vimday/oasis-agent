import unittest
from decimal import Decimal

from oasis_agent.farming.strategy import TransparentFeeStrategy
from oasis_agent.models.finance import PoolState
from oasis_agent.models.v4 import SwapDirection


class DynamicFeeTests(unittest.TestCase):
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

    def test_fee_is_bounded_and_reproducible(self) -> None:
        state = self.state(Decimal("0.2"), Decimal("0.4"), Decimal("0.1"), Decimal("0.05"))
        strategy = TransparentFeeStrategy(100, 10_000, 500)
        first = strategy.quote(state, SwapDirection.ZERO_FOR_ONE)
        second = strategy.quote(state, SwapDirection.ZERO_FOR_ONE)
        self.assertEqual(first, second)
        self.assertGreaterEqual(first.effective_fee, first.min_fee)
        self.assertLessEqual(first.effective_fee, first.max_fee)

    def test_direction_changes_inventory_component(self) -> None:
        state = self.state(inventory=Decimal("0.4"))
        strategy = TransparentFeeStrategy(100, 10_000, 500)
        zero_for_one = strategy.quote(state, SwapDirection.ZERO_FOR_ONE)
        one_for_zero = strategy.quote(state, SwapDirection.ONE_FOR_ZERO)
        self.assertNotEqual(zero_for_one.effective_fee, one_for_zero.effective_fee)
        self.assertEqual(zero_for_one.inputs[1], Decimal("0.4"))
        self.assertEqual(one_for_zero.inputs[1], Decimal("-0.4"))

    def test_unsigned_features_cannot_be_negative(self) -> None:
        strategy = TransparentFeeStrategy(100, 10_000, 5_000)
        for field in ("volatility", "adverse_selection", "rebalance_incentive"):
            values = {
                "volatility": Decimal(0),
                "inventory_imbalance": Decimal(0),
                "adverse_selection": Decimal(0),
                "rebalance_incentive": Decimal(0),
            }
            values[field] = Decimal("-0.1")
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "nonnegative"):
                strategy.quote(
                    PoolState(
                        **values,
                        observed_at_timestamp=1_000,
                        oracle_timestamp=995,
                    ),
                    SwapDirection.ZERO_FOR_ONE,
                )

    def test_stale_oracle_is_rejected(self) -> None:
        state = self.state()
        with self.assertRaisesRegex(ValueError, "stale"):
            TransparentFeeStrategy(100, 10_000, 500, max_oracle_age_seconds=4).quote(
                state, SwapDirection.ZERO_FOR_ONE
            )

    def test_fee_change_is_bounded_by_elapsed_time(self) -> None:
        state = self.state(
            Decimal(10),
            adverse=Decimal(10),
            previous_fee=500,
            previous_fee_timestamp=998,
        )
        strategy = TransparentFeeStrategy(100, 10_000, 500, max_fee_change_per_second=25)
        quote = strategy.quote(state, SwapDirection.ZERO_FOR_ONE)
        self.assertEqual(quote.effective_fee, 550)
        self.assertEqual(quote.rate_limit_elapsed_seconds, 2)

    def test_same_timestamp_burst_cannot_ratchet_fee(self) -> None:
        state = self.state(
            Decimal(10),
            previous_fee=500,
            previous_fee_timestamp=1_000,
        )
        quote = TransparentFeeStrategy(100, 10_000, 500).quote(state, SwapDirection.ZERO_FOR_ONE)
        self.assertEqual(quote.effective_fee, 500)
        self.assertEqual(quote.valid_until_timestamp, 1_005)
        self.assertEqual(len(quote.state_hash), 64)

    def test_timestamp_pairs_and_future_oracle_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "supplied together"):
            TransparentFeeStrategy(100, 10_000, 500).quote(
                self.state(previous_fee=500), SwapDirection.ZERO_FOR_ONE
            )
        with self.assertRaisesRegex(ValueError, "future"):
            TransparentFeeStrategy(100, 10_000, 500).quote(
                PoolState(
                    Decimal(0),
                    Decimal(0),
                    Decimal(0),
                    Decimal(0),
                    observed_at_timestamp=1_000,
                    oracle_timestamp=1_001,
                ),
                SwapDirection.ZERO_FOR_ONE,
            )


if __name__ == "__main__":
    unittest.main()
