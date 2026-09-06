import unittest
from dataclasses import replace

from oasis_agent.models.chain import BlockSnapshot, LogRecord
from oasis_agent.models.common import BlockRef
from oasis_agent.models.v4 import DYNAMIC_FEE_FLAG, PoolKey
from oasis_agent.rpc.collector import BlockRangeDataset
from oasis_agent.uniswap_v4.dataset_replay import decode_pool_dataset, replay_pool_dataset
from oasis_agent.uniswap_v4.events import (
    MODIFY_LIQUIDITY_TOPIC,
    POOL_INITIALIZED_TOPIC,
    SWAP_TOPIC,
)
from oasis_agent.uniswap_v4.liquidity_math import sqrt_price_at_tick
from oasis_agent.uniswap_v4.pool_id import pool_id_from_key


def word(value: int) -> str:
    return f"{value & ((1 << 256) - 1):064x}"


def address_topic(address: str) -> str:
    return "0x" + "0" * 24 + address[2:]


class DatasetReplayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = "0x" + "a" * 40
        self.h9 = "0x" + "9" * 64
        self.h10 = "0x" + "1" * 64
        self.h11 = "0x" + "2" * 64
        self.blocks = (
            BlockSnapshot(BlockRef(4663, 10, self.h10, 1_000), self.h9),
            BlockSnapshot(BlockRef(4663, 11, self.h11, 1_002), self.h10),
        )
        self.key = PoolKey(
            "0x" + "1" * 40,
            "0x" + "2" * 40,
            DYNAMIC_FEE_FLAG,
            60,
            "0x" + "3" * 40,
        )
        self.pool_id = pool_id_from_key(self.key)
        self.logs = (
            LogRecord(
                self.manager,
                (
                    POOL_INITIALIZED_TOPIC,
                    self.pool_id,
                    address_topic(self.key.currency0),
                    address_topic(self.key.currency1),
                ),
                "0x"
                + "".join(
                    (
                        word(self.key.fee),
                        word(self.key.tick_spacing),
                        word(int(self.key.hooks, 16)),
                        word(sqrt_price_at_tick(0)),
                        word(0),
                    )
                ),
                10,
                "0x" + "4" * 64,
                0,
                self.h10,
                0,
            ),
            LogRecord(
                self.manager,
                (MODIFY_LIQUIDITY_TOPIC, self.pool_id, address_topic(self.manager)),
                "0x" + "".join((word(-60), word(60), word(500), word(7))),
                10,
                "0x" + "5" * 64,
                1,
                self.h10,
                0,
            ),
            LogRecord(
                self.manager,
                (SWAP_TOPIC, self.pool_id, address_topic("0x" + "4" * 40)),
                "0x"
                + "".join(
                    (
                        word(-100),
                        word(90),
                        word(sqrt_price_at_tick(1)),
                        word(500),
                        word(1),
                        word(2_000),
                    )
                ),
                11,
                "0x" + "6" * 64,
                0,
                self.h11,
                0,
            ),
            LogRecord(
                self.manager,
                ("0x" + "f" * 64,),
                "0x",
                11,
                "0x" + "7" * 64,
                1,
                self.h11,
                0,
            ),
        )

    def dataset(self) -> BlockRangeDataset:
        return BlockRangeDataset("rpc-a", self.blocks, self.logs)

    def test_decodes_and_replays_canonical_dataset(self) -> None:
        dataset = self.dataset()
        decoded = decode_pool_dataset(
            dataset,
            pool_manager=self.manager,
            pool_id=self.pool_id,
        )
        self.assertEqual(decoded.dataset_hash, dataset.content_hash)
        self.assertEqual(len(decoded.swaps), 1)
        self.assertEqual(len(decoded.liquidity_events), 1)
        self.assertEqual(decoded.ignored_log_count, 1)

        replay = replay_pool_dataset(decoded, dataset)
        self.assertEqual(replay.final_state.liquidity, 500)
        self.assertEqual(replay.final_state.tick, 1)
        self.assertEqual(replay.final_state.last_fee, 2_000)
        self.assertEqual(replay.final_state.swap_count, 1)

    def test_infers_only_unambiguous_pool_and_rejects_wrong_contract(self) -> None:
        decoded = decode_pool_dataset(self.dataset(), pool_manager=self.manager)
        self.assertEqual(decoded.pool_id, self.pool_id)

        foreign = replace(self.logs[0], address="0x" + "b" * 40)
        dataset = BlockRangeDataset("rpc-a", self.blocks, (foreign, *self.logs[1:]))
        with self.assertRaisesRegex(ValueError, "another contract"):
            decode_pool_dataset(dataset, pool_manager=self.manager, pool_id=self.pool_id)

    def test_requires_initialization_and_dataset_identity(self) -> None:
        without_initialization = BlockRangeDataset("rpc-a", self.blocks, self.logs[1:])
        with self.assertRaisesRegex(ValueError, "exactly one Initialize"):
            decode_pool_dataset(
                without_initialization,
                pool_manager=self.manager,
                pool_id=self.pool_id,
            )

        dataset = self.dataset()
        decoded = decode_pool_dataset(dataset, pool_manager=self.manager, pool_id=self.pool_id)
        other = BlockRangeDataset("rpc-a", self.blocks, self.logs[:-1])
        with self.assertRaisesRegex(ValueError, "do not belong"):
            replay_pool_dataset(decoded, other)


if __name__ == "__main__":
    unittest.main()
