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


@dataclass(frozen=True, slots=True)
class Band:
    """One row of a discrete monotone fee band table.

    ``upper_bound`` is the exclusive integer magnitude bound in the band's
    declared unit: token base units for impact tables, basis points for
    volatility/toxicity/inventory tables. Premiums must be non-decreasing
    across ascending bounds so the resulting fee rule is monotone in every
    input; the blueprint (5.3) requires a compact, reviewable, replayable
    policy rather than an unconstrained learned model.
    """

    upper_bound: int
    premium: int

    def __post_init__(self) -> None:
        if isinstance(self.upper_bound, bool) or not isinstance(self.upper_bound, int):
            raise TypeError("band upper bound must be an integer")
        if self.upper_bound <= 0:
            raise ValueError("band upper bound must be positive")
        if isinstance(self.premium, bool) or not isinstance(self.premium, int):
            raise TypeError("band premium must be an integer")
        if self.premium < 0:
            raise ValueError("band premium must be nonnegative")


def _band_premium(bands: tuple[Band, ...], magnitude: int) -> int:
    """First band whose exclusive bound the magnitude falls below, else the last premium."""
    if isinstance(magnitude, bool) or not isinstance(magnitude, int):
        raise TypeError("band magnitude must be an integer")
    if magnitude < 0:
        raise ValueError("band magnitude must be nonnegative")
    premium = 0
    for band in bands:
        if magnitude < band.upper_bound:
            return band.premium
        premium = band.premium
    return premium


def _validate_band_table(bands: tuple[Band, ...], name: str, term_cap: int) -> None:
    """Strictly ascending bounds, monotone non-decreasing premiums, per-term cap."""
    if not isinstance(bands, tuple):
        raise TypeError(f"{name} must be a tuple of bands")
    previous_bound = 0
    previous_premium = 0
    for band in bands:
        if not isinstance(band, Band):
            raise TypeError(f"{name} entries must be Band instances")
        if band.upper_bound <= previous_bound:
            raise ValueError(f"{name} bounds must be strictly ascending")
        if band.premium < previous_premium:
            raise ValueError(f"{name} premiums must be non-decreasing (monotone)")
        if band.premium > term_cap:
            raise ValueError(f"{name} premium exceeds the per-term cap")
        previous_bound = band.upper_bound
        previous_premium = band.premium


def _to_bps(value: Decimal) -> int:
    """Convert a finite nonnegative decimal fraction to integer basis points."""
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise ValueError("band input must be a finite nonnegative Decimal")
    return int(value * Decimal(10_000))


@dataclass(frozen=True, slots=True)
class BandTableFeeStrategy:
    """Discrete monotone band-table fee policy (blueprint section 5.3).

    fee_pips = clamp(
        base_fee + volatility_band_premium + expected_impact_premium
        + lagged_toxicity_premium + max(0, direction_worsening_inventory_premium)
        - benign_flow_discount,
        min_fee, max_fee,
    )

    Each term is capped separately at construction, and the total change is
    rate-limited by elapsed seconds. Size bands are denominated separately in
    token0 and token1 integer base units; ``amountSpecified`` names the input
    asset on exact-input swaps and the output asset on exact-output swaps, so
    the four direction/mode paths select the correct token threshold. Inventory
    skew follows blueprint 5.2: ``I > 0`` means excess token0 and a zeroForOne
    swap worsens it, so the directional premium must never take the absolute
    value before sign checks.
    """

    min_fee: int
    max_fee: int
    base_fee: int
    volatility_bands: tuple[Band, ...]
    impact_bands0: tuple[Band, ...]
    impact_bands1: tuple[Band, ...]
    toxicity_bands: tuple[Band, ...]
    inventory_bands: tuple[Band, ...]
    benign_flow_discount: int = 0
    rule_version: str = "band-table-v1"
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
        term_cap = self.max_fee - self.min_fee
        for name, table in (
            ("volatility_bands", self.volatility_bands),
            ("impact_bands0", self.impact_bands0),
            ("impact_bands1", self.impact_bands1),
            ("toxicity_bands", self.toxicity_bands),
            ("inventory_bands", self.inventory_bands),
        ):
            _validate_band_table(table, name, term_cap)
        if (
            isinstance(self.benign_flow_discount, bool)
            or not isinstance(self.benign_flow_discount, int)
            or not 0 <= self.benign_flow_discount <= term_cap
        ):
            raise ValueError("benign flow discount must be an integer within the per-term cap")
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
        if not self.rule_version:
            raise ValueError("rule_version is required")

    def quote(
        self,
        state: PoolState,
        direction: SwapDirection,
        amount_specified: int,
        exact_input: bool,
        benign_flow: bool = False,
    ) -> FeeQuote:
        TransparentFeeStrategy._validate_state(state)
        oracle_age = state.observed_at_timestamp - state.oracle_timestamp
        if oracle_age > self.max_oracle_age_seconds:
            raise ValueError("oracle state is stale")
        if state.oracle_deviation > self.max_oracle_deviation:
            raise ValueError("oracle state deviates beyond the configured guard")
        if isinstance(amount_specified, bool) or not isinstance(amount_specified, int):
            raise TypeError("amount_specified must be an integer base-unit amount")
        if amount_specified == 0:
            raise ValueError("amount_specified must be nonzero")

        magnitude = abs(amount_specified)
        # v4 names amountSpecified in the input asset for exact-input swaps and the output
        # asset for exact-output swaps; the specified currency therefore differs per path.
        specified_currency0 = (direction is SwapDirection.ZERO_FOR_ONE) == exact_input
        impact_premium = _band_premium(
            self.impact_bands0 if specified_currency0 else self.impact_bands1, magnitude
        )
        volatility_premium = _band_premium(self.volatility_bands, _to_bps(state.volatility))
        toxicity_premium = _band_premium(self.toxicity_bands, _to_bps(state.adverse_selection))

        inventory = state.inventory_imbalance
        # I > 0 means excess token0; zeroForOne sends token0 into the pool and worsens
        # positive I, while oneForZero worsens negative I. Sign must never be erased.
        worsening = (direction is SwapDirection.ZERO_FOR_ONE and inventory > 0) or (
            direction is SwapDirection.ONE_FOR_ZERO and inventory < 0
        )
        inventory_premium = (
            _band_premium(self.inventory_bands, _to_bps(abs(inventory))) if worsening else 0
        )
        discount = self.benign_flow_discount if benign_flow else 0

        total = (
            self.base_fee
            + volatility_premium
            + impact_premium
            + toxicity_premium
            + inventory_premium
            - discount
        )
        bounded = max(self.min_fee, min(self.max_fee, total))
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
            Decimal(magnitude),
            Decimal(volatility_premium),
            Decimal(impact_premium),
            Decimal(toxicity_premium),
            Decimal(inventory_premium),
            Decimal(discount),
            Decimal(oracle_age),
            state.oracle_deviation,
        )
        state_hash = sha256_hex(
            canonical_json(
                {
                    "direction": direction.value,
                    "exact_input": exact_input,
                    "benign_flow": benign_flow,
                    "amount_specified": amount_specified,
                    "bands": {
                        "volatility": [[b.upper_bound, b.premium] for b in self.volatility_bands],
                        "impact0": [[b.upper_bound, b.premium] for b in self.impact_bands0],
                        "impact1": [[b.upper_bound, b.premium] for b in self.impact_bands1],
                        "toxicity": [[b.upper_bound, b.premium] for b in self.toxicity_bands],
                        "inventory": [[b.upper_bound, b.premium] for b in self.inventory_bands],
                    },
                    "benign_flow_discount": self.benign_flow_discount,
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
