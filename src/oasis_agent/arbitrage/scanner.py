from __future__ import annotations

from decimal import Decimal

from ..models.common import canonical_json, sha256_hex
from ..models.execution import ArbitrageOpportunity
from .models import MarketSnapshot


def net_profit(gross_output: Decimal, capital_cost: Decimal, gas_cost: Decimal) -> Decimal:
    return gross_output - capital_cost - gas_cost


def scan_opportunities(
    snapshot: MarketSnapshot, current_block: int = 0, min_profit: Decimal = Decimal(0)
) -> tuple[ArbitrageOpportunity, ...]:
    profit = snapshot.expected_profit
    if profit < min_profit or profit <= 0:
        return ()
    if snapshot.expiry_block < current_block:
        return ()
    identity = {
        "route": snapshot.route,
        "gross_output": str(snapshot.gross_output),
        "required_capital": str(snapshot.required_capital),
        "gas_cost": str(snapshot.gas_cost),
        "fee_cost": str(snapshot.fee_cost),
        "slippage_cost": str(snapshot.slippage_cost),
        "price_impact_cost": str(snapshot.price_impact_cost),
        "expiry_block": snapshot.expiry_block,
        "worst_case_profit": str(snapshot.worst_case_profit),
        "confidence": str(snapshot.confidence),
        "evidence_level": snapshot.evidence_level.value,
        "targets": snapshot.targets,
    }
    opportunity = ArbitrageOpportunity(
        opportunity_id="route-" + sha256_hex(canonical_json(identity)),
        path=snapshot.route,
        required_capital=snapshot.required_capital,
        expected_profit=profit,
        worst_case_profit=snapshot.worst_case_profit,
        gas_cost=snapshot.gas_cost,
        expiry_block=snapshot.expiry_block,
        confidence=snapshot.confidence,
        evidence_level=snapshot.evidence_level,
    )
    return (opportunity,)
