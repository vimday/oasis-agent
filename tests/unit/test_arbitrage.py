import unittest
from decimal import Decimal

from oasis_agent.arbitrage.scanner import net_profit
from oasis_agent.models.execution import ArbitrageOpportunity, RiskLevel
from oasis_agent.models.common import EvidenceLevel
from oasis_agent.risk.policy import RiskLimits, evaluate_opportunity


class ArbitrageRiskTests(unittest.TestCase):
    def test_negative_net_profit_is_not_an_opportunity(self) -> None:
        self.assertLess(net_profit(Decimal("10"), Decimal("3"), Decimal("8")), Decimal("0"))

    def test_unresolved_evidence_is_blocked(self) -> None:
        opportunity = ArbitrageOpportunity("x", ("a", "b"), Decimal("100"), Decimal("5"), Decimal("2"), Decimal("1"), 10, Decimal("0.9"), EvidenceLevel.UNRESOLVED)
        decision = evaluate_opportunity(opportunity, RiskLimits(Decimal("1"), 20, Decimal("100")))
        self.assertEqual(decision.level, RiskLevel.BLOCKED)
        self.assertFalse(decision.allowed)


if __name__ == "__main__":
    unittest.main()
