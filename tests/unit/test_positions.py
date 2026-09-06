import unittest

from oasis_agent.farming.positions import (
    Q128,
    CorePositionState,
    decode_position_info,
    position_id,
    read_position_snapshot,
)
from oasis_agent.models.common import BlockRef
from oasis_agent.models.v4 import DYNAMIC_FEE_FLAG, PoolKey
from oasis_agent.uniswap_v4.pool_id import pool_id_from_key


class FakePositionSource:
    def __init__(self, key: PoolKey, packed_info: int) -> None:
        self.key = key
        self.packed_info = packed_info
        self.owner = "0x" + "4" * 40
        self.manager_liquidity = 100
        self.core = CorePositionState(100, 10 * Q128, 5 * Q128)
        self.current_fee_growth = (12 * Q128, 8 * Q128)

    def owner_of(self, token_id: int, block: BlockRef) -> str:
        return self.owner

    def pool_and_position_info(self, token_id: int, block: BlockRef) -> tuple[PoolKey, int]:
        return self.key, self.packed_info

    def position_liquidity(self, token_id: int, block: BlockRef) -> int:
        return self.manager_liquidity

    def core_position(
        self, pool_id: str, core_position_id: str, block: BlockRef
    ) -> CorePositionState:
        return self.core

    def fee_growth_inside(
        self, pool_id: str, tick_lower: int, tick_upper: int, block: BlockRef
    ) -> tuple[int, int]:
        return self.current_fee_growth


def pack_position_info(pool_id: str, tick_lower: int, tick_upper: int, subscribed: int = 0) -> int:
    pool_prefix = int(pool_id, 16) >> 56
    return (
        (pool_prefix << 56)
        | ((tick_upper & 0xFFFFFF) << 32)
        | ((tick_lower & 0xFFFFFF) << 8)
        | subscribed
    )


class PositionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = "0x" + "1" * 40
        self.key = PoolKey(
            "0x" + "2" * 40,
            "0x" + "3" * 40,
            DYNAMIC_FEE_FLAG,
            60,
            "0x" + "5" * 40,
        )
        self.pool_id = pool_id_from_key(self.key)
        self.packed_info = pack_position_info(self.pool_id, -120, 120)
        self.block = BlockRef(4663, 100, "0x" + "a" * 64, 1_000)

    def test_position_id_matches_official_abi_encode_packed_vector(self) -> None:
        self.assertEqual(
            position_id(self.manager, -120, 120, 7),
            "0xf2e18ca03a9351b17fab58e0b3ee2d846048e51c159876442685a2c56ef8a391",
        )

    def test_decodes_packed_position_info_with_signed_ticks(self) -> None:
        decoded = decode_position_info(self.packed_info)
        self.assertEqual(decoded.tick_lower, -120)
        self.assertEqual(decoded.tick_upper, 120)
        self.assertFalse(decoded.has_subscriber)
        self.assertEqual(decoded.pool_id_prefix, self.pool_id[:52])

    def test_reads_consistent_block_pinned_position_and_accrues_fees(self) -> None:
        snapshot = read_position_snapshot(
            self.manager,
            7,
            self.block,
            FakePositionSource(self.key, self.packed_info),
        )
        self.assertEqual(snapshot.nft_owner, "0x" + "4" * 40)
        self.assertEqual(snapshot.core_position_owner, self.manager)
        self.assertEqual(snapshot.core_position_id, position_id(self.manager, -120, 120, 7))
        self.assertEqual(snapshot.liquidity, 100)
        self.assertEqual(snapshot.uncollected_fee0, 200)
        self.assertEqual(snapshot.uncollected_fee1, 300)

    def test_rejects_position_manager_and_core_liquidity_disagreement(self) -> None:
        source = FakePositionSource(self.key, self.packed_info)
        source.manager_liquidity = 99
        with self.assertRaisesRegex(ValueError, "liquidity disagreement"):
            read_position_snapshot(self.manager, 7, self.block, source)

    def test_rejects_pool_prefix_or_tick_alignment_mismatch(self) -> None:
        wrong_prefix = self.packed_info ^ (1 << 255)
        with self.assertRaisesRegex(ValueError, "pool ID prefix"):
            read_position_snapshot(
                self.manager,
                7,
                self.block,
                FakePositionSource(self.key, wrong_prefix),
            )
        misaligned = pack_position_info(self.pool_id, -119, 120)
        with self.assertRaisesRegex(ValueError, "tick spacing"):
            read_position_snapshot(
                self.manager,
                7,
                self.block,
                FakePositionSource(self.key, misaligned),
            )

    def test_fee_growth_wraparound_matches_uint256_semantics(self) -> None:
        source = FakePositionSource(self.key, self.packed_info)
        source.core = CorePositionState(2, (1 << 256) - Q128, (1 << 256) - 2 * Q128)
        source.manager_liquidity = 2
        source.current_fee_growth = (Q128, Q128)
        snapshot = read_position_snapshot(self.manager, 7, self.block, source)
        self.assertEqual(snapshot.uncollected_fee0, 4)
        self.assertEqual(snapshot.uncollected_fee1, 6)

    def test_rejects_zero_owner_and_protocol_invalid_ticks(self) -> None:
        source = FakePositionSource(self.key, self.packed_info)
        source.owner = "0x" + "0" * 40
        with self.assertRaises(ValueError):
            read_position_snapshot(self.manager, 7, self.block, source)
        for lower, upper in ((-887_280, 120), (-120, 887_280)):
            with self.subTest(lower=lower, upper=upper), self.assertRaises(ValueError):
                read_position_snapshot(
                    self.manager,
                    7,
                    self.block,
                    FakePositionSource(self.key, pack_position_info(self.pool_id, lower, upper)),
                )


if __name__ == "__main__":
    unittest.main()
