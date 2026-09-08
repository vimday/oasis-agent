import unittest
from dataclasses import replace

from oasis_agent.farming.accounting_replay import (
    AccountingReplayStatus,
    ValuationPoint,
    ValuedLedgerEntry,
    replay_position_accounting,
    value_in_currency1,
)
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
from oasis_agent.uniswap_v4.liquidity_math import sqrt_price_at_tick
from oasis_agent.uniswap_v4.pool_id import pool_id_from_key


class PositionAccountingReplayTests(unittest.TestCase):
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
        self.b0 = BlockRef(4663, 10, "0x" + "aa" * 32, 1_000)
        self.b1 = BlockRef(4663, 11, "0x" + "bb" * 32, 1_001)
        self.b2 = BlockRef(4663, 12, "0x" + "cc" * 32, 1_002)
        # Canonical lineage with proper parent_hash linkage
        self.canonical_blocks = (
            BlockSnapshot(self.b0, "0x" + "99" * 32),  # b0 has arbitrary parent
            BlockSnapshot(self.b1, self.b0.block_hash),  # b1's parent is b0
            BlockSnapshot(self.b2, self.b1.block_hash),  # b2's parent is b1
        )

    def evidence(self, block: BlockRef, kind: str = "valuation") -> EvidenceRef:
        return EvidenceRef(kind, "1" * 64, block, "rpc-a")

    def snapshot(self, block: BlockRef, liquidity: int) -> PositionSnapshot:
        return PositionSnapshot(
            block,
            self.manager,
            7,
            self.owner,
            self.manager,
            self.key,
            self.pool_id,
            "0x" + "66" * 32,
            -60,
            60,
            False,
            liquidity,
            0,
            0,
            0,
            0,
            0,
            0,
        )

    def entry(self, resolved: bool) -> PositionLedgerEntry:
        return PositionLedgerEntry(
            self.b1,
            "0x" + "77" * 32,
            1,
            2,
            ModifyLiquidityObservation(
                self.pool_id,
                self.manager,
                -60,
                60,
                20,
                "0x" + (7).to_bytes(32, "big").hex(),
                11,
            ),
            -50 if resolved else None,
            -80 if resolved else None,
            SettlementAttribution.POSITION_TRACE if resolved else None,
            self.evidence(self.b1, "settlement") if resolved else None,
        )

    def valuation(self, block: BlockRef, tick: int = 0) -> ValuationPoint:
        return ValuationPoint(block, sqrt_price_at_tick(tick), self.evidence(block))

    def test_complete_replay_outputs_components_and_cash_flow_pnl(self) -> None:
        entry = self.entry(True)
        opening = self.snapshot(self.b0, 100)
        closing = self.snapshot(self.b2, 120)
        ledger = build_position_token_ledger(
            opening,
            closing,
            (entry,),
            canonical_blocks=self.canonical_blocks,
            ownership_replay=replay_position_ownership(opening, closing, ()),
        )
        report = replay_position_accounting(
            ledger,
            self.valuation(self.b0),
            self.valuation(self.b2),
            (ValuedLedgerEntry(entry, self.valuation(self.b1)),),
        )
        self.assertEqual(report.status, AccountingReplayStatus.COMPLETE)
        self.assertEqual(report.cumulative_flow_value1, -130)
        self.assertEqual(len(report.flows), 1)
        self.assertEqual(
            report.net_pnl1,
            report.closing_value1 - 130 - report.opening_value1,
        )

    def test_unresolved_settlement_returns_no_partial_pnl(self) -> None:
        ledger = build_position_token_ledger(
            self.snapshot(self.b0, 100),
            self.snapshot(self.b2, 120),
            (self.entry(False),),
            canonical_blocks=self.canonical_blocks,
        )
        report = replay_position_accounting(
            ledger,
            self.valuation(self.b0),
            self.valuation(self.b2),
            (),
        )
        self.assertEqual(report.status, AccountingReplayStatus.UNRESOLVED_SETTLEMENT)
        self.assertIsNone(report.cumulative_flow_value1)
        self.assertIsNone(report.net_pnl1)

    def test_nonzero_external_cost_requires_evidence(self) -> None:
        ledger = build_position_token_ledger(
            self.snapshot(self.b0, 100),
            self.snapshot(self.b2, 100),
            (),
            canonical_blocks=self.canonical_blocks,
        )
        with self.assertRaisesRegex(ValueError, "requires evidence"):
            replay_position_accounting(
                ledger,
                self.valuation(self.b0),
                self.valuation(self.b2),
                (),
                external_cost1=1,
            )

    def test_signed_valuation_truncates_toward_zero(self) -> None:
        self.assertEqual(value_in_currency1(3, 5, (1 << 192) // 2), 6)
        self.assertEqual(value_in_currency1(-3, -5, (1 << 192) // 2), -6)

    def test_owner_transfer_never_becomes_pnl_without_beneficial_continuity(self) -> None:
        opening = self.snapshot(self.b0, 100)
        closing = replace(self.snapshot(self.b2, 100), nft_owner="0x" + "99" * 20)
        transfer = PositionTransferEvent(
            self.b1,
            "0x" + "88" * 32,
            1,
            2,
            self.manager,
            self.owner,
            closing.nft_owner,
            7,
        )
        ownership = replay_position_ownership(opening, closing, (transfer,))
        ledger = build_position_token_ledger(
            opening,
            closing,
            (),
            canonical_blocks=self.canonical_blocks,
            ownership_replay=ownership,
        )
        unresolved = replay_position_accounting(
            ledger,
            self.valuation(self.b0),
            self.valuation(self.b2),
            (),
        )
        self.assertEqual(unresolved.status, AccountingReplayStatus.UNRESOLVED_OWNERSHIP)
        self.assertIsNone(unresolved.net_pnl1)

        continuity = self.evidence(self.b1, "beneficial_owner_continuity")
        resolved = replay_position_accounting(
            ledger,
            self.valuation(self.b0),
            self.valuation(self.b2),
            (),
            beneficial_owner_continuity_evidence=continuity,
        )
        self.assertEqual(resolved.status, AccountingReplayStatus.COMPLETE)

    def test_round_trip_owner_transfer_does_not_prove_beneficial_continuity(self) -> None:
        opening = self.snapshot(self.b0, 100)
        closing = self.snapshot(self.b2, 100)
        other = "0x" + "99" * 20
        outgoing = PositionTransferEvent(
            self.b1, "0x" + "88" * 32, 1, 2, self.manager, self.owner, other, 7
        )
        incoming = replace(outgoing, log_index=3, from_owner=other, to_owner=self.owner)
        ownership = replay_position_ownership(opening, closing, (outgoing, incoming))
        ledger = build_position_token_ledger(
            opening,
            closing,
            (),
            canonical_blocks=self.canonical_blocks,
            ownership_replay=ownership,
        )
        result = replay_position_accounting(
            ledger, self.valuation(self.b0), self.valuation(self.b2), ()
        )
        self.assertEqual(result.status, AccountingReplayStatus.UNRESOLVED_OWNERSHIP)
        self.assertIsNone(result.net_pnl1)

    def test_missing_ownership_replay_cannot_prove_continuity(self) -> None:
        ledger = build_position_token_ledger(
            self.snapshot(self.b0, 100),
            self.snapshot(self.b2, 100),
            (),
            canonical_blocks=self.canonical_blocks,
        )
        result = replay_position_accounting(
            ledger, self.valuation(self.b0), self.valuation(self.b2), ()
        )
        self.assertEqual(result.status, AccountingReplayStatus.UNRESOLVED_OWNERSHIP)
        self.assertIsNone(result.net_pnl1)

    def test_self_transfer_does_not_require_beneficial_owner_attestation(self) -> None:
        opening = self.snapshot(self.b0, 100)
        closing = self.snapshot(self.b2, 100)
        transfer = PositionTransferEvent(
            self.b1, "0x" + "88" * 32, 1, 2, self.manager, self.owner, self.owner, 7
        )
        ledger = build_position_token_ledger(
            opening,
            closing,
            (),
            canonical_blocks=self.canonical_blocks,
            ownership_replay=replay_position_ownership(opening, closing, (transfer,)),
        )
        result = replay_position_accounting(
            ledger, self.valuation(self.b0), self.valuation(self.b2), ()
        )
        self.assertEqual(result.status, AccountingReplayStatus.COMPLETE)
        self.assertEqual(result.net_pnl1, 0)


if __name__ == "__main__":
    unittest.main()
