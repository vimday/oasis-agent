from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from decimal import Decimal

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
    evidence_level: EvidenceLevel = EvidenceLevel.CONFIRMED


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
