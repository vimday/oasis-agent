"""Automated range and inventory controller (blueprint sections 6.2-6.4).

Tranches are bounded, tick-aligned position slices with an explicit purpose,
capital in integer token units, worst-case loss, and expected fee/LVR/exit
metrics. For a candidate action ``a`` over horizon ``H``:

    marginal_value(a) = expected_incremental_fees
                      - expected_incremental_LVR
                      - inventory_risk_charge
                      - swap/rebalance/gas/MEV_cost
                      - failure_and_model_uncertainty_buffer

Act only when the lower confidence bound of ``marginal_value(a)`` exceeds a
precommitted minimum benefit; the model-uncertainty buffer is the v1 lower
confidence allowance. "Do nothing" is always a valid candidate. Rebalance
triggers require every applicable condition, including dwell time, hysteresis,
cooldown/daily limits, state freshness, circuit breakers, post-action limits,
and a successful pinned simulation.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from .lp_state_machine import TickRange


class TranchePurpose(str, Enum):
    CORE_MARKET_MAKING = "core_market_making"
    OUTER_INVENTORY = "outer_inventory"
    IDLE_RESERVE = "idle_reserve"


class LpActionType(str, Enum):
    HOLD = "hold"
    REMOVE = "remove"
    RECENTER = "recenter"
    REDUCE = "reduce"
    UNWIND = "unwind"


@dataclass(frozen=True, slots=True)
class Tranche:
    """One bounded position slice with its own identifier and ledger."""

    position_id: str
    purpose: TranchePurpose
    tick_range: TickRange
    capital0: int
    capital1: int
    liquidity: int
    max_loss_if_zero: Decimal
    expected_fee_density: Decimal
    expected_lvr: Decimal
    expected_time_in_range: Decimal
    exit_depth: Decimal

    def __post_init__(self) -> None:
        if not self.position_id:
            raise ValueError("position_id is required")
        if not isinstance(self.purpose, TranchePurpose):
            raise TypeError("purpose must be a TranchePurpose")
        if not isinstance(self.tick_range, TickRange):
            raise TypeError("tick_range must be a TickRange")
        for name, value in (
            ("capital0", self.capital0),
            ("capital1", self.capital1),
            ("liquidity", self.liquidity),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        for name, decimal_value in (
            ("max_loss_if_zero", self.max_loss_if_zero),
            ("expected_fee_density", self.expected_fee_density),
            ("expected_lvr", self.expected_lvr),
            ("expected_time_in_range", self.expected_time_in_range),
            ("exit_depth", self.exit_depth),
        ):
            if not isinstance(decimal_value, Decimal) or not decimal_value.is_finite():
                raise ValueError(f"{name} must be a finite Decimal")
        if self.expected_lvr < 0 or self.max_loss_if_zero < 0 or self.exit_depth < 0:
            raise ValueError("lvr, loss, and exit depth must be nonnegative")
        if not Decimal(0) <= self.expected_time_in_range <= Decimal(1):
            raise ValueError("expected_time_in_range must be between zero and one")


@dataclass(frozen=True, slots=True)
class MarginalValueInputs:
    """Nonnegative quote-currency terms for one candidate action over horizon H."""

    expected_incremental_fees: Decimal
    expected_incremental_lvr: Decimal
    inventory_risk_charge: Decimal
    swap_rebalance_gas_mev_cost: Decimal
    failure_and_model_uncertainty_buffer: Decimal

    def __post_init__(self) -> None:
        for name, value in (
            ("expected_incremental_fees", self.expected_incremental_fees),
            ("expected_incremental_lvr", self.expected_incremental_lvr),
            ("inventory_risk_charge", self.inventory_risk_charge),
            ("swap_rebalance_gas_mev_cost", self.swap_rebalance_gas_mev_cost),
            (
                "failure_and_model_uncertainty_buffer",
                self.failure_and_model_uncertainty_buffer,
            ),
        ):
            if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
                raise ValueError(f"{name} must be a finite nonnegative Decimal")


def expected_value(inputs: MarginalValueInputs) -> Decimal:
    """Expected incremental value before the model-uncertainty buffer."""
    return (
        inputs.expected_incremental_fees
        - inputs.expected_incremental_lvr
        - inputs.inventory_risk_charge
        - inputs.swap_rebalance_gas_mev_cost
    )


def marginal_value(inputs: MarginalValueInputs) -> Decimal:
    """Blueprint 6.3 formula including the failure/model-uncertainty buffer."""
    return expected_value(inputs) - inputs.failure_and_model_uncertainty_buffer


def lower_confidence_bound(inputs: MarginalValueInputs) -> Decimal:
    """v1 lower confidence bound: the model-uncertainty buffer is the allowance."""
    return marginal_value(inputs)


@dataclass(frozen=True, slots=True)
class RebalanceGateInput:
    """Every applicable trigger condition; all must hold for a proposal to pass."""

    price_left_buffer: bool
    dwell_met: bool
    marginal_value: Decimal
    hysteresis_threshold: Decimal
    volatility: Decimal
    max_volatility: Decimal
    toxicity: Decimal
    max_toxicity: Decimal
    reference_fresh: bool
    route_fresh: bool
    nonce_fresh: bool
    balances_fresh: bool
    approvals_fresh: bool
    gas_reserve_fresh: bool
    post_action_limits_ok: bool
    pin_simulated_ok: bool
    cooldown_met: bool
    daily_limit_ok: bool

    def __post_init__(self) -> None:
        for name, value in (
            ("marginal_value", self.marginal_value),
            ("hysteresis_threshold", self.hysteresis_threshold),
            ("volatility", self.volatility),
            ("max_volatility", self.max_volatility),
            ("toxicity", self.toxicity),
            ("max_toxicity", self.max_toxicity),
        ):
            if not isinstance(value, Decimal) or not value.is_finite():
                raise ValueError(f"{name} must be a finite Decimal")
        for name, value in (
            ("hysteresis_threshold", self.hysteresis_threshold),
            ("volatility", self.volatility),
            ("max_volatility", self.max_volatility),
            ("toxicity", self.toxicity),
            ("max_toxicity", self.max_toxicity),
        ):
            if value < 0:
                raise ValueError(f"{name} must be nonnegative")
        for name, bool_value in (
            ("price_left_buffer", self.price_left_buffer),
            ("dwell_met", self.dwell_met),
            ("reference_fresh", self.reference_fresh),
            ("route_fresh", self.route_fresh),
            ("nonce_fresh", self.nonce_fresh),
            ("balances_fresh", self.balances_fresh),
            ("approvals_fresh", self.approvals_fresh),
            ("gas_reserve_fresh", self.gas_reserve_fresh),
            ("post_action_limits_ok", self.post_action_limits_ok),
            ("pin_simulated_ok", self.pin_simulated_ok),
            ("cooldown_met", self.cooldown_met),
            ("daily_limit_ok", self.daily_limit_ok),
        ):
            if not isinstance(bool_value, bool):
                raise TypeError(f"{name} must be a bool")


@dataclass(frozen=True, slots=True)
class RebalanceDecision:
    allowed: bool
    reasons: tuple[str, ...]


def rebalance_gate(gate_input: RebalanceGateInput) -> RebalanceDecision:
    """Evaluate all blueprint 6.4 triggers; any unmet condition blocks the action."""
    reasons: list[str] = []
    if not gate_input.price_left_buffer:
        reasons.append("price has not left the inner buffer or target band")
    if not gate_input.dwell_met:
        reasons.append("minimum wall-clock dwell time not met")
    if gate_input.marginal_value <= gate_input.hysteresis_threshold:
        reasons.append("expected benefit does not exceed total cost and hysteresis")
    if gate_input.volatility > gate_input.max_volatility:
        reasons.append("volatility above the no-recenter circuit breaker")
    if gate_input.toxicity > gate_input.max_toxicity:
        reasons.append("toxicity above the no-recenter circuit breaker")
    if not gate_input.reference_fresh:
        reasons.append("reference price is stale")
    if not gate_input.route_fresh:
        reasons.append("route depth is stale")
    if not gate_input.nonce_fresh:
        reasons.append("nonce is stale")
    if not gate_input.balances_fresh:
        reasons.append("balances are stale")
    if not gate_input.approvals_fresh:
        reasons.append("approvals are stale")
    if not gate_input.gas_reserve_fresh:
        reasons.append("gas reserve is not fresh")
    if not gate_input.post_action_limits_ok:
        reasons.append("post-action position violates token/range/concentration/loss limits")
    if not gate_input.pin_simulated_ok:
        reasons.append("pinned simulation did not succeed")
    if not gate_input.cooldown_met:
        reasons.append("minimum cooldown not met")
    if not gate_input.daily_limit_ok:
        reasons.append("daily rebalance limit reached")
    if reasons:
        return RebalanceDecision(False, tuple(reasons))
    return RebalanceDecision(True, ())


@dataclass(frozen=True, slots=True)
class LpAction:
    action_type: LpActionType
    marginal_value: Decimal
    lower_confidence_bound: Decimal
    target_range: TickRange | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.action_type, LpActionType):
            raise TypeError("action_type must be an LpActionType")
        for name, value in (
            ("marginal_value", self.marginal_value),
            ("lower_confidence_bound", self.lower_confidence_bound),
        ):
            if not isinstance(value, Decimal) or not value.is_finite():
                raise ValueError(f"{name} must be a finite Decimal")
        if self.action_type is not LpActionType.HOLD and self.target_range is None:
            raise ValueError("non-hold actions require a target range")


def select_action(
    candidates: Mapping[LpActionType, tuple[MarginalValueInputs, TickRange]],
    min_benefit: Decimal,
) -> LpAction:
    """Choose the candidate with the best lower confidence bound above the minimum benefit.

    Every non-hold candidate must name the tick range it would result in, so an
    action is never proposed without a concrete target range. "Do nothing" is
    always a valid candidate and often the correct one: if no candidate clears
    the precommitted minimum benefit, HOLD is returned. This prevents repeated
    buy-high/sell-low recentering from being counted as operational success
    instead of realized LVR.
    """
    if not isinstance(min_benefit, Decimal) or not min_benefit.is_finite():
        raise ValueError("min_benefit must be a finite Decimal")
    hold = LpAction(LpActionType.HOLD, Decimal(0), Decimal(0))
    best = hold
    for action_type, (inputs, target_range) in candidates.items():
        mv = marginal_value(inputs)
        lcb = lower_confidence_bound(inputs)
        if lcb > best.lower_confidence_bound:
            best = LpAction(action_type, mv, lcb, target_range=target_range)
    if best.lower_confidence_bound > min_benefit and best.action_type is not LpActionType.HOLD:
        return best
    return hold


__all__ = [
    "LpAction",
    "LpActionType",
    "MarginalValueInputs",
    "RebalanceDecision",
    "RebalanceGateInput",
    "Tranche",
    "TranchePurpose",
    "expected_value",
    "lower_confidence_bound",
    "marginal_value",
    "rebalance_gate",
    "select_action",
]
