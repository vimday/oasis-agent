from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ..models.common import EvidenceLevel
from ..models.execution import ArbitrageOpportunity, RiskDecision, RiskLevel


@dataclass(frozen=True, slots=True)
class RiskLimits:
    min_worst_case_profit: Decimal
    current_block: int
    max_capital: Decimal


def evaluate_opportunity(opportunity: ArbitrageOpportunity, limits: RiskLimits) -> RiskDecision:
    reasons: list[str] = []
    if opportunity.evidence_level is not EvidenceLevel.CONFIRMED:
        reasons.append("dynamic fee or accounting evidence is unresolved")
    if opportunity.expiry_block < limits.current_block:
        reasons.append("opportunity expired")
    if opportunity.required_capital > limits.max_capital:
        reasons.append("capital limit exceeded")
    if opportunity.worst_case_profit < limits.min_worst_case_profit:
        reasons.append("worst-case profit below safety margin")
    if reasons:
        return RiskDecision(RiskLevel.BLOCKED, False, tuple(reasons))
    return RiskDecision(RiskLevel.LOW, True, ())
