from __future__ import annotations

import unittest

from oasis_agent.farming.position_events import correlate_position_modification
from oasis_agent.models.chain import LogRecord
from oasis_agent.models.common import BlockRef
from oasis_agent.uniswap_v4.events import MODIFY_LIQUIDITY_TOPIC, MODIFY_POSITION_TOPIC


def word(value: int) -> str:
    return f"{value & ((1 << 256) - 1):064x}"


class PositionEventTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pool_manager = "0x" + "11" * 20
        self.position_manager = "0x" + "22" * 20
        self.initiator = "0x" + "33" * 20
        self.block = BlockRef(4663, 10, "0x" + "aa" * 32, 1_000)
        self.transaction_hash = "0x" + "bb" * 32
        self.pool_id = "0x" + "44" * 32
        self.data = "0x" + "".join([word(-120), word(120), word(50), word(7)])

    @staticmethod
    def address_topic(address: str) -> str:
        return "0x" + "0" * 24 + address[2:]

    def log(self, address: str, topic: str, sender: str, index: int) -> LogRecord:
        return LogRecord(
            address,
            (topic, self.pool_id, self.address_topic(sender)),
            self.data,
            10,
            self.transaction_hash,
            index,
            self.block.block_hash,
            3,
        )

    def test_correlates_core_and_position_manager_mirror(self) -> None:
        core = self.log(
            self.pool_manager,
            MODIFY_LIQUIDITY_TOPIC,
            self.position_manager,
            4,
        )
        mirror = self.log(
            self.position_manager,
            MODIFY_POSITION_TOPIC,
            self.initiator,
            5,
        )
        result = correlate_position_modification(
            self.block,
            self.pool_manager,
            self.position_manager,
            core,
            mirror,
        )
        self.assertEqual(result.initiator, self.initiator)
        self.assertEqual(result.observation.salt, "0x" + "0" * 63 + "7")

    def test_rejects_mismatched_mirror_or_reversed_log_order(self) -> None:
        core = self.log(
            self.pool_manager,
            MODIFY_LIQUIDITY_TOPIC,
            self.position_manager,
            4,
        )
        mismatched = self.log(
            self.position_manager,
            MODIFY_POSITION_TOPIC,
            self.initiator,
            5,
        )
        changed_data = "0x" + "".join([word(-120), word(120), word(51), word(7)])
        mismatched = LogRecord(
            mismatched.address,
            mismatched.topics,
            changed_data,
            mismatched.block_number,
            mismatched.transaction_hash,
            mismatched.log_index,
            mismatched.block_hash,
            mismatched.transaction_index,
        )
        with self.assertRaisesRegex(ValueError, "does not match"):
            correlate_position_modification(
                self.block,
                self.pool_manager,
                self.position_manager,
                core,
                mismatched,
            )
        with self.assertRaisesRegex(ValueError, "must follow"):
            correlate_position_modification(
                self.block,
                self.pool_manager,
                self.position_manager,
                core,
                self.log(
                    self.position_manager,
                    MODIFY_POSITION_TOPIC,
                    self.initiator,
                    3,
                ),
            )


if __name__ == "__main__":
    unittest.main()
