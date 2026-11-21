"""Passive CEX Post-Only Maker + Atomic Base DEX Taker Hedge shadow evaluator.

Models the two-leg cross-venue maker/hedge pattern (`AlephTX` Saga style) across
Coinbase Exchange and Base DEX: resting competitive Post-Only maker quotes inside
the CEX spread (capturing 0 bps VIP maker fees instead of paying 3.5 bps taker fees)
and hedging confirmed out-of-sample `t -> t+1` fills atomically on Base DEX.

All quotes, fills, and PnL summaries are strictly read-only shadow measurements
(`execution_eligible = False`).
"""

import math
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Final, Literal

from .coinbase_trade_flow import CoinbaseTradePrint
from .live_cex_dex_shadow import CoinbaseL2Book, resolve_coinbase_product

_BPS_DIVISOR: Final[Decimal] = Decimal(10_000)
_USDC_SCALE: Final[Decimal] = Decimal(1_000_000)
_POST_ONLY_NUDGE: Final[Decimal] = Decimal("0.0001")


class MakerHedgeError(ValueError):
    """Fail-closed validation error for passive maker-hedge shadow evaluation."""


def _require_finite_decimal(
    value: object,
    name: str,
    *,
    require_positive: bool = False,
    require_nonneg: bool = False,
) -> Decimal:
    if isinstance(value, bool):
        raise MakerHedgeError(f"{name} must not be a boolean")
    if isinstance(value, float):
        raise MakerHedgeError(f"{name} must be Decimal, int, or decimal string, not float")
    try:
        if isinstance(value, Decimal):
            dec = value
        elif isinstance(value, int):
            dec = Decimal(value)
        elif isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                raise MakerHedgeError(f"{name} must not be empty")
            dec = Decimal(stripped)
        else:
            raise MakerHedgeError(f"{name} has unsupported type {type(value).__name__}")
    except InvalidOperation as exc:
        raise MakerHedgeError(f"{name} is not a valid decimal number") from exc

    if not dec.is_finite():
        raise MakerHedgeError(f"{name} must be a finite decimal")
    if require_positive and dec <= 0:
        raise MakerHedgeError(f"{name} must be positive")
    if require_nonneg and dec < 0:
        raise MakerHedgeError(f"{name} must be non-negative")
    return dec


