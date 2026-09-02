import unittest
from decimal import Decimal

from oasis_agent.errors import AccountingError
from oasis_agent.farming.metrics import reconcile_swap
from oasis_agent.models.finance import AccountingComponents, CostModel
from oasis_agent.farming.metrics import calculate_lp_net_yield


class FarmingTests(unittest.TestCase):
    def test_unexplained_delta_is_rejected(self) -> None:
        components = AccountingComponents(
            trader_input=Decimal("100"), trader_output=Decimal("99"), pool_delta=Decimal("-1"),
            protocol_fee=Decimal("0"), lp_fee=Decimal("0"), hook_delta=Decimal("0"),
            token_transfer_delta=Decimal("0"), unexplained=Decimal("1"),
        )
        with self.assertRaises(AccountingError):
            reconcile_swap(components)

    def test_net_yield_subtracts_costs_and_il(self) -> None:
        result = calculate_lp_net_yield(
            capital=Decimal("1000"), gross_fees=Decimal("100"), impermanent_loss=Decimal("25"),
            costs=CostModel(Decimal("10"), Decimal("5"), Decimal("8"), Decimal("2")),
        )
        self.assertEqual(result.net_pnl, Decimal("50"))
        self.assertEqual(result.net_yield, Decimal("0.05"))


if __name__ == "__main__":
    unittest.main()
