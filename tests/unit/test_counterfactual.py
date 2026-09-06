from __future__ import annotations

import unittest
from dataclasses import replace

from oasis_agent.farming.counterfactual import (
    CounterfactualRun,
    FeePolicyKind,
    RangePolicyKind,
    compare_lp_factorial,
)
from oasis_agent.models.common import BlockRef, EvidenceRef


class CounterfactualTests(unittest.TestCase):
    def setUp(self) -> None:
        self.start = BlockRef(4663, 10, "0x" + "aa" * 32, 1_000)
        self.end = BlockRef(4663, 20, "0x" + "bb" * 32, 1_010)
        self.evidence = (EvidenceRef("dataset", "1" * 64, self.start, "archive-a"),)

    def make_run(
        self,
        fee: FeePolicyKind,
        range_policy: RangePolicyKind,
        fee_income: int,
        inventory_pnl: int,
        gas: int = 0,
    ) -> CounterfactualRun:
        return CounterfactualRun(
            fee,
            range_policy,
            self.start,
            self.end,
            "2" * 64,
            "replay-v1",
            f"{fee.value}-{range_policy.value}-v1",
            1_000_000,
            fee_income,
            0,
            inventory_pnl,
            gas,
            0,
            0,
            0,
            50,
            "elasticity-v1",
            self.evidence,
        )

    def test_factorial_attribution_uses_net_results(self) -> None:
        r00 = self.make_run(FeePolicyKind.STATIC, RangePolicyKind.FIXED, 100, 20)
        r01 = self.make_run(FeePolicyKind.STATIC, RangePolicyKind.ACTIVE, 140, 30, 10)
        r10 = self.make_run(FeePolicyKind.DYNAMIC, RangePolicyKind.FIXED, 160, 20)
        r11 = self.make_run(FeePolicyKind.DYNAMIC, RangePolicyKind.ACTIVE, 230, 30, 10)
        result = compare_lp_factorial((r11, r00, r10, r01))
        self.assertEqual(result.fee_contribution_fixed1, 60)
        self.assertEqual(result.range_contribution_static1, 40)
        self.assertEqual(result.combined_excess1, 130)
        self.assertEqual(result.interaction1, 30)
        self.assertEqual(result.r11_dynamic_active.excess_vs_hold1, 200)

    def test_rejects_missing_cell_or_incomparable_dataset(self) -> None:
        r00 = self.make_run(FeePolicyKind.STATIC, RangePolicyKind.FIXED, 100, 20)
        r01 = self.make_run(FeePolicyKind.STATIC, RangePolicyKind.ACTIVE, 140, 30)
        r10 = self.make_run(FeePolicyKind.DYNAMIC, RangePolicyKind.FIXED, 160, 20)
        r11 = self.make_run(FeePolicyKind.DYNAMIC, RangePolicyKind.ACTIVE, 230, 30)
        with self.assertRaisesRegex(ValueError, "four cells"):
            compare_lp_factorial((r00, r01, r10, r10))
        with self.assertRaisesRegex(ValueError, "dataset_hash"):
            compare_lp_factorial((r00, r01, r10, replace(r11, dataset_hash="3" * 64)))

    def test_rejects_unversioned_flow_response_assumption(self) -> None:
        with self.assertRaisesRegex(ValueError, "flow-response"):
            replace(
                self.make_run(FeePolicyKind.STATIC, RangePolicyKind.FIXED, 100, 20),
                flow_response_model="",
            )


if __name__ == "__main__":
    unittest.main()
