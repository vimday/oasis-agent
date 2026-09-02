from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class CostModel:
    gas_cost: Decimal
    rebalancing_cost: Decimal
    adverse_selection_cost: Decimal
    inventory_risk_cost: Decimal


@dataclass(frozen=True, slots=True)
class LpPerformance:
    capital: Decimal
    gross_fees: Decimal
    impermanent_loss: Decimal
    costs: Decimal
    net_pnl: Decimal
    net_yield: Decimal


@dataclass(frozen=True, slots=True)
class AccountingComponents:
    trader_input: Decimal
    trader_output: Decimal
    pool_delta: Decimal
    protocol_fee: Decimal
    lp_fee: Decimal
    hook_delta: Decimal
    token_transfer_delta: Decimal
    unexplained: Decimal = Decimal("0")


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    closed: bool
    residual: Decimal


@dataclass(frozen=True, slots=True)
class PoolState:
    volatility: Decimal
    inventory_imbalance: Decimal
    adverse_selection: Decimal
    rebalance_incentive: Decimal
