import unittest
from decimal import Decimal

from oasis_agent.errors import ExecutionBlockedError
from oasis_agent.execution.state_machine import ExecutionStateMachine
from oasis_agent.models.common import EvidenceLevel
from oasis_agent.models.execution import ArbitrageOpportunity, ExecutionMode


class ExecutionTests(unittest.TestCase):
    def test_observe_does_not_broadcast(self) -> None:
        machine = ExecutionStateMachine(ExecutionMode.OBSERVE)
        result = machine.observe("opp")
        self.assertEqual(result.state, "observed")
        self.assertIsNone(result.transaction_hash)

    def test_expired_opportunity_is_blocked(self) -> None:
        machine = ExecutionStateMachine(ExecutionMode.APPROVE)
        opportunity = ArbitrageOpportunity(
            "x",
            ("a", "b"),
            Decimal(1),
            Decimal(2),
            Decimal(1),
            Decimal(0),
            10,
            Decimal(1),
            EvidenceLevel.CONFIRMED,
        )
        with self.assertRaises(ExecutionBlockedError):
            machine.approve(opportunity, current_block=11, risk_allowed=True)


if __name__ == "__main__":
    unittest.main()
