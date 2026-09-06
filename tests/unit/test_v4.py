import unittest

from oasis_agent.models import PoolKey
from oasis_agent.uniswap_v4.delta import decode_balance_delta
from oasis_agent.uniswap_v4.fees import validate_fee_override


class V4PrimitiveTests(unittest.TestCase):
    def test_signed_delta(self) -> None:
        value = ((1 << 128) - 7) << 128 | 9
        delta = decode_balance_delta(value.to_bytes(32, "big"))
        self.assertEqual(delta.amount0, -7)
        self.assertEqual(delta.amount1, 9)

    def test_override_flag_only_decodes_zero_fee(self) -> None:
        self.assertEqual(validate_fee_override(0x400000), 0)

    def test_fee_override_is_decoded(self) -> None:
        self.assertEqual(validate_fee_override(0x400000 | 2500), 2500)

    def test_pool_key_rejects_fee_over_uint24(self) -> None:
        with self.assertRaises(ValueError):
            PoolKey("0x" + "0" * 40, "0x" + "1" * 40, 0x1000000, 60, "0x" + "2" * 40)

    def test_pool_key_rejects_protocol_invalid_tick_spacing(self) -> None:
        for spacing in (-1, 0, 32_768):
            with self.subTest(spacing=spacing), self.assertRaisesRegex(ValueError, "tick_spacing"):
                PoolKey(
                    "0x" + "0" * 40,
                    "0x" + "1" * 40,
                    3_000,
                    spacing,
                    "0x" + "2" * 40,
                )

    def test_pool_key_requires_canonical_currency_order(self) -> None:
        with self.assertRaisesRegex(ValueError, "canonical currency order"):
            PoolKey(
                "0x" + "2" * 40,
                "0x" + "1" * 40,
                3_000,
                60,
                "0x" + "3" * 40,
            )


if __name__ == "__main__":
    unittest.main()
