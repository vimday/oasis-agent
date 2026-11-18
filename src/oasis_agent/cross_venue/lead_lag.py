"""Exact integer USD-micros evaluator for non-atomic CEX-DEX lead-lag arbitrage."""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from .aleph_bridge import ShmBboQuote


class LeadLagDirection(StrEnum):
    DEX_BUY_CEX_SELL = "dex_buy_cex_sell"
    DEX_SELL_CEX_BUY = "dex_sell_cex_buy"


def _require_nonneg_int(value: object, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")
    return value


def _require_pos_int(value: object, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _ceil_bps(notional_usd_micros: int, bps: int) -> int:
    if bps == 0 or notional_usd_micros == 0:
        return 0
    return (notional_usd_micros * bps + 9_999) // 10_000


@dataclass(frozen=True, slots=True)
class LeadLagOpportunity:
    """Worst-case attributed evaluation of a single-hop Base DEX vs CEX BBO hedge."""

    instrument: str
    direction: LeadLagDirection
    qty_base: int
    notional_usd_micros: int
    gross_spread_usd_micros: int
    dex_fee_usd_micros: int
    cex_fee_usd_micros: int
    hedge_slippage_usd_micros: int
    gas_usd_micros: int
    latency_drift_usd_micros: int
    total_cost_usd_micros: int
    worst_case_net_usd_micros: int
    edge_bps: int
    approved: bool
    rejection_reasons: tuple[str, ...]
    execution_eligible: bool = False


def evaluate_cex_dex_lead_lag(
    *,
    instrument: str,
    direction: LeadLagDirection | str,
    qty_base: int,
    base_decimals: int,
    dex_quote_usd_micros: int,
    dex_executable_depth_base: int,
    dex_quote_age_ms: int,
    cex_bbo: ShmBboQuote | None,
    dex_pool_fee_bps: int,
    cex_taker_fee_bps: int,
    hedge_slippage_bps: int,
    base_l2_gas_usd_micros: int,
    base_l1_gas_usd_micros: int,
    latency_drift_bps: int,
    max_dex_age_ms: int = 1_000,
    max_cex_age_ns: int = 1_000_000_000,
    min_net_edge_bps: int = 1,
) -> LeadLagOpportunity:
    """Evaluate non-atomic single-hop DEX vs CEX BBO hedge in exact integer USD micros."""
    if not isinstance(instrument, str) or not instrument.strip():
        raise ValueError("instrument must be a nonempty string")
    eff_dir = direction if isinstance(direction, LeadLagDirection) else LeadLagDirection(direction)
    _require_pos_int(qty_base, "qty_base")
    _require_nonneg_int(base_decimals, "base_decimals")
    if base_decimals > 77:
        raise ValueError("base_decimals cannot exceed 77")
    _require_pos_int(dex_quote_usd_micros, "dex_quote_usd_micros")
    _require_nonneg_int(dex_executable_depth_base, "dex_executable_depth_base")
    _require_nonneg_int(dex_quote_age_ms, "dex_quote_age_ms")
    _require_nonneg_int(dex_pool_fee_bps, "dex_pool_fee_bps")
    _require_nonneg_int(cex_taker_fee_bps, "cex_taker_fee_bps")
    _require_nonneg_int(hedge_slippage_bps, "hedge_slippage_bps")
    _require_nonneg_int(base_l2_gas_usd_micros, "base_l2_gas_usd_micros")
    _require_nonneg_int(base_l1_gas_usd_micros, "base_l1_gas_usd_micros")
    _require_nonneg_int(latency_drift_bps, "latency_drift_bps")
    _require_nonneg_int(max_dex_age_ms, "max_dex_age_ms")
    _require_nonneg_int(max_cex_age_ns, "max_cex_age_ns")
    _require_pos_int(min_net_edge_bps, "min_net_edge_bps")

    reasons: list[str] = []
    if dex_quote_age_ms > max_dex_age_ms:
        reasons.append("dex_quote_stale")
    if qty_base > dex_executable_depth_base:
        reasons.append("insufficient_dex_depth")

    scale = 10**base_decimals
    gas_usd_micros = base_l2_gas_usd_micros + base_l1_gas_usd_micros

    if cex_bbo is None:
        reasons.append("cex_bbo_unavailable_or_stale")
        dex_notional = (dex_quote_usd_micros * qty_base + scale - 1) // scale
        dex_fee = _ceil_bps(dex_notional, dex_pool_fee_bps)
        return LeadLagOpportunity(
            instrument=instrument,
            direction=eff_dir,
            qty_base=qty_base,
            notional_usd_micros=dex_notional,
            gross_spread_usd_micros=0,
            dex_fee_usd_micros=dex_fee,
            cex_fee_usd_micros=0,
            hedge_slippage_usd_micros=0,
            gas_usd_micros=gas_usd_micros,
            latency_drift_usd_micros=0,
            total_cost_usd_micros=dex_fee + gas_usd_micros,
            worst_case_net_usd_micros=-(dex_fee + gas_usd_micros),
            edge_bps=-10_000,
            approved=False,
            rejection_reasons=tuple(reasons),
        )

    if cex_bbo.age_ns > max_cex_age_ns:
        reasons.append("cex_bbo_stale")

    qty_whole = Decimal(qty_base) / Decimal(scale)
    if eff_dir is LeadLagDirection.DEX_BUY_CEX_SELL:
        if qty_whole > cex_bbo.bid_size:
            reasons.append("insufficient_cex_bid_depth")
        dex_notional = (dex_quote_usd_micros * qty_base + scale - 1) // scale
        cex_notional = (cex_bbo.bid_usd_micros * qty_base) // scale
        gross_spread = cex_notional - dex_notional
    else:
        if qty_whole > cex_bbo.ask_size:
            reasons.append("insufficient_cex_ask_depth")
        dex_notional = (dex_quote_usd_micros * qty_base) // scale
        cex_notional = (cex_bbo.ask_usd_micros * qty_base + scale - 1) // scale
        gross_spread = dex_notional - cex_notional

    notional_usd_micros = max(1, dex_notional)
    dex_fee_usd_micros = _ceil_bps(dex_notional, dex_pool_fee_bps)
    cex_fee_usd_micros = _ceil_bps(cex_notional, cex_taker_fee_bps)
    hedge_slippage_usd_micros = _ceil_bps(cex_notional, hedge_slippage_bps)
    latency_drift_usd_micros = _ceil_bps(max(dex_notional, cex_notional), latency_drift_bps)
    total_cost_usd_micros = (
        dex_fee_usd_micros
        + cex_fee_usd_micros
        + hedge_slippage_usd_micros
        + gas_usd_micros
        + latency_drift_usd_micros
    )
    worst_case_net_usd_micros = gross_spread - total_cost_usd_micros
    edge_bps = (worst_case_net_usd_micros * 10_000) // notional_usd_micros

    if worst_case_net_usd_micros <= 0:
        reasons.append("nonpositive_worst_case_net_usd")
    elif edge_bps < min_net_edge_bps:
        reasons.append("below_min_net_edge_bps")

    return LeadLagOpportunity(
        instrument=instrument,
        direction=eff_dir,
        qty_base=qty_base,
        notional_usd_micros=notional_usd_micros,
        gross_spread_usd_micros=gross_spread,
        dex_fee_usd_micros=dex_fee_usd_micros,
        cex_fee_usd_micros=cex_fee_usd_micros,
        hedge_slippage_usd_micros=hedge_slippage_usd_micros,
        gas_usd_micros=gas_usd_micros,
        latency_drift_usd_micros=latency_drift_usd_micros,
        total_cost_usd_micros=total_cost_usd_micros,
        worst_case_net_usd_micros=worst_case_net_usd_micros,
        edge_bps=edge_bps,
        approved=len(reasons) == 0,
        rejection_reasons=tuple(reasons),
    )


__all__ = [
    "LeadLagDirection",
    "LeadLagOpportunity",
    "evaluate_cex_dex_lead_lag",
]
