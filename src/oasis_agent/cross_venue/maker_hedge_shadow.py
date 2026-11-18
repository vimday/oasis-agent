"""Passive CEX Post-Only Maker + Atomic Base DEX Taker Hedge shadow evaluator.

Models the two-leg cross-venue maker/hedge pattern (`AlephTX` Saga style) across
Coinbase Exchange and Base DEX: resting competitive Post-Only maker quotes inside
the CEX spread (capturing 0 bps VIP maker fees instead of paying 3.5 bps taker fees)
and hedging confirmed out-of-sample `t -> t+1` fills atomically on Base DEX.

All quotes, fills, and PnL summaries are strictly read-only shadow measurements
(`execution_eligible = False`).
"""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Final, Literal

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
    fill_classification: Literal["strict_cross", "touch_lift"]
    quoted_price_usd: Decimal
    next_book_price_usd: Decimal
    hedge_dex_price_usd: Decimal
    realized_net_bps: Decimal
    realized_net_profit_usd: Decimal
    latency_drift_bps: Decimal
    execution_eligible: Literal[False] = False

    def __post_init__(self) -> None:
        if self.side not in ("ask_maker", "bid_maker"):
            raise MakerHedgeError(f"invalid maker fill side: {self.side!r}")
        if self.fill_classification not in ("strict_cross", "touch_lift"):
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
) -> tuple[MakerQuoteCandidate, Decimal | None, Decimal | None]:
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
    return candidate, dex_buy_unit_px, dex_sell_unit_px


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
    candidate, _, _ = _compute_quote_and_unit_prices(
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
    return candidate


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
        """Return all observed fills (both `strict_cross` and diagnostic `touch_lift`)."""
        return tuple(self._all_fills)

    def strict_fills(self) -> tuple[MakerFillEvent, ...]:
        """Return only strictly crossed (`strict_cross`) fills."""
        return tuple(f for f in self._all_fills if f.fill_classification == "strict_cross")

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
                    t1_dex_cost_usd = Decimal(trade_notional_usdc) / _USDC_SCALE
                    t1_ask_qty = Decimal(dex_buy_token_out) / token_scale
                    hedge_dex_px = t1_dex_cost_usd / t1_ask_qty
                    latency_drift_bps = (
                        (hedge_dex_px - prev_state.ask_t_dex_unit_px) / prev_state.ask_t_dex_unit_px
                    ) * _BPS_DIVISOR
                    cex_proceeds = prev_state.ask_price * t1_ask_qty
                    cex_fee = cex_proceeds * (self._config.cex_maker_fee_bps / _BPS_DIVISOR)
                    adv_cost = t1_dex_cost_usd * (self._config.adverse_selection_bps / _BPS_DIVISOR)
                    realized_profit = cex_proceeds - cex_fee - t1_dex_cost_usd - adv_cost - gas_usd
                    realized_bps = (realized_profit / t1_dex_cost_usd) * _BPS_DIVISOR
                    fills.append(
                        MakerFillEvent(
                            product_id=product_id,
                            side="ask_maker",
                            fill_classification=classification,
                            quoted_price_usd=prev_state.ask_price,
                            next_book_price_usd=next_book_px,
                            hedge_dex_price_usd=hedge_dex_px,
                            realized_net_bps=realized_bps,
                            realized_net_profit_usd=realized_profit,
                            latency_drift_bps=latency_drift_bps,
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
                    t1_sell_qty = Decimal(trade_notional_token) / token_scale
                    t1_dex_proceeds = Decimal(dex_sell_usdc_out) / _USDC_SCALE
                    hedge_dex_px = t1_dex_proceeds / t1_sell_qty
                    latency_drift_bps = (
                        (prev_state.bid_t_dex_unit_px - hedge_dex_px) / prev_state.bid_t_dex_unit_px
                    ) * _BPS_DIVISOR
                    cex_cost = prev_state.bid_price * t1_sell_qty
                    cex_fee = cex_cost * (self._config.cex_maker_fee_bps / _BPS_DIVISOR)
                    adv_cost = cex_cost * (self._config.adverse_selection_bps / _BPS_DIVISOR)
                    realized_profit = t1_dex_proceeds - cex_cost - cex_fee - adv_cost - gas_usd
                    realized_bps = (realized_profit / cex_cost) * _BPS_DIVISOR
                    fills.append(
                        MakerFillEvent(
                            product_id=product_id,
                            side="bid_maker",
                            fill_classification=classification,
                            quoted_price_usd=prev_state.bid_price,
                            next_book_price_usd=next_book_px,
                            hedge_dex_price_usd=hedge_dex_px,
                            realized_net_bps=realized_bps,
                            realized_net_profit_usd=realized_profit,
                            latency_drift_bps=latency_drift_bps,
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
    ) -> MakerStepResult:
        """Evaluate prior tick's resting quotes on `current_book`, then refresh quotes."""
        spec = resolve_coinbase_product(product_id)
        candidate, dex_buy_unit_px, dex_sell_unit_px = _compute_quote_and_unit_prices(
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

        fills: list[MakerFillEvent] = []
        unhedged: list[str] = []
        prev_state = self._resting_by_product.get(spec.product_id)
        if prev_state is not None:
            fills, unhedged = self._evaluate_resting_fills(
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

        self._resting_by_product[spec.product_id] = _RestingQuoteState(
            book_best_bid=current_book.best_bid,
            book_best_ask=current_book.best_ask,
            ask_price=candidate.quoted_ask_usd if candidate.ask_active else None,
            ask_t_dex_unit_px=dex_buy_unit_px if candidate.ask_active else None,
            bid_price=candidate.quoted_bid_usd if candidate.bid_active else None,
            bid_t_dex_unit_px=dex_sell_unit_px if candidate.bid_active else None,
        )

        self._total_steps += 1
        self._products_seen.add(spec.product_id)
        self._active_quote_legs += int(candidate.ask_active) + int(candidate.bid_active)
        if candidate.ask_edge_bps is not None:
            self._quoted_edges_bps.append(candidate.ask_edge_bps)
        if candidate.bid_edge_bps is not None:
            self._quoted_edges_bps.append(candidate.bid_edge_bps)
        self._all_fills.extend(fills)
        self._unhedged_warnings.extend(unhedged)

        strict_fills = tuple(f for f in fills if f.fill_classification == "strict_cross")
        touch_fills = tuple(f for f in fills if f.fill_classification == "touch_lift")
        return MakerStepResult(
            product_id=spec.product_id,
            fills=tuple(fills),
            strict_fills=strict_fills,
            touch_lift_fills=touch_fills,
            quote=candidate,
            unhedged_fill_reasons=tuple(unhedged),
        )

    def summary(self) -> dict[str, Any]:
        """Return conservative strict-cross and diagnostic shadow summary metrics."""
        strict = [f for f in self._all_fills if f.fill_classification == "strict_cross"]
        touch = [f for f in self._all_fills if f.fill_classification == "touch_lift"]
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
