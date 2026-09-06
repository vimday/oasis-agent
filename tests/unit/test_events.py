import unittest

from oasis_agent.models.chain import LogRecord
from oasis_agent.uniswap_v4.events import (
    MODIFY_LIQUIDITY_TOPIC,
    MODIFY_POSITION_TOPIC,
    POOL_INITIALIZED_TOPIC,
    SWAP_TOPIC,
    decode_modify_liquidity,
    decode_modify_position,
    decode_pool_initialized,
    decode_swap,
)


def word(value: int) -> str:
    return f"{value & ((1 << 256) - 1):064x}"


class EventTests(unittest.TestCase):
    def test_decode_modify_liquidity_preserves_position_salt(self) -> None:
        pool_id = "0x" + "1" * 64
        sender = "0x" + "0" * 24 + "2" * 40
        salt = 7
        data = "0x" + "".join([word(-120), word(120), word(-456), word(salt)])
        log = LogRecord(
            "0x" + "3" * 40,
            (MODIFY_LIQUIDITY_TOPIC, pool_id, sender),
            data,
            10,
            "0x" + "4" * 64,
            0,
        )
        decoded = decode_modify_liquidity(log)
        self.assertEqual(decoded.pool_id, pool_id)
        self.assertEqual(decoded.sender, "0x" + "2" * 40)
        self.assertEqual((decoded.tick_lower, decoded.tick_upper), (-120, 120))
        self.assertEqual(decoded.liquidity_delta, -456)
        self.assertEqual(decoded.salt, "0x" + "0" * 63 + "7")

    def test_decode_modify_liquidity_allows_zero_delta_fee_poke(self) -> None:
        data = "0x" + "".join([word(-120), word(120), word(0), word(7)])
        log = LogRecord(
            "0x" + "3" * 40,
            (
                MODIFY_LIQUIDITY_TOPIC,
                "0x" + "1" * 64,
                "0x" + "0" * 24 + "2" * 40,
            ),
            data,
            10,
            "0x" + "4" * 64,
            0,
        )
        self.assertEqual(decode_modify_liquidity(log).liquidity_delta, 0)

    def test_decode_position_manager_mirror_event(self) -> None:
        data = "0x" + "".join([word(-120), word(120), word(10), word(7)])
        log = LogRecord(
            "0x" + "3" * 40,
            (
                MODIFY_POSITION_TOPIC,
                "0x" + "1" * 64,
                "0x" + "0" * 24 + "2" * 40,
            ),
            data,
            10,
            "0x" + "4" * 64,
            0,
        )
        self.assertEqual(decode_modify_position(log).sender, "0x" + "2" * 40)

    def test_decode_swap_uses_indexed_sender_and_fee_word(self) -> None:
        pool_id = "0x" + "1" * 64
        data = "0x" + "".join(
            [
                word((1 << 256) - 7),
                word(9),
                word(123),
                word(456),
                word((1 << 256) - 2),
                word(2500),
            ]
        )
        log = LogRecord(
            "0x" + "3" * 40,
            (SWAP_TOPIC, pool_id, "0x" + "0" * 24 + "2" * 40),
            data,
            10,
            "0x" + "4" * 64,
            0,
        )
        decoded = decode_swap(log)
        self.assertEqual(decoded.pool_id, pool_id)
        self.assertEqual(decoded.sender, "0x" + "2" * 40)
        self.assertEqual(decoded.amount0, -7)
        self.assertEqual(decoded.amount1, 9)
        self.assertEqual(decoded.tick, -2)
        self.assertEqual(decoded.fee, 2500)

    def test_decode_swap_rejects_shifted_topics(self) -> None:
        pool_id = "0x" + "1" * 64
        sender = "0x" + "0" * 24 + "2" * 40
        data = "0x" + "".join([word(-7), word(9), word(123), word(456), word(-2), word(2500)])
        log = LogRecord("0x" + "3" * 40, (pool_id, sender), data, 10, "0x" + "4" * 64, 0)
        with self.assertRaisesRegex(ValueError, "invalid Swap event topics"):
            decode_swap(log)

    def test_decode_swap_rejects_extra_topics(self) -> None:
        pool_id = "0x" + "1" * 64
        sender = "0x" + "0" * 24 + "2" * 40
        data = "0x" + "".join([word(-7), word(9), word(123), word(456), word(-2), word(2500)])
        log = LogRecord(
            "0x" + "3" * 40,
            (SWAP_TOPIC, pool_id, sender, "0x" + "0" * 64),
            data,
            10,
            "0x" + "4" * 64,
            0,
        )
        with self.assertRaisesRegex(ValueError, "invalid Swap event topics"):
            decode_swap(log)

    def test_decode_swap_rejects_noncanonical_signed_word(self) -> None:
        pool_id = "0x" + "1" * 64
        sender = "0x" + "0" * 24 + "2" * 40
        data = "0x" + "".join(
            [word((1 << 128) - 7), word(9), word(123), word(456), word(-2), word(2500)]
        )
        log = LogRecord(
            "0x" + "3" * 40,
            (SWAP_TOPIC, pool_id, sender),
            data,
            10,
            "0x" + "4" * 64,
            0,
        )
        with self.assertRaisesRegex(ValueError, "sign-extended"):
            decode_swap(log)

    def test_decode_pool_initialized_uses_three_indexed_topics(self) -> None:
        pool_id = "0x" + "1" * 64
        currency0 = "0x" + "0" * 24 + "a" * 40
        currency1 = "0x" + "0" * 24 + "b" * 40
        data = "0x" + "".join(
            [
                word(60),
                word(123),
                word(int("c" * 40, 16)),
                word(456),
                word(-1),
            ]
        )
        log = LogRecord(
            "0x" + "3" * 40,
            (POOL_INITIALIZED_TOPIC, pool_id, currency0, currency1),
            data,
            10,
            "0x" + "4" * 64,
            0,
        )
        decoded = decode_pool_initialized(log)
        self.assertEqual(decoded.pool_id, pool_id)
        self.assertEqual(decoded.key.currency0, "0x" + "a" * 40)
        self.assertEqual(decoded.key.currency1, "0x" + "b" * 40)
        self.assertEqual(decoded.key.tick_spacing, 123)
        self.assertEqual(decoded.tick, -1)

    def test_decode_pool_initialized_rejects_sqrt_price_over_uint160(self) -> None:
        pool_id = "0x" + "1" * 64
        currency0 = "0x" + "0" * 24 + "a" * 40
        currency1 = "0x" + "0" * 24 + "b" * 40
        data = "0x" + "".join([word(60), word(123), word(0), word(1 << 160), word(0)])
        log = LogRecord(
            "0x" + "3" * 40,
            (POOL_INITIALIZED_TOPIC, pool_id, currency0, currency1),
            data,
            10,
            "0x" + "4" * 64,
            0,
        )
        with self.assertRaisesRegex(ValueError, "out-of-range"):
            decode_pool_initialized(log)


if __name__ == "__main__":
    unittest.main()
