import unittest
from decimal import Decimal

from oasis_agent.models.common import EvidenceLevel
from oasis_agent.models.execution import ArbitrageOpportunity, RiskLevel
from oasis_agent.risk.policy import RiskLimits, evaluate_opportunity


class ArbitrageRiskTests(unittest.TestCase):
    def test_unresolved_evidence_is_blocked(self) -> None:
        opportunity = ArbitrageOpportunity(
            "x",
            ("a", "b"),
            Decimal(100),
            Decimal(5),
            Decimal(2),
            Decimal(1),
            10,
            Decimal("0.9"),
            EvidenceLevel.UNRESOLVED,
        )
        decision = evaluate_opportunity(opportunity, RiskLimits(Decimal(1), 20, Decimal(100)))
        self.assertEqual(decision.level, RiskLevel.BLOCKED)
        self.assertFalse(decision.allowed)

    def test_opportunity_model_rejects_nonfinite_values_and_invalid_path(self) -> None:
        valid = {
            "opportunity_id": "x",
            "path": ("a", "b"),
            "required_capital": Decimal(100),
            "expected_profit": Decimal(5),
            "worst_case_profit": Decimal(2),
            "gas_cost": Decimal(1),
            "expiry_block": 10,
            "confidence": Decimal("0.9"),
            "evidence_level": EvidenceLevel.CONFIRMED,
        }
        with self.assertRaisesRegex(ValueError, "path"):
            ArbitrageOpportunity(**(valid | {"path": ("a",)}))
        with self.assertRaisesRegex(ValueError, "finite"):
            ArbitrageOpportunity(**(valid | {"expected_profit": Decimal("NaN")}))
        with self.assertRaisesRegex(ValueError, "cannot exceed"):
            ArbitrageOpportunity(**(valid | {"worst_case_profit": Decimal(6)}))

    def test_missing_evidence_defaults_to_unresolved(self) -> None:
        opportunity = ArbitrageOpportunity(
            "x",
            ("a", "b"),
            Decimal(100),
            Decimal(5),
            Decimal(2),
            Decimal(1),
            10,
            Decimal("0.9"),
        )
        self.assertEqual(opportunity.evidence_level, EvidenceLevel.UNRESOLVED)

    def test_risk_limits_reject_nan_and_negative_capital(self) -> None:
        with self.assertRaisesRegex(ValueError, "finite"):
            RiskLimits(Decimal("NaN"), 10, Decimal(100))
        with self.assertRaisesRegex(ValueError, "nonnegative"):
            RiskLimits(Decimal(1), 10, Decimal(-1))

    def test_risk_limits_reject_negative_worst_case_profit(self) -> None:
        with self.assertRaisesRegex(ValueError, "min_worst_case_profit"):
            RiskLimits(Decimal(-1), 10, Decimal(100))


if __name__ == "__main__":
    unittest.main()
