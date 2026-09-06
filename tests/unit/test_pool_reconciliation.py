from __future__ import annotations

import unittest

from oasis_agent.models.chain import BlockSnapshot
from oasis_agent.models.common import BlockRef
from oasis_agent.models.v4 import DYNAMIC_FEE_FLAG, PoolInitialized, PoolKey, SwapObservation
from oasis_agent.uniswap_v4.pool_id import pool_id_from_key
from oasis_agent.uniswap_v4.reconciliation import PoolCheckpoint, reconcile_pool_checkpoint
from oasis_agent.uniswap_v4.replay import PoolInitializationEvent, SwapReplayEvent, replay_swaps
from oasis_agent.uniswap_v4.state_rpc import Slot0State


class PoolReconciliationTests(unittest.TestCase):
    def make_replay(self, fee: int, effective_fee: int):
        block = BlockRef(4663, 10, "0x" + "aa" * 32, 1_000)
        snapshot = BlockSnapshot(block, "0x" + "bb" * 32)
        key = PoolKey(
            "0x" + "11" * 20,
            "0x" + "22" * 20,
            fee,
            60,
            "0x" + "33" * 20,
        )
        pool_id = pool_id_from_key(key)
        initialization = PoolInitializationEvent(
            block,
            "0x" + "44" * 32,
            0,
            0,
            PoolInitialized(pool_id, key, 1 << 96, 0, 10),
        )
        swap = SwapReplayEvent(
            block,
            "0x" + "55" * 32,
            0,
            1,
            SwapObservation(
                pool_id,
                "0x" + "66" * 20,
                -10,
                9,
                (1 << 96) + 1,
                123,
                1,
                effective_fee,
                10,
            ),
        )
        return replay_swaps(initialization, (snapshot,), (swap,)), pool_id, block

    def test_dynamic_persistent_and_per_swap_fee_are_not_conflated(self) -> None:
        replay, pool_id, block = self.make_replay(DYNAMIC_FEE_FLAG, 2_000)
        result = reconcile_pool_checkpoint(
            replay,
            PoolCheckpoint(block, pool_id, Slot0State((1 << 96) + 1, 1, 0, 0), 123),
        )
        self.assertTrue(result.closed)
        self.assertEqual(result.dynamic_persistent_fee, 0)
        self.assertEqual(result.last_effective_swap_fee, 2_000)

    def test_static_fee_must_match_state_view(self) -> None:
        replay, pool_id, block = self.make_replay(3_000, 3_000)
        with self.assertRaisesRegex(ValueError, "static configured fee"):
            reconcile_pool_checkpoint(
                replay,
                PoolCheckpoint(block, pool_id, Slot0State((1 << 96) + 1, 1, 0, 3_001), 123),
            )


if __name__ == "__main__":
    unittest.main()
