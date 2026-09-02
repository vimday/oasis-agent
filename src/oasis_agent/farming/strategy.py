from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ..models.finance import PoolState
from ..models.v4 import SwapDirection


@dataclass(frozen=True, slots=True)
class FeeQuote:
    effective_fee: int
    min_fee: int
    max_fee: int
    rule_version: str
    inputs: tuple[Decimal, ...]


@dataclass(frozen=True, slots=True)
class TransparentFeeStrategy:
    min_fee: int
    max_fee: int
    base_fee: int
    rule_version: str = "transparent-v1"

    def __post_init__(self) -> None:
        if not 0 <= self.min_fee <= self.base_fee <= self.max_fee <= 0x3FFFFF:
            raise ValueError("fee bounds are invalid")

    def quote(self, state: PoolState, direction: SwapDirection) -> FeeQuote:
        del direction
        score = state.volatility + abs(state.inventory_imbalance) + state.adverse_selection - state.rebalance_incentive
        fee = self.base_fee + int(score * Decimal("10000"))
        return FeeQuote(max(self.min_fee, min(self.max_fee, fee)), self.min_fee, self.max_fee, self.rule_version, (state.volatility, state.inventory_imbalance, state.adverse_selection, state.rebalance_incentive))
