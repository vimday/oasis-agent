"""Pure sell-trigger evaluation. No I/O; every quantity is integer micros.

Cost basis and current value are USD micros computed from the Chainlink
ETH/USD feed and exact-in sell quotes. Fractional-token sales use integer
division; the caller decides the exact base units.
"""

from dataclasses import dataclass

from .config import ExecutionConfig


@dataclass(frozen=True)
class ExitEvaluation:
    action: str  # "hold" | "sell_half" | "sell_all" | "time_stop"
    reason: str
    value_usd_micros: int
    cost_basis_usd_micros: int
    multiple: int  # current value / cost basis, integer floor


def evaluate_exit(
    *,
    cost_basis_usd_micros: int,
    value_usd_micros: int,
    started_at: int,
    now: int,
    config: ExecutionConfig,
    half_sold: bool,
) -> ExitEvaluation:
    if cost_basis_usd_micros <= 0 or value_usd_micros < 0:
        raise ValueError("positive cost basis and non-negative value required")
    if now < started_at:
        raise ValueError("clock regression in exit evaluation")
    multiple = value_usd_micros // cost_basis_usd_micros
    stop_loss_usd = cost_basis_usd_micros * (100 - config.stop_loss_value_pct) // 100
    if value_usd_micros <= stop_loss_usd:
        return ExitEvaluation(
            action="sell_all",
            reason=f"stop_loss_value_pct={config.stop_loss_value_pct}",
            value_usd_micros=value_usd_micros,
            cost_basis_usd_micros=cost_basis_usd_micros,
            multiple=multiple,
        )
    if not half_sold and multiple >= config.take_profit_multiple:
        return ExitEvaluation(
            action="sell_half",
            reason=f"take_profit_multiple={config.take_profit_multiple}",
            value_usd_micros=value_usd_micros,
            cost_basis_usd_micros=cost_basis_usd_micros,
            multiple=multiple,
        )
    if half_sold and multiple <= config.post_half_sell_floor_multiple:
        return ExitEvaluation(
            action="sell_all",
            reason=f"post_half_sell_floor_multiple={config.post_half_sell_floor_multiple}",
            value_usd_micros=value_usd_micros,
            cost_basis_usd_micros=cost_basis_usd_micros,
            multiple=multiple,
        )
    if now - started_at >= config.time_stop_seconds:
        return ExitEvaluation(
            action="time_stop",
            reason=f"time_stop_seconds={config.time_stop_seconds}",
            value_usd_micros=value_usd_micros,
            cost_basis_usd_micros=cost_basis_usd_micros,
            multiple=multiple,
        )
    return ExitEvaluation(
        action="hold",
        reason="no_exit_trigger",
        value_usd_micros=value_usd_micros,
        cost_basis_usd_micros=cost_basis_usd_micros,
        multiple=multiple,
    )
