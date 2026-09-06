import unittest
from decimal import Decimal

from oasis_agent.execution.lp_state_machine import TickRange
from oasis_agent.execution.range_controller import (
    LpAction,
    LpActionType,
    MarginalValueInputs,
    RebalanceDecision,
    RebalanceGateInput,
    Tranche,
    TranchePurpose,
    expected_value,
    lower_confidence_bound,
    marginal_value,
    rebalance_gate,
    select_action,
)


def inputs(**overrides: Decimal) -> MarginalValueInputs:
    base = {
        "expected_incremental_fees": Decimal(100),
        "expected_incremental_lvr": Decimal(20),
        "inventory_risk_charge": Decimal(10),
        "swap_rebalance_gas_mev_cost": Decimal(30),
        "failure_and_model_uncertainty_buffer": Decimal(15),
    }
    base.update(overrides)
    return MarginalValueInputs(**base)  # type: ignore[arg-type]


def gate(**overrides: object) -> RebalanceGateInput:
    base = {
        "price_left_buffer": True,
        "dwell_met": True,
        "marginal_value": Decimal(100),
        "hysteresis_threshold": Decimal(40),
        "volatility": Decimal("0.02"),
        "max_volatility": Decimal("0.10"),
        "toxicity": Decimal("0.01"),
        "max_toxicity": Decimal("0.05"),
        "reference_fresh": True,
        "route_fresh": True,
        "nonce_fresh": True,
        "balances_fresh": True,
        "approvals_fresh": True,
        "gas_reserve_fresh": True,
        "post_action_limits_ok": True,
        "pin_simulated_ok": True,
        "cooldown_met": True,
        "daily_limit_ok": True,
    }
    base.update(overrides)
    return RebalanceGateInput(**base)  # type: ignore[arg-type]


class MarginalValueTests(unittest.TestCase):
    def test_formula_matches_blueprint(self) -> None:
        mv = marginal_value(inputs())
        # 100 - 20 - 10 - 30 - 15 = 25
        self.assertEqual(mv, Decimal(25))
        self.assertEqual(expected_value(inputs()), Decimal(40))
        self.assertEqual(lower_confidence_bound(inputs()), mv)

    def test_terms_must_be_nonnegative(self) -> None:
        with self.assertRaisesRegex(ValueError, "nonnegative"):
            inputs(expected_incremental_fees=Decimal(-1))


class RebalanceGateTests(unittest.TestCase):
    def test_all_conditions_met_allows(self) -> None:
        decision = rebalance_gate(gate())
        self.assertIsInstance(decision, RebalanceDecision)
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.reasons, ())

    def test_each_condition_is_independent(self) -> None:
        cases = {
            "price_left_buffer": "inner buffer",
            "dwell_met": "dwell time",
            "reference_fresh": "reference price",
            "route_fresh": "route depth",
            "nonce_fresh": "nonce",
            "balances_fresh": "balances",
            "approvals_fresh": "approvals",
            "gas_reserve_fresh": "gas reserve",
            "post_action_limits_ok": "limits",
            "pin_simulated_ok": "pinned simulation",
            "cooldown_met": "cooldown",
            "daily_limit_ok": "daily rebalance limit",
        }
        for field, fragment in cases.items():
            with self.subTest(field=field):
                decision = rebalance_gate(gate(**{field: False}))
                self.assertFalse(decision.allowed)
                self.assertTrue(any(fragment in reason for reason in decision.reasons))

    def test_benefit_must_exceed_hysteresis(self) -> None:
        decision = rebalance_gate(
            gate(marginal_value=Decimal(40), hysteresis_threshold=Decimal(40))
        )
        self.assertFalse(decision.allowed)
        self.assertTrue(any("hysteresis" in reason for reason in decision.reasons))

    def test_circuit_breakers_block_recenter(self) -> None:
        decision = rebalance_gate(gate(volatility=Decimal("0.11")))
        self.assertFalse(decision.allowed)
        self.assertTrue(any("volatility" in reason for reason in decision.reasons))
        decision = rebalance_gate(gate(toxicity=Decimal("0.06")))
        self.assertFalse(decision.allowed)
        self.assertTrue(any("toxicity" in reason for reason in decision.reasons))


