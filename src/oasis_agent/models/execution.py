from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from .common import EvidenceLevel


class ExecutionMode(str, Enum):
    OBSERVE = "observe"
    SIMULATE = "simulate"
    APPROVE = "approve"
    EXECUTE = "execute"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class ArbitrageOpportunity:
    opportunity_id: str
    path: tuple[str, ...]
    required_capital: Decimal
    expected_profit: Decimal
    worst_case_profit: Decimal
    gas_cost: Decimal
    expiry_block: int
    confidence: Decimal
    evidence_level: EvidenceLevel = EvidenceLevel.UNRESOLVED

    def __post_init__(self) -> None:
        if not self.opportunity_id:
            raise ValueError("opportunity_id is required")
        if len(self.path) < 2 or any(not hop for hop in self.path):
            raise ValueError("opportunity path must contain at least two nonempty hops")
        for name, value in (
            ("required_capital", self.required_capital),
            ("expected_profit", self.expected_profit),
            ("worst_case_profit", self.worst_case_profit),
            ("gas_cost", self.gas_cost),
            ("confidence", self.confidence),
        ):
            if not isinstance(value, Decimal):
                raise TypeError(f"{name} must be a Decimal")
            if not value.is_finite():
                raise ValueError(f"{name} must be finite")
        if self.required_capital < 0 or self.gas_cost < 0:
            raise ValueError("capital and gas cost must be nonnegative")
        if self.worst_case_profit > self.expected_profit:
            raise ValueError("worst_case_profit cannot exceed expected_profit")
        if isinstance(self.expiry_block, bool) or not isinstance(self.expiry_block, int):
            raise TypeError("expiry_block must be an integer")
        if self.expiry_block <= 0:
            raise ValueError("expiry_block must be positive")
        if not Decimal(0) <= self.confidence <= Decimal(1):
            raise ValueError("confidence must be between zero and one")
        if not isinstance(self.evidence_level, EvidenceLevel):
            raise TypeError("evidence_level must be an EvidenceLevel")


@dataclass(frozen=True, slots=True)
class RiskDecision:
    level: RiskLevel
    allowed: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ExecutionRecord:
    opportunity_id: str
    mode: ExecutionMode
    state: str
    transaction_hash: str | None = None
