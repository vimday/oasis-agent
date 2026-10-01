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
        failed_tx_cost: int = 0,
        opportunity_cost: int = 0,
    ) -> CounterfactualRun:
        return CounterfactualRun(
            fee_policy=fee,
            range_policy=range_policy,
            start_block=self.start,
            end_block=self.end,
            dataset_hash="2" * 64,
            engine_version="replay-v1",
            policy_version=f"{fee.value}-{range_policy.value}-v1",
            initial_capital1=1_000_000,
            fee_income1=fee_income,
            incentive_income1=0,
            inventory_pnl1=inventory_pnl,
            gas_cost1=gas,
            rebalance_cost1=0,
            adverse_selection_cost1=0,
            other_cost1=0,
            failed_tx_cost1=failed_tx_cost,
            opportunity_cost1=opportunity_cost,
            hold_net_pnl1=50,
            flow_response_model="elasticity-v1",
            evidence=self.evidence,
        )

    def test_factorial_attribution_uses_net_results(self) -> None:
        r00 = self.make_run(FeePolicyKind.STATIC, RangePolicyKind.FIXED, 100, 20)
        r01 = self.make_run(FeePolicyKind.STATIC, RangePolicyKind.ACTIVE, 140, 30, 10)
        r10 = self.make_run(FeePolicyKind.DYNAMIC, RangePolicyKind.FIXED, 160, 20)
        r11 = self.make_run(FeePolicyKind.DYNAMIC, RangePolicyKind.ACTIVE, 230, 30, 10, 4, 6)
        result = compare_lp_factorial((r11, r00, r10, r01))
        self.assertEqual(result.fee_contribution_fixed1, 60)
        self.assertEqual(result.range_contribution_static1, 40)
        # r11 net = 230 + 30 - 10 (gas) - 4 (failed tx) - 6 (opportunity) = 240
        self.assertEqual(result.combined_excess1, 120)
        self.assertEqual(result.interaction1, 20)
        self.assertEqual(result.r11_dynamic_active.excess_vs_hold1, 190)
        self.assertEqual(result.r11_dynamic_active.failed_tx_cost1, 4)
        self.assertEqual(result.r11_dynamic_active.opportunity_cost1, 6)

    def test_rejects_missing_cell_or_incomparable_dataset(self) -> None:
        r00 = self.make_run(FeePolicyKind.STATIC, RangePolicyKind.FIXED, 100, 20)
        r01 = self.make_run(FeePolicyKind.STATIC, RangePolicyKind.ACTIVE, 140, 30)
        r10 = self.make_run(FeePolicyKind.DYNAMIC, RangePolicyKind.FIXED, 160, 20)
        r11 = self.make_run(FeePolicyKind.DYNAMIC, RangePolicyKind.ACTIVE, 230, 30)
        with self.assertRaisesRegex(ValueError, "four cells"):
            compare_lp_factorial((r00, r01, r10, r10))
        with self.assertRaisesRegex(ValueError, "dataset_hash"):
            compare_lp_factorial((r00, r01, r10, replace(r11, dataset_hash="3" * 64)))

    def test_rejects_changed_hold_baseline_between_cells(self) -> None:
        runs = tuple(
            self.make_run(fee, range_policy, 100, 20)
            for fee in FeePolicyKind
            for range_policy in RangePolicyKind
        )
        with self.assertRaisesRegex(ValueError, "hold_net_pnl1"):
            compare_lp_factorial((*runs[:-1], replace(runs[-1], hold_net_pnl1=-999)))

    def test_rejects_unversioned_flow_response_assumption(self) -> None:
        with self.assertRaisesRegex(ValueError, "flow-response"):
            replace(
                self.make_run(FeePolicyKind.STATIC, RangePolicyKind.FIXED, 100, 20),
                flow_response_model="",
            )

    def test_all_costs_require_integer_base_units(self) -> None:
        run = self.make_run(FeePolicyKind.STATIC, RangePolicyKind.FIXED, 100, 20)
        for name in (
            "gas_cost1",
            "rebalance_cost1",
            "adverse_selection_cost1",
            "other_cost1",
            "failed_tx_cost1",
            "opportunity_cost1",
        ):
            for value in (True, 0.5, float("nan"), float("inf")):
                with self.subTest(field=name, value=value), self.assertRaisesRegex(TypeError, name):
                    replace(run, **{name: value})
            with self.subTest(field=name, value=-1), self.assertRaisesRegex(ValueError, name):
                replace(run, **{name: -1})


if __name__ == "__main__":
    unittest.main()
