"""Shadow-only UniswapX V3DutchOrder filler evaluator with 500 ms SLA budget check."""

from dataclasses import dataclass, field

from ..errors import ExecutionBlockedError
from .dutch_v3 import BPS, DutchV3Order, ResolvedDutchAmounts, resolve_dutch_v3_amounts


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
    return (notional_usd_micros * bps + BPS - 1) // BPS


@dataclass(frozen=True, slots=True)
class IntentFillDecision:
    """Shadow evaluation of filling a Base V3DutchOrder against hedge and gas costs."""

    order_hash: str
    fill_block: int
    resolved: ResolvedDutchAmounts | None
    input_proceeds_usd_micros: int
    output_hedge_cost_usd_micros: int
    hedge_fee_usd_micros: int
    hedge_slippage_usd_micros: int
    reactor_callback_gas_usd_micros: int
    worst_case_net_usd_micros: int
    net_edge_bps: int
    saved_amm_fee_bps: int
    saved_amm_fee_usd_micros: int
    quote_latency_ms: int
    sla_budget_ms: int
    approved: bool
    rejection_reasons: tuple[str, ...]
    execution_eligible: bool = field(default=False, init=False)


def evaluate_dutch_v3_fill(
    *,
    order: DutchV3Order,
    fill_block: int,
    filler_address: str,
    input_token_decimals: int,
    input_hedge_bid_usd_micros: int,
    output_token_decimals: tuple[int, ...],
    output_hedge_ask_usd_micros: tuple[int, ...],
    hedge_fee_bps: int,
    hedge_slippage_bps: int,
    reactor_callback_gas_usd_micros: int,
    quote_latency_ms: int,
    amm_baseline_fee_bps: int = 5,
    sla_budget_ms: int = 500,
    min_net_edge_bps: int = 1,
) -> IntentFillDecision:
    """Evaluate shadow V3DutchOrder fill economics and 500 ms SLA budget."""
    if not isinstance(order, DutchV3Order):
        raise TypeError("order must be a DutchV3Order")
    _require_nonneg_int(input_token_decimals, "input_token_decimals")
    if input_token_decimals > 77:
        raise ValueError("input_token_decimals cannot exceed 77")
    _require_pos_int(input_hedge_bid_usd_micros, "input_hedge_bid_usd_micros")
    if len(output_token_decimals) != len(order.outputs) or len(output_hedge_ask_usd_micros) != len(
        order.outputs
    ):
        raise ValueError("output decimals and ask prices must match order.outputs length")
    for dec, ask_px in zip(output_token_decimals, output_hedge_ask_usd_micros, strict=True):
        _require_nonneg_int(dec, "output_token_decimals")
        if dec > 77:
            raise ValueError("output_token_decimals cannot exceed 77")
        _require_pos_int(ask_px, "output_hedge_ask_usd_micros")
    _require_nonneg_int(hedge_fee_bps, "hedge_fee_bps")
    _require_nonneg_int(hedge_slippage_bps, "hedge_slippage_bps")
    _require_nonneg_int(reactor_callback_gas_usd_micros, "reactor_callback_gas_usd_micros")
    _require_nonneg_int(quote_latency_ms, "quote_latency_ms")
    _require_nonneg_int(amm_baseline_fee_bps, "amm_baseline_fee_bps")
    _require_pos_int(sla_budget_ms, "sla_budget_ms")
    _require_pos_int(min_net_edge_bps, "min_net_edge_bps")

    reasons: list[str] = []
    if quote_latency_ms > sla_budget_ms:
        reasons.append("sla_budget_exceeded")

    try:
        resolved = resolve_dutch_v3_amounts(order, fill_block, filler_address)
    except ExecutionBlockedError as exc:
        reasons.append(str(exc))
        return IntentFillDecision(
            order_hash=order.order_hash,
            fill_block=fill_block,
            resolved=None,
            input_proceeds_usd_micros=0,
            output_hedge_cost_usd_micros=0,
            hedge_fee_usd_micros=0,
            hedge_slippage_usd_micros=0,
            reactor_callback_gas_usd_micros=reactor_callback_gas_usd_micros,
            worst_case_net_usd_micros=-reactor_callback_gas_usd_micros,
            net_edge_bps=-BPS,
            saved_amm_fee_bps=0,
            saved_amm_fee_usd_micros=0,
            quote_latency_ms=quote_latency_ms,
            sla_budget_ms=sla_budget_ms,
            approved=False,
            rejection_reasons=tuple(reasons),
        )

    input_scale = 10**input_token_decimals
    input_proceeds = (resolved.input_amount * input_hedge_bid_usd_micros) // input_scale

    output_cost = 0
    for out, dec, ask_px in zip(
        resolved.outputs, output_token_decimals, output_hedge_ask_usd_micros, strict=True
    ):
        scale = 10**dec
        output_cost += (out.amount * ask_px + scale - 1) // scale

    hedge_notional = max(input_proceeds, output_cost)
    hedge_fee_usd = _ceil_bps(hedge_notional, hedge_fee_bps)
    hedge_slippage_usd = _ceil_bps(hedge_notional, hedge_slippage_bps)
    worst_case_net = (
        input_proceeds
        - output_cost
        - hedge_fee_usd
        - hedge_slippage_usd
        - reactor_callback_gas_usd_micros
    )
    ref_notional = max(1, input_proceeds)
    net_edge_bps = (worst_case_net * BPS) // ref_notional
    saved_amm_fee_usd = (input_proceeds * amm_baseline_fee_bps) // BPS

    if worst_case_net <= 0:
        reasons.append("nonpositive_worst_case_net_usd")
    elif net_edge_bps < min_net_edge_bps:
        reasons.append("below_min_net_edge_bps")

    return IntentFillDecision(
        order_hash=order.order_hash,
        fill_block=fill_block,
        resolved=resolved,
        input_proceeds_usd_micros=input_proceeds,
        output_hedge_cost_usd_micros=output_cost,
        hedge_fee_usd_micros=hedge_fee_usd,
        hedge_slippage_usd_micros=hedge_slippage_usd,
        reactor_callback_gas_usd_micros=reactor_callback_gas_usd_micros,
        worst_case_net_usd_micros=worst_case_net,
        net_edge_bps=net_edge_bps,
        saved_amm_fee_bps=amm_baseline_fee_bps,
        saved_amm_fee_usd_micros=saved_amm_fee_usd,
        quote_latency_ms=quote_latency_ms,
        sla_budget_ms=sla_budget_ms,
        approved=len(reasons) == 0,
        rejection_reasons=tuple(reasons),
    )


__all__ = [
    "IntentFillDecision",
    "evaluate_dutch_v3_fill",
]
