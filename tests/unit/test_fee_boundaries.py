import unittest
from decimal import Decimal

from oasis_agent.errors import FeeEncodingError
from oasis_agent.models.v4 import DYNAMIC_FEE_FLAG, OVERRIDE_FEE_FLAG, SwapDirection
from oasis_agent.uniswap_v4.fees import (
    calculate_swap_fee,
    fee_fraction,
    protocol_fee_for_direction,
    settled_fee_breakdown,
    validate_fee_override,
    validate_lp_fee,
    validate_pool_fee,
    validate_protocol_fee,
)


class FeeBoundaryTests(unittest.TestCase):
    def test_lp_fee_rejects_values_above_one_hundred_percent(self) -> None:
        self.assertEqual(validate_lp_fee(1_000_000), 1_000_000)
        self.assertEqual(fee_fraction(1_000_000), Decimal(1))
        with self.assertRaises(FeeEncodingError):
            validate_lp_fee(1_000_001)
        with self.assertRaises(FeeEncodingError):
            validate_lp_fee(0xFFFFFF)

    def test_dynamic_pool_flag_is_not_an_lp_fee(self) -> None:
        self.assertEqual(validate_pool_fee(DYNAMIC_FEE_FLAG), DYNAMIC_FEE_FLAG)
        with self.assertRaises(FeeEncodingError):
            validate_lp_fee(DYNAMIC_FEE_FLAG)

    def test_override_flag_is_removed_before_economic_validation(self) -> None:
        self.assertEqual(validate_fee_override(OVERRIDE_FEE_FLAG | 2500), 2500)
        with self.assertRaises(FeeEncodingError):
            validate_fee_override(OVERRIDE_FEE_FLAG | 1_000_001)

    def test_protocol_fee_is_packed_and_directional(self) -> None:
        packed = (300 << 12) | 200
        self.assertEqual(validate_protocol_fee(packed), packed)
        self.assertEqual(protocol_fee_for_direction(packed, SwapDirection.ZERO_FOR_ONE), 200)
        self.assertEqual(protocol_fee_for_direction(packed, SwapDirection.ONE_FOR_ZERO), 300)

    def test_protocol_and_lp_fees_compound_instead_of_adding(self) -> None:
        packed = (300 << 12) | 200
        breakdown = settled_fee_breakdown(100_000, packed, SwapDirection.ZERO_FOR_ONE)
        self.assertEqual(breakdown.lp_fee, 100_000)
        self.assertEqual(breakdown.protocol_fee, 200)
        self.assertEqual(breakdown.swap_fee, 100_180)
        self.assertEqual(calculate_swap_fee(100_000, 200), 100_180)


if __name__ == "__main__":
    unittest.main()
