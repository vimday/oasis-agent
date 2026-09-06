from __future__ import annotations

import unittest
from dataclasses import replace

from oasis_agent.farming.position_ledger import (
    PositionLedgerEntry,
    SettlementAttribution,
    build_position_token_ledger,
)
from oasis_agent.farming.position_ownership import (
    PositionTransferEvent,
    replay_position_ownership,
)
from oasis_agent.farming.positions import PositionSnapshot
from oasis_agent.models.chain import BlockSnapshot
from oasis_agent.models.common import BlockRef, EvidenceRef
from oasis_agent.models.v4 import DYNAMIC_FEE_FLAG, ModifyLiquidityObservation, PoolKey
from oasis_agent.uniswap_v4.pool_id import pool_id_from_key


class PositionLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = "0x" + "11" * 20
        self.owner = "0x" + "22" * 20
        self.key = PoolKey(
            "0x" + "33" * 20,
            "0x" + "44" * 20,
            DYNAMIC_FEE_FLAG,
            60,
            "0x" + "55" * 20,
        )
        self.pool_id = pool_id_from_key(self.key)
        self.open_block = BlockRef(4663, 10, "0x" + "aa" * 32, 1_000)
        self.event_block = BlockRef(4663, 11, "0x" + "bb" * 32, 1_001)
        self.close_block = BlockRef(4663, 12, "0x" + "cc" * 32, 1_002)
        self.opening = self.snapshot(self.open_block, 100)
        self.closing = self.snapshot(self.close_block, 120)
        # Create canonical block lineage
        self.canonical_blocks = (
            BlockSnapshot(self.open_block, "0x" + "00" * 32),
            BlockSnapshot(self.event_block, "0x" + "aa" * 32),
            BlockSnapshot(self.close_block, "0x" + "bb" * 32),
        )

    def snapshot(self, block: BlockRef, liquidity: int) -> PositionSnapshot:
        return PositionSnapshot(
            block=block,
            position_manager=self.manager,
            token_id=7,
            nft_owner=self.owner,
            core_position_owner=self.manager,
            pool_key=self.key,
            pool_id=self.pool_id,
            core_position_id="0x" + "66" * 32,
            tick_lower=-120,
            tick_upper=120,
            has_subscriber=False,
            liquidity=liquidity,
            fee_growth_inside0_last_x128=0,
            fee_growth_inside1_last_x128=0,
            fee_growth_inside0_x128=0,
            fee_growth_inside1_x128=0,
            uncollected_fee0=0,
            uncollected_fee1=0,
        )

    def observation(self, delta: int = 20, *, salt: int = 7) -> ModifyLiquidityObservation:
        return ModifyLiquidityObservation(
            self.pool_id,
            self.manager,
            -120,
            120,
            delta,
            "0x" + salt.to_bytes(32, "big").hex(),
            self.event_block.number,
        )

    def evidence(self) -> EvidenceRef:
        return EvidenceRef(
            "position_settlement_trace",
            "1" * 64,
            self.event_block,
            "rpc-a",
            request_hash="2" * 64,
            raw_response_hash="3" * 64,
            interpretation_hash="4" * 64,
        )

    def entry(self, *, resolved: bool = True, salt: int = 7) -> PositionLedgerEntry:
        return PositionLedgerEntry(
            self.event_block,
            "0x" + "77" * 32,
            1,
            2,
            self.observation(salt=salt),
            -50 if resolved else None,
            -80 if resolved else None,
            SettlementAttribution.POSITION_TRACE if resolved else None,
            self.evidence() if resolved else None,
        )

    def test_replays_liquidity_and_settlement_base_units(self) -> None:
        ledger = build_position_token_ledger(
            self.opening, self.closing, (self.entry(),), canonical_blocks=self.canonical_blocks
        )
        self.assertTrue(ledger.settlement_complete)
        self.assertEqual(ledger.unresolved_settlements, 0)
        self.assertEqual(
            (ledger.cumulative_owner_amount0, ledger.cumulative_owner_amount1), (-50, -80)
        )

    def test_ownership_replay_must_match_canonical_lineage_and_recomputed_state(self) -> None:
        closing = replace(self.closing, nft_owner="0x" + "99" * 20)
        transfer = PositionTransferEvent(
            self.event_block,
            "0x" + "88" * 32,
            1,
            3,
            self.manager,
            self.owner,
            closing.nft_owner,
            7,
        )
        ownership = replay_position_ownership(self.opening, closing, (transfer,))
        invalid_replays = (
            replace(
                ownership,
                transfers=(
                    replace(transfer, block=replace(self.event_block, block_hash="0x" + "ff" * 32)),
                ),
            ),
            replace(
                ownership,
                transfers=(replace(transfer, block=replace(self.event_block, timestamp=2_000)),),
            ),
            replace(ownership, transfers=()),
            replace(ownership, final_owner=self.owner),
        )
        for invalid in invalid_replays:
            with self.subTest(replay=invalid), self.assertRaises(ValueError):
                build_position_token_ledger(
                    self.opening,
                    closing,
                    (self.entry(),),
                    canonical_blocks=self.canonical_blocks,
                    ownership_replay=invalid,
                )

    def test_unresolved_settlement_never_emits_partial_totals(self) -> None:
        ledger = build_position_token_ledger(
            self.opening,
            self.closing,
            (self.entry(resolved=False),),
            canonical_blocks=self.canonical_blocks,
        )
        self.assertFalse(ledger.settlement_complete)
        self.assertEqual(ledger.unresolved_settlements, 1)
        self.assertIsNone(ledger.cumulative_owner_amount0)
        self.assertIsNone(ledger.cumulative_owner_amount1)

    def test_rejects_salt_or_closing_liquidity_mismatch(self) -> None:
        with self.assertRaisesRegex(ValueError, "salt"):
            build_position_token_ledger(
                self.opening,
                self.closing,
                (self.entry(salt=8),),
                canonical_blocks=self.canonical_blocks,
            )
        with self.assertRaisesRegex(ValueError, "closing snapshot"):
            build_position_token_ledger(
                self.opening,
                replace(self.closing, liquidity=121),
                (self.entry(),),
                canonical_blocks=self.canonical_blocks,
            )

    def test_owner_change_requires_complete_transfer_replay(self) -> None:
        new_owner = "0x" + "99" * 20
        closing = replace(self.closing, nft_owner=new_owner)
        with self.assertRaisesRegex(ValueError, "without an ownership replay"):
            build_position_token_ledger(
                self.opening,
                closing,
                (self.entry(),),
                canonical_blocks=self.canonical_blocks,
            )
        transfer = PositionTransferEvent(
            self.event_block,
            "0x" + "88" * 32,
            1,
            3,
            self.manager,
            self.owner,
            new_owner,
            7,
        )
        ownership = replay_position_ownership(self.opening, closing, (transfer,))
        ledger = build_position_token_ledger(
            self.opening,
            closing,
            (self.entry(),),
            canonical_blocks=self.canonical_blocks,
            ownership_replay=ownership,
        )
        self.assertEqual(ledger.ownership_replay, ownership)

    def test_opening_block_event_is_not_counted_twice(self) -> None:
        observation = replace(self.observation(), block_number=self.open_block.number)
        entry = replace(
            self.entry(resolved=False),
            block=self.open_block,
            observation=observation,
        )
        with self.assertRaisesRegex(ValueError, "snapshot interval"):
            build_position_token_ledger(
                self.opening,
                self.closing,
                (entry,),
                canonical_blocks=self.canonical_blocks,
            )

    def test_resolved_settlement_requires_block_pinned_evidence(self) -> None:
        wrong = EvidenceRef(
            "position_settlement_trace",
            "1" * 64,
            self.close_block,
            "rpc-a",
        )
        with self.assertRaisesRegex(ValueError, "must match entry block exactly"):
            PositionLedgerEntry(
                self.event_block,
                "0x" + "77" * 32,
                1,
                2,
                self.observation(),
                -1,
                -1,
                SettlementAttribution.POSITION_TRACE,
                wrong,
            )

    def test_rejects_settlement_evidence_with_tampered_block_ref(self) -> None:
        """Ensure settlement evidence block binding is validated exactly."""
        tampered = BlockRef(4664, 11, "0x" + "bb" * 32, 1_001)  # Different chain_id
        wrong_evidence = EvidenceRef(
            "position_settlement_trace",
            "1" * 64,
            tampered,
            "rpc-a",
        )
        with self.assertRaisesRegex(ValueError, "must match entry block exactly"):
            PositionLedgerEntry(
                self.event_block,
                "0x" + "77" * 32,
                1,
                2,
                self.observation(),
                -1,
                -1,
                SettlementAttribution.POSITION_TRACE,
                wrong_evidence,
            )

        # Test mismatched block_hash
        tampered_hash = BlockRef(4663, 11, "0x" + "cc" * 32, 1_001)  # Different block_hash
        wrong_hash_evidence = EvidenceRef(
            "position_settlement_trace",
            "1" * 64,
            tampered_hash,
            "rpc-a",
        )
        with self.assertRaisesRegex(ValueError, "must match entry block exactly"):
            PositionLedgerEntry(
                self.event_block,
                "0x" + "77" * 32,
                1,
                2,
                self.observation(),
                -1,
                -1,
                SettlementAttribution.POSITION_TRACE,
                wrong_hash_evidence,
            )

        # Test mismatched block_number
        tampered_number = BlockRef(4663, 12, "0x" + "bb" * 32, 1_001)  # Different number
        wrong_number_evidence = EvidenceRef(
            "position_settlement_trace",
            "1" * 64,
            tampered_number,
            "rpc-a",
        )
        with self.assertRaisesRegex(ValueError, "must match entry block exactly"):
            PositionLedgerEntry(
                self.event_block,
                "0x" + "77" * 32,
                1,
                2,
                self.observation(),
                -1,
                -1,
                SettlementAttribution.POSITION_TRACE,
                wrong_number_evidence,
            )

    def test_rejects_entry_with_tampered_block_hash(self) -> None:
        """Ensure entry block is validated against canonical lineage."""
        # Create unresolved entry with same block number but different hash
        tampered_block = BlockRef(4663, 11, "0x" + "dd" * 32, 1_001)  # Different hash
        tampered_entry = replace(self.entry(resolved=False), block=tampered_block)

        with self.assertRaisesRegex(ValueError, "does not match canonical lineage"):
            build_position_token_ledger(
                self.opening,
                self.closing,
                (tampered_entry,),
                canonical_blocks=self.canonical_blocks,
            )

    def test_rejects_entry_block_not_in_canonical_lineage(self) -> None:
        """Ensure entry block number must exist in canonical lineage."""
        # Create unresolved entry with block number not in canonical blocks
        unknown_block = BlockRef(4663, 99, "0x" + "ee" * 32, 9_999)
        unknown_observation = replace(self.observation(), block_number=99)
        unknown_entry = replace(
            self.entry(resolved=False), block=unknown_block, observation=unknown_observation
        )

        with self.assertRaisesRegex(ValueError, "not in canonical lineage"):
            build_position_token_ledger(
                self.opening,
                self.closing,
                (unknown_entry,),
                canonical_blocks=self.canonical_blocks,
            )

    def test_rejects_canonical_lineage_with_missing_middle_block(self) -> None:
        """Ensure canonical lineage must be continuous (no gaps)."""
        # Create incomplete canonical blocks with a gap (missing block 11)
        incomplete_blocks = (
            BlockSnapshot(self.open_block, "0x" + "00" * 32),
            BlockSnapshot(self.close_block, "0x" + "aa" * 32),
            # Missing block at height 11
        )

        with self.assertRaisesRegex(ValueError, "length|does not match"):
            build_position_token_ledger(
                self.opening,
                self.closing,
                (self.entry(resolved=False),),
                canonical_blocks=incomplete_blocks,
            )

    def test_rejects_canonical_lineage_with_broken_parent_linkage(self) -> None:
        """Ensure adjacent blocks maintain parent_hash linkage."""
        # Create blocks where parent_hash linkage is broken
        broken_blocks = (
            BlockSnapshot(self.open_block, "0x" + "00" * 32),
            BlockSnapshot(self.event_block, "0x" + "ff" * 32),  # Wrong parent_hash
            BlockSnapshot(self.close_block, "0x" + "aa" * 32),  # Also wrong parent_hash
        )

        with self.assertRaisesRegex(ValueError, "broken parent linkage|parent_hash"):
            build_position_token_ledger(
                self.opening,
                self.closing,
                (self.entry(resolved=False),),
                canonical_blocks=broken_blocks,
            )

    def test_rejects_entry_with_mismatched_timestamp(self) -> None:
        """Ensure entry block timestamp must match canonical lineage exactly."""
        # Create entry with correct hash but wrong timestamp
        wrong_timestamp_block = BlockRef(
            4663, self.event_block.number, self.event_block.block_hash, 9_999
        )  # Different timestamp
        wrong_entry = replace(self.entry(resolved=False), block=wrong_timestamp_block)

        with self.assertRaisesRegex(ValueError, "timestamp mismatch|does not match"):
            build_position_token_ledger(
                self.opening,
                self.closing,
                (wrong_entry,),
                canonical_blocks=self.canonical_blocks,
            )

    def test_rejects_canonical_blocks_shuffled_out_of_order(self) -> None:
        """Ensure canonical_blocks must be in input order (not shuffled)."""
        # Shuffle the canonical blocks (put last first)
        shuffled_blocks = (
            self.canonical_blocks[2],  # close_block first
            self.canonical_blocks[0],  # open_block second
            self.canonical_blocks[1],  # event_block last
        )

        with self.assertRaisesRegex(ValueError, "first canonical block must be|opening.block"):
            build_position_token_ledger(
                self.opening,
                self.closing,
                (self.entry(resolved=False),),
                canonical_blocks=shuffled_blocks,
            )

    def test_rejects_canonical_blocks_with_duplicate_height(self) -> None:
        """Ensure canonical_blocks rejects duplicate heights even if identical refs."""
        # Create duplicate at same height
        duplicate_blocks = (
            self.canonical_blocks[0],
            self.canonical_blocks[1],
            self.canonical_blocks[1],  # Duplicate event_block
        )

        with self.assertRaisesRegex(ValueError, "length|does not match|last canonical"):
            build_position_token_ledger(
                self.opening,
                self.closing,
                (self.entry(resolved=False),),
                canonical_blocks=duplicate_blocks,
            )

    def test_rejects_canonical_blocks_with_extra_blocks_before(self) -> None:
        """Ensure canonical_blocks cannot have extra blocks before opening."""
        # Create block before opening
        extra_before = BlockRef(4663, 9, "0x" + "99" * 32, 999)
        extra_snapshot = BlockSnapshot(extra_before, "0x" + "88" * 32)
        oversized_blocks = (extra_snapshot,) + self.canonical_blocks

        with self.assertRaisesRegex(ValueError, "first canonical block must be|opening.block"):
            build_position_token_ledger(
                self.opening,
                self.closing,
                (self.entry(resolved=False),),
                canonical_blocks=oversized_blocks,
            )

    def test_rejects_canonical_blocks_with_extra_blocks_after(self) -> None:
        """Ensure canonical_blocks cannot have extra blocks after closing."""
        # Create block after closing
        extra_after = BlockRef(4663, 13, "0x" + "dd" * 32, 1_003)
        extra_snapshot = BlockSnapshot(extra_after, self.canonical_blocks[-1].ref.block_hash)
        oversized_blocks = self.canonical_blocks + (extra_snapshot,)

        with self.assertRaisesRegex(ValueError, "last canonical block must be|closing.block"):
            build_position_token_ledger(
                self.opening,
                self.closing,
                (self.entry(resolved=False),),
                canonical_blocks=oversized_blocks,
            )

    def test_rejects_settlement_evidence_with_tampered_timestamp(self) -> None:
        """Ensure settlement evidence timestamp must match entry block exactly."""
        # Create entry with resolved settlement but evidence with wrong timestamp
        tampered_evidence = EvidenceRef(
            "position_settlement_trace",
            "1" * 64,
            BlockRef(
                4663, self.event_block.number, self.event_block.block_hash, 9_999
            ),  # Wrong timestamp
            "rpc-a",
            request_hash="2" * 64,
            raw_response_hash="3" * 64,
            interpretation_hash="4" * 64,
        )

        # This should fail at construction time now because timestamp validation is in __post_init__
        with self.assertRaises(ValueError):
            replace(self.entry(), settlement_evidence=tampered_evidence)


if __name__ == "__main__":
    unittest.main()
