import unittest
from decimal import Decimal

from oasis_agent.risk.lp_policy import (
    BASIS_POINT,
    FAILURE_MATRIX,
    CanaryCap,
    CanaryRiskLimits,
    FailureResponse,
    canary_cap,
    classify_failure,
)


class CanarySizingTests(unittest.TestCase):
    @staticmethod
    def limits(**overrides: Decimal) -> CanaryRiskLimits:
        base = {
            "approved_experiment_loss": Decimal(1000),
            "portfolio_nav": Decimal(1000000),
            "exit_depth_two_sided": Decimal(200000),
            "reserve_requirement": Decimal(50000),
        }
        base.update(overrides)
        return CanaryRiskLimits(**base)  # type: ignore[arg-type]

    def test_cap_is_minimum_of_four_ceilings(self) -> None:
        cap = canary_cap(self.limits())
        # nav cap = 5bp of 1M = 500; depth cap = 1bp of 200k = 20; loss = 1000; reserve = 950k.
        self.assertEqual(cap.nav_cap, Decimal(500))
        self.assertEqual(cap.exit_depth_cap, Decimal(20))
        self.assertEqual(cap.reserve_preserving_cap, Decimal(950000))
        self.assertEqual(cap.cap, Decimal(20))

    def test_each_ceiling_can_bind(self) -> None:
        cases = (
            (self.limits(approved_experiment_loss=Decimal(5)), Decimal(5)),
            # reserve_requirement must stay below nav so the 5bp nav ceiling binds.
            (
                self.limits(portfolio_nav=Decimal(1000), reserve_requirement=Decimal(500)),
                Decimal("0.5"),
            ),
            (self.limits(exit_depth_two_sided=Decimal(3000)), Decimal("0.3")),
            (
                self.limits(portfolio_nav=Decimal(10000), reserve_requirement=Decimal(9999)),
                Decimal(1),
            ),
        )
        for limits, expected in cases:
            with self.subTest(limits=limits):
                self.assertEqual(canary_cap(limits).cap, expected)

    def test_reserve_preserving_cap_is_floored_at_zero(self) -> None:
        cap = canary_cap(self.limits(portfolio_nav=Decimal(100), reserve_requirement=Decimal(500)))
        self.assertEqual(cap.reserve_preserving_cap, Decimal(0))
        self.assertEqual(cap.cap, Decimal(0))

    def test_cap_components_use_declared_fractions(self) -> None:
        cap = canary_cap(self.limits())
        self.assertEqual(
            cap.nav_cap, cap.portfolio_nav if False else Decimal(1000000) * BASIS_POINT * Decimal(5)
        )
        self.assertEqual(cap.exit_depth_cap, Decimal(200000) * BASIS_POINT)

    def test_invalid_limits_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "finite"):
            CanaryRiskLimits(Decimal("nan"), Decimal(1), Decimal(1), Decimal(1))
        with self.assertRaisesRegex(ValueError, "nonnegative"):
            CanaryRiskLimits(Decimal(-1), Decimal(1), Decimal(1), Decimal(1))

    def test_cap_validates_its_own_minimum(self) -> None:
        with self.assertRaisesRegex(ValueError, "minimum"):
            CanaryCap(Decimal(1), Decimal(2), Decimal(3), Decimal(4), Decimal(9))


class FailureMatrixTests(unittest.TestCase):
    def test_matrix_has_all_blueprint_triggers(self) -> None:
        responses = {trigger: response for trigger, response, _ in FAILURE_MATRIX}
        self.assertEqual(
            set(responses.values()),
            set(FailureResponse),
        )

    def test_classify_matches_normalized_triggers(self) -> None:
        cases = {
            "rpc/feed stale, gap, or disagreement": FailureResponse.NO_NEW_EXPOSURE_RECONCILE,
            "sequencer outage or abnormal finality lag": FailureResponse.HALT_SUBMISSIONS_PRESERVE_GAS,
            "reference price stale/manipulable": FailureResponse.CONSERVATIVE_FEE_FALLBACK,
            "quote/settlement mismatch or hook revert": FailureResponse.DISABLE_ROUTE_AND_RULE,
            "position/balance/allowance/nonce mismatch": FailureResponse.HALT_EXECUTOR_NO_COMPENSATING_TRADE,
            "token bytecode/admin/transfer behavior changes": FailureResponse.REJECT_TOKEN_PREPARE_REMOVAL,
            "hook/poolmanager/router/quoter code hash changes": FailureResponse.FAIL_CLOSED_NEW_AUDIT,
            "range exit during toxic/jump regime": FailureResponse.NO_REFLEXIVE_RECENTER_EVALUATE,
            "daily loss, drawdown, residual, or gas budget breach": FailureResponse.INDEPENDENT_KILL_SWITCH,
            "governance/security incident": FailureResponse.FREEZE_PARAMETER_CHANGES,
        }
        for trigger, expected in cases.items():
            with self.subTest(trigger=trigger):
                decision = classify_failure(trigger)
                self.assertEqual(decision.response, expected)
                self.assertTrue(decision.reduces_exposure_only)
                self.assertTrue(decision.runbook)

    def test_whitespace_and_case_are_normalized(self) -> None:
        decision = classify_failure("  RPC/Feed   STALE, GAP, or Disagreement ")
        self.assertEqual(decision.response, FailureResponse.NO_NEW_EXPOSURE_RECONCILE)

    def test_unknown_trigger_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown failure trigger"):
            classify_failure("market regime change")


if __name__ == "__main__":
    unittest.main()
