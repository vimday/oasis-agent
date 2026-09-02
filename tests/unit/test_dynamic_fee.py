import unittest
from decimal import Decimal

from oasis_agent.farming.strategy import TransparentFeeStrategy
from oasis_agent.models.finance import PoolState
from oasis_agent.models.v4 import SwapDirection


class DynamicFeeTests(unittest.TestCase):
    def test_fee_is_bounded_and_reproducible(self) -> None:
        state = PoolState(Decimal("0.2"), Decimal("0.4"), Decimal("0.1"), Decimal("0.05"))
        strategy = TransparentFeeStrategy(100, 10_000, 500)
        first = strategy.quote(state, SwapDirection.ZERO_FOR_ONE)
        second = strategy.quote(state, SwapDirection.ZERO_FOR_ONE)
        self.assertEqual(first, second)
        self.assertGreaterEqual(first.effective_fee, first.min_fee)
        self.assertLessEqual(first.effective_fee, first.max_fee)


if __name__ == "__main__":
    unittest.main()
