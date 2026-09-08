from decimal import Decimal

from ..errors import AccountingError
from ..models.finance import AccountingComponents, CostModel, LpPerformance, ReconciliationResult


def reconcile_swap(components: AccountingComponents) -> ReconciliationResult:
    if not components.unexplained.is_zero():
        raise AccountingError(f"unexplained accounting residual: {components.unexplained}")
    residual = components.trader_input + components.trader_output
    residual = residual + components.pool_delta
    residual = residual + components.protocol_fee + components.lp_fee
    residual = residual + components.hook_delta + components.token_transfer_delta
    if not residual.is_zero():
        raise AccountingError(f"accounting does not close: {residual}")
    return ReconciliationResult(closed=True, residual=components.trader_input.zero())


def calculate_lp_net_yield(
    capital: Decimal, gross_fees: Decimal, impermanent_loss: Decimal, costs: CostModel
) -> LpPerformance:
    for name, value in (
        ("capital", capital),
        ("gross_fees", gross_fees),
        ("impermanent_loss", impermanent_loss),
    ):
        if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
            raise ValueError(f"{name} must be a finite nonnegative Decimal")
    if capital <= 0:
        raise ValueError("capital must be positive")
    total_costs = (
        costs.gas_cost
        + costs.rebalancing_cost
        + costs.adverse_selection_cost
        + costs.inventory_risk_cost
        + costs.failed_tx_cost
        + costs.opportunity_cost
    )
    net_pnl = gross_fees - impermanent_loss - total_costs
    return LpPerformance(
        capital, gross_fees, impermanent_loss, total_costs, net_pnl, net_pnl / capital
    )
