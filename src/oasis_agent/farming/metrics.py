from __future__ import annotations

from decimal import Decimal

from ..errors import AccountingError
from ..models.finance import AccountingComponents, CostModel, LpPerformance, ReconciliationResult


def reconcile_swap(components: AccountingComponents) -> ReconciliationResult:
    if components.unexplained != 0:
        raise AccountingError(f"unexplained accounting residual: {components.unexplained}")
    residual = components.trader_input + components.trader_output + components.pool_delta
    residual += components.protocol_fee + components.lp_fee + components.hook_delta
    residual += components.token_transfer_delta
    if residual != 0:
        raise AccountingError(f"accounting does not close: {residual}")
    return ReconciliationResult(closed=True, residual=Decimal("0"))


def calculate_lp_net_yield(capital: Decimal, gross_fees: Decimal, impermanent_loss: Decimal, costs: CostModel) -> LpPerformance:
    if capital <= 0:
        raise ValueError("capital must be positive")
    total_costs = costs.gas_cost + costs.rebalancing_cost + costs.adverse_selection_cost + costs.inventory_risk_cost
    net_pnl = gross_fees - impermanent_loss - total_costs
    return LpPerformance(capital, gross_fees, impermanent_loss, total_costs, net_pnl, net_pnl / capital)
