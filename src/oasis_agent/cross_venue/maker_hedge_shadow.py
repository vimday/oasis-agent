"""Passive CEX Post-Only Maker + Atomic Base DEX Taker Hedge shadow evaluator.

Models the two-leg cross-venue maker/hedge pattern (`AlephTX` Saga style) across
Coinbase Exchange and Base DEX: resting competitive Post-Only maker quotes inside
the CEX spread (capturing 0 bps VIP maker fees instead of paying 3.5 bps taker fees)
and hedging confirmed out-of-sample `t -> t+1` fills atomically on Base DEX.

All quotes, fills, and PnL summaries are strictly read-only shadow measurements
(`execution_eligible = False`).
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
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
        object.__setattr__(self, "cex_maker_fee_bps", fee)
        object.__setattr__(self, "adverse_selection_bps", adv)
        object.__setattr__(self, "min_quote_edge_bps", min_edge)
        object.__setattr__(self, "tick_improvement_bps", improve)


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

    def __post_init__(self) -> None:
        if self.execution_eligible is not False:
            raise MakerHedgeError("MakerQuoteCandidate cannot set execution_eligible=True")

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
            "execution_eligible": self.execution_eligible,
        }


@dataclass(frozen=True, slots=True)
class MakerFillEvent:
    """Out-of-sample `t -> t+1` fill event hedged against tick `t+1` DEX liquidity."""

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

    def __post_init__(self) -> None:
        if self.side not in ("ask_maker", "bid_maker"):
            raise MakerHedgeError(f"invalid maker fill side: {self.side!r}")
        if self.fill_classification not in ("strict_cross", "touch_lift", "trade_flow_fifo"):
            raise MakerHedgeError(f"invalid fill_classification: {self.fill_classification!r}")
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
            "execution_eligible": self.execution_eligible,
        }


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
            Decimal(1)
            + (config.cex_maker_fee_bps + config.adverse_selection_bps + ask_gas_bps) / _BPS_DIVISOR
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
            - config.adverse_selection_bps
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
            Decimal(1) + (config.cex_maker_fee_bps + config.adverse_selection_bps) / _BPS_DIVISOR
        )
        bid_edge_bps = (
            ((dex_sell_unit_px - quoted_bid) / quoted_bid) * _BPS_DIVISOR
            - config.cex_maker_fee_bps
            - config.adverse_selection_bps
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
    cex_fee = cex_proceeds * (config.cex_maker_fee_bps / _BPS_DIVISOR)
    adv_cost = filled_dex_cost * (config.adverse_selection_bps / _BPS_DIVISOR)
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
    cex_fee = cex_cost * (config.cex_maker_fee_bps / _BPS_DIVISOR)
    adv_cost = cex_cost * (config.adverse_selection_bps / _BPS_DIVISOR)
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


class MakerHedgeLifecycleTracker:
    """Stateful out-of-sample `t -> t+1` passive maker-quote lifecycle tracker."""

    def __init__(self, config: MakerHedgeConfig | None = None) -> None:
        self._config: MakerHedgeConfig = config if config is not None else MakerHedgeConfig()
        self._resting_by_product: dict[str, _RestingQuoteState] = {}
        self._total_steps: int = 0
        self._active_quote_legs: int = 0
        self._all_fills: list[MakerFillEvent] = []
        self._quoted_edges_bps: list[Decimal] = []
        self._unhedged_warnings: list[str] = []
        self._products_seen: set[str] = set()
        self._last_trade_id_by_product: dict[str, int] = {}
        self._feed_gap_cancels: int = 0

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

    def all_fills(self) -> tuple[MakerFillEvent, ...]:
        """Return all observed fills across strict, touch-lift, and FIFO trade-flow legs."""
        return tuple(self._all_fills)

    def strict_fills(self) -> tuple[MakerFillEvent, ...]:
        """Return only strictly crossed (`strict_cross`) fills."""
        return tuple(f for f in self._all_fills if f.fill_classification == "strict_cross")

    def fifo_fills(self) -> tuple[MakerFillEvent, ...]:
        """Return only trade-flow FIFO (`trade_flow_fifo`) fills."""
        return tuple(f for f in self._all_fills if f.fill_classification == "trade_flow_fifo")

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

    def _evaluate_fifo_fills(
        self,
        *,
        product_id: str,
        prev_state: _RestingQuoteState,
        product_prints: tuple[CoinbaseTradePrint, ...],
        trade_notional_usdc: int,
        dex_buy_token_out: int | None,
        trade_notional_token: int,
        dex_sell_usdc_out: int | None,
        token_decimals: int,
        gas_usd: Decimal,
    ) -> tuple[list[MakerFillEvent], list[str]]:
        if not product_prints:
            return [], []

        fills: list[MakerFillEvent] = []
        unhedged: list[str] = []
        token_scale = Decimal(10**token_decimals)

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
                if dex_buy_token_out is None or dex_buy_token_out <= 0:
                    unhedged.append(
                        f"{product_id}:ask_maker:trade_flow_fifo:"
                        "fifo_ask_fill_missing_t1_dex_buy_hedge"
                    )
                else:
                    ratio = min(1.0, float(filled_ask / prev_state.ask_qty_tokens))
                    if ratio > 0.0:
                        fills.append(
                            _build_ask_maker_fill(
                                product_id=product_id,
                                classification="trade_flow_fifo",
                                quoted_ask=prev_state.ask_price,
                                next_book_px=ask_next_px,
                                t0_dex_unit_px=prev_state.ask_t_dex_unit_px,
                                trade_notional_usdc=trade_notional_usdc,
                                dex_buy_token_out=dex_buy_token_out,
                                token_scale=token_scale,
                                gas_usd=gas_usd,
                                config=self._config,
                                filled_qty=filled_ask,
                                fill_ratio=ratio,
                                matched_trade_ids=ask_matched_ids,
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
                if dex_sell_usdc_out is None or dex_sell_usdc_out <= 0:
                    unhedged.append(
                        f"{product_id}:bid_maker:trade_flow_fifo:"
                        "fifo_bid_fill_missing_t1_dex_sell_hedge"
                    )
                else:
                    ratio = min(1.0, float(filled_bid / prev_state.bid_qty_tokens))
                    if ratio > 0.0:
                        fills.append(
                            _build_bid_maker_fill(
                                product_id=product_id,
                                classification="trade_flow_fifo",
                                quoted_bid=prev_state.bid_price,
                                next_book_px=bid_next_px,
                                t0_dex_unit_px=prev_state.bid_t_dex_unit_px,
                                trade_notional_token=trade_notional_token,
                                dex_sell_usdc_out=dex_sell_usdc_out,
                                token_scale=token_scale,
                                gas_usd=gas_usd,
                                config=self._config,
                                filled_qty=filled_bid,
                                fill_ratio=ratio,
                                matched_trade_ids=bid_matched_ids,
                            )
                        )

        return fills, unhedged

    def _evaluate_resting_fills(
        self,
        *,
        product_id: str,
        prev_state: _RestingQuoteState,
        current_book: CoinbaseL2Book,
        trade_notional_usdc: int,
        dex_buy_token_out: int | None,
        trade_notional_token: int,
        dex_sell_usdc_out: int | None,
        token_decimals: int,
        gas_usd: Decimal,
    ) -> tuple[list[MakerFillEvent], list[str]]:
        fills: list[MakerFillEvent] = []
        unhedged: list[str] = []
        token_scale = Decimal(10**token_decimals)

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
                if dex_buy_token_out is None or dex_buy_token_out <= 0:
                    unhedged.append(
                        f"{product_id}:ask_maker:{classification}:dex_buy_quote_unavailable"
                    )
                else:
                    fills.append(
                        _build_ask_maker_fill(
                            product_id=product_id,
                            classification=classification,
                            quoted_ask=prev_state.ask_price,
                            next_book_px=next_book_px,
                            t0_dex_unit_px=prev_state.ask_t_dex_unit_px,
                            trade_notional_usdc=trade_notional_usdc,
                            dex_buy_token_out=dex_buy_token_out,
                            token_scale=token_scale,
                            gas_usd=gas_usd,
                            config=self._config,
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
                if dex_sell_usdc_out is None or dex_sell_usdc_out <= 0:
                    unhedged.append(
                        f"{product_id}:bid_maker:{classification}:dex_sell_quote_unavailable"
                    )
                else:
                    fills.append(
                        _build_bid_maker_fill(
                            product_id=product_id,
                            classification=classification,
                            quoted_bid=prev_state.bid_price,
                            next_book_px=next_book_px,
                            t0_dex_unit_px=prev_state.bid_t_dex_unit_px,
                            trade_notional_token=trade_notional_token,
                            dex_sell_usdc_out=dex_sell_usdc_out,
                            token_scale=token_scale,
                            gas_usd=gas_usd,
                            config=self._config,
                        )
                    )

        return fills, unhedged

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
        )
        candidate = computed.candidate
        new_prints = self._extract_new_product_trades(spec.product_id, recent_trades)

        fills: list[MakerFillEvent] = []
        unhedged: list[str] = []
        prev_state = self._resting_by_product.get(spec.product_id)

        if feed_gap_detected:
            self._resting_by_product.pop(spec.product_id, None)
            if prev_state is not None and (
                prev_state.ask_price is not None or prev_state.bid_price is not None
            ):
                self._feed_gap_cancels += 1
                unhedged.append(f"{spec.product_id}:feed_gap_cancelled_prior_quote")
        else:
            if prev_state is not None:
                if new_prints:
                    fifo_evaluated, fifo_unhedged = self._evaluate_fifo_fills(
                        product_id=spec.product_id,
                        prev_state=prev_state,
                        product_prints=new_prints,
                        trade_notional_usdc=trade_notional_usdc,
                        dex_buy_token_out=dex_buy_token_out,
                        trade_notional_token=trade_notional_token,
                        dex_sell_usdc_out=dex_sell_usdc_out,
                        token_decimals=token_decimals,
                        gas_usd=candidate.gas_cost_usd,
                    )
                    fills.extend(fifo_evaluated)
                    unhedged.extend(fifo_unhedged)

                book_fills, book_unhedged = self._evaluate_resting_fills(
                    product_id=spec.product_id,
                    prev_state=prev_state,
                    current_book=current_book,
                    trade_notional_usdc=trade_notional_usdc,
                    dex_buy_token_out=dex_buy_token_out,
                    trade_notional_token=trade_notional_token,
                    dex_sell_usdc_out=dex_sell_usdc_out,
                    token_decimals=token_decimals,
                    gas_usd=candidate.gas_cost_usd,
                )
                fills.extend(book_fills)
                unhedged.extend(book_unhedged)

            queue_ahead_ask = Decimal(0)
            if (
                candidate.ask_active
                and candidate.quoted_ask_usd is not None
                and candidate.quoted_ask_usd >= current_book.best_ask
            ):
                queue_ahead_ask = current_book.asks[0].size

            queue_ahead_bid = Decimal(0)
            if (
                candidate.bid_active
                and candidate.quoted_bid_usd is not None
                and candidate.quoted_bid_usd <= current_book.best_bid
            ):
                queue_ahead_bid = current_book.bids[0].size

            self._resting_by_product[spec.product_id] = _RestingQuoteState(
                book_best_bid=current_book.best_bid,
                book_best_ask=current_book.best_ask,
                ask_price=candidate.quoted_ask_usd if candidate.ask_active else None,
                ask_t_dex_unit_px=computed.dex_buy_unit_px if candidate.ask_active else None,
                bid_price=candidate.quoted_bid_usd if candidate.bid_active else None,
                bid_t_dex_unit_px=computed.dex_sell_unit_px if candidate.bid_active else None,
                ask_qty_tokens=computed.ask_qty_tokens,
                bid_qty_tokens=computed.bid_qty_tokens,
                queue_ahead_ask=queue_ahead_ask,
                queue_ahead_bid=queue_ahead_bid,
            )

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
    "build_maker_quote_candidate",
]
