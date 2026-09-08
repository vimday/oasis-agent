import unittest
from dataclasses import replace

from oasis_agent.farming.position_ownership import (
    TRANSFER_TOPIC,
    decode_position_transfer,
    replay_position_ownership,
)
from oasis_agent.farming.positions import PositionSnapshot
from oasis_agent.models.chain import LogRecord
from oasis_agent.models.common import BlockRef
from oasis_agent.models.v4 import PoolKey
from oasis_agent.uniswap_v4.pool_id import pool_id_from_key


def address_topic(address: str) -> str:
    return "0x" + "0" * 24 + address[2:]


class PositionOwnershipTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = "0x" + "1" * 40
        self.owner = "0x" + "2" * 40
        self.new_owner = "0x" + "3" * 40
        self.open_block = BlockRef(4663, 10, "0x" + "a" * 64, 1_000)
        self.transfer_block = BlockRef(4663, 11, "0x" + "b" * 64, 1_001)
        self.close_block = BlockRef(4663, 12, "0x" + "c" * 64, 1_002)
        key = PoolKey(
            "0x" + "4" * 40,
            "0x" + "5" * 40,
            3_000,
            60,
            "0x" + "6" * 40,
        )
        pool_id = pool_id_from_key(key)
        self.opening = PositionSnapshot(
            self.open_block,
            self.manager,
            7,
            self.owner,
            self.manager,
            key,
            pool_id,
            "0x" + "7" * 64,
            -120,
            120,
            False,
            100,
            0,
            0,
            0,
            0,
            0,
            0,
        )
        self.closing = replace(self.opening, block=self.close_block, nft_owner=self.new_owner)

    def transfer_log(self, from_owner: str | None = None) -> LogRecord:
        return LogRecord(
            self.manager,
            (
                TRANSFER_TOPIC,
                address_topic(from_owner or self.owner),
                address_topic(self.new_owner),
                "0x" + f"{7:064x}",
            ),
            "0x",
            self.transfer_block.number,
            "0x" + "d" * 64,
            2,
            self.transfer_block.block_hash,
            1,
        )

    def test_decodes_and_replays_owner_transition(self) -> None:
        event = decode_position_transfer(
            self.transfer_block,
            self.manager,
            self.transfer_log(),
        )
        replay = replay_position_ownership(self.opening, self.closing, (event,))
        self.assertEqual(replay.final_owner, self.new_owner)

    def test_self_transfer_is_retained_without_changing_owner(self) -> None:
        log = self.transfer_log()
        log = replace(log, topics=(*log.topics[:2], address_topic(self.owner), log.topics[3]))
        event = decode_position_transfer(self.transfer_block, self.manager, log)
        replay = replay_position_ownership(
            self.opening, replace(self.closing, nft_owner=self.owner), (event,)
        )
        self.assertEqual(replay.final_owner, self.owner)
        self.assertEqual(replay.transfers, (event,))

    def test_removed_and_noncanonical_token_topic_are_rejected(self) -> None:
        log = self.transfer_log()
        invalid_logs = (
            replace(log, removed=True),
            replace(log, topics=(*log.topics[:3], "0x07")),
            replace(log, topics=(*log.topics[:3], "0x00" + log.topics[3][2:])),
        )
        for invalid in invalid_logs:
            with self.subTest(log=invalid), self.assertRaises(ValueError):
                decode_position_transfer(self.transfer_block, self.manager, invalid)

    def test_rejects_discontinuous_or_unpinned_transfer(self) -> None:
        discontinuous = decode_position_transfer(
            self.transfer_block,
            self.manager,
            self.transfer_log("0x" + "9" * 40),
        )
        with self.assertRaisesRegex(ValueError, "discontinuous"):
            replay_position_ownership(self.opening, self.closing, (discontinuous,))

        unpinned = replace(self.transfer_log(), block_hash=None)
        with self.assertRaisesRegex(ValueError, "lacks block binding"):
            decode_position_transfer(self.transfer_block, self.manager, unpinned)

    def test_rejects_opening_block_transfer_as_already_reflected(self) -> None:
        event = decode_position_transfer(
            self.transfer_block,
            self.manager,
            self.transfer_log(),
        )
        with self.assertRaisesRegex(ValueError, "snapshot interval"):
            replay_position_ownership(
                replace(self.opening, block=self.transfer_block),
                self.closing,
                (event,),
            )


if __name__ == "__main__":
    unittest.main()
