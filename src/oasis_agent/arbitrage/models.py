from dataclasses import dataclass
from decimal import Decimal

from ..models.common import EvidenceLevel, normalize_address


def _finite_nonnegative(value: Decimal, name: str) -> None:
    if not value.is_finite() or value < 0:
        raise ValueError(f"{name} must be finite and nonnegative")


def _finite(value: Decimal, name: str) -> None:
    if not value.is_finite():
        raise ValueError(f"{name} must be finite")


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    route: tuple[str, ...]
    gross_output: Decimal
    required_capital: Decimal
    gas_cost: Decimal
    expiry_block: int
    worst_case_profit: Decimal
    fee_cost: Decimal = Decimal(0)
    slippage_cost: Decimal = Decimal(0)
    price_impact_cost: Decimal = Decimal(0)
    confidence: Decimal = Decimal(0)
    evidence_level: EvidenceLevel = EvidenceLevel.UNRESOLVED
    targets: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if len(self.route) < 2:
            raise ValueError("route must contain at least two hops")
        _finite_nonnegative(self.gross_output, "gross_output")
        _finite_nonnegative(self.required_capital, "required_capital")
        _finite_nonnegative(self.gas_cost, "gas_cost")
        _finite_nonnegative(self.fee_cost, "fee_cost")
        _finite_nonnegative(self.slippage_cost, "slippage_cost")
        _finite_nonnegative(self.price_impact_cost, "price_impact_cost")
        if self.expiry_block <= 0:
            raise ValueError("expiry_block must be positive")
        _finite(self.worst_case_profit, "worst_case_profit")
        if self.worst_case_profit > self.expected_profit:
            raise ValueError("worst_case_profit cannot exceed expected profit")
        _finite(self.confidence, "confidence")
        if not Decimal(0) <= self.confidence <= Decimal(1):
            raise ValueError("confidence must be between zero and one")
        if not isinstance(self.evidence_level, EvidenceLevel):
            raise TypeError("evidence_level must be an EvidenceLevel")
        object.__setattr__(self, "targets", tuple(normalize_address(x) for x in self.targets))

    @property
    def expected_profit(self) -> Decimal:
        return (
            self.gross_output
            - self.required_capital
            - self.gas_cost
            - self.fee_cost
            - self.slippage_cost
            - self.price_impact_cost
        )


@dataclass(frozen=True, slots=True)
class RouteConstraint:
    snapshot: MarketSnapshot
    min_profit: Decimal = Decimal(0)


__all__ = ["MarketSnapshot", "RouteConstraint"]
