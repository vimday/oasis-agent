import unittest
from decimal import Decimal

from oasis_agent.errors import AccountingError
from oasis_agent.farming.metrics import calculate_lp_net_yield, reconcile_swap
from oasis_agent.models.finance import AccountingComponents, AssetPair, CostModel, TokenAmount

TOKEN0 = "0x" + "1" * 40
TOKEN1 = "0x" + "2" * 40


def pair(amount0: str | int, amount1: str | int) -> AssetPair:
    return AssetPair(
        TokenAmount(TOKEN0, 6, int(amount0)),
        TokenAmount(TOKEN1, 18, int(amount1)),
    )


class FarmingTests(unittest.TestCase):
    def test_two_asset_accounting_closes_independently(self) -> None:
        components = AccountingComponents(
            trader_input=pair(-100, 0),
            trader_output=pair(0, 9),
            pool_delta=pair(99, -9),
            protocol_fee=pair(0, 0),
            lp_fee=pair(1, 0),
            hook_delta=pair(0, 0),
            token_transfer_delta=pair(0, 0),
            unexplained=pair(0, 0),
        )
        result = reconcile_swap(components)
        self.assertTrue(result.closed)
        self.assertEqual(result.residual.amount0, 0)
        self.assertEqual(result.residual.amount1, 0)

    def test_unexplained_delta_is_rejected_per_asset(self) -> None:
        components = AccountingComponents(
            trader_input=pair(-100, 0),
            trader_output=pair(0, 9),
            pool_delta=pair(99, -9),
            protocol_fee=pair(0, 0),
            lp_fee=pair(1, 0),
            hook_delta=pair(0, 0),
            token_transfer_delta=pair(0, 0),
            unexplained=pair(0, 1),
        )
        with self.assertRaises(AccountingError):
            reconcile_swap(components)

    def test_asset_pairs_with_different_tokens_cannot_be_added(self) -> None:
        other = AssetPair(
            TokenAmount(TOKEN0, 6, 1),
            TokenAmount("0x" + "3" * 40, 18, 1),
        )
        with self.assertRaisesRegex(ValueError, "different token ordering"):
            pair(1, 1) + other

    def test_token_amount_requires_integer_base_units(self) -> None:
        for amount in (Decimal(1), Decimal("1.5"), True):
            with (
                self.subTest(amount=amount),
                self.assertRaisesRegex(TypeError, "integer base-unit"),
            ):
                TokenAmount(TOKEN0, 6, amount)  # type: ignore[arg-type]

    def test_asset_pair_requires_canonical_token_order(self) -> None:
        with self.assertRaisesRegex(ValueError, "canonical token order"):
            AssetPair(TokenAmount(TOKEN1, 18, 1), TokenAmount(TOKEN0, 6, 1))

    def test_net_yield_subtracts_costs_and_il(self) -> None:
        result = calculate_lp_net_yield(
            capital=Decimal(1000),
            gross_fees=Decimal(100),
            impermanent_loss=Decimal(25),
            costs=CostModel(Decimal(10), Decimal(5), Decimal(8), Decimal(2)),
        )
        self.assertEqual(result.net_pnl, Decimal(50))
        self.assertEqual(result.net_yield, Decimal("0.05"))


if __name__ == "__main__":
    unittest.main()
