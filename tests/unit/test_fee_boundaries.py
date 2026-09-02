import unittest
from decimal import Decimal

from oasis_agent.uniswap_v4.fees import effective_fee


class FeeBoundaryTests(unittest.TestCase):
    def test_effective_fee_accepts_full_uint24(self) -> None:
        self.assertEqual(effective_fee(0xFFFFFF, 0), Decimal("16.777215"))


if __name__ == "__main__":
    unittest.main()
