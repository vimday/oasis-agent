"""Executable-quote parsing and a paper book for flip decisions (simulation only).

Entry needs a fresh USDC->token exact-input quote and a same-size token->USDC unwind
quote, so the round-trip loss is measured, not assumed. Held positions are marked only
by a fresh sell quote of the exact held quantity. Nothing here signs or broadcasts.
"""

from dataclasses import dataclass, replace
from typing import Any, Literal

from ..models.common import normalize_address
from .onchainos_source import BASE_USDC
from .opportunity import USD, DecisionPolicy, count, micros, pct_bps

ExitReason = Literal[
    "stop_loss", "take_profit", "trailing_stop", "time_stop", "risk_flag", "quote_lost"
]


@dataclass(frozen=True, slots=True)
class Quote:
    src: str
    dst: str
    amount_in: int
    amount_out: int
    impact_bps: int
    fee_usd_micros: int
    raw_sha256: str
    retrieved_at: int


def parse_quote(
    data: Any, *, chain: int, src: str, dst: str, amount: int, raw_sha256: str, at: int
) -> Quote:
    """Validate one exact-input quote; any identity, amount or metadata mismatch refuses."""
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
        raise ValueError("one quote row required")
    row = data[0]

    def token(side: str, expected: str) -> dict[str, Any]:
        meta = row.get(side)
        if (
            not isinstance(meta, dict)
            or normalize_address(str(meta.get("tokenContractAddress", ""))) != expected
        ):
            raise ValueError("quote token identity differs")
        if meta.get("isHoneyPot") is not False or pct_bps(meta.get("taxRate")) != 0:
            raise ValueError("quote token honeypot or taxed")
        return meta

    token("fromToken", normalize_address(src))
    token("toToken", normalize_address(dst))
    amount_in, amount_out = count(row.get("fromTokenAmount")), count(row.get("toTokenAmount"))
    impact, fee = pct_bps(row.get("priceImpactPercent")), micros(row.get("tradeFee"))
    if (
        row.get("chainIndex") != str(chain)
        or row.get("swapMode", "exactIn") != "exactIn"
        or not isinstance(row.get("dexRouterList"), list)
        or not row["dexRouterList"]
        or amount_in != amount
        or not amount_out
        or amount_out >= 2**256
        or impact is None
        or fee is None
    ):
        raise ValueError("quote chain, mode, route or amount refused")
    return Quote(src, dst, amount, amount_out, abs(impact), fee, raw_sha256, at)


def entry_cost(buy: Quote, policy: DecisionPolicy) -> int:
    """USDC micros a buy really costs: input, fee or declared gas, plus execution slippage.

    Quote-to-inclusion drift is charged as an extra cost rather than fewer units, so the
    held quantity always equals the quantity the unwind quote priced.
    """
    slip = -(-buy.amount_in * policy.execution_slippage_bps // 10_000)
    return buy.amount_in + max(buy.fee_usd_micros, policy.gas_per_leg_usd_micros) + slip


def round_trip_bps(buy: Quote, sell: Quote, policy: DecisionPolicy) -> int:
    """Loss of buying and unwinding immediately, after fees, gas and slippage, rounded up."""
    if buy.amount_out != sell.amount_in or buy.src != sell.dst or buy.dst != sell.src:
        raise ValueError("quotes are not one round trip")
    loss = entry_cost(buy, policy) - exit_value(sell, policy)
    return -(-loss * 10_000 // buy.amount_in)


def entry_size(cash: int, liquidity: int | None, policy: DecisionPolicy) -> int:
    """USDC micros to spend: a wallet fraction, never more than a small share of pool depth."""
    if liquidity is None:
        return 0
    return min(
        cash * policy.position_bps_of_wallet // 10_000,
        liquidity * policy.max_liquidity_share_bps // 10_000,
    )


def exit_value(sell: Quote, policy: DecisionPolicy) -> int:
    """Net USDC micros a sell now would realize after slippage, its fee and declared gas."""
    received = sell.amount_out * (10_000 - policy.execution_slippage_bps) // 10_000
    return max(0, received - max(sell.fee_usd_micros, policy.gas_per_leg_usd_micros))


@dataclass(frozen=True, slots=True)
class Position:
    token: str
    symbol: str
    units: int
    cost_usd_micros: int
    opened_at: int
    high_water_usd_micros: int
    last_value_usd_micros: int
    entry_quote_sha256: str
    missed_marks: int = 0


def check_exit(
    position: Position, value: int, now: int, policy: DecisionPolicy
) -> ExitReason | None:
    cost = position.cost_usd_micros
    if value * 10_000 <= cost * (10_000 - policy.stop_loss_bps):
        return "stop_loss"
    if value * 10_000 >= cost * (10_000 + policy.take_profit_bps):
        return "take_profit"
    high = max(position.high_water_usd_micros, value)
    if high > cost and value * 10_000 <= high * (10_000 - policy.trailing_bps):
        return "trailing_stop"
    if now - position.opened_at >= policy.max_hold_seconds:
        return "time_stop"
    return None


def mark(position: Position, value: int) -> Position:
    return replace(
        position,
        last_value_usd_micros=value,
        high_water_usd_micros=max(position.high_water_usd_micros, value),
        missed_marks=0,
    )


USDC_UNIT = USD  # Base USDC has 6 decimals, so USDC base units equal USD micros at par.
SETTLEMENT = BASE_USDC
