import unittest

from oasis_agent.models.chain import BlockSnapshot, LogRecord
from oasis_agent.models.common import BlockRef
from oasis_agent.models.v4 import (
    DYNAMIC_FEE_FLAG,
    ModifyLiquidityObservation,
    PoolInitialized,
    PoolKey,
    SwapObservation,
)
from oasis_agent.uniswap_v4.pool_id import pool_id_from_key
from oasis_agent.uniswap_v4.replay import (
    LiquidityReplayEvent,
    PoolInitializationEvent,
    SwapReplayEvent,
    bind_swap_observation,
    replay_pool,
    replay_swaps,
)


def block(number: int, timestamp: int, block_hash: str, parent_hash: str) -> BlockSnapshot:
    return BlockSnapshot(BlockRef(4663, number, block_hash, timestamp), parent_hash)


class PoolReplayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h9 = "0x" + "9" * 64
        self.h10 = "0x" + "a" * 64
        self.h11 = "0x" + "b" * 64
        self.h12 = "0x" + "c" * 64
        self.blocks = (
            block(10, 1_000, self.h10, self.h9),
            block(11, 1_002, self.h11, self.h10),
            block(12, 1_004, self.h12, self.h11),
        )
        key = PoolKey(
            "0x" + "1" * 40,
            "0x" + "2" * 40,
            DYNAMIC_FEE_FLAG,
            60,
            "0x" + "3" * 40,
        )
        self.pool_id = pool_id_from_key(key)
        initialized = PoolInitialized(self.pool_id, key, 1 << 96, 0, 10)
        self.initialization = PoolInitializationEvent(
            self.blocks[0].ref,
            "0x" + "d" * 64,
            1,
            1,
            initialized,
        )

    def swap(
        self,
        block_index: int,
        transaction_index: int,
        log_index: int,
        amount0: int,
        amount1: int,
        sqrt_price_x96: int,
        tick: int,
        liquidity: int,
        fee: int,
    ) -> SwapReplayEvent:
        snapshot = self.blocks[block_index]
        observation = SwapObservation(
            self.pool_id,
            "0x" + "4" * 40,
            amount0,
            amount1,
            sqrt_price_x96,
            liquidity,
            tick,
            fee,
            snapshot.ref.number,
        )
        return SwapReplayEvent(
            snapshot.ref,
            "0x" + f"{block_index + 1:x}" * 64,
            transaction_index,
            log_index,
            observation,
        )

    def liquidity(
        self,
        block_index: int,
        transaction_index: int,
        log_index: int,
        tick_lower: int,
        tick_upper: int,
        delta: int,
    ) -> LiquidityReplayEvent:
        snapshot = self.blocks[block_index]
        return LiquidityReplayEvent(
            snapshot.ref,
            "0x" + f"{block_index + 5:x}" * 64,
            transaction_index,
            log_index,
            ModifyLiquidityObservation(
                self.pool_id,
                "0x" + "3" * 40,
                tick_lower,
                tick_upper,
                delta,
                "0x" + "7" * 64,
                snapshot.ref.number,
            ),
        )

    def test_replay_records_each_state_transition_and_sparse_swap_blocks(self) -> None:
        swaps = (
            self.swap(0, 1, 2, -100, 90, 1_010, -1, 500, 3_000),
            self.swap(2, 0, 0, 80, -70, 990, 1, 450, 4_000),
        )
        result = replay_swaps(self.initialization, self.blocks, swaps)

        self.assertEqual(result.processed_blocks, 3)
        self.assertEqual(len(result.steps), 2)
        self.assertEqual(result.steps[0].previous_sqrt_price_x96, 1 << 96)
        self.assertEqual(result.steps[0].sqrt_price_x96, 1_010)
        self.assertEqual(result.steps[0].previous_price_x192, (1 << 96) ** 2)
        self.assertEqual(result.steps[0].price_x192, 1_010**2)
        self.assertEqual(result.steps[0].price_change_x192, 1_010**2 - (1 << 96) ** 2)
        self.assertEqual(result.steps[0].tick_change, -1)
        self.assertEqual(result.steps[0].liquidity_change, 500)
        self.assertEqual(result.steps[0].fee, 3_000)
        self.assertEqual(result.final_state.block, self.blocks[-1].ref)
        self.assertEqual(result.final_state.swap_count, 2)
        self.assertEqual(result.final_state.cumulative_amount0, -20)
        self.assertEqual(result.final_state.cumulative_amount1, 20)
        self.assertEqual(result, replay_swaps(self.initialization, self.blocks, swaps))

    def test_replay_rejects_duplicate_or_out_of_order_events(self) -> None:
        later = self.swap(0, 2, 0, -100, 90, 1_010, -1, 500, 3_000)
        earlier = self.swap(0, 1, 2, 80, -70, 990, 1, 450, 4_000)
        with self.assertRaisesRegex(ValueError, "strict event order"):
            replay_swaps(self.initialization, self.blocks, (later, earlier))
        with self.assertRaisesRegex(ValueError, "strict event order"):
            replay_swaps(self.initialization, self.blocks, (later, later))

    def test_replay_rejects_missing_or_broken_block_lineage(self) -> None:
        with self.assertRaisesRegex(ValueError, "contiguous"):
            replay_swaps(self.initialization, (self.blocks[0], self.blocks[2]), ())
        broken = block(11, 1_002, self.h11, "0x" + "f" * 64)
        with self.assertRaisesRegex(ValueError, "parent hash"):
            replay_swaps(self.initialization, (self.blocks[0], broken), ())
        backwards = block(11, 999, self.h11, self.h10)
        with self.assertRaisesRegex(ValueError, "timestamps"):
            replay_swaps(self.initialization, (self.blocks[0], backwards), ())

    def test_replay_rejects_cross_pool_and_unpinned_events(self) -> None:
        event = self.swap(2, 0, 0, -100, 90, 1_010, -1, 500, 3_000)
        other = SwapObservation(
            "0x" + "f" * 64,
            event.observation.sender,
            event.observation.amount0,
            event.observation.amount1,
            event.observation.sqrt_price_x96,
            event.observation.liquidity,
            event.observation.tick,
            event.observation.fee,
            event.observation.block_number,
        )
        cross_pool = SwapReplayEvent(
            event.block,
            event.transaction_hash,
            event.transaction_index,
            event.log_index,
            other,
        )
        with self.assertRaisesRegex(ValueError, "different pool"):
            replay_swaps(self.initialization, self.blocks, (cross_pool,))

        wrong_block = BlockRef(4663, 12, "0x" + "e" * 64, 1_004)
        unpinned = SwapReplayEvent(
            wrong_block,
            event.transaction_hash,
            event.transaction_index,
            event.log_index,
            event.observation,
        )
        with self.assertRaisesRegex(ValueError, "pinned block"):
            replay_swaps(self.initialization, self.blocks, (unpinned,))

    def test_log_binding_requires_block_hash_and_transaction_index(self) -> None:
        event = self.swap(2, 3, 4, -100, 90, 1_010, -1, 500, 3_000)
        complete_log = LogRecord(
            "0x" + "5" * 40,
            (),
            "0x",
            event.block.number,
            event.transaction_hash,
            event.log_index,
            event.block.block_hash,
            event.transaction_index,
        )
        self.assertEqual(
            bind_swap_observation(event.block, complete_log, event.observation),
            event,
        )
        incomplete_log = LogRecord(
            complete_log.address,
            complete_log.topics,
            complete_log.data,
            complete_log.block_number,
            complete_log.transaction_hash,
            complete_log.log_index,
        )
        with self.assertRaisesRegex(ValueError, "lacks block hash"):
            bind_swap_observation(event.block, incomplete_log, event.observation)

    def test_static_and_dynamic_fees_share_replay_with_static_consistency_check(self) -> None:
        static_key = PoolKey(
            "0x" + "1" * 40,
            "0x" + "2" * 40,
            3_000,
            60,
            "0x" + "3" * 40,
        )
        static_pool = pool_id_from_key(static_key)
        initialization = PoolInitializationEvent(
            self.blocks[0].ref,
            "0x" + "e" * 64,
            1,
            1,
            PoolInitialized(static_pool, static_key, 1 << 96, 0, 10),
        )
        valid = SwapReplayEvent(
            self.blocks[0].ref,
            "0x" + "f" * 64,
            1,
            2,
            SwapObservation(
                static_pool,
                "0x" + "4" * 40,
                -100,
                90,
                1_010,
                500,
                -1,
                3_000,
                10,
            ),
        )
        replay = replay_swaps(initialization, self.blocks, (valid,))
        self.assertFalse(replay.final_state.dynamic_fee)
        self.assertEqual(replay.final_state.configured_pool_fee, 3_000)

        invalid = SwapReplayEvent(
            valid.block,
            valid.transaction_hash,
            valid.transaction_index,
            valid.log_index,
            SwapObservation(
                static_pool,
                valid.observation.sender,
                -100,
                90,
                1_010,
                500,
                -1,
                3_001,
                10,
            ),
        )
        with self.assertRaisesRegex(ValueError, "static-fee"):
            replay_swaps(initialization, self.blocks, (invalid,))

    def test_unified_replay_applies_only_active_range_liquidity(self) -> None:
        active_add = self.liquidity(0, 1, 2, -60, 60, 500)
        swap = self.swap(0, 1, 3, -100, 90, 1_010, 1, 500, 2_000)
        inactive_add = self.liquidity(1, 0, 0, 120, 240, 700)
        fee_poke = self.liquidity(2, 0, 0, -60, 60, 0)
        result = replay_pool(
            self.initialization,
            self.blocks,
            (swap,),
            (active_add, inactive_add, fee_poke),
        )
        self.assertEqual(result.final_state.liquidity, 500)
        self.assertEqual(result.final_state.liquidity_event_count, 3)
        self.assertEqual(len(result.liquidity_steps), 3)
        self.assertTrue(result.liquidity_steps[0].active_at_current_tick)
        self.assertFalse(result.liquidity_steps[1].active_at_current_tick)
        self.assertEqual(result.liquidity_steps[2].liquidity_delta, 0)

    def test_unified_replay_rejects_event_position_collision_and_underflow(self) -> None:
        liquidity = self.liquidity(0, 1, 2, -60, 60, 1)
        colliding_swap = self.swap(0, 1, 2, -100, 90, 1_010, 1, 1, 2_000)
        with self.assertRaisesRegex(ValueError, "unique positions"):
            replay_pool(
                self.initialization,
                self.blocks,
                (colliding_swap,),
                (liquidity,),
            )
        remove = self.liquidity(0, 1, 2, -60, 60, -1)
        with self.assertRaisesRegex(ValueError, "underflow"):
            replay_pool(self.initialization, self.blocks, (), (remove,))

    def test_zero_delta_swap_is_recorded_as_no_fill(self) -> None:
        no_fill = self.swap(
            2,
            0,
            0,
            0,
            0,
            1_461_373_636_630_002_862_890_420_172_285_094_607_950_328,
            887_271,
            0,
            100_000,
        )
        result = replay_swaps(self.initialization, self.blocks, (no_fill,))
        self.assertEqual(result.final_state.swap_count, 1)
        self.assertEqual(result.final_state.filled_swap_count, 0)
        self.assertEqual(result.final_state.no_fill_swap_count, 1)
        self.assertEqual(result.final_state.cumulative_amount0, 0)
        self.assertEqual(result.final_state.cumulative_amount1, 0)


if __name__ == "__main__":
    unittest.main()