class SelectActionTests(unittest.TestCase):
    def test_do_nothing_when_nothing_clears_minimum_benefit(self) -> None:
        candidates = {
            LpActionType.RECENTER: (
                inputs(expected_incremental_fees=Decimal(50)),
                TickRange(-180, 60, 60),
            ),
            LpActionType.REDUCE: (
                inputs(expected_incremental_fees=Decimal(40)),
                TickRange(-120, 120, 60),
            ),
        }
        action = select_action(candidates, Decimal(25))
        self.assertEqual(action.action_type, LpActionType.HOLD)
        self.assertEqual(action.marginal_value, Decimal(0))

    def test_selects_best_candidate_above_minimum(self) -> None:
        candidates = {
            LpActionType.RECENTER: (inputs(), TickRange(-180, 60, 60)),
            LpActionType.REMOVE: (
                inputs(expected_incremental_fees=Decimal(200)),
                TickRange(-120, 120, 60),
            ),
        }
        action = select_action(candidates, Decimal(20))
        self.assertEqual(action.action_type, LpActionType.REMOVE)
        self.assertEqual(action.lower_confidence_bound, Decimal(125))
        self.assertEqual(action.target_range, TickRange(-120, 120, 60))

    def test_hold_is_always_valid(self) -> None:
        action = select_action({}, Decimal(-1))
        self.assertEqual(action.action_type, LpActionType.HOLD)


class TrancheTests(unittest.TestCase):
    def test_tranche_validation(self) -> None:
        tick_range = TickRange(-180, 180, 60)
        tranche = Tranche(
            position_id="p1",
            purpose=TranchePurpose.CORE_MARKET_MAKING,
            tick_range=tick_range,
            capital0=1_000_000,
            capital1=2_000_000,
            liquidity=5_000_000,
            max_loss_if_zero=Decimal(1000),
            expected_fee_density=Decimal(80),
            expected_lvr=Decimal(20),
            expected_time_in_range=Decimal("0.6"),
            exit_depth=Decimal(5000),
        )
        self.assertEqual(tranche.position_id, "p1")
        with self.assertRaisesRegex(ValueError, "time_in_range"):
            Tranche(
                position_id="p2",
                purpose=TranchePurpose.IDLE_RESERVE,
                tick_range=tick_range,
                capital0=0,
                capital1=0,
                liquidity=0,
                max_loss_if_zero=Decimal(0),
                expected_fee_density=Decimal(0),
                expected_lvr=Decimal(0),
                expected_time_in_range=Decimal("1.5"),
                exit_depth=Decimal(0),
            )
        with self.assertRaisesRegex(ValueError, "nonnegative integer"):
            Tranche(
                position_id="p3",
                purpose=TranchePurpose.IDLE_RESERVE,
                tick_range=tick_range,
                capital0=-1,
                capital1=0,
                liquidity=0,
                max_loss_if_zero=Decimal(0),
                expected_fee_density=Decimal(0),
                expected_lvr=Decimal(0),
                expected_time_in_range=Decimal(0),
                exit_depth=Decimal(0),
            )

    def test_lp_action_requires_target_range_for_non_hold(self) -> None:
        with self.assertRaisesRegex(ValueError, "target range"):
            LpAction(LpActionType.RECENTER, Decimal(1), Decimal(1))
        action = LpAction(
            LpActionType.RECENTER,
            Decimal(1),
            Decimal(1),
            target_range=TickRange(-180, 60, 60),
        )
        self.assertEqual(action.action_type, LpActionType.RECENTER)


if __name__ == "__main__":
    unittest.main()
