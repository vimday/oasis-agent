from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ..models.common import canonical_json, sha256_hex
from ..models.finance import PoolState
from ..models.v4 import SwapDirection
from ..uniswap_v4.fees import MAX_LP_FEE, validate_lp_fee


@dataclass(frozen=True, slots=True)
class FeeQuote:
    effective_fee: int
    min_fee: int
    max_fee: int
    rule_version: str
    inputs: tuple[Decimal, ...]
    state_hash: str
    valid_until_timestamp: int
    rate_limit_elapsed_seconds: int | None


@dataclass(frozen=True, slots=True)
class TransparentFeeStrategy:
    min_fee: int
    max_fee: int
    base_fee: int
    rule_version: str = "transparent-v1"
    max_fee_change_per_second: int = 10_000
    max_rate_limit_elapsed_seconds: int = 300
    max_oracle_age_seconds: int = 30
    quote_validity_seconds: int = 5
    max_oracle_deviation: Decimal = Decimal("0.02")

    def __post_init__(self) -> None:
        validate_lp_fee(self.min_fee)
        validate_lp_fee(self.base_fee)
        validate_lp_fee(self.max_fee)
        if not self.min_fee <= self.base_fee <= self.max_fee:
            raise ValueError("fee bounds are invalid")
        for name, value in (
            ("max_fee_change_per_second", self.max_fee_change_per_second),
            ("max_rate_limit_elapsed_seconds", self.max_rate_limit_elapsed_seconds),
            ("max_oracle_age_seconds", self.max_oracle_age_seconds),
            ("quote_validity_seconds", self.quote_validity_seconds),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        if self.max_fee_change_per_second > MAX_LP_FEE:
            raise ValueError("max fee change must fit the LP fee limit")
        if self.max_rate_limit_elapsed_seconds == 0:
            raise ValueError("rate-limit elapsed-time cap must be positive")
        if not self.max_oracle_deviation.is_finite() or self.max_oracle_deviation < 0:
            raise ValueError("max oracle deviation must be finite and nonnegative")

    @staticmethod
    def _validate_state(state: PoolState) -> None:
        values = (
            ("volatility", state.volatility),
            ("inventory_imbalance", state.inventory_imbalance),
            ("adverse_selection", state.adverse_selection),
            ("rebalance_incentive", state.rebalance_incentive),
        )
        for name, value in values:
            if not value.is_finite() or abs(value) > Decimal(1_000_000):
                raise ValueError(f"{name} is outside the supported finite range")
        for name, value in (
            ("volatility", state.volatility),
            ("adverse_selection", state.adverse_selection),
            ("rebalance_incentive", state.rebalance_incentive),
        ):
            if value < 0:
                raise ValueError(f"{name} must be nonnegative")
        for name, integer_value in (
            ("observed_at_timestamp", state.observed_at_timestamp),
            ("oracle_timestamp", state.oracle_timestamp),
        ):
            if (
                isinstance(integer_value, bool)
                or not isinstance(integer_value, int)
                or integer_value < 0
            ):
                raise ValueError(f"{name} must be a nonnegative integer")
        if state.oracle_timestamp > state.observed_at_timestamp:
            raise ValueError("oracle timestamp cannot be in the future")
        if not state.oracle_deviation.is_finite() or state.oracle_deviation < 0:
            raise ValueError("oracle deviation is invalid")
        if (state.previous_fee is None) != (state.previous_fee_timestamp is None):
            raise ValueError("previous fee and timestamp must be supplied together")
        if state.previous_fee_timestamp is not None and (
            isinstance(state.previous_fee_timestamp, bool)
            or not isinstance(state.previous_fee_timestamp, int)
            or state.previous_fee_timestamp < 0
            or state.previous_fee_timestamp > state.observed_at_timestamp
        ):
            raise ValueError("previous fee timestamp is invalid")

    def quote(self, state: PoolState, direction: SwapDirection) -> FeeQuote:
        self._validate_state(state)
        oracle_age = state.observed_at_timestamp - state.oracle_timestamp
        if oracle_age > self.max_oracle_age_seconds:
            raise ValueError("oracle state is stale")
        if state.oracle_deviation > self.max_oracle_deviation:
            raise ValueError("oracle state deviates beyond the configured guard")

        directional_inventory = state.inventory_imbalance
        if direction is SwapDirection.ONE_FOR_ZERO:
            directional_inventory = -directional_inventory
        score = (
            state.volatility
            + directional_inventory
            + state.adverse_selection
            - state.rebalance_incentive
        )
        target = self.base_fee + int(score * Decimal(10_000))
        bounded = max(self.min_fee, min(self.max_fee, target))
        elapsed: int | None = None
        if state.previous_fee is not None:
            validate_lp_fee(state.previous_fee)
            assert state.previous_fee_timestamp is not None
            elapsed = min(
                state.observed_at_timestamp - state.previous_fee_timestamp,
                self.max_rate_limit_elapsed_seconds,
            )
            maximum_change = self.max_fee_change_per_second * elapsed
            bounded = max(
                state.previous_fee - maximum_change,
                min(state.previous_fee + maximum_change, bounded),
            )
            bounded = max(self.min_fee, min(self.max_fee, bounded))
        inputs = (
            state.volatility,
            directional_inventory,
            state.adverse_selection,
            state.rebalance_incentive,
            Decimal(oracle_age),
            state.oracle_deviation,
        )
        state_hash = sha256_hex(
            canonical_json(
                {
                    "direction": direction.value,
                    "inputs": tuple(str(value) for value in inputs),
                    "observed_at_timestamp": state.observed_at_timestamp,
                    "oracle_timestamp": state.oracle_timestamp,
                    "previous_fee": state.previous_fee,
                    "previous_fee_timestamp": state.previous_fee_timestamp,
                    "rule_version": self.rule_version,
                }
            )
        )
        return FeeQuote(
            bounded,
            self.min_fee,
            self.max_fee,
            self.rule_version,
            inputs,
            state_hash,
            state.observed_at_timestamp + self.quote_validity_seconds,
            elapsed,
        )
