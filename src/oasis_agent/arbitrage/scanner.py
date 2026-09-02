from __future__ import annotations

from decimal import Decimal


def net_profit(gross_output: Decimal, capital_cost: Decimal, gas_cost: Decimal) -> Decimal:
    return gross_output - capital_cost - gas_cost
