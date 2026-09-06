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
    min_confidence: Decimal = Decimal(0)

    def __post_init__(self) -> None:
        for name, value in (
            ("min_worst_case_profit", self.min_worst_case_profit),
            ("max_capital", self.max_capital),
            ("min_confidence", self.min_confidence),
        ):
            if not isinstance(value, Decimal):
                raise TypeError(f"{name} must be a Decimal")
            if not value.is_finite():
                raise ValueError(f"{name} must be finite")
        if isinstance(self.current_block, bool) or not isinstance(self.current_block, int):
            raise TypeError("current_block must be an integer")
        if self.current_block < 0:
            raise ValueError("current_block must be nonnegative")
        if self.max_capital < 0:
            raise ValueError("max_capital must be nonnegative")
        if not Decimal(0) <= self.min_confidence <= Decimal(1):
            raise ValueError("min_confidence must be between zero and one")


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
    if opportunity.confidence < limits.min_confidence:
        reasons.append("confidence below policy threshold")
    if opportunity.expected_profit <= 0:
        reasons.append("expected profit is not positive")
    if reasons:
        return RiskDecision(RiskLevel.BLOCKED, False, tuple(reasons))
    return RiskDecision(RiskLevel.LOW, True, ())