def _require_pos_int(value: object, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise MakerHedgeError(f"{name} must be a positive integer")
    return value


def _require_nonneg_int(value: object, name: str) -> int:
    if type(value) is not int or value < 0:
        raise MakerHedgeError(f"{name} must be a non-negative integer")
    return value


def _decimal_round_float(value: Decimal, *, places: int = 6) -> float:
    return round(float(value), places)


def _format_decimal_plain(value: Decimal) -> str:
    normalized = value.normalize()
    if normalized == 0:
        return "0"
    return f"{normalized:f}"


@dataclass(frozen=True, slots=True)
class MakerHedgeConfig:
    """Configuration for passive CEX Post-Only Maker + Base DEX Taker Hedge shadow."""

    trade_notional_usdc: int = 200_000_000
    cex_maker_fee_bps: Decimal = Decimal(0)
    adverse_selection_bps: Decimal = Decimal("1.5")
    min_quote_edge_bps: Decimal = Decimal("3.0")
    tick_improvement_bps: Decimal = Decimal("0.5")
    require_strict_cross_for_fill: bool = True
    execution_eligible: Literal[False] = False
    adaptive_volatility_enabled: bool = False
    volatility_multiplier: Decimal = Decimal("1.5")
    volatility_window_steps: int = 16
    max_adaptive_adverse_bps: Decimal = Decimal("60.0")
    min_hedge_notional_usdc: int = 0
    max_acceptable_gas_drag_bps: Decimal = Decimal("5.0")
    max_unhedged_steps: int = 8
    max_unhedged_drift_bps: Decimal = Decimal("15.0")
    preserve_queue_priority: bool = False
    deduplicate_fifo_and_strict: bool = False
    cex_dust_unwind_enabled: bool = False
    cex_dust_unwind_taker_fee_bps: Decimal = Decimal("3.5")
    post_fill_cooldown_steps: int = 0
    max_same_dir_momentum_bps: Decimal | None = None

    def __post_init__(self) -> None:
        _require_pos_int(self.trade_notional_usdc, "trade_notional_usdc")
        fee = _require_finite_decimal(
            self.cex_maker_fee_bps, "cex_maker_fee_bps", require_nonneg=True
        )
        adv = _require_finite_decimal(
            self.adverse_selection_bps, "adverse_selection_bps", require_nonneg=True
        )
        min_edge = _require_finite_decimal(
            self.min_quote_edge_bps, "min_quote_edge_bps", require_nonneg=True
        )
        improve = _require_finite_decimal(
            self.tick_improvement_bps, "tick_improvement_bps", require_nonneg=True
        )
        if improve >= _BPS_DIVISOR:
            raise MakerHedgeError("tick_improvement_bps must be less than 10000")
        if type(self.require_strict_cross_for_fill) is not bool:
            raise MakerHedgeError("require_strict_cross_for_fill must be a boolean")
        if self.execution_eligible is not False:
            raise MakerHedgeError("MakerHedgeConfig cannot set execution_eligible=True")
        if type(self.adaptive_volatility_enabled) is not bool:
            raise MakerHedgeError("adaptive_volatility_enabled must be a boolean")
        vol_mult = _require_finite_decimal(
            self.volatility_multiplier, "volatility_multiplier", require_nonneg=True
        )
        _require_pos_int(self.volatility_window_steps, "volatility_window_steps")
        max_adv = _require_finite_decimal(
            self.max_adaptive_adverse_bps, "max_adaptive_adverse_bps", require_nonneg=True
        )
        if self.adaptive_volatility_enabled and max_adv < adv:
            raise MakerHedgeError(
                "max_adaptive_adverse_bps must be >= adverse_selection_bps when enabled"
            )
        _require_nonneg_int(self.min_hedge_notional_usdc, "min_hedge_notional_usdc")
        max_gas_drag = _require_finite_decimal(
            self.max_acceptable_gas_drag_bps,
            "max_acceptable_gas_drag_bps",
            require_positive=True,
        )
        _require_pos_int(self.max_unhedged_steps, "max_unhedged_steps")
        max_drift = _require_finite_decimal(
            self.max_unhedged_drift_bps, "max_unhedged_drift_bps", require_nonneg=True
        )
        if type(self.preserve_queue_priority) is not bool:
            raise MakerHedgeError("preserve_queue_priority must be a boolean")
        if type(self.deduplicate_fifo_and_strict) is not bool:
            raise MakerHedgeError("deduplicate_fifo_and_strict must be a boolean")
        if type(self.cex_dust_unwind_enabled) is not bool:
            raise MakerHedgeError("cex_dust_unwind_enabled must be a boolean")
        dust_taker_fee = _require_finite_decimal(
            self.cex_dust_unwind_taker_fee_bps,
            "cex_dust_unwind_taker_fee_bps",
            require_nonneg=True,
        )
        _require_nonneg_int(self.post_fill_cooldown_steps, "post_fill_cooldown_steps")
        max_mom = (
            _require_finite_decimal(
                self.max_same_dir_momentum_bps,
                "max_same_dir_momentum_bps",
                require_positive=True,
            )
            if self.max_same_dir_momentum_bps is not None
            else None
        )
        object.__setattr__(self, "cex_maker_fee_bps", fee)
        object.__setattr__(self, "adverse_selection_bps", adv)
        object.__setattr__(self, "min_quote_edge_bps", min_edge)
        object.__setattr__(self, "tick_improvement_bps", improve)
        object.__setattr__(self, "volatility_multiplier", vol_mult)
        object.__setattr__(self, "max_adaptive_adverse_bps", max_adv)
        object.__setattr__(self, "max_acceptable_gas_drag_bps", max_gas_drag)
        object.__setattr__(self, "max_unhedged_drift_bps", max_drift)
        object.__setattr__(self, "cex_dust_unwind_taker_fee_bps", dust_taker_fee)
        object.__setattr__(self, "max_same_dir_momentum_bps", max_mom)


@dataclass(frozen=True, slots=True)
class MakerQuoteCandidate:
    """Computed Post-Only Ask/Bid maker quotes and net hedge edges for one tick."""

    product_id: str
    token_symbol: str
    qty_tokens_dec: Decimal
    quoted_ask_usd: Decimal | None
    quoted_bid_usd: Decimal | None
    ask_dex_breakeven_usd: Decimal | None
    bid_dex_breakeven_usd: Decimal | None
    ask_edge_bps: Decimal | None
    bid_edge_bps: Decimal | None
    ask_active: bool
    bid_active: bool
    spread_capture_bps: Decimal
    gas_cost_usd: Decimal
    execution_eligible: Literal[False] = False
    effective_adverse_bps: Decimal = Decimal("1.5")

    def __post_init__(self) -> None:
        if self.execution_eligible is not False:
            raise MakerHedgeError("MakerQuoteCandidate cannot set execution_eligible=True")
        eff_adv = _require_finite_decimal(
            self.effective_adverse_bps, "effective_adverse_bps", require_nonneg=True
        )
        object.__setattr__(self, "effective_adverse_bps", eff_adv)

    def to_dict(self) -> dict[str, Any]:
        """Serialize quote candidate to a JSON-compatible dictionary."""
        return {
            "product_id": self.product_id,
            "token_symbol": self.token_symbol,
            "qty_tokens_dec": str(self.qty_tokens_dec.normalize()),
            "quoted_ask_usd": (
                _decimal_round_float(self.quoted_ask_usd)
                if self.quoted_ask_usd is not None
                else None
            ),
            "quoted_bid_usd": (
                _decimal_round_float(self.quoted_bid_usd)
                if self.quoted_bid_usd is not None
                else None
            ),
            "ask_dex_breakeven_usd": (
                _decimal_round_float(self.ask_dex_breakeven_usd)
                if self.ask_dex_breakeven_usd is not None
                else None
            ),
            "bid_dex_breakeven_usd": (
                _decimal_round_float(self.bid_dex_breakeven_usd)
                if self.bid_dex_breakeven_usd is not None
                else None
            ),
            "ask_edge_bps": (
                _decimal_round_float(self.ask_edge_bps) if self.ask_edge_bps is not None else None
            ),
            "bid_edge_bps": (
                _decimal_round_float(self.bid_edge_bps) if self.bid_edge_bps is not None else None
            ),
            "ask_active": self.ask_active,
            "bid_active": self.bid_active,
            "spread_capture_bps": _decimal_round_float(self.spread_capture_bps),
            "gas_cost_usd": _decimal_round_float(self.gas_cost_usd),
            "effective_adverse_bps": str(self.effective_adverse_bps),
            "execution_eligible": self.execution_eligible,
        }


@dataclass(frozen=True, slots=True)
class MakerFillEvent:
    """Out-of-sample `t -> t+1` fill event hedged via DEX or natural opposite-side netting."""

    product_id: str
    side: Literal["ask_maker", "bid_maker"]
    fill_classification: Literal["strict_cross", "touch_lift", "trade_flow_fifo"]
    quoted_price_usd: Decimal
    next_book_price_usd: Decimal
    hedge_dex_price_usd: Decimal
    realized_net_bps: Decimal
    realized_net_profit_usd: Decimal
    latency_drift_bps: Decimal
    execution_eligible: Literal[False] = False
    filled_qty_tokens: str | None = None
    fill_ratio: float = 1.0
    matched_trade_ids: tuple[int, ...] = ()
    hedge_mode: Literal["immediate_dex", "batched_dex", "natural_netted", "cex_dust_unwind"] = (
        "immediate_dex"
    )
    hedge_note: str = ""

    def __post_init__(self) -> None:
        if self.side not in ("ask_maker", "bid_maker"):
            raise MakerHedgeError(f"invalid maker fill side: {self.side!r}")
        if self.fill_classification not in ("strict_cross", "touch_lift", "trade_flow_fifo"):
            raise MakerHedgeError(f"invalid fill_classification: {self.fill_classification!r}")
        if self.hedge_mode not in (
            "immediate_dex",
            "batched_dex",
            "natural_netted",
            "cex_dust_unwind",
        ):
            raise MakerHedgeError(f"invalid hedge_mode: {self.hedge_mode!r}")
        if not isinstance(self.hedge_note, str):
            raise MakerHedgeError("hedge_note must be a string")
        if self.execution_eligible is not False:
            raise MakerHedgeError("MakerFillEvent cannot set execution_eligible=True")
        _require_finite_decimal(self.quoted_price_usd, "quoted_price_usd", require_positive=True)
        _require_finite_decimal(
            self.next_book_price_usd, "next_book_price_usd", require_positive=True
        )
        _require_finite_decimal(
            self.hedge_dex_price_usd, "hedge_dex_price_usd", require_positive=True
        )
        _require_finite_decimal(self.realized_net_bps, "realized_net_bps")
        _require_finite_decimal(self.realized_net_profit_usd, "realized_net_profit_usd")
        _require_finite_decimal(self.latency_drift_bps, "latency_drift_bps")
        if self.filled_qty_tokens is not None:
            if not isinstance(self.filled_qty_tokens, str):
                raise MakerHedgeError("filled_qty_tokens must be a decimal string or None")
            _require_finite_decimal(
                self.filled_qty_tokens, "filled_qty_tokens", require_positive=True
            )
        if (
            isinstance(self.fill_ratio, bool)
            or not isinstance(self.fill_ratio, (int, float))
            or not math.isfinite(float(self.fill_ratio))
            or not (0.0 < float(self.fill_ratio) <= 1.0)
        ):
            raise MakerHedgeError("fill_ratio must be a finite float in (0.0, 1.0]")
        if type(self.fill_ratio) is int:
            object.__setattr__(self, "fill_ratio", float(self.fill_ratio))
        if not isinstance(self.matched_trade_ids, tuple) or any(
            type(tid) is not int or tid <= 0 for tid in self.matched_trade_ids
        ):
            raise MakerHedgeError("matched_trade_ids must be a tuple of positive integers")

    def to_dict(self) -> dict[str, Any]:
        """Serialize fill event to a JSON-compatible dictionary."""
        return {
            "product_id": self.product_id,
            "side": self.side,
            "fill_classification": self.fill_classification,
            "quoted_price_usd": _decimal_round_float(self.quoted_price_usd),
            "next_book_price_usd": _decimal_round_float(self.next_book_price_usd),
            "hedge_dex_price_usd": _decimal_round_float(self.hedge_dex_price_usd),
            "realized_net_bps": _decimal_round_float(self.realized_net_bps),
            "realized_net_profit_usd": _decimal_round_float(self.realized_net_profit_usd),
            "latency_drift_bps": _decimal_round_float(self.latency_drift_bps),
            "filled_qty_tokens": self.filled_qty_tokens,
            "fill_ratio": round(float(self.fill_ratio), 6),
            "matched_trade_ids": list(self.matched_trade_ids),
            "hedge_mode": self.hedge_mode,
            "hedge_note": self.hedge_note,
            "execution_eligible": self.execution_eligible,
        }


@dataclass(frozen=True, slots=True)
class UnhedgedMakerLot:
    """Open unhedged Coinbase maker inventory lot awaiting batch DEX hedge or natural net."""

    product_id: str
    side: Literal["ask_maker", "bid_maker"]
    notional_usdc: int
    notional_token: int
    fill_price_usdc: Decimal
    maker_fee_usd: Decimal
    adverse_cost_usd: Decimal
    fill_step_idx: int
    fill_mode: Literal["strict_cross", "touch_lift", "trade_flow_fifo"]
    adverse_bps: Decimal
    recorded_at_utc: str = ""
    token_decimals: int = 18
    next_book_price_usd: Decimal | None = None
    t0_dex_unit_px: Decimal | None = None
    quote_qty_tokens: Decimal | None = None
    matched_trade_ids: tuple[int, ...] = ()
    execution_eligible: Literal[False] = False

    def __post_init__(self) -> None:
        spec = resolve_coinbase_product(self.product_id)
        if self.product_id != spec.product_id:
            object.__setattr__(self, "product_id", spec.product_id)
        if self.side not in ("ask_maker", "bid_maker"):
            raise MakerHedgeError(f"invalid unhedged lot side: {self.side!r}")
        if self.fill_mode not in ("strict_cross", "touch_lift", "trade_flow_fifo"):
            raise MakerHedgeError(f"invalid unhedged lot fill_mode: {self.fill_mode!r}")
        _require_pos_int(self.notional_usdc, "notional_usdc")
        _require_pos_int(self.notional_token, "notional_token")
        fill_px = _require_finite_decimal(
            self.fill_price_usdc, "fill_price_usdc", require_positive=True
        )
        fee_usd = _require_finite_decimal(self.maker_fee_usd, "maker_fee_usd", require_nonneg=True)
        adv_usd = _require_finite_decimal(
            self.adverse_cost_usd, "adverse_cost_usd", require_nonneg=True
        )
        _require_nonneg_int(self.fill_step_idx, "fill_step_idx")
        adv_bps = _require_finite_decimal(self.adverse_bps, "adverse_bps", require_nonneg=True)
        if not isinstance(self.recorded_at_utc, str):
            raise MakerHedgeError("recorded_at_utc must be a string")
        if type(self.token_decimals) is not int or not 0 <= self.token_decimals <= 77:
            raise MakerHedgeError("token_decimals must be an integer in 0..77")
        if self.next_book_price_usd is not None:
            next_px = _require_finite_decimal(
                self.next_book_price_usd, "next_book_price_usd", require_positive=True
            )
            object.__setattr__(self, "next_book_price_usd", next_px)
        if self.t0_dex_unit_px is not None:
            t0_px = _require_finite_decimal(
                self.t0_dex_unit_px, "t0_dex_unit_px", require_positive=True
            )
            object.__setattr__(self, "t0_dex_unit_px", t0_px)
        if self.quote_qty_tokens is not None:
            q_qty = _require_finite_decimal(
                self.quote_qty_tokens, "quote_qty_tokens", require_positive=True
            )
            object.__setattr__(self, "quote_qty_tokens", q_qty)
        if not isinstance(self.matched_trade_ids, tuple) or any(
            type(tid) is not int or tid <= 0 for tid in self.matched_trade_ids
        ):
            raise MakerHedgeError("matched_trade_ids must be a tuple of positive integers")
        if self.execution_eligible is not False:
            raise MakerHedgeError("UnhedgedMakerLot cannot set execution_eligible=True")
        object.__setattr__(self, "fill_price_usdc", fill_px)
        object.__setattr__(self, "maker_fee_usd", fee_usd)
        object.__setattr__(self, "adverse_cost_usd", adv_usd)
        object.__setattr__(self, "adverse_bps", adv_bps)


@dataclass(frozen=True, slots=True)
class MakerStepResult:
    """Result of one out-of-sample `t -> t+1` lifecycle step for a single product."""

    product_id: str
    fills: tuple[MakerFillEvent, ...]
    strict_fills: tuple[MakerFillEvent, ...]
    touch_lift_fills: tuple[MakerFillEvent, ...]
    quote: MakerQuoteCandidate
    unhedged_fill_reasons: tuple[str, ...] = ()
    execution_eligible: Literal[False] = False
    fifo_fills: tuple[MakerFillEvent, ...] = ()

    def __post_init__(self) -> None:
        if self.execution_eligible is not False:
            raise MakerHedgeError("MakerStepResult cannot set execution_eligible=True")


@dataclass(frozen=True, slots=True)
class _RestingQuoteState:
    book_best_bid: Decimal
    book_best_ask: Decimal
    ask_price: Decimal | None
    ask_t_dex_unit_px: Decimal | None
    bid_price: Decimal | None
    bid_t_dex_unit_px: Decimal | None
    ask_qty_tokens: Decimal | None = None
    bid_qty_tokens: Decimal | None = None
    queue_ahead_ask: Decimal = Decimal(0)
    queue_ahead_bid: Decimal = Decimal(0)


@dataclass(frozen=True, slots=True)
class _QuoteComputation:
    candidate: MakerQuoteCandidate
    dex_buy_unit_px: Decimal | None
    dex_sell_unit_px: Decimal | None
    ask_qty_tokens: Decimal | None
    bid_qty_tokens: Decimal | None


def _compute_quote_and_unit_prices(
    *,
    product_or_symbol: str,
    current_book: CoinbaseL2Book,
    trade_notional_usdc: int,
    dex_buy_token_out: int | None,
    trade_notional_token: int,
    dex_sell_usdc_out: int | None,
    token_decimals: int,
    gas_cost_usd: Decimal,
    config: MakerHedgeConfig,
    effective_adverse_bps: Decimal | None = None,
) -> _QuoteComputation:
    spec = resolve_coinbase_product(product_or_symbol)
    if current_book.product_id != spec.product_id:
        raise MakerHedgeError(
            f"book product_id {current_book.product_id} does not match {spec.product_id}"
        )
    _require_pos_int(trade_notional_usdc, "trade_notional_usdc")
    _require_pos_int(trade_notional_token, "trade_notional_token")
    if type(token_decimals) is not int or not 0 <= token_decimals <= 77:
        raise MakerHedgeError("token_decimals must be an integer in 0..77")
    if dex_buy_token_out is not None and type(dex_buy_token_out) is not int:
        raise TypeError("dex_buy_token_out must be an int or None")
    if dex_sell_usdc_out is not None and type(dex_sell_usdc_out) is not int:
        raise TypeError("dex_sell_usdc_out must be an int or None")
    gas_usd = _require_finite_decimal(gas_cost_usd, "gas_cost_usd", require_nonneg=True)
    eff_adv_bps = (
        config.adverse_selection_bps
        if effective_adverse_bps is None
        else _require_finite_decimal(
            effective_adverse_bps, "effective_adverse_bps", require_nonneg=True
        )
    )

    token_scale = Decimal(10**token_decimals)
    improve = config.tick_improvement_bps / _BPS_DIVISOR
    spread_capture_bps = (
        (current_book.best_ask - current_book.best_bid) / current_book.mid_price
    ) * _BPS_DIVISOR

    ask_qty: Decimal | None = None
    quoted_ask: Decimal | None = None
    ask_breakeven: Decimal | None = None
    ask_edge_bps: Decimal | None = None
    ask_active = False
    dex_buy_unit_px: Decimal | None = None

    if dex_buy_token_out is not None and dex_buy_token_out > 0:
        ask_qty = Decimal(dex_buy_token_out) / token_scale
        dex_buy_cost_usd = Decimal(trade_notional_usdc) / _USDC_SCALE
        dex_buy_unit_px = dex_buy_cost_usd / ask_qty
        ask_gas_bps = (gas_usd / dex_buy_cost_usd) * _BPS_DIVISOR
        ask_breakeven = dex_buy_unit_px * (
            Decimal(1) + (config.cex_maker_fee_bps + eff_adv_bps + ask_gas_bps) / _BPS_DIVISOR
        )

        raw_ask = max(
            current_book.best_bid * (Decimal(1) + _POST_ONLY_NUDGE),
            current_book.best_ask * (Decimal(1) - improve),
        )
        quoted_ask = min(current_book.best_ask, raw_ask)
        if quoted_ask <= current_book.best_bid:
            quoted_ask = current_book.best_ask

        ask_edge_bps = (
            ((quoted_ask - dex_buy_unit_px) / dex_buy_unit_px) * _BPS_DIVISOR
            - config.cex_maker_fee_bps
            - eff_adv_bps
            - ask_gas_bps
        )
        ask_active = (
            quoted_ask > current_book.best_bid and ask_edge_bps >= config.min_quote_edge_bps
        )

    sell_qty = Decimal(trade_notional_token) / token_scale
    quoted_bid: Decimal | None = None
    bid_breakeven: Decimal | None = None
    bid_edge_bps: Decimal | None = None
    bid_active = False
    dex_sell_unit_px: Decimal | None = None

    if dex_sell_usdc_out is not None and dex_sell_usdc_out > 0:
        dex_sell_proceeds_usd = Decimal(dex_sell_usdc_out) / _USDC_SCALE
        dex_sell_unit_px = dex_sell_proceeds_usd / sell_qty

        raw_bid = min(
            current_book.best_ask * (Decimal(1) - _POST_ONLY_NUDGE),
            current_book.best_bid * (Decimal(1) + improve),
        )
        quoted_bid = max(current_book.best_bid, raw_bid)
        if quoted_bid >= current_book.best_ask:
            quoted_bid = current_book.best_bid

        cex_bid_notional = quoted_bid * sell_qty
        bid_gas_bps = (gas_usd / cex_bid_notional) * _BPS_DIVISOR
        bid_breakeven = (dex_sell_unit_px - (gas_usd / sell_qty)) / (
            Decimal(1) + (config.cex_maker_fee_bps + eff_adv_bps) / _BPS_DIVISOR
        )
        bid_edge_bps = (
            ((dex_sell_unit_px - quoted_bid) / quoted_bid) * _BPS_DIVISOR
            - config.cex_maker_fee_bps
            - eff_adv_bps
            - bid_gas_bps
        )
        bid_active = (
            quoted_bid < current_book.best_ask and bid_edge_bps >= config.min_quote_edge_bps
        )

    qty_tokens_dec = ask_qty if ask_qty is not None else sell_qty
    candidate = MakerQuoteCandidate(
        product_id=spec.product_id,
        token_symbol=spec.token_symbol,
        qty_tokens_dec=qty_tokens_dec,
        quoted_ask_usd=quoted_ask,
        quoted_bid_usd=quoted_bid,
        ask_dex_breakeven_usd=ask_breakeven,
        bid_dex_breakeven_usd=bid_breakeven,
        ask_edge_bps=ask_edge_bps,
        bid_edge_bps=bid_edge_bps,
        ask_active=ask_active,
        bid_active=bid_active,
        spread_capture_bps=spread_capture_bps,
        gas_cost_usd=gas_usd,
        effective_adverse_bps=eff_adv_bps,
    )
    return _QuoteComputation(
        candidate=candidate,
        dex_buy_unit_px=dex_buy_unit_px,
        dex_sell_unit_px=dex_sell_unit_px,
        ask_qty_tokens=ask_qty if candidate.ask_active else None,
        bid_qty_tokens=sell_qty if candidate.bid_active else None,
    )


def build_maker_quote_candidate(
    product_id: str,
    *,
    current_book: CoinbaseL2Book,
    trade_notional_usdc: int,
    dex_buy_token_out: int | None,
    trade_notional_token: int,
    dex_sell_usdc_out: int | None,
    token_decimals: int,
    gas_cost_usd: Decimal,
    config: MakerHedgeConfig | None = None,
    effective_adverse_bps: Decimal | None = None,
) -> MakerQuoteCandidate:
    """Compute competitive Post-Only Ask/Bid quotes and hedge breakevens for one tick."""
    eff_cfg = config if config is not None else MakerHedgeConfig()
    computed = _compute_quote_and_unit_prices(
        product_or_symbol=product_id,
        current_book=current_book,
        trade_notional_usdc=trade_notional_usdc,
        dex_buy_token_out=dex_buy_token_out,
        trade_notional_token=trade_notional_token,
        dex_sell_usdc_out=dex_sell_usdc_out,
        token_decimals=token_decimals,
        gas_cost_usd=gas_cost_usd,
        config=eff_cfg,
        effective_adverse_bps=effective_adverse_bps,
    )
    return computed.candidate


def _build_ask_maker_fill(
    *,
    product_id: str,
    classification: Literal["strict_cross", "touch_lift", "trade_flow_fifo"],
    quoted_ask: Decimal,
    next_book_px: Decimal,
    t0_dex_unit_px: Decimal,
    trade_notional_usdc: int,
    dex_buy_token_out: int,
    token_scale: Decimal,
    gas_usd: Decimal,
    config: MakerHedgeConfig,
    filled_qty: Decimal | None = None,
    fill_ratio: float = 1.0,
    matched_trade_ids: tuple[int, ...] = (),
    effective_adverse_bps: Decimal | None = None,
    override_adv_cost_usd: Decimal | None = None,
    override_maker_fee_usd: Decimal | None = None,
    hedge_mode: Literal["immediate_dex", "batched_dex", "natural_netted"] = "immediate_dex",
    hedge_note: str = "",
) -> MakerFillEvent:
    t1_dex_cost_full = Decimal(trade_notional_usdc) / _USDC_SCALE
    t1_ask_qty_full = Decimal(dex_buy_token_out) / token_scale
    hedge_dex_px = t1_dex_cost_full / t1_ask_qty_full
    active_qty = t1_ask_qty_full if filled_qty is None else filled_qty
    filled_dex_cost = (
        t1_dex_cost_full if active_qty == t1_ask_qty_full else active_qty * hedge_dex_px
    )
    latency_drift_bps = ((hedge_dex_px - t0_dex_unit_px) / t0_dex_unit_px) * _BPS_DIVISOR
    cex_proceeds = quoted_ask * active_qty
    cex_fee = (
        override_maker_fee_usd
        if override_maker_fee_usd is not None
        else cex_proceeds * (config.cex_maker_fee_bps / _BPS_DIVISOR)
    )
    adv_bps = (
        config.adverse_selection_bps if effective_adverse_bps is None else effective_adverse_bps
    )
    adv_cost = (
        override_adv_cost_usd
        if override_adv_cost_usd is not None
        else filled_dex_cost * (adv_bps / _BPS_DIVISOR)
    )
    realized_profit = cex_proceeds - cex_fee - filled_dex_cost - adv_cost - gas_usd
    realized_bps = (realized_profit / filled_dex_cost) * _BPS_DIVISOR
    return MakerFillEvent(
        product_id=product_id,
        side="ask_maker",
        fill_classification=classification,
        quoted_price_usd=quoted_ask,
        next_book_price_usd=next_book_px,
        hedge_dex_price_usd=hedge_dex_px,
        realized_net_bps=realized_bps,
        realized_net_profit_usd=realized_profit,
        latency_drift_bps=latency_drift_bps,
        filled_qty_tokens=_format_decimal_plain(active_qty) if filled_qty is not None else None,
        fill_ratio=fill_ratio,
        matched_trade_ids=matched_trade_ids,
        hedge_mode=hedge_mode,
        hedge_note=hedge_note,
    )


def _build_bid_maker_fill(
    *,
    product_id: str,
    classification: Literal["strict_cross", "touch_lift", "trade_flow_fifo"],
    quoted_bid: Decimal,
    next_book_px: Decimal,
    t0_dex_unit_px: Decimal,
    trade_notional_token: int,
    dex_sell_usdc_out: int,
    token_scale: Decimal,
    gas_usd: Decimal,
    config: MakerHedgeConfig,
    filled_qty: Decimal | None = None,
    fill_ratio: float = 1.0,
    matched_trade_ids: tuple[int, ...] = (),
    effective_adverse_bps: Decimal | None = None,
    override_adv_cost_usd: Decimal | None = None,
    override_maker_fee_usd: Decimal | None = None,
    hedge_mode: Literal["immediate_dex", "batched_dex", "natural_netted"] = "immediate_dex",
    hedge_note: str = "",
) -> MakerFillEvent:
    t1_sell_qty_full = Decimal(trade_notional_token) / token_scale
    t1_dex_proceeds_full = Decimal(dex_sell_usdc_out) / _USDC_SCALE
    hedge_dex_px = t1_dex_proceeds_full / t1_sell_qty_full
    active_qty = t1_sell_qty_full if filled_qty is None else filled_qty
    filled_dex_proceeds = (
        t1_dex_proceeds_full if active_qty == t1_sell_qty_full else active_qty * hedge_dex_px
    )
    latency_drift_bps = ((t0_dex_unit_px - hedge_dex_px) / t0_dex_unit_px) * _BPS_DIVISOR
    cex_cost = quoted_bid * active_qty
    cex_fee = (
        override_maker_fee_usd
        if override_maker_fee_usd is not None
        else cex_cost * (config.cex_maker_fee_bps / _BPS_DIVISOR)
    )
    adv_bps = (
        config.adverse_selection_bps if effective_adverse_bps is None else effective_adverse_bps
    )
    adv_cost = (
        override_adv_cost_usd
        if override_adv_cost_usd is not None
        else cex_cost * (adv_bps / _BPS_DIVISOR)
    )
    realized_profit = filled_dex_proceeds - cex_cost - cex_fee - adv_cost - gas_usd
    realized_bps = (realized_profit / cex_cost) * _BPS_DIVISOR
    return MakerFillEvent(
        product_id=product_id,
        side="bid_maker",
        fill_classification=classification,
        quoted_price_usd=quoted_bid,
        next_book_price_usd=next_book_px,
        hedge_dex_price_usd=hedge_dex_px,
        realized_net_bps=realized_bps,
        realized_net_profit_usd=realized_profit,
        latency_drift_bps=latency_drift_bps,
        filled_qty_tokens=_format_decimal_plain(active_qty) if filled_qty is not None else None,
        fill_ratio=fill_ratio,
        matched_trade_ids=matched_trade_ids,
        hedge_mode=hedge_mode,
        hedge_note=hedge_note,
    )


def _consume_fifo_queue_prints(
    *,
    prints: tuple[CoinbaseTradePrint, ...],
    quoted_price: Decimal,
    quote_qty: Decimal,
    initial_queue_ahead: Decimal,
    is_ask: bool,
) -> tuple[Decimal, Decimal, tuple[int, ...]]:
    """Simulate FIFO queue depletion across chronological prints for one resting side."""
    q_ahead = initial_queue_ahead
    rem_qty = quote_qty
    filled_qty = Decimal(0)
    weighted_px_sum = Decimal(0)
    matched_ids: list[int] = []
    join_maker_side = "sell" if is_ask else "buy"

    for tr in prints:
        if rem_qty <= 0:
            break
        swept = tr.price > quoted_price if is_ask else tr.price < quoted_price
        if swept:
            q_ahead = Decimal(0)
            take = min(rem_qty, tr.size)
        elif tr.price == quoted_price and tr.maker_side == join_maker_side:
            drained = min(q_ahead, tr.size)
            q_ahead -= drained
            rem_print = tr.size - drained
            if rem_print <= 0:
                continue
            take = min(rem_qty, rem_print)
        else:
            continue
        filled_qty += take
        rem_qty -= take
        weighted_px_sum += take * tr.price
        matched_ids.append(tr.trade_id)

    if filled_qty <= 0:
        return Decimal(0), Decimal(0), ()
    return filled_qty, weighted_px_sum / filled_qty, tuple(matched_ids)


def _drain_fifo_queue_ahead(
    *,
    prints: tuple[CoinbaseTradePrint, ...],
    quoted_price: Decimal,
    initial_queue_ahead: Decimal,
    is_ask: bool,
) -> Decimal:
    """Drain FIFO queue ahead at ``quoted_price`` across ``prints`` without resetting."""
    q_ahead = initial_queue_ahead
    if q_ahead <= 0 or not prints:
        return Decimal(0)
    join_maker_side = "sell" if is_ask else "buy"
    for tr in prints:
        swept = tr.price > quoted_price if is_ask else tr.price < quoted_price
        if swept:
            return Decimal(0)
        if tr.price == quoted_price and tr.maker_side == join_maker_side:
            q_ahead -= min(q_ahead, tr.size)
            if q_ahead <= 0:
                return Decimal(0)
    return q_ahead


@dataclass(frozen=True, slots=True)
class _DetectedFill:
    side: Literal["ask_maker", "bid_maker"]
    classification: Literal["strict_cross", "touch_lift", "trade_flow_fifo"]
    quoted_price_usd: Decimal
    next_book_price_usd: Decimal
    t0_dex_unit_px: Decimal
    filled_qty_dec: Decimal | None
    quote_qty_dec: Decimal | None
    fill_ratio: float
    matched_trade_ids: tuple[int, ...]
    recorded_at_utc: str


def _compute_dynamic_min_hedge_usdc(config: MakerHedgeConfig, gas_usd: Decimal) -> int:
    if gas_usd <= 0:
        return config.min_hedge_notional_usdc
    target_micros = (gas_usd * _BPS_DIVISOR * _USDC_SCALE) / config.max_acceptable_gas_drag_bps
    ceil_micros = int(target_micros.to_integral_value(rounding=ROUND_CEILING))
    return max(config.min_hedge_notional_usdc, ceil_micros)


def _lot_adverse_drift_bps(lot: UnhedgedMakerLot, current_mid: Decimal) -> Decimal:
    if lot.side == "ask_maker":
        return ((current_mid - lot.fill_price_usdc) / lot.fill_price_usdc) * _BPS_DIVISOR
    return ((lot.fill_price_usdc - current_mid) / lot.fill_price_usdc) * _BPS_DIVISOR


class MakerHedgeLifecycleTracker:
    """Stateful out-of-sample `t -> t+1` passive maker-quote lifecycle tracker."""

    def __init__(self, config: MakerHedgeConfig | None = None) -> None:
        self._config: MakerHedgeConfig = config if config is not None else MakerHedgeConfig()
        self._resting_by_product: dict[str, _RestingQuoteState] = {}
        self._total_steps: int = 0
        self._step_index_by_product: dict[str, int] = {}
        self._active_quote_legs: int = 0
        self._all_fills: list[MakerFillEvent] = []
        self._quoted_edges_bps: list[Decimal] = []
        self._unhedged_warnings: list[str] = []
        self._products_seen: set[str] = set()
        self._last_trade_id_by_product: dict[str, int] = {}
        self._feed_gap_cancels: int = 0
        self._open_unhedged_lots: dict[str, list[UnhedgedMakerLot]] = {}
        self._mid_returns_bps_by_product: dict[str, deque[Decimal]] = {}
        self._last_mid_by_product: dict[str, Decimal] = {}
        self._last_signed_return_bps_by_product: dict[str, Decimal] = {}
        self._cooldown_until_step_by_product_side: dict[tuple[str, str], int] = {}
        self._last_book_by_product: dict[str, CoinbaseL2Book] = {}
        self._batched_fifo_hedges: int = 0
        self._natural_netted_fills: int = 0
        self._cex_dust_unwinds: int = 0
        self._cooldown_quote_skips: int = 0
        self._momentum_quote_skips: int = 0

    @property
    def config(self) -> MakerHedgeConfig:
        return self._config

    def resting_quotes(self) -> dict[str, dict[str, str | None]]:
        """Return currently resting active quote prices per product."""
        return {
            product_id: {
                "ask_price_usd": (str(state.ask_price) if state.ask_price is not None else None),
                "bid_price_usd": (str(state.bid_price) if state.bid_price is not None else None),
            }
            for product_id, state in sorted(self._resting_by_product.items())
        }

    def open_lots(self) -> dict[str, tuple[UnhedgedMakerLot, ...]]:
        """Return immutable snapshot of currently open unhedged lots per product."""
        return {
            product_id: tuple(lots)
            for product_id, lots in sorted(self._open_unhedged_lots.items())
            if lots
        }

    def unhedged_exposure_usd_micros(self) -> int:
        """Return total gross open unhedged notional in 6-decimal USDC micros."""
        return sum(lot.notional_usdc for lots in self._open_unhedged_lots.values() for lot in lots)

    def unrealized_drawdown_usd_micros(self) -> int:
        """Return non-negative mark-to-market unrealized loss in USD micros vs latest mid."""
        total_loss_usd = Decimal(0)
        for product_id, lots in self._open_unhedged_lots.items():
            if not lots:
                continue
            latest_mid = self._last_mid_by_product.get(product_id)
            if latest_mid is None:
                continue
            product_mtm_usd = Decimal(0)
            for lot in lots:
                qty_dec = Decimal(lot.notional_token) / Decimal(10**lot.token_decimals)
                if lot.side == "ask_maker":
                    product_mtm_usd += (lot.fill_price_usdc - latest_mid) * qty_dec
                else:
                    product_mtm_usd += (latest_mid - lot.fill_price_usdc) * qty_dec
            if product_mtm_usd < 0:
                total_loss_usd += -product_mtm_usd
        if total_loss_usd <= 0:
            return 0
        return int((total_loss_usd * _USDC_SCALE).to_integral_value(rounding=ROUND_CEILING))

    def all_fills(self) -> tuple[MakerFillEvent, ...]:
        """Return all observed fills across strict, touch-lift, and FIFO trade-flow legs."""
        return tuple(self._all_fills)

    def strict_fills(self) -> tuple[MakerFillEvent, ...]:
        """Return only strictly crossed (`strict_cross`) fills."""
        return tuple(f for f in self._all_fills if f.fill_classification == "strict_cross")

    def fifo_fills(self) -> tuple[MakerFillEvent, ...]:
        """Return only trade-flow FIFO (`trade_flow_fifo`) fills."""
        return tuple(f for f in self._all_fills if f.fill_classification == "trade_flow_fifo")

    def peek_effective_adverse_bps(self, product_id: str, current_mid: Decimal) -> Decimal:
        """Preview effective adaptive adverse bps without mutating tracker state."""
        spec = resolve_coinbase_product(product_id)
        mid_dec = _require_finite_decimal(current_mid, "current_mid", require_positive=True)
        if not self._config.adaptive_volatility_enabled:
            return self._config.adverse_selection_bps
        prev_mid = self._last_mid_by_product.get(spec.product_id)
        existing = self._mid_returns_bps_by_product.get(spec.product_id)
        candidate_returns = (
            deque(existing, maxlen=self._config.volatility_window_steps)
            if existing is not None
            else deque(maxlen=self._config.volatility_window_steps)
        )
        step_ret_bps: Decimal | None = None
        if prev_mid is not None and prev_mid > 0:
            step_ret_bps = (abs(mid_dec - prev_mid) / prev_mid) * _BPS_DIVISOR
            candidate_returns.append(step_ret_bps)
        if not candidate_returns:
            return self._config.adverse_selection_bps
        rolling_vol_bps = sum(candidate_returns, Decimal(0)) / Decimal(len(candidate_returns))
        base_vol_bps = (
            max(rolling_vol_bps, step_ret_bps)
            if self._config.max_same_dir_momentum_bps is not None and step_ret_bps is not None
            else rolling_vol_bps
        )
        scaled = base_vol_bps * self._config.volatility_multiplier
        return min(
            self._config.max_adaptive_adverse_bps,
            max(self._config.adverse_selection_bps, scaled),
        )

    def _update_volatility_and_get_effective_adverse(
        self,
        product_id: str,
        mid_price: Decimal,
        *,
        record_return: bool,
    ) -> Decimal:
        prev_mid = self._last_mid_by_product.get(product_id)
        if record_return and prev_mid is not None and prev_mid > 0:
            signed_ret_bps = ((mid_price - prev_mid) / prev_mid) * _BPS_DIVISOR
            self._last_signed_return_bps_by_product[product_id] = signed_ret_bps
            step_ret_bps = abs(signed_ret_bps)
            returns = self._mid_returns_bps_by_product.get(product_id)
            if returns is None:
                returns = deque(maxlen=self._config.volatility_window_steps)
                self._mid_returns_bps_by_product[product_id] = returns
            returns.append(step_ret_bps)
        else:
            self._last_signed_return_bps_by_product[product_id] = Decimal(0)
        self._last_mid_by_product[product_id] = mid_price

        if not self._config.adaptive_volatility_enabled:
            return self._config.adverse_selection_bps
        returns = self._mid_returns_bps_by_product.get(product_id)
        if not returns:
            return self._config.adverse_selection_bps
        rolling_vol_bps = sum(returns, Decimal(0)) / Decimal(len(returns))
        base_vol_bps = (
            max(rolling_vol_bps, returns[-1])
            if self._config.max_same_dir_momentum_bps is not None
            else rolling_vol_bps
        )
        scaled = base_vol_bps * self._config.volatility_multiplier
        return min(
            self._config.max_adaptive_adverse_bps,
            max(self._config.adverse_selection_bps, scaled),
        )

    def _extract_new_product_trades(
        self,
        product_id: str,
        recent_trades: Sequence[CoinbaseTradePrint],
    ) -> tuple[CoinbaseTradePrint, ...]:
        if isinstance(recent_trades, (str, bytes)):
            raise MakerHedgeError("recent_trades must be a sequence of CoinbaseTradePrint")
        if not recent_trades:
            return ()

        watermark = self._last_trade_id_by_product.get(product_id, 0)
        by_trade_id: dict[int, CoinbaseTradePrint] = {}
        for idx, item in enumerate(recent_trades):
            if not isinstance(item, CoinbaseTradePrint):
                raise MakerHedgeError(
                    f"recent_trades[{idx}] must be CoinbaseTradePrint, not {type(item).__name__}"
                )
            if item.product_id != product_id or item.trade_id <= watermark:
                continue
            existing = by_trade_id.get(item.trade_id)
            if existing is not None:
                if existing != item:
                    raise MakerHedgeError(
                        f"conflicting trade print for {product_id} trade_id {item.trade_id}"
                    )
                continue
            by_trade_id[item.trade_id] = item

        if not by_trade_id:
            return ()
        ordered = tuple(sorted(by_trade_id.values(), key=lambda item: item.trade_id))
        self._last_trade_id_by_product[product_id] = ordered[-1].trade_id
        return ordered

    def _detect_fifo_fills(
        self,
        *,
        prev_state: _RestingQuoteState,
        product_prints: tuple[CoinbaseTradePrint, ...],
    ) -> list[_DetectedFill]:
        if not product_prints:
            return []
        detected: list[_DetectedFill] = []
        recorded_ts = product_prints[-1].traded_at_utc

        if (
            prev_state.ask_price is not None
            and prev_state.ask_qty_tokens is not None
            and prev_state.ask_t_dex_unit_px is not None
        ):
            filled_ask, ask_next_px, ask_matched_ids = _consume_fifo_queue_prints(
                prints=product_prints,
                quoted_price=prev_state.ask_price,
                quote_qty=prev_state.ask_qty_tokens,
                initial_queue_ahead=prev_state.queue_ahead_ask,
                is_ask=True,
            )
            if filled_ask > 0:
                ratio = min(1.0, float(filled_ask / prev_state.ask_qty_tokens))
                if ratio > 0.0:
                    detected.append(
                        _DetectedFill(
                            side="ask_maker",
                            classification="trade_flow_fifo",
                            quoted_price_usd=prev_state.ask_price,
                            next_book_price_usd=ask_next_px,
                            t0_dex_unit_px=prev_state.ask_t_dex_unit_px,
                            filled_qty_dec=filled_ask,
                            quote_qty_dec=prev_state.ask_qty_tokens,
                            fill_ratio=ratio,
                            matched_trade_ids=ask_matched_ids,
                            recorded_at_utc=recorded_ts,
                        )
                    )

        if (
            prev_state.bid_price is not None
            and prev_state.bid_qty_tokens is not None
            and prev_state.bid_t_dex_unit_px is not None
        ):
            filled_bid, bid_next_px, bid_matched_ids = _consume_fifo_queue_prints(
                prints=product_prints,
                quoted_price=prev_state.bid_price,
                quote_qty=prev_state.bid_qty_tokens,
                initial_queue_ahead=prev_state.queue_ahead_bid,
                is_ask=False,
            )
            if filled_bid > 0:
                ratio = min(1.0, float(filled_bid / prev_state.bid_qty_tokens))
                if ratio > 0.0:
                    detected.append(
                        _DetectedFill(
                            side="bid_maker",
                            classification="trade_flow_fifo",
                            quoted_price_usd=prev_state.bid_price,
                            next_book_price_usd=bid_next_px,
                            t0_dex_unit_px=prev_state.bid_t_dex_unit_px,
                            filled_qty_dec=filled_bid,
                            quote_qty_dec=prev_state.bid_qty_tokens,
                            fill_ratio=ratio,
                            matched_trade_ids=bid_matched_ids,
                            recorded_at_utc=recorded_ts,
                        )
                    )

        return detected

    def _detect_resting_fills(
        self,
        *,
        prev_state: _RestingQuoteState,
        current_book: CoinbaseL2Book,
    ) -> list[_DetectedFill]:
        detected: list[_DetectedFill] = []

        if prev_state.ask_price is not None and prev_state.ask_t_dex_unit_px is not None:
            is_strict = current_book.best_bid >= prev_state.ask_price
            is_touch = (
                not is_strict
                and prev_state.ask_price < current_book.best_ask
                and current_book.best_ask > prev_state.book_best_ask
            )
            if is_strict or is_touch:
                classification: Literal["strict_cross", "touch_lift"] = (
                    "strict_cross" if is_strict else "touch_lift"
                )
                next_book_px = current_book.best_bid if is_strict else current_book.best_ask
                detected.append(
                    _DetectedFill(
                        side="ask_maker",
                        classification=classification,
                        quoted_price_usd=prev_state.ask_price,
                        next_book_price_usd=next_book_px,
                        t0_dex_unit_px=prev_state.ask_t_dex_unit_px,
                        filled_qty_dec=None,
                        quote_qty_dec=prev_state.ask_qty_tokens,
                        fill_ratio=1.0,
                        matched_trade_ids=(),
                        recorded_at_utc=current_book.retrieved_at_utc,
                    )
                )

        if prev_state.bid_price is not None and prev_state.bid_t_dex_unit_px is not None:
            is_strict = current_book.best_ask <= prev_state.bid_price
            is_touch = (
                not is_strict
                and prev_state.bid_price > current_book.best_bid
                and current_book.best_bid < prev_state.book_best_bid
            )
            if is_strict or is_touch:
                classification = "strict_cross" if is_strict else "touch_lift"
                next_book_px = current_book.best_ask if is_strict else current_book.best_bid
                detected.append(
                    _DetectedFill(
                        side="bid_maker",
                        classification=classification,
                        quoted_price_usd=prev_state.bid_price,
                        next_book_price_usd=next_book_px,
                        t0_dex_unit_px=prev_state.bid_t_dex_unit_px,
                        filled_qty_dec=None,
                        quote_qty_dec=prev_state.bid_qty_tokens,
                        fill_ratio=1.0,
                        matched_trade_ids=(),
                        recorded_at_utc=current_book.retrieved_at_utc,
                    )
                )

        return detected

    def _hedge_detected_immediate(
        self,
        *,
        product_id: str,
        detected: _DetectedFill,
        trade_notional_usdc: int,
        dex_buy_token_out: int | None,
        trade_notional_token: int,
        dex_sell_usdc_out: int | None,
        token_scale: Decimal,
        gas_usd: Decimal,
        effective_adverse_bps: Decimal,
    ) -> tuple[MakerFillEvent | None, str | None]:
        if detected.side == "ask_maker":
            if dex_buy_token_out is None or dex_buy_token_out <= 0:
                if detected.classification == "trade_flow_fifo":
                    return (
                        None,
                        (
                            f"{product_id}:ask_maker:trade_flow_fifo:"
                            "fifo_ask_fill_missing_t1_dex_buy_hedge"
                        ),
                    )
                return (
                    None,
                    f"{product_id}:ask_maker:{detected.classification}:dex_buy_quote_unavailable",
                )
            return (
                _build_ask_maker_fill(
                    product_id=product_id,
                    classification=detected.classification,
                    quoted_ask=detected.quoted_price_usd,
                    next_book_px=detected.next_book_price_usd,
                    t0_dex_unit_px=detected.t0_dex_unit_px,
                    trade_notional_usdc=trade_notional_usdc,
                    dex_buy_token_out=dex_buy_token_out,
                    token_scale=token_scale,
                    gas_usd=gas_usd,
                    config=self._config,
                    filled_qty=detected.filled_qty_dec,
                    fill_ratio=detected.fill_ratio,
                    matched_trade_ids=detected.matched_trade_ids,
                    effective_adverse_bps=effective_adverse_bps,
                ),
                None,
            )

        if dex_sell_usdc_out is None or dex_sell_usdc_out <= 0:
            if detected.classification == "trade_flow_fifo":
                return (
                    None,
                    (
                        f"{product_id}:bid_maker:trade_flow_fifo:"
                        "fifo_bid_fill_missing_t1_dex_sell_hedge"
                    ),
                )
            return (
                None,
                f"{product_id}:bid_maker:{detected.classification}:dex_sell_quote_unavailable",
            )
        return (
            _build_bid_maker_fill(
                product_id=product_id,
                classification=detected.classification,
                quoted_bid=detected.quoted_price_usd,
                next_book_px=detected.next_book_price_usd,
                t0_dex_unit_px=detected.t0_dex_unit_px,
                trade_notional_token=trade_notional_token,
                dex_sell_usdc_out=dex_sell_usdc_out,
                token_scale=token_scale,
                gas_usd=gas_usd,
                config=self._config,
                filled_qty=detected.filled_qty_dec,
                fill_ratio=detected.fill_ratio,
                matched_trade_ids=detected.matched_trade_ids,
                effective_adverse_bps=effective_adverse_bps,
            ),
            None,
        )

    def _ingest_cex_fill_for_batching(
        self,
        *,
        product_id: str,
        detected: _DetectedFill,
        token_decimals: int,
        effective_adverse_bps: Decimal,
        fill_step_idx: int,
    ) -> list[MakerFillEvent]:
        fill_qty_dec = (
            detected.filled_qty_dec
            if detected.filled_qty_dec is not None
            else detected.quote_qty_dec
        )
        if fill_qty_dec is None or fill_qty_dec <= 0:
            return []
        quote_qty_dec = (
            detected.quote_qty_dec
            if detected.quote_qty_dec is not None and detected.quote_qty_dec > 0
            else fill_qty_dec
        )
        token_scale = Decimal(10**token_decimals)
        notional_token = int((fill_qty_dec * token_scale).to_integral_value(rounding=ROUND_HALF_UP))
        if notional_token <= 0:
            return []

        exact_qty_dec = Decimal(notional_token) / token_scale
        fill_usd = exact_qty_dec * detected.quoted_price_usd
        maker_fee_usd = fill_usd * (self._config.cex_maker_fee_bps / _BPS_DIVISOR)
        adverse_cost_usd = fill_usd * (effective_adverse_bps / _BPS_DIVISOR)

        opp_side: Literal["ask_maker", "bid_maker"] = (
            "bid_maker" if detected.side == "ask_maker" else "ask_maker"
        )
        existing_lots = self._open_unhedged_lots.get(product_id, [])
        updated_lots: list[UnhedgedMakerLot] = []

        rem_tokens = notional_token
        matched_tokens_total = 0
        matched_opp_usd = Decimal(0)
        matched_opp_fee_usd = Decimal(0)
        matched_lots_count = 0

        for lot in existing_lots:
            if lot.side == opp_side and rem_tokens > 0:
                take = min(rem_tokens, lot.notional_token)
                lot_scale = Decimal(10**lot.token_decimals)
                take_qty_dec = Decimal(take) / lot_scale
                matched_opp_usd += take_qty_dec * lot.fill_price_usdc
                if take == lot.notional_token:
                    matched_opp_fee_usd += lot.maker_fee_usd
                else:
                    ratio = Decimal(take) / Decimal(lot.notional_token)
                    lot_fee_take = lot.maker_fee_usd * ratio
                    lot_adv_take = lot.adverse_cost_usd * ratio
                    lot_usdc_take = (lot.notional_usdc * take) // lot.notional_token
                    rem_lot_tokens = lot.notional_token - take
                    rem_lot_usdc = max(1, lot.notional_usdc - lot_usdc_take)
                    updated_lots.append(
                        UnhedgedMakerLot(
                            product_id=lot.product_id,
                            side=lot.side,
                            notional_usdc=rem_lot_usdc,
                            notional_token=rem_lot_tokens,
                            fill_price_usdc=lot.fill_price_usdc,
                            maker_fee_usd=max(Decimal(0), lot.maker_fee_usd - lot_fee_take),
                            adverse_cost_usd=max(Decimal(0), lot.adverse_cost_usd - lot_adv_take),
                            fill_step_idx=lot.fill_step_idx,
                            fill_mode=lot.fill_mode,
                            adverse_bps=lot.adverse_bps,
                            recorded_at_utc=lot.recorded_at_utc,
                            token_decimals=lot.token_decimals,
                            next_book_price_usd=lot.next_book_price_usd,
                            t0_dex_unit_px=lot.t0_dex_unit_px,
                            quote_qty_tokens=lot.quote_qty_tokens,
                            matched_trade_ids=lot.matched_trade_ids,
                        )
                    )
                    matched_opp_fee_usd += lot_fee_take
                rem_tokens -= take
                matched_tokens_total += take
                matched_lots_count += 1
            else:
                updated_lots.append(lot)

        emitted: list[MakerFillEvent] = []
        if matched_tokens_total > 0:
            matched_qty_dec = Decimal(matched_tokens_total) / token_scale
            opp_weighted_px = matched_opp_usd / matched_qty_dec
            matched_share = Decimal(matched_tokens_total) / Decimal(notional_token)
            new_fee_share = maker_fee_usd * matched_share
            if detected.side == "ask_maker":
                sell_usd = matched_qty_dec * detected.quoted_price_usd
                buy_usd = matched_opp_usd
            else:
                sell_usd = matched_opp_usd
                buy_usd = matched_qty_dec * detected.quoted_price_usd
            realized_profit = sell_usd - buy_usd - new_fee_share - matched_opp_fee_usd
            realized_bps = (realized_profit / buy_usd) * _BPS_DIVISOR
            netted_ratio = max(1e-12, min(1.0, float(matched_qty_dec / quote_qty_dec)))
            emitted.append(
                MakerFillEvent(
                    product_id=product_id,
                    side=detected.side,
                    fill_classification=detected.classification,
                    quoted_price_usd=detected.quoted_price_usd,
                    next_book_price_usd=detected.next_book_price_usd,
                    hedge_dex_price_usd=opp_weighted_px,
                    realized_net_bps=realized_bps,
                    realized_net_profit_usd=realized_profit,
                    latency_drift_bps=Decimal(0),
                    filled_qty_tokens=_format_decimal_plain(matched_qty_dec),
                    fill_ratio=netted_ratio,
                    matched_trade_ids=detected.matched_trade_ids,
                    hedge_mode="natural_netted",
                    hedge_note=(
                        f"natural_netted_vs_{opp_side}:lots={matched_lots_count}:zero_dex_gas"
                    ),
                )
            )
            self._natural_netted_fills += 1

        if rem_tokens > 0:
            rem_qty_dec = Decimal(rem_tokens) / token_scale
            rem_ratio = Decimal(rem_tokens) / Decimal(notional_token)
            rem_usdc = max(
                1,
                int(
                    (rem_qty_dec * detected.quoted_price_usd * _USDC_SCALE).to_integral_value(
                        rounding=ROUND_HALF_UP
                    )
                ),
            )
            updated_lots.append(
                UnhedgedMakerLot(
                    product_id=product_id,
                    side=detected.side,
                    notional_usdc=rem_usdc,
                    notional_token=rem_tokens,
                    fill_price_usdc=detected.quoted_price_usd,
                    maker_fee_usd=maker_fee_usd * rem_ratio,
                    adverse_cost_usd=adverse_cost_usd * rem_ratio,
                    fill_step_idx=fill_step_idx,
                    fill_mode=detected.classification,
                    adverse_bps=effective_adverse_bps,
                    recorded_at_utc=detected.recorded_at_utc,
                    token_decimals=token_decimals,
                    next_book_price_usd=detected.next_book_price_usd,
                    t0_dex_unit_px=detected.t0_dex_unit_px,
                    quote_qty_tokens=quote_qty_dec,
                    matched_trade_ids=detected.matched_trade_ids,
                )
            )

        if updated_lots:
            self._open_unhedged_lots[product_id] = updated_lots
        else:
            self._open_unhedged_lots.pop(product_id, None)
        return emitted

    def _build_cex_dust_unwind_events(
        self,
        *,
        product_id: str,
        side: Literal["ask_maker", "bid_maker"],
        side_lots: Sequence[UnhedgedMakerLot],
        current_book: CoinbaseL2Book,
        note_prefix: str,
    ) -> list[MakerFillEvent]:
        unwind_px = current_book.best_ask if side == "ask_maker" else current_book.best_bid
        taker_fee_frac = self._config.cex_dust_unwind_taker_fee_bps / _BPS_DIVISOR
        unwind_note = f"{note_prefix}:cex_dust_unwind:lots={len(side_lots)}:zero_dex_gas"
        events: list[MakerFillEvent] = []
        for lot in side_lots:
            lot_scale = Decimal(10**lot.token_decimals)
            lot_qty_dec = Decimal(lot.notional_token) / lot_scale
            quote_qty = lot.quote_qty_tokens if lot.quote_qty_tokens is not None else lot_qty_dec
            lot_ratio = max(1e-12, min(1.0, float(lot_qty_dec / quote_qty)))
            next_book_px = (
                lot.next_book_price_usd
                if lot.next_book_price_usd is not None
                else lot.fill_price_usdc
            )
            if side == "ask_maker":
                proceeds_usd = lot_qty_dec * lot.fill_price_usdc
                unwind_cost_usd = lot_qty_dec * unwind_px
                taker_fee_usd = unwind_cost_usd * taker_fee_frac
                net_profit_usd = proceeds_usd - unwind_cost_usd - lot.maker_fee_usd - taker_fee_usd
                denom_usd = unwind_cost_usd
            else:
                buy_cost_usd = lot_qty_dec * lot.fill_price_usdc
                unwind_proceeds_usd = lot_qty_dec * unwind_px
                taker_fee_usd = unwind_proceeds_usd * taker_fee_frac
                net_profit_usd = (
                    unwind_proceeds_usd - buy_cost_usd - lot.maker_fee_usd - taker_fee_usd
                )
                denom_usd = buy_cost_usd
            net_bps = (net_profit_usd / denom_usd) * _BPS_DIVISOR if denom_usd > 0 else Decimal(0)
            events.append(
                MakerFillEvent(
                    product_id=product_id,
                    side=side,
                    fill_classification=lot.fill_mode,
                    quoted_price_usd=lot.fill_price_usdc,
                    next_book_price_usd=next_book_px,
                    hedge_dex_price_usd=unwind_px,
                    realized_net_bps=net_bps,
                    realized_net_profit_usd=net_profit_usd,
                    latency_drift_bps=Decimal(0),
                    filled_qty_tokens=_format_decimal_plain(lot_qty_dec),
                    fill_ratio=lot_ratio,
                    matched_trade_ids=lot.matched_trade_ids,
                    hedge_mode="cex_dust_unwind",
                    hedge_note=unwind_note,
                )
            )
        return events

    def _flush_product_side_lots(
        self,
        *,
        product_id: str,
        side: Literal["ask_maker", "bid_maker"],
        trade_notional_usdc: int,
        dex_buy_token_out: int | None,
        trade_notional_token: int,
        dex_sell_usdc_out: int | None,
        token_decimals: int,
        gas_usd: Decimal,
        note_prefix: str,
        allow_cex_dust_unwind: bool = False,
        current_book: CoinbaseL2Book | None = None,
    ) -> tuple[list[MakerFillEvent], list[str]]:
        product_lots = self._open_unhedged_lots.get(product_id, [])
        side_lots = [lot for lot in product_lots if lot.side == side]
        if not side_lots:
            return [], []

        cex_unwind_events: list[MakerFillEvent] | None = None
        if allow_cex_dust_unwind and current_book is not None:
            cex_unwind_events = self._build_cex_dust_unwind_events(
                product_id=product_id,
                side=side,
                side_lots=side_lots,
                current_book=current_book,
                note_prefix=note_prefix,
            )

        token_scale = Decimal(10**token_decimals)
        dex_quote_ok = (
            (dex_buy_token_out is not None and dex_buy_token_out > 0)
            if side == "ask_maker"
            else (dex_sell_usdc_out is not None and dex_sell_usdc_out > 0)
        )
        if not dex_quote_ok:
            if cex_unwind_events is not None:
                remaining = [lot for lot in product_lots if lot.side != side]
                if remaining:
                    self._open_unhedged_lots[product_id] = remaining
                else:
                    self._open_unhedged_lots.pop(product_id, None)
                self._cex_dust_unwinds += 1
                return cex_unwind_events, []
            return [], [f"{product_id}:{side}:{note_prefix}_missing_dex_quote"]

        if side == "ask_maker":
            if dex_buy_token_out is None or dex_buy_token_out <= 0:
                return [], [f"{product_id}:{side}:{note_prefix}_missing_dex_quote"]
            hedge_px_fallback = (Decimal(trade_notional_usdc) / _USDC_SCALE) / (
                Decimal(dex_buy_token_out) / token_scale
            )
        else:
            if dex_sell_usdc_out is None or dex_sell_usdc_out <= 0:
                return [], [f"{product_id}:{side}:{note_prefix}_missing_dex_quote"]
            hedge_px_fallback = (Decimal(dex_sell_usdc_out) / _USDC_SCALE) / (
                Decimal(trade_notional_token) / token_scale
            )

        batch_qty_dec = sum(
            (Decimal(lot.notional_token) / Decimal(10**lot.token_decimals) for lot in side_lots),
            Decimal(0),
        )
        allocated_gas = Decimal(0)
        emitted: list[MakerFillEvent] = []
        batch_note = f"{note_prefix}:batch_lots={len(side_lots)}"

        for idx, lot in enumerate(side_lots):
            lot_scale = Decimal(10**lot.token_decimals)
            lot_qty_dec = Decimal(lot.notional_token) / lot_scale
            if idx == len(side_lots) - 1:
                lot_gas = max(Decimal(0), gas_usd - allocated_gas)
            else:
                lot_gas = min(
                    max(Decimal(0), gas_usd - allocated_gas),
                    gas_usd * (lot_qty_dec / batch_qty_dec),
                )
                allocated_gas += lot_gas

            next_book_px = (
                lot.next_book_price_usd
                if lot.next_book_price_usd is not None
                else lot.fill_price_usdc
            )
            t0_px = lot.t0_dex_unit_px if lot.t0_dex_unit_px is not None else hedge_px_fallback
            quote_qty = lot.quote_qty_tokens if lot.quote_qty_tokens is not None else lot_qty_dec
            lot_ratio = max(1e-12, min(1.0, float(lot_qty_dec / quote_qty)))

            if side == "ask_maker":
                if dex_buy_token_out is None:
                    continue
                ev = _build_ask_maker_fill(
                    product_id=product_id,
                    classification=lot.fill_mode,
                    quoted_ask=lot.fill_price_usdc,
                    next_book_px=next_book_px,
                    t0_dex_unit_px=t0_px,
                    trade_notional_usdc=trade_notional_usdc,
                    dex_buy_token_out=dex_buy_token_out,
                    token_scale=lot_scale,
                    gas_usd=lot_gas,
                    config=self._config,
                    filled_qty=lot_qty_dec,
                    fill_ratio=lot_ratio,
                    matched_trade_ids=lot.matched_trade_ids,
                    effective_adverse_bps=lot.adverse_bps,
                    override_adv_cost_usd=lot.adverse_cost_usd,
                    override_maker_fee_usd=lot.maker_fee_usd,
                    hedge_mode="batched_dex",
                    hedge_note=batch_note,
                )
            else:
                if dex_sell_usdc_out is None:
                    continue
                ev = _build_bid_maker_fill(
                    product_id=product_id,
                    classification=lot.fill_mode,
                    quoted_bid=lot.fill_price_usdc,
                    next_book_px=next_book_px,
                    t0_dex_unit_px=t0_px,
                    trade_notional_token=trade_notional_token,
                    dex_sell_usdc_out=dex_sell_usdc_out,
                    token_scale=lot_scale,
                    gas_usd=lot_gas,
                    config=self._config,
                    filled_qty=lot_qty_dec,
                    fill_ratio=lot_ratio,
                    matched_trade_ids=lot.matched_trade_ids,
                    effective_adverse_bps=lot.adverse_bps,
                    override_adv_cost_usd=lot.adverse_cost_usd,
                    override_maker_fee_usd=lot.maker_fee_usd,
                    hedge_mode="batched_dex",
                    hedge_note=batch_note,
                )
            emitted.append(ev)

        remaining = [lot for lot in product_lots if lot.side != side]
        if remaining:
            self._open_unhedged_lots[product_id] = remaining
        else:
            self._open_unhedged_lots.pop(product_id, None)
        if cex_unwind_events is not None:
            dex_total_profit = sum((ev.realized_net_profit_usd for ev in emitted), Decimal(0))
            cex_total_profit = sum(
                (ev.realized_net_profit_usd for ev in cex_unwind_events), Decimal(0)
            )
            if cex_total_profit > dex_total_profit:
                self._cex_dust_unwinds += 1
                return cex_unwind_events, []
        self._batched_fifo_hedges += 1
        return emitted, []

    def _check_and_flush_triggered_lots(
        self,
        *,
        product_id: str,
        current_mid: Decimal,
        current_step_idx: int,
        trade_notional_usdc: int,
        dex_buy_token_out: int | None,
        trade_notional_token: int,
        dex_sell_usdc_out: int | None,
        token_decimals: int,
        gas_usd: Decimal,
        current_book: CoinbaseL2Book | None = None,
    ) -> tuple[list[MakerFillEvent], list[str]]:
        product_lots = self._open_unhedged_lots.get(product_id)
        if not product_lots:
            return [], []

        dynamic_min_hedge_usdc = _compute_dynamic_min_hedge_usdc(self._config, gas_usd)
        flushed: list[MakerFillEvent] = []
        warnings: list[str] = []

        for side in ("ask_maker", "bid_maker"):
            side_lots = [
                lot for lot in self._open_unhedged_lots.get(product_id, []) if lot.side == side
            ]
            if not side_lots:
                continue
            total_notional_usdc = sum(lot.notional_usdc for lot in side_lots)
            oldest_age = max(current_step_idx - lot.fill_step_idx for lot in side_lots)
            max_drift = max(_lot_adverse_drift_bps(lot, current_mid) for lot in side_lots)

            trigger_reason: str | None = None
            if total_notional_usdc >= dynamic_min_hedge_usdc:
                trigger_reason = "batch_notional_flush"
            elif oldest_age >= self._config.max_unhedged_steps:
                trigger_reason = "batch_age_flush"
            elif max_drift >= self._config.max_unhedged_drift_bps:
                trigger_reason = "batch_drift_stoploss_flush"

            if trigger_reason is None:
                continue
            allow_cex_unwind = (
                self._config.cex_dust_unwind_enabled
                and total_notional_usdc < dynamic_min_hedge_usdc
            )
            side_fills, side_warnings = self._flush_product_side_lots(
                product_id=product_id,
                side=side,
                trade_notional_usdc=trade_notional_usdc,
                dex_buy_token_out=dex_buy_token_out,
                trade_notional_token=trade_notional_token,
                dex_sell_usdc_out=dex_sell_usdc_out,
                token_decimals=token_decimals,
                gas_usd=gas_usd,
                note_prefix=trigger_reason,
                allow_cex_dust_unwind=allow_cex_unwind,
                current_book=current_book,
            )
            flushed.extend(side_fills)
            warnings.extend(side_warnings)

        return flushed, warnings

    def flush_all_unhedged(
        self,
        latest_dex_quotes_by_product: Mapping[
            str, tuple[int, int | None, int, int | None, int, Decimal]
        ],
        recorded_at_utc: str = "",
    ) -> tuple[MakerFillEvent, ...]:
        """Force-settle all remaining open unhedged lots at campaign end or shutdown.

        Each entry in ``latest_dex_quotes_by_product`` maps a Coinbase product ID (or alias) to
        ``(trade_notional_usdc, dex_buy_token_out, trade_notional_token, dex_sell_usdc_out,
        token_decimals, gas_cost_usd)``. For each ``(product_id, side)`` with open lots and a
        valid DEX quote for that side, all open lots are settled via one batched DEX hedge
        (charging ``gas_cost_usd`` once per side batch) and emitted as realized ``MakerFillEvent``
        entries. If a product or side lacks a valid DEX quote, an explicit unhedged warning is
        recorded and the unhedgeable lots are retained as explicit open residuals without crashing.
        """
        if not isinstance(recorded_at_utc, str):
            raise MakerHedgeError("recorded_at_utc must be a string")
        if not isinstance(latest_dex_quotes_by_product, Mapping):
            raise MakerHedgeError("latest_dex_quotes_by_product must be a mapping")

        validated_quotes: dict[str, tuple[int, int | None, int, int | None, int, Decimal]] = {}
        for raw_pid, quote_row in latest_dex_quotes_by_product.items():
            spec = resolve_coinbase_product(raw_pid)
            if spec.product_id in validated_quotes:
                raise MakerHedgeError(
                    f"duplicate product entry in latest_dex_quotes_by_product: {spec.product_id}"
                )
            if not isinstance(quote_row, tuple) or len(quote_row) != 6:
                raise MakerHedgeError(f"quote tuple for {spec.product_id} must have 6 elements")
            t_usdc, buy_out, t_tok, sell_out, tok_dec, gas_raw = quote_row
            _require_pos_int(t_usdc, "trade_notional_usdc")
            _require_pos_int(t_tok, "trade_notional_token")
            if buy_out is not None and type(buy_out) is not int:
                raise TypeError("dex_buy_token_out must be an int or None")
            if sell_out is not None and type(sell_out) is not int:
                raise TypeError("dex_sell_usdc_out must be an int or None")
            if type(tok_dec) is not int or not 0 <= tok_dec <= 77:
                raise MakerHedgeError("token_decimals must be an integer in 0..77")
            gas_dec = _require_finite_decimal(gas_raw, "gas_cost_usd", require_nonneg=True)
            validated_quotes[spec.product_id] = (t_usdc, buy_out, t_tok, sell_out, tok_dec, gas_dec)

        flushed_fills: list[MakerFillEvent] = []
        note_prefix = f"forced_flush:{recorded_at_utc}" if recorded_at_utc else "forced_flush"
        for pid in sorted(self._open_unhedged_lots.keys()):
            quote_entry = validated_quotes.get(pid)
            for side in ("ask_maker", "bid_maker"):
                side_lots = [
                    lot for lot in self._open_unhedged_lots.get(pid, []) if lot.side == side
                ]
                if not side_lots:
                    continue
                if quote_entry is None:
                    self._unhedged_warnings.append(f"{pid}:{side}:{note_prefix}_missing_dex_quote")
                    continue
                t_usdc, buy_out, t_tok, sell_out, tok_dec, gas_dec = quote_entry
                dyn_min = _compute_dynamic_min_hedge_usdc(self._config, gas_dec)
                side_notional_usdc = sum(lot.notional_usdc for lot in side_lots)
                allow_cex_unwind = (
                    self._config.cex_dust_unwind_enabled and side_notional_usdc < dyn_min
                )
                side_fills, side_warnings = self._flush_product_side_lots(
                    product_id=pid,
                    side=side,
                    trade_notional_usdc=t_usdc,
                    dex_buy_token_out=buy_out,
                    trade_notional_token=t_tok,
                    dex_sell_usdc_out=sell_out,
                    token_decimals=tok_dec,
                    gas_usd=gas_dec,
                    note_prefix=note_prefix,
                    allow_cex_dust_unwind=allow_cex_unwind,
                    current_book=self._last_book_by_product.get(pid),
                )
                flushed_fills.extend(side_fills)
                self._unhedged_warnings.extend(side_warnings)

        self._all_fills.extend(flushed_fills)
        return tuple(flushed_fills)

    def step(
        self,
        product_id: str,
        *,
        current_book: CoinbaseL2Book,
        trade_notional_usdc: int,
        dex_buy_token_out: int | None,
        trade_notional_token: int,
        dex_sell_usdc_out: int | None,
        token_decimals: int,
        gas_cost_usd: Decimal,
        recent_trades: Sequence[CoinbaseTradePrint] = (),
        feed_gap_detected: bool = False,
    ) -> MakerStepResult:
        """Evaluate prior tick's resting quotes on `current_book` & trades, then refresh quotes."""
        if type(feed_gap_detected) is not bool:
            raise MakerHedgeError("feed_gap_detected must be a boolean")
        spec = resolve_coinbase_product(product_id)
        if current_book.product_id != spec.product_id:
            raise MakerHedgeError(
                f"book product_id {current_book.product_id} does not match {spec.product_id}"
            )

        current_step_idx = self._step_index_by_product.get(spec.product_id, 0)
        self._last_book_by_product[spec.product_id] = current_book
        eff_adv_bps = self._update_volatility_and_get_effective_adverse(
            spec.product_id,
            current_book.mid_price,
            record_return=not feed_gap_detected,
        )
        computed = _compute_quote_and_unit_prices(
            product_or_symbol=spec.product_id,
            current_book=current_book,
            trade_notional_usdc=trade_notional_usdc,
            dex_buy_token_out=dex_buy_token_out,
            trade_notional_token=trade_notional_token,
            dex_sell_usdc_out=dex_sell_usdc_out,
            token_decimals=token_decimals,
            gas_cost_usd=gas_cost_usd,
            config=self._config,
            effective_adverse_bps=eff_adv_bps,
        )
        candidate = computed.candidate
        new_prints = self._extract_new_product_trades(spec.product_id, recent_trades)

        fills: list[MakerFillEvent] = []
        unhedged: list[str] = []
        prev_state = self._resting_by_product.get(spec.product_id)
        token_scale = Decimal(10**token_decimals)

        if feed_gap_detected:
            self._resting_by_product.pop(spec.product_id, None)
            if prev_state is not None and (
                prev_state.ask_price is not None or prev_state.bid_price is not None
            ):
                self._feed_gap_cancels += 1
                unhedged.append(f"{spec.product_id}:feed_gap_cancelled_prior_quote")
        else:
            detected_fills: list[_DetectedFill] = []
            if prev_state is not None:
                fifo_detected = (
                    self._detect_fifo_fills(prev_state=prev_state, product_prints=new_prints)
                    if new_prints
                    else []
                )
                resting_detected = self._detect_resting_fills(
                    prev_state=prev_state, current_book=current_book
                )
                if self._config.deduplicate_fifo_and_strict and fifo_detected:
                    fifo_sides = {fill.side for fill in fifo_detected}
                    resting_detected = [
                        fill for fill in resting_detected if fill.side not in fifo_sides
                    ]
                detected_fills.extend(fifo_detected)
                detected_fills.extend(resting_detected)

                for detected in detected_fills:
                    if self._config.post_fill_cooldown_steps > 0:
                        self._cooldown_until_step_by_product_side[
                            (spec.product_id, detected.side)
                        ] = current_step_idx + self._config.post_fill_cooldown_steps + 1
                    if (
                        self._config.min_hedge_notional_usdc <= 0
                        or detected.classification == "touch_lift"
                    ):
                        ev, warn = self._hedge_detected_immediate(
                            product_id=spec.product_id,
                            detected=detected,
                            trade_notional_usdc=trade_notional_usdc,
                            dex_buy_token_out=dex_buy_token_out,
                            trade_notional_token=trade_notional_token,
                            dex_sell_usdc_out=dex_sell_usdc_out,
                            token_scale=token_scale,
                            gas_usd=candidate.gas_cost_usd,
                            effective_adverse_bps=eff_adv_bps,
                        )
                        if ev is not None:
                            fills.append(ev)
                        if warn is not None:
                            unhedged.append(warn)
                    else:
                        fills.extend(
                            self._ingest_cex_fill_for_batching(
                                product_id=spec.product_id,
                                detected=detected,
                                token_decimals=token_decimals,
                                effective_adverse_bps=eff_adv_bps,
                                fill_step_idx=current_step_idx,
                            )
                        )

            signed_ret_bps = self._last_signed_return_bps_by_product.get(
                spec.product_id, Decimal(0)
            )
            ask_cooldown = current_step_idx < self._cooldown_until_step_by_product_side.get(
                (spec.product_id, "ask_maker"), 0
            )
            bid_cooldown = current_step_idx < self._cooldown_until_step_by_product_side.get(
                (spec.product_id, "bid_maker"), 0
            )
            ask_momentum_toxic = (
                self._config.max_same_dir_momentum_bps is not None
                and signed_ret_bps > self._config.max_same_dir_momentum_bps
            )
            bid_momentum_toxic = (
                self._config.max_same_dir_momentum_bps is not None
                and signed_ret_bps < -self._config.max_same_dir_momentum_bps
            )
            new_ask_active = candidate.ask_active
            if candidate.ask_active and (ask_cooldown or ask_momentum_toxic):
                new_ask_active = False
                if ask_cooldown:
                    self._cooldown_quote_skips += 1
                else:
                    self._momentum_quote_skips += 1
            new_bid_active = candidate.bid_active
            if candidate.bid_active and (bid_cooldown or bid_momentum_toxic):
                new_bid_active = False
                if bid_cooldown:
                    self._cooldown_quote_skips += 1
                else:
                    self._momentum_quote_skips += 1
            if new_ask_active != candidate.ask_active or new_bid_active != candidate.bid_active:
                candidate = replace(candidate, ask_active=new_ask_active, bid_active=new_bid_active)

            filled_sides = {detected.side for detected in detected_fills}
            queue_ahead_ask = Decimal(0)
            if (
                candidate.ask_active
                and candidate.quoted_ask_usd is not None
                and candidate.quoted_ask_usd >= current_book.best_ask
            ):
                if (
                    self._config.preserve_queue_priority
                    and prev_state is not None
                    and prev_state.ask_price == candidate.quoted_ask_usd
                    and "ask_maker" not in filled_sides
                ):
                    drained_ask = _drain_fifo_queue_ahead(
                        prints=new_prints,
                        quoted_price=prev_state.ask_price,
                        initial_queue_ahead=prev_state.queue_ahead_ask,
                        is_ask=True,
                    )
                    queue_ahead_ask = min(drained_ask, current_book.asks[0].size)
                else:
                    queue_ahead_ask = current_book.asks[0].size

            queue_ahead_bid = Decimal(0)
            if (
                candidate.bid_active
                and candidate.quoted_bid_usd is not None
                and candidate.quoted_bid_usd <= current_book.best_bid
            ):
                if (
                    self._config.preserve_queue_priority
                    and prev_state is not None
                    and prev_state.bid_price == candidate.quoted_bid_usd
                    and "bid_maker" not in filled_sides
                ):
                    drained_bid = _drain_fifo_queue_ahead(
                        prints=new_prints,
                        quoted_price=prev_state.bid_price,
                        initial_queue_ahead=prev_state.queue_ahead_bid,
                        is_ask=False,
                    )
                    queue_ahead_bid = min(drained_bid, current_book.bids[0].size)
                else:
                    queue_ahead_bid = current_book.bids[0].size

            self._resting_by_product[spec.product_id] = _RestingQuoteState(
                book_best_bid=current_book.best_bid,
                book_best_ask=current_book.best_ask,
                ask_price=candidate.quoted_ask_usd if candidate.ask_active else None,
                ask_t_dex_unit_px=computed.dex_buy_unit_px if candidate.ask_active else None,
                bid_price=candidate.quoted_bid_usd if candidate.bid_active else None,
                bid_t_dex_unit_px=computed.dex_sell_unit_px if candidate.bid_active else None,
                ask_qty_tokens=computed.ask_qty_tokens if candidate.ask_active else None,
                bid_qty_tokens=computed.bid_qty_tokens if candidate.bid_active else None,
                queue_ahead_ask=queue_ahead_ask,
                queue_ahead_bid=queue_ahead_bid,
            )

        if self._config.min_hedge_notional_usdc > 0:
            batch_fills, batch_warnings = self._check_and_flush_triggered_lots(
                product_id=spec.product_id,
                current_mid=current_book.mid_price,
                current_step_idx=current_step_idx,
                trade_notional_usdc=trade_notional_usdc,
                dex_buy_token_out=dex_buy_token_out,
                trade_notional_token=trade_notional_token,
                dex_sell_usdc_out=dex_sell_usdc_out,
                token_decimals=token_decimals,
                gas_usd=candidate.gas_cost_usd,
                current_book=current_book,
            )
            fills.extend(batch_fills)
            unhedged.extend(batch_warnings)

        self._step_index_by_product[spec.product_id] = current_step_idx + 1
        self._total_steps += 1
        self._products_seen.add(spec.product_id)
        if not feed_gap_detected:
            self._active_quote_legs += int(candidate.ask_active) + int(candidate.bid_active)
            if candidate.ask_edge_bps is not None:
                self._quoted_edges_bps.append(candidate.ask_edge_bps)
            if candidate.bid_edge_bps is not None:
                self._quoted_edges_bps.append(candidate.bid_edge_bps)
        self._all_fills.extend(fills)
        self._unhedged_warnings.extend(unhedged)

        strict_fills = tuple(f for f in fills if f.fill_classification == "strict_cross")
        touch_fills = tuple(f for f in fills if f.fill_classification == "touch_lift")
        fifo_fills = tuple(f for f in fills if f.fill_classification == "trade_flow_fifo")
        return MakerStepResult(
            product_id=spec.product_id,
            fills=tuple(fills),
            strict_fills=strict_fills,
            touch_lift_fills=touch_fills,
            quote=candidate,
            unhedged_fill_reasons=tuple(unhedged),
            fifo_fills=fifo_fills,
        )

    def summary(self) -> dict[str, Any]:
        """Return conservative strict-cross, FIFO trade-flow, and diagnostic shadow metrics."""
        strict = [f for f in self._all_fills if f.fill_classification == "strict_cross"]
        touch = [f for f in self._all_fills if f.fill_classification == "touch_lift"]
        fifo = [f for f in self._all_fills if f.fill_classification == "trade_flow_fifo"]

        strict_mean_bps = (
            _decimal_round_float(
                sum((f.realized_net_bps for f in strict), Decimal(0)) / Decimal(len(strict))
            )
            if strict
            else None
        )
        strict_total_profit = _decimal_round_float(
            sum((f.realized_net_profit_usd for f in strict), Decimal(0))
        )
        strict_positive = sum(1 for f in strict if f.realized_net_profit_usd > 0)

        fifo_mean_bps = (
            _decimal_round_float(
                sum((f.realized_net_bps for f in fifo), Decimal(0)) / Decimal(len(fifo))
            )
            if fifo
            else None
        )
        fifo_total_profit = _decimal_round_float(
            sum((f.realized_net_profit_usd for f in fifo), Decimal(0))
        )
        fifo_positive = sum(1 for f in fifo if f.realized_net_profit_usd > 0)
        fifo_filled_notional = _decimal_round_float(
            sum(
                (
                    f.quoted_price_usd * Decimal(f.filled_qty_tokens)
                    for f in fifo
                    if f.filled_qty_tokens is not None
                ),
                Decimal(0),
            )
        )

        best_quoted_edge = (
            _decimal_round_float(max(self._quoted_edges_bps)) if self._quoted_edges_bps else None
        )
        open_lot_count = sum(len(lots) for lots in self._open_unhedged_lots.values())
        return {
            "total_steps": self._total_steps,
            "active_quote_legs": self._active_quote_legs,
            "strict_cross_fills": len(strict),
            "touch_lift_fills": len(touch),
            "strict_mean_realized_net_bps": strict_mean_bps,
            "strict_total_net_profit_usd": strict_total_profit,
            "strict_positive_fills": strict_positive,
            "trade_flow_fifo_fills": len(fifo),
            "fifo_positive_fills": fifo_positive,
            "fifo_mean_realized_net_bps": fifo_mean_bps,
            "fifo_total_net_profit_usd": fifo_total_profit,
            "fifo_total_filled_notional_usd": fifo_filled_notional,
            "feed_gap_cancels": self._feed_gap_cancels,
            "best_quoted_edge_bps": best_quoted_edge,
            "unhedged_fill_warnings": len(self._unhedged_warnings),
            "batched_fifo_hedges": self._batched_fifo_hedges,
            "natural_netted_fills": self._natural_netted_fills,
            "cex_dust_unwinds": self._cex_dust_unwinds,
            "cooldown_quote_skips": self._cooldown_quote_skips,
            "momentum_quote_skips": self._momentum_quote_skips,
            "open_unhedged_lots": open_lot_count,
            "unhedged_exposure_usd_micros": self.unhedged_exposure_usd_micros(),
            "adaptive_volatility_enabled": self._config.adaptive_volatility_enabled,
            "products_tracked": sorted(self._products_seen),
            "execution_eligible": False,
        }


__all__ = [
    "MakerFillEvent",
    "MakerHedgeConfig",
    "MakerHedgeError",
    "MakerHedgeLifecycleTracker",
    "MakerQuoteCandidate",
    "MakerStepResult",
    "UnhedgedMakerLot",
    "build_maker_quote_candidate",
]
