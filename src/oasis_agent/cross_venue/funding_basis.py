"""Cross-market funding/basis evaluator with fail-closed RWA session and margin checks."""

from dataclasses import dataclass
from enum import StrEnum


class SessionStatus(StrEnum):
    REGULAR = "regular"
    EXTENDED = "extended"
    OVERNIGHT = "overnight"
    AUCTION = "auction"
    HALTED = "halted"
    CLOSED = "closed"
    STALE = "stale"


def _require_int(value: object, name: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{name} must be an integer")
    return value


def _require_nonneg_int(value: object, name: str) -> int:
    parsed = _require_int(value, name)
    if parsed < 0:
        raise ValueError(f"{name} must be nonnegative")
    return parsed


def _require_pos_int(value: object, name: str) -> int:
    parsed = _require_int(value, name)
    if parsed <= 0:
        raise ValueError(f"{name} must be positive")
    return parsed


@dataclass(frozen=True, slots=True)
class FundingBasisDecision:
    """Attributed funding/basis opportunity result in integer USD micros."""

    instrument: str
    session_status: SessionStatus
    gross_basis_usd_micros: int
    expected_funding_and_carry_usd_micros: int
    total_costs_and_reserves_usd_micros: int
    net_edge_usd_micros: int
    edge_bps: int
    liquidation_distance_bps: int
    approved: bool
    rejection_reasons: tuple[str, ...]
    execution_eligible: bool = False


def evaluate_funding_basis(
    *,
    instrument: str,
    proceeds_bid_s_usd_micros: int,
    cost_ask_b_usd_micros: int,
    expected_funding_usd_micros: int,
    fees_usd_micros: int,
    borrow_usd_micros: int,
    gas_usd_micros: int,
    hedge_slippage_usd_micros: int,
    gap_reserve_usd_micros: int,
    tail_charge_usd_micros: int,
    session_status: SessionStatus | str,
    liquidation_distance_bps: int,
    min_liquidation_distance_bps: int,
    fx_conversion_usd_micros: int = 0,
    oracle_staleness_ms: int = 0,
    max_oracle_staleness_ms: int = 5_000,
    corporate_action_pending: bool = False,
    min_net_edge_bps: int = 1,
) -> FundingBasisDecision:
    """Evaluate PERP_CROSS_MARKET_ARBITRAGE.md §2 net edge with fail-closed RWA guards."""
    if not isinstance(instrument, str) or not instrument.strip():
        raise ValueError("instrument must be a nonempty string")
    eff_session = (
        session_status
        if isinstance(session_status, SessionStatus)
        else SessionStatus(session_status)
    )
    _require_pos_int(proceeds_bid_s_usd_micros, "proceeds_bid_s_usd_micros")
    _require_pos_int(cost_ask_b_usd_micros, "cost_ask_b_usd_micros")
    _require_int(expected_funding_usd_micros, "expected_funding_usd_micros")
    _require_nonneg_int(fees_usd_micros, "fees_usd_micros")
    _require_nonneg_int(borrow_usd_micros, "borrow_usd_micros")
    _require_nonneg_int(gas_usd_micros, "gas_usd_micros")
    _require_nonneg_int(hedge_slippage_usd_micros, "hedge_slippage_usd_micros")
    _require_nonneg_int(gap_reserve_usd_micros, "gap_reserve_usd_micros")
    _require_nonneg_int(tail_charge_usd_micros, "tail_charge_usd_micros")
    _require_nonneg_int(fx_conversion_usd_micros, "fx_conversion_usd_micros")
    _require_nonneg_int(liquidation_distance_bps, "liquidation_distance_bps")
    _require_pos_int(min_liquidation_distance_bps, "min_liquidation_distance_bps")
    _require_nonneg_int(oracle_staleness_ms, "oracle_staleness_ms")
    _require_nonneg_int(max_oracle_staleness_ms, "max_oracle_staleness_ms")
    if type(corporate_action_pending) is not bool:
        raise TypeError("corporate_action_pending must be a bool")
    _require_pos_int(min_net_edge_bps, "min_net_edge_bps")

    reasons: list[str] = []
    if eff_session is not SessionStatus.REGULAR:
        reasons.append(f"session_not_regular:{eff_session.value}")
    if liquidation_distance_bps < min_liquidation_distance_bps:
        reasons.append("liquidation_distance_below_minimum")
    if oracle_staleness_ms > max_oracle_staleness_ms:
        reasons.append("oracle_stale")
    if corporate_action_pending:
        reasons.append("corporate_action_pending")

    gross_basis = proceeds_bid_s_usd_micros - cost_ask_b_usd_micros
    total_costs = (
        fees_usd_micros
        + borrow_usd_micros
        + gas_usd_micros
        + fx_conversion_usd_micros
        + hedge_slippage_usd_micros
        + gap_reserve_usd_micros
        + tail_charge_usd_micros
    )
    net_edge = gross_basis + expected_funding_usd_micros - total_costs
    reference_notional = max(proceeds_bid_s_usd_micros, cost_ask_b_usd_micros)
    edge_bps = (net_edge * 10_000) // reference_notional

    if net_edge <= 0:
        reasons.append("nonpositive_net_edge")
    elif edge_bps < min_net_edge_bps:
        reasons.append("below_min_net_edge_bps")

    return FundingBasisDecision(
        instrument=instrument,
        session_status=eff_session,
        gross_basis_usd_micros=gross_basis,
        expected_funding_and_carry_usd_micros=expected_funding_usd_micros,
        total_costs_and_reserves_usd_micros=total_costs,
        net_edge_usd_micros=net_edge,
        edge_bps=edge_bps,
        liquidation_distance_bps=liquidation_distance_bps,
        approved=len(reasons) == 0,
        rejection_reasons=tuple(reasons),
    )


__all__ = [
    "FundingBasisDecision",
    "SessionStatus",
    "evaluate_funding_basis",
]
