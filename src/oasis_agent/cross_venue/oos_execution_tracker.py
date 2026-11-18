"""True event-time ``t -> t+1`` walk-forward out-of-sample (OOS) execution tracker.

Evaluates both CEX-DEX taker persistence signals and passive CEX maker + DEX hedge
fills without look-ahead bias:
- Pending taker intents formed at tick ``t`` (after passing persistence filtering)
  are settled exclusively against fresh quotes at tick ``>= t+1``.
- Completed maker round-trip fills are deduplicated across repeated snapshots and
  attributed identically into realized OOS trade records.
- Both profitable and adverse realized outcomes are retained faithfully.

All outputs are read-only shadow measurements (``execution_eligible = False``).
"""

import math
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal, InvalidOperation
from typing import Any, Final, Literal

from .live_cex_dex_shadow import resolve_coinbase_product

_BPS_DIVISOR: Final[Decimal] = Decimal(10_000)
_USDC_SCALE: Final[Decimal] = Decimal(1_000_000)
_EPOCH_UTC: Final[datetime] = datetime(1970, 1, 1, tzinfo=UTC)
_LCB_Z_SCORE: Final[Decimal] = Decimal("1.96")
_USD_QUANT: Final[Decimal] = Decimal("0.000001")
_BPS_QUANT: Final[Decimal] = Decimal("0.000001")
_RATE_QUANT: Final[Decimal] = Decimal("0.0001")

_DIRECTION_BUY_DEX: Final[str] = "BUY_DEX_SELL_CEX"
_DIRECTION_SELL_DEX: Final[str] = "SELL_DEX_BUY_CEX"

_DIRECTION_ALIASES: Final[dict[str, Literal["BUY_DEX_SELL_CEX", "SELL_DEX_BUY_CEX"]]] = {
    "BUY_DEX_SELL_CEX": "BUY_DEX_SELL_CEX",
    "DEX_BUY_CEX_SELL": "BUY_DEX_SELL_CEX",
    "SELL_DEX_BUY_CEX": "SELL_DEX_BUY_CEX",
    "BUY_CEX_SELL_DEX": "SELL_DEX_BUY_CEX",
    "DEX_SELL_CEX_BUY": "SELL_DEX_BUY_CEX",
}

_UNHEDGED_STATUSES: Final[frozenset[str]] = frozenset(
    {
        "unhedged",
        "pending",
        "open",
        "resting",
        "cancelled",
        "canceled",
        "rejected",
        "failed",
    }
)


def _to_finite_decimal(
    value: object,
    name: str,
    *,
    require_positive: bool = False,
    require_nonneg: bool = False,
    allow_float: bool = False,
) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{name} must not be a boolean")
    try:
        if isinstance(value, Decimal):
            dec = value
        elif isinstance(value, int):
            dec = Decimal(value)
        elif isinstance(value, float):
            if not allow_float:
                raise ValueError(f"{name} must be Decimal, int, or string, not float")
            if not math.isfinite(value):
                raise ValueError(f"{name} must be finite")
            dec = Decimal(str(value))
        elif isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                raise ValueError(f"{name} must not be empty")
            dec = Decimal(stripped)
        else:
            raise ValueError(f"{name} has unsupported type {type(value).__name__}")
    except InvalidOperation as exc:
        raise ValueError(f"{name} is not a valid decimal number") from exc

    if not dec.is_finite():
        raise ValueError(f"{name} must be finite")
    if require_positive and dec <= 0:
        raise ValueError(f"{name} must be positive")
    if require_nonneg and dec < 0:
        raise ValueError(f"{name} must be non-negative")
    return dec


def _maybe_finite_decimal(
    value: object,
    *,
    allow_float: bool = True,
) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return _to_finite_decimal(value, "value", allow_float=allow_float)
    except ValueError:
        return None


def _maybe_int(value: object) -> int | None:
    if type(value) is int:
        return value
    if isinstance(value, Decimal) and value.is_finite() and value == value.to_integral_value():
        return int(value)
    return None


def _format_decimal_plain(value: Decimal) -> str:
    normalized = value.normalize()
    if normalized == 0:
        return "0"
    return f"{normalized:f}"


def _parse_observed_at_ms(observed_at_utc: str, observed_at_ms: int | None) -> int:
    if not isinstance(observed_at_utc, str) or not observed_at_utc.strip():
        raise ValueError("observed_at_utc must be a non-empty ISO-8601 string")
    if observed_at_ms is not None:
        if type(observed_at_ms) is not int or observed_at_ms < 0:
            raise ValueError("observed_at_ms must be a non-negative integer")
        return observed_at_ms
    try:
        parsed = datetime.fromisoformat(observed_at_utc.strip())
    except ValueError as exc:
        raise ValueError(f"invalid observed_at_utc timestamp: {observed_at_utc!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("observed_at_utc must be timezone-aware ISO-8601")
    delta = parsed.astimezone(UTC) - _EPOCH_UTC
    ms = delta // timedelta(milliseconds=1)
    if ms < 0:
        raise ValueError("observed_at_utc cannot precede Unix epoch")
    return ms


def _normalize_product_id(raw: object) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("product_id must be a non-empty string")
    cleaned = raw.strip().upper()
    try:
        return resolve_coinbase_product(cleaned).product_id
    except ValueError:
        return cleaned


def _get_field(item: Any, *names: str) -> object:
    if isinstance(item, Mapping):
        for name in names:
            if name in item:
                return item[name]
        return None
    for name in names:
        if hasattr(item, name):
            return getattr(item, name)
    return None


def _has_field(item: Any, *names: str) -> bool:
    if isinstance(item, Mapping):
        return any(name in item for name in names)
    return any(hasattr(item, name) for name in names)


def _normalize_taker_direction(
    raw: object,
) -> Literal["BUY_DEX_SELL_CEX", "SELL_DEX_BUY_CEX"] | None:
    if raw is None:
        return None
    text = str(raw).strip().upper()
    return _DIRECTION_ALIASES.get(text)


def _default_decimals_for_product(product_id: str) -> int | None:
    try:
        resolve_spec = resolve_coinbase_product(product_id)
    except ValueError:
        return None
    return resolve_spec.token_decimals


@dataclass(frozen=True, slots=True)
class OosTrackerConfig:
    """Configuration for walk-forward ``t -> t+1`` out-of-sample execution tracking."""

    taker_persistence_ticks: int = 2
    min_signal_edge_bps: int = 1
    max_signal_age_ms: int = 30_000
    require_same_direction: bool = True
    default_gas_cost_usd: Decimal = Decimal("0.0040")
    coinbase_taker_fee_bps: Decimal = Decimal(15)
    execution_eligible: Literal[False] = False

    def __post_init__(self) -> None:
        if type(self.taker_persistence_ticks) is not int or self.taker_persistence_ticks <= 0:
            raise ValueError("taker_persistence_ticks must be a positive integer")
        if type(self.min_signal_edge_bps) is not int or self.min_signal_edge_bps <= 0:
            raise ValueError("min_signal_edge_bps must be a positive integer")
        if type(self.max_signal_age_ms) is not int or self.max_signal_age_ms <= 0:
            raise ValueError("max_signal_age_ms must be a positive integer")
        if type(self.require_same_direction) is not bool:
            raise TypeError("require_same_direction must be a boolean")
        gas = _to_finite_decimal(
            self.default_gas_cost_usd,
            "default_gas_cost_usd",
            require_nonneg=True,
        )
        fee = _to_finite_decimal(
            self.coinbase_taker_fee_bps,
            "coinbase_taker_fee_bps",
            require_nonneg=True,
        )
        if self.execution_eligible is not False:
            raise ValueError("OosTrackerConfig cannot set execution_eligible=True")
        object.__setattr__(self, "default_gas_cost_usd", gas)
        object.__setattr__(self, "coinbase_taker_fee_bps", fee)


@dataclass(frozen=True, slots=True)
class PendingTakerIntent:
    """Queued tick-``t`` taker arbitrage intent awaiting settlement at tick ``>= t+1``."""

    step_seq: int
    observed_at_utc: str
    observed_at_ms: int
    product_id: str
    direction: Literal["BUY_DEX_SELL_CEX", "SELL_DEX_BUY_CEX"]
    notional_usdc_micros: int
    notional_token_base: int
    token_decimals: int
    quoted_net_edge_bps: Decimal
    quoted_net_profit_usd: Decimal
    execution_eligible: Literal[False] = False

    def __post_init__(self) -> None:
        if type(self.step_seq) is not int or self.step_seq < 0:
            raise ValueError("step_seq must be a non-negative integer")
        if not isinstance(self.observed_at_utc, str) or not self.observed_at_utc.strip():
            raise ValueError("observed_at_utc must be a non-empty string")
        if type(self.observed_at_ms) is not int or self.observed_at_ms < 0:
            raise ValueError("observed_at_ms must be a non-negative integer")
        normalized_product = _normalize_product_id(self.product_id)
        object.__setattr__(self, "product_id", normalized_product)
        if self.direction not in (_DIRECTION_BUY_DEX, _DIRECTION_SELL_DEX):
            raise ValueError(f"unsupported taker intent direction: {self.direction!r}")
        if type(self.notional_usdc_micros) is not int or self.notional_usdc_micros <= 0:
            raise ValueError("notional_usdc_micros must be a positive integer")
        if type(self.notional_token_base) is not int or self.notional_token_base <= 0:
            raise ValueError("notional_token_base must be a positive integer")
        if type(self.token_decimals) is not int or not 0 <= self.token_decimals <= 77:
            raise ValueError("token_decimals must be an integer in 0..77")
        edge = _to_finite_decimal(self.quoted_net_edge_bps, "quoted_net_edge_bps")
        profit = _to_finite_decimal(self.quoted_net_profit_usd, "quoted_net_profit_usd")
        object.__setattr__(self, "quoted_net_edge_bps", edge)
        object.__setattr__(self, "quoted_net_profit_usd", profit)
        if self.execution_eligible is not False:
            raise ValueError("PendingTakerIntent cannot set execution_eligible=True")

    def to_dict(self) -> dict[str, Any]:
        """Serialize pending taker intent to a JSON-compatible dictionary."""
        return {
            "step_seq": self.step_seq,
            "observed_at_utc": self.observed_at_utc,
            "observed_at_ms": self.observed_at_ms,
            "product_id": self.product_id,
            "direction": self.direction,
            "notional_usdc_micros": self.notional_usdc_micros,
            "notional_token_base": self.notional_token_base,
            "token_decimals": self.token_decimals,
            "quoted_net_edge_bps": _format_decimal_plain(self.quoted_net_edge_bps),
            "quoted_net_profit_usd": _format_decimal_plain(self.quoted_net_profit_usd),
            "execution_eligible": self.execution_eligible,
        }


@dataclass(frozen=True, slots=True)
class RealizedOosTrade:
    """Realized out-of-sample trade record for either taker or maker execution."""

    trade_id: str
    strategy: Literal["taker", "maker"]
    product_id: str
    direction: str
    decision_step_seq: int
    settlement_step_seq: int
    observed_at_utc: str
    notional_usd: Decimal
    quoted_net_edge_bps: Decimal
    realized_net_edge_bps: Decimal
    realized_net_profit_usd: Decimal
    gas_cost_usd: Decimal
    fee_usd: Decimal
    execution_eligible: Literal[False] = False

    def __post_init__(self) -> None:
        if not isinstance(self.trade_id, str) or not self.trade_id.strip():
            raise ValueError("trade_id must be a non-empty string")
        if self.strategy not in ("taker", "maker"):
            raise ValueError(f"strategy must be 'taker' or 'maker', got {self.strategy!r}")
        object.__setattr__(self, "product_id", _normalize_product_id(self.product_id))
        if not isinstance(self.direction, str) or not self.direction.strip():
            raise ValueError("direction must be a non-empty string")
        if type(self.decision_step_seq) is not int or self.decision_step_seq < 0:
            raise ValueError("decision_step_seq must be a non-negative integer")
        if (
            type(self.settlement_step_seq) is not int
            or self.settlement_step_seq < self.decision_step_seq
        ):
            raise ValueError("settlement_step_seq must be >= decision_step_seq")
        if not isinstance(self.observed_at_utc, str) or not self.observed_at_utc.strip():
            raise ValueError("observed_at_utc must be a non-empty string")
        notional = _to_finite_decimal(self.notional_usd, "notional_usd", require_nonneg=True)
        quoted_edge = _to_finite_decimal(self.quoted_net_edge_bps, "quoted_net_edge_bps")
        realized_edge = _to_finite_decimal(self.realized_net_edge_bps, "realized_net_edge_bps")
        realized_profit = _to_finite_decimal(
            self.realized_net_profit_usd, "realized_net_profit_usd"
        )
        gas = _to_finite_decimal(self.gas_cost_usd, "gas_cost_usd", require_nonneg=True)
        fee = _to_finite_decimal(self.fee_usd, "fee_usd", require_nonneg=True)
        object.__setattr__(self, "notional_usd", notional)
        object.__setattr__(self, "quoted_net_edge_bps", quoted_edge)
        object.__setattr__(self, "realized_net_edge_bps", realized_edge)
        object.__setattr__(self, "realized_net_profit_usd", realized_profit)
        object.__setattr__(self, "gas_cost_usd", gas)
        object.__setattr__(self, "fee_usd", fee)
        if self.execution_eligible is not False:
            raise ValueError("RealizedOosTrade cannot set execution_eligible=True")

    def to_dict(self) -> dict[str, Any]:
        """Serialize realized OOS trade to a JSON-compatible dictionary."""
        return {
            "trade_id": self.trade_id,
            "strategy": self.strategy,
            "product_id": self.product_id,
            "direction": self.direction,
            "decision_step_seq": self.decision_step_seq,
            "settlement_step_seq": self.settlement_step_seq,
            "observed_at_utc": self.observed_at_utc,
            "notional_usd": _format_decimal_plain(self.notional_usd),
            "quoted_net_edge_bps": _format_decimal_plain(self.quoted_net_edge_bps),
            "realized_net_edge_bps": _format_decimal_plain(self.realized_net_edge_bps),
            "realized_net_profit_usd": _format_decimal_plain(self.realized_net_profit_usd),
            "gas_cost_usd": _format_decimal_plain(self.gas_cost_usd),
            "fee_usd": _format_decimal_plain(self.fee_usd),
            "execution_eligible": self.execution_eligible,
        }


@dataclass(frozen=True, slots=True)
class OosStepResult:
    """Outcome of one causal ``step_tick(...)`` step across taker and maker tracks."""

    step_seq: int
    settled_taker_trades: tuple[RealizedOosTrade, ...]
    settled_maker_trades: tuple[RealizedOosTrade, ...]
    new_pending_intents: tuple[PendingTakerIntent, ...]
    expired_pending_intents: int
    rejected_flickers: int
    execution_eligible: Literal[False] = False

    def __post_init__(self) -> None:
        if type(self.step_seq) is not int or self.step_seq < 0:
            raise ValueError("step_seq must be a non-negative integer")
        if type(self.expired_pending_intents) is not int or self.expired_pending_intents < 0:
            raise ValueError("expired_pending_intents must be a non-negative integer")
        if type(self.rejected_flickers) is not int or self.rejected_flickers < 0:
            raise ValueError("rejected_flickers must be a non-negative integer")
        if self.execution_eligible is not False:
            raise ValueError("OosStepResult cannot set execution_eligible=True")

    def to_dict(self) -> dict[str, Any]:
        """Serialize step result to a JSON-compatible dictionary."""
        return {
            "step_seq": self.step_seq,
            "settled_taker_trades": [t.to_dict() for t in self.settled_taker_trades],
            "settled_maker_trades": [t.to_dict() for t in self.settled_maker_trades],
            "new_pending_intents": [i.to_dict() for i in self.new_pending_intents],
            "expired_pending_intents": self.expired_pending_intents,
            "rejected_flickers": self.rejected_flickers,
            "execution_eligible": self.execution_eligible,
        }


@dataclass(frozen=True, slots=True)
class _ParsedDexQuote:
    trade_notional_usdc: int | None
    dex_buy_token_out: int | None
    trade_notional_token: int | None
    dex_sell_usdc_out: int | None
    token_decimals: int | None
    gas_cost_usd: Decimal | None


@dataclass(frozen=True, slots=True)
class _ParsedTakerOpportunity:
    product_id: str
    direction: Literal["BUY_DEX_SELL_CEX", "SELL_DEX_BUY_CEX"]
    notional_usdc_micros: int
    notional_token_base: int
    token_decimals: int
    quoted_net_edge_bps: Decimal
    quoted_net_profit_usd: Decimal


@dataclass(frozen=True, slots=True)
class _ParsedMakerFill:
    dedup_key: tuple[Any, ...]
    product_id: str
    direction: str
    decision_step_seq: int
    observed_at_utc: str
    notional_usd: Decimal
    quoted_net_edge_bps: Decimal
    realized_net_edge_bps: Decimal
    realized_net_profit_usd: Decimal
    gas_cost_usd: Decimal
    fee_usd: Decimal


def _normalize_product_mapping(mapping: Mapping[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for raw_key, value in mapping.items():
        pid = _normalize_product_id(raw_key)
        if pid in normalized:
            raise ValueError(f"duplicate product key after normalization: {pid}")
        normalized[pid] = value
    return normalized


def _extract_bbo(raw_bbo: object) -> tuple[Decimal, Decimal] | None:
    if raw_bbo is None:
        return None
    if isinstance(raw_bbo, (tuple, list)) and len(raw_bbo) == 2:
        bid = _maybe_finite_decimal(raw_bbo[0], allow_float=True)
        ask = _maybe_finite_decimal(raw_bbo[1], allow_float=True)
    else:
        raw_bid = _get_field(
            raw_bbo,
            "best_bid_price",
            "best_bid",
            "best_bid_usd",
            "bid_price",
            "bid",
        )
        raw_ask = _get_field(
            raw_bbo,
            "best_ask_price",
            "best_ask",
            "best_ask_usd",
            "ask_price",
            "ask",
        )
        if raw_bid is None and raw_ask is None:
            bid_micros = _maybe_int(_get_field(raw_bbo, "bid_usd_micros"))
            ask_micros = _maybe_int(_get_field(raw_bbo, "ask_usd_micros"))
            if bid_micros is None or ask_micros is None:
                return None
            bid = Decimal(bid_micros) / _USDC_SCALE
            ask = Decimal(ask_micros) / _USDC_SCALE
        else:
            bid = _maybe_finite_decimal(raw_bid, allow_float=True)
            ask = _maybe_finite_decimal(raw_ask, allow_float=True)

    if bid is None or ask is None or bid <= 0 or ask <= 0 or bid >= ask:
        return None
    return bid, ask


def _extract_dex_quote(raw_quote: object) -> _ParsedDexQuote | None:
    if raw_quote is None:
        return None
    if isinstance(raw_quote, (tuple, list)):
        if len(raw_quote) not in (5, 6):
            raise ValueError(
                "DEX quote tuple must have 5 or 6 elements: "
                "(trade_notional_usdc, dex_buy_token_out, trade_notional_token, "
                "dex_sell_usdc_out, token_decimals[, gas_cost_usd])"
            )
        usdc_in = _maybe_int(raw_quote[0])
        buy_out = _maybe_int(raw_quote[1])
        tok_in = _maybe_int(raw_quote[2])
        sell_out = _maybe_int(raw_quote[3])
        decimals = _maybe_int(raw_quote[4])
        gas = _maybe_finite_decimal(raw_quote[5]) if len(raw_quote) == 6 else None
        return _ParsedDexQuote(
            trade_notional_usdc=usdc_in,
            dex_buy_token_out=buy_out,
            trade_notional_token=tok_in,
            dex_sell_usdc_out=sell_out,
            token_decimals=decimals,
            gas_cost_usd=gas,
        )

    usdc_in = _maybe_int(_get_field(raw_quote, "trade_notional_usdc", "notional_usdc"))
    buy_out = _maybe_int(_get_field(raw_quote, "dex_buy_token_out", "dex_buy_token_for_usdc_out"))
    tok_in = _maybe_int(
        _get_field(raw_quote, "trade_notional_token", "notional_token_base", "token_amount")
    )
    sell_out = _maybe_int(_get_field(raw_quote, "dex_sell_usdc_out", "dex_sell_token_for_usdc_out"))
    decimals = _maybe_int(_get_field(raw_quote, "token_decimals", "base_decimals", "decimals"))
    gas = _maybe_finite_decimal(_get_field(raw_quote, "gas_cost_usd"))
    return _ParsedDexQuote(
        trade_notional_usdc=usdc_in,
        dex_buy_token_out=buy_out,
        trade_notional_token=tok_in,
        dex_sell_usdc_out=sell_out,
        token_decimals=decimals,
        gas_cost_usd=gas,
    )


def _extract_taker_opportunity(
    item: Any,
    *,
    min_signal_edge_bps: int,
    dex_quotes_by_product: Mapping[str, _ParsedDexQuote],
) -> tuple[str, _ParsedTakerOpportunity | None]:
    if _get_field(item, "execution_eligible") is True:
        raise ValueError("taker opportunity cannot set execution_eligible=True")

    raw_pid = _get_field(item, "product_id", "product", "symbol", "instrument")
    product_id = _normalize_product_id(raw_pid)

    raw_status = _get_field(item, "status")
    if raw_status is not None and str(raw_status).strip().lower() not in ("ok", "qualified"):
        return product_id, None

    for flag_name in ("execution_feasible", "feasible", "approved"):
        if _has_field(item, flag_name) and _get_field(item, flag_name) is not True:
            return product_id, None

    direction = _normalize_taker_direction(_get_field(item, "direction", "best_direction"))
    if direction is None:
        return product_id, None

    # Support CexDexTickEvaluation (and its to_dict() shape) via sub-direction object.
    sub_dir = None
    if _has_field(item, "buy_dex_sell_cex", "buy_cex_sell_dex"):
        sub_name = "buy_dex_sell_cex" if direction == _DIRECTION_BUY_DEX else "buy_cex_sell_dex"
        sub_dir = _get_field(item, sub_name)
        if sub_dir is not None and _get_field(sub_dir, "feasible") is not True:
            return product_id, None

    raw_edge = _get_field(
        item,
        "net_edge_bps",
        "best_net_bps",
        "net_bps",
        "edge_bps",
        "quoted_net_edge_bps",
    )
    if raw_edge is None and sub_dir is not None:
        raw_edge = _get_field(sub_dir, "net_bps", "net_edge_bps")
    edge_bps = _maybe_finite_decimal(raw_edge, allow_float=True)
    if edge_bps is None or edge_bps < Decimal(min_signal_edge_bps):
        return product_id, None

    dex_quote = dex_quotes_by_product.get(product_id)
    decimals = _maybe_int(_get_field(item, "token_decimals", "base_decimals", "decimals"))
    if decimals is None and dex_quote is not None:
        decimals = dex_quote.token_decimals
    if decimals is None:
        decimals = _default_decimals_for_product(product_id)
    if decimals is None or not 0 <= decimals <= 77:
        return product_id, None

    raw_usdc = _get_field(
        item,
        "notional_usdc_micros",
        "notional_usdc",
        "recommended_notional_usdc",
        "trade_notional_usdc",
        "selected_notional_usdc",
        "notional_usd_micros",
    )
    usdc_micros = _maybe_int(raw_usdc)
    if usdc_micros is None and not _has_field(
        item,
        "notional_usdc_micros",
        "notional_usdc",
        "recommended_notional_usdc",
        "trade_notional_usdc",
        "notional_usd_micros",
    ):
        raw_usd = _get_field(item, "notional_usd")
        if raw_usd is None and sub_dir is not None:
            raw_usd = _get_field(sub_dir, "dex_notional_usd")
        usd_dec = _maybe_finite_decimal(raw_usd, allow_float=True)
        if usd_dec is not None:
            usdc_micros = int((usd_dec * _USDC_SCALE).to_integral_value(rounding=ROUND_FLOOR))
        elif dex_quote is not None:
            usdc_micros = dex_quote.trade_notional_usdc
    if usdc_micros is None or usdc_micros <= 0:
        return product_id, None

    raw_tok = _get_field(
        item,
        "notional_token_base",
        "token_amount",
        "recommended_token_amount",
        "trade_notional_token",
        "qty_base",
        "token_amount_base",
    )
    token_base = _maybe_int(raw_tok)
    if token_base is None and not _has_field(
        item,
        "notional_token_base",
        "token_amount",
        "recommended_token_amount",
        "trade_notional_token",
        "qty_base",
        "token_amount_base",
    ):
        raw_qty = _get_field(item, "qty_tokens")
        if raw_qty is None and sub_dir is not None:
            raw_qty = _get_field(sub_dir, "qty_tokens")
        qty_dec = _maybe_finite_decimal(raw_qty, allow_float=True)
        if qty_dec is not None:
            token_base = int(
                (qty_dec * Decimal(10**decimals)).to_integral_value(rounding=ROUND_FLOOR)
            )
        elif dex_quote is not None:
            if direction == _DIRECTION_BUY_DEX:
                if (
                    dex_quote.dex_buy_token_out is not None
                    and dex_quote.trade_notional_usdc is not None
                    and dex_quote.trade_notional_usdc > 0
                ):
                    token_base = (
                        dex_quote.dex_buy_token_out * usdc_micros
                    ) // dex_quote.trade_notional_usdc
            else:
                token_base = dex_quote.trade_notional_token
    if token_base is None or token_base <= 0:
        return product_id, None

    raw_profit = _get_field(
        item,
        "quoted_net_profit_usd",
        "net_profit_usd",
        "expected_net_profit_usd",
    )
    if raw_profit is None and sub_dir is not None:
        raw_profit = _get_field(sub_dir, "net_profit_usd")
    quoted_profit = _maybe_finite_decimal(raw_profit, allow_float=True)
    if quoted_profit is None:
        profit_micros = _maybe_int(_get_field(item, "worst_case_net_usd_micros"))
        if profit_micros is not None:
            quoted_profit = Decimal(profit_micros) / _USDC_SCALE
        else:
            quoted_profit = (Decimal(usdc_micros) / _USDC_SCALE) * edge_bps / _BPS_DIVISOR

    return product_id, _ParsedTakerOpportunity(
        product_id=product_id,
        direction=direction,
        notional_usdc_micros=usdc_micros,
        notional_token_base=token_base,
        token_decimals=decimals,
        quoted_net_edge_bps=edge_bps,
        quoted_net_profit_usd=quoted_profit,
    )


def _extract_maker_fill(
    item: Any,
    *,
    step_seq: int,
    step_observed_at_utc: str,
    default_gas_usd: Decimal,
    gas_by_product: Mapping[str, Decimal],
    dex_quotes_by_product: Mapping[str, _ParsedDexQuote],
) -> _ParsedMakerFill | None:
    if _get_field(item, "execution_eligible") is True:
        raise ValueError("maker fill cannot set execution_eligible=True")

    raw_status = _get_field(item, "status")
    if raw_status is not None:
        status_norm = str(raw_status).strip().lower()
        if (
            status_norm in _UNHEDGED_STATUSES
            or "unhedged" in status_norm
            or "pending" in status_norm
        ):
            return None

    for flag_name in ("hedged", "is_hedged", "settled"):
        if _has_field(item, flag_name) and _get_field(item, flag_name) is False:
            return None

    raw_event_type = _get_field(item, "event_type", "fill_classification")
    event_type = str(raw_event_type).strip() if raw_event_type is not None else "maker_fill"
    event_type_lower = event_type.lower()
    if (
        event_type_lower == "touch_lift"
        or "unhedged" in event_type_lower
        or "pending" in event_type_lower
    ):
        return None

    product_id = _normalize_product_id(_get_field(item, "product_id", "product", "symbol"))
    raw_side = _get_field(item, "side", "direction")
    direction = str(raw_side).strip() if raw_side is not None else "maker"
    if not direction:
        direction = "maker"

    raw_recorded = _get_field(item, "recorded_at_utc", "observed_at_utc", "timestamp_utc")
    recorded_at_utc = (
        str(raw_recorded).strip()
        if isinstance(raw_recorded, str) and raw_recorded.strip()
        else step_observed_at_utc
    )

    raw_profit = _get_field(item, "realized_net_profit_usd", "net_profit_usd")
    net_profit = _maybe_finite_decimal(raw_profit, allow_float=True)
    if net_profit is None:
        return None

    raw_bps = _get_field(item, "realized_net_bps", "net_edge_bps", "net_bps")
    realized_bps = _maybe_finite_decimal(raw_bps, allow_float=True)

    notional_usd = _maybe_finite_decimal(_get_field(item, "notional_usd"), allow_float=True)
    raw_notional_usdc = _maybe_int(
        _get_field(item, "notional_usdc", "notional_usdc_micros", "trade_notional_usdc")
    )
    if notional_usd is None and raw_notional_usdc is not None and raw_notional_usdc > 0:
        notional_usd = Decimal(raw_notional_usdc) / _USDC_SCALE
    if notional_usd is None:
        filled_qty = _maybe_finite_decimal(_get_field(item, "filled_qty_tokens"), allow_float=True)
        quoted_px = _maybe_finite_decimal(_get_field(item, "quoted_price_usd"), allow_float=True)
        if filled_qty is not None and quoted_px is not None and filled_qty > 0 and quoted_px > 0:
            notional_usd = filled_qty * quoted_px
    if notional_usd is None and realized_bps is not None and realized_bps != 0 and net_profit != 0:
        derived = abs(net_profit * _BPS_DIVISOR / realized_bps)
        if derived > 0:
            notional_usd = derived
    if notional_usd is None:
        dex_quote = dex_quotes_by_product.get(product_id)
        if (
            dex_quote is not None
            and dex_quote.trade_notional_usdc is not None
            and dex_quote.trade_notional_usdc > 0
        ):
            notional_usd = Decimal(dex_quote.trade_notional_usdc) / _USDC_SCALE
    if notional_usd is None or notional_usd < 0:
        notional_usd = Decimal(0)

    if realized_bps is None:
        realized_bps = (
            (net_profit / notional_usd) * _BPS_DIVISOR if notional_usd > 0 else Decimal(0)
        )

    quoted_edge = _maybe_finite_decimal(
        _get_field(item, "quoted_net_edge_bps", "quoted_edge_bps"),
        allow_float=True,
    )
    if quoted_edge is None:
        quoted_edge = realized_bps

    gas_cost = _maybe_finite_decimal(
        _get_field(item, "hedge_tx_cost_usd", "gas_cost_usd"),
        allow_float=True,
    )
    if gas_cost is None or gas_cost < 0:
        quote_entry = dex_quotes_by_product.get(product_id)
        quote_gas = quote_entry.gas_cost_usd if quote_entry is not None else None
        if product_id in gas_by_product:
            gas_cost = gas_by_product[product_id]
        elif quote_gas is not None and quote_gas >= 0:
            gas_cost = quote_gas
        else:
            gas_cost = default_gas_usd

    fee_usd = _maybe_finite_decimal(_get_field(item, "fee_usd", "cex_fee_usd"), allow_float=True)
    if fee_usd is None or fee_usd < 0:
        fee_usd = Decimal(0)

    raw_dec_seq = _maybe_int(_get_field(item, "decision_step_seq"))
    decision_seq = (
        raw_dec_seq
        if (raw_dec_seq is not None and 0 <= raw_dec_seq <= step_seq)
        else max(0, step_seq - 1)
    )

    raw_matched = _get_field(item, "matched_trade_ids")
    matched_ids = (
        tuple(int(x) for x in raw_matched) if isinstance(raw_matched, (tuple, list)) else ()
    )
    raw_id = _get_field(item, "trade_id", "fill_id", "id")
    explicit_id = str(raw_id) if raw_id is not None else ""
    quoted_px_str = str(_get_field(item, "quoted_price_usd") or "")
    next_px_str = str(_get_field(item, "next_book_price_usd") or "")
    hedge_px_str = str(_get_field(item, "hedge_dex_price_usd") or "")
    raw_recorded_key = str(raw_recorded) if raw_recorded is not None else ""
    notional_key = (
        str(raw_notional_usdc)
        if raw_notional_usdc is not None
        else _format_decimal_plain(notional_usd)
    )

    dedup_key = (
        explicit_id,
        event_type,
        product_id,
        direction,
        raw_recorded_key,
        notional_key,
        _format_decimal_plain(net_profit),
        _format_decimal_plain(gas_cost),
        _format_decimal_plain(realized_bps),
        quoted_px_str,
        next_px_str,
        hedge_px_str,
        matched_ids,
    )

    return _ParsedMakerFill(
        dedup_key=dedup_key,
        product_id=product_id,
        direction=direction,
        decision_step_seq=decision_seq,
        observed_at_utc=recorded_at_utc,
        notional_usd=notional_usd,
        quoted_net_edge_bps=quoted_edge,
        realized_net_edge_bps=realized_bps,
        realized_net_profit_usd=net_profit,
        gas_cost_usd=gas_cost,
        fee_usd=fee_usd,
    )


class OosStrategyExecutionTracker:
    """Stateful walk-forward ``t -> t+1`` OOS execution and attribution tracker."""

    def __init__(self, config: OosTrackerConfig | None = None) -> None:
        self._config: OosTrackerConfig = config if config is not None else OosTrackerConfig()
        self._pending_intents: list[PendingTakerIntent] = []
        self._realized_trades: list[RealizedOosTrade] = []
        self._streak_by_product: dict[str, tuple[int, str]] = {}
        self._seen_maker_key_counts: Counter[tuple[Any, ...]] = Counter()
        self._expired_pending_intents: int = 0
        self._rejected_one_tick_flickers: int = 0
        self._trade_counter: int = 0
        self._last_step_seq: int | None = None
        self._last_observed_at_ms: int | None = None

    @property
    def config(self) -> OosTrackerConfig:
        return self._config

    @property
    def realized_trades(self) -> tuple[RealizedOosTrade, ...]:
        return tuple(self._realized_trades)

    @property
    def pending_intents(self) -> tuple[PendingTakerIntent, ...]:
        return tuple(self._pending_intents)

    def _next_trade_id(self, strategy: str, step_seq: int, product_id: str) -> str:
        self._trade_counter += 1
        return f"oos-{strategy}-s{step_seq}-{product_id.lower()}-{self._trade_counter:06d}"

    def _settle_pending_intent(
        self,
        intent: PendingTakerIntent,
        *,
        step_seq: int,
        observed_at_utc: str,
        dex_quotes: Mapping[str, _ParsedDexQuote],
        bbos: Mapping[str, tuple[Decimal, Decimal]],
        gas_by_product: Mapping[str, Decimal],
        fee_bps: Decimal,
    ) -> RealizedOosTrade | None:
        dex_quote = dex_quotes.get(intent.product_id)
        bbo = bbos.get(intent.product_id)
        if dex_quote is None or bbo is None:
            return None

        best_bid, best_ask = bbo
        decimals = (
            dex_quote.token_decimals
            if dex_quote.token_decimals is not None
            else intent.token_decimals
        )
        if decimals != intent.token_decimals:
            raise ValueError(
                f"token_decimals mismatch for {intent.product_id}: "
                f"intent={intent.token_decimals}, quote={decimals}"
            )

        if intent.product_id in gas_by_product:
            gas_usd = gas_by_product[intent.product_id]
        elif dex_quote.gas_cost_usd is not None and dex_quote.gas_cost_usd >= 0:
            gas_usd = dex_quote.gas_cost_usd
        else:
            gas_usd = self._config.default_gas_cost_usd

        token_scale = Decimal(10**decimals)
        fee_rate = fee_bps / _BPS_DIVISOR

        if intent.direction == _DIRECTION_BUY_DEX:
            if (
                dex_quote.trade_notional_usdc is None
                or dex_quote.trade_notional_usdc <= 0
                or dex_quote.dex_buy_token_out is None
                or dex_quote.dex_buy_token_out <= 0
            ):
                return None
            scaled_tokens_base = (
                Decimal(dex_quote.dex_buy_token_out)
                * Decimal(intent.notional_usdc_micros)
                / Decimal(dex_quote.trade_notional_usdc)
            )
            dex_tokens_received = scaled_tokens_base / token_scale
            notional_usd = Decimal(intent.notional_usdc_micros) / _USDC_SCALE
            gross_cex_proceeds = dex_tokens_received * best_bid
            fee_usd = gross_cex_proceeds * fee_rate
            realized_profit = (gross_cex_proceeds - fee_usd) - notional_usd - gas_usd
            realized_edge_bps = (realized_profit / notional_usd) * _BPS_DIVISOR
        else:
            if (
                dex_quote.trade_notional_token is None
                or dex_quote.trade_notional_token <= 0
                or dex_quote.dex_sell_usdc_out is None
                or dex_quote.dex_sell_usdc_out <= 0
            ):
                return None
            scaled_usdc_micros = (
                Decimal(dex_quote.dex_sell_usdc_out)
                * Decimal(intent.notional_token_base)
                / Decimal(dex_quote.trade_notional_token)
            )
            dex_usdc_received = scaled_usdc_micros / _USDC_SCALE
            notional_tokens = Decimal(intent.notional_token_base) / token_scale
            gross_cex_cost = notional_tokens * best_ask
            fee_usd = gross_cex_cost * fee_rate
            realized_profit = dex_usdc_received - (gross_cex_cost + fee_usd) - gas_usd
            notional_usd = gross_cex_cost
            realized_edge_bps = (realized_profit / notional_usd) * _BPS_DIVISOR

        return RealizedOosTrade(
            trade_id=self._next_trade_id("taker", step_seq, intent.product_id),
            strategy="taker",
            product_id=intent.product_id,
            direction=intent.direction,
            decision_step_seq=intent.step_seq,
            settlement_step_seq=step_seq,
            observed_at_utc=observed_at_utc,
            notional_usd=notional_usd,
            quoted_net_edge_bps=intent.quoted_net_edge_bps,
            realized_net_edge_bps=realized_edge_bps,
            realized_net_profit_usd=realized_profit,
            gas_cost_usd=gas_usd,
            fee_usd=fee_usd,
        )

    def step_tick(
        self,
        *,
        step_seq: int,
        observed_at_utc: str,
        cex_dex_opportunities: Sequence[Any],
        latest_dex_quotes_by_product: Mapping[str, Any],
        latest_bbo_by_product: Mapping[str, Any],
        maker_fills: Sequence[Any] = (),
        gas_cost_usd_by_product: Mapping[str, Decimal] | None = None,
        coinbase_taker_fee_bps: Decimal | None = None,
        observed_at_ms: int | None = None,
    ) -> OosStepResult:
        """Advance one event-time tick: settle pending intents, ingest maker fills, queue new."""
        if type(step_seq) is not int or step_seq < 0:
            raise ValueError("step_seq must be a non-negative integer")
        if self._last_step_seq is not None and step_seq <= self._last_step_seq:
            raise ValueError(
                f"step_seq must be strictly increasing ({step_seq} <= {self._last_step_seq})"
            )
        obs_ms = _parse_observed_at_ms(observed_at_utc, observed_at_ms)
        if self._last_observed_at_ms is not None and obs_ms < self._last_observed_at_ms:
            raise ValueError("observed_at_ms cannot move backwards")

        if isinstance(cex_dex_opportunities, (str, bytes)):
            raise TypeError("cex_dex_opportunities must be a sequence of opportunities")
        if isinstance(maker_fills, (str, bytes)):
            raise TypeError("maker_fills must be a sequence of fills")

        eff_fee_bps = (
            _to_finite_decimal(
                coinbase_taker_fee_bps,
                "coinbase_taker_fee_bps",
                require_nonneg=True,
            )
            if coinbase_taker_fee_bps is not None
            else self._config.coinbase_taker_fee_bps
        )

        gas_by_product: dict[str, Decimal] = {}
        if gas_cost_usd_by_product is not None:
            for pid, raw_gas in _normalize_product_mapping(gas_cost_usd_by_product).items():
                gas_by_product[pid] = _to_finite_decimal(
                    raw_gas,
                    f"gas_cost_usd_by_product[{pid}]",
                    require_nonneg=True,
                )

        dex_quotes: dict[str, _ParsedDexQuote] = {}
        for pid, raw_q in _normalize_product_mapping(latest_dex_quotes_by_product).items():
            parsed_q = _extract_dex_quote(raw_q)
            if parsed_q is not None:
                dex_quotes[pid] = parsed_q

        bbos: dict[str, tuple[Decimal, Decimal]] = {}
        for pid, raw_bbo in _normalize_product_mapping(latest_bbo_by_product).items():
            parsed_bbo = _extract_bbo(raw_bbo)
            if parsed_bbo is not None:
                bbos[pid] = parsed_bbo

        # Step A: Settle existing _pending_intents created at <= t-1 on tick t's fresh quotes.
        settled_taker: list[RealizedOosTrade] = []
        remaining_pending: list[PendingTakerIntent] = []
        expired_this_step = 0
        for intent in self._pending_intents:
            if obs_ms - intent.observed_at_ms > self._config.max_signal_age_ms:
                expired_this_step += 1
                self._expired_pending_intents += 1
                continue
            trade = self._settle_pending_intent(
                intent,
                step_seq=step_seq,
                observed_at_utc=observed_at_utc,
                dex_quotes=dex_quotes,
                bbos=bbos,
                gas_by_product=gas_by_product,
                fee_bps=eff_fee_bps,
            )
            if trade is None:
                remaining_pending.append(intent)
            else:
                settled_taker.append(trade)
                self._realized_trades.append(trade)
        self._pending_intents = remaining_pending

        # Step B: Deduplicate completed Maker fills without double-counting across snapshots.
        settled_maker: list[RealizedOosTrade] = []
        parsed_maker: list[_ParsedMakerFill] = []
        for raw_fill in maker_fills:
            parsed_fill = _extract_maker_fill(
                raw_fill,
                step_seq=step_seq,
                step_observed_at_utc=observed_at_utc,
                default_gas_usd=self._config.default_gas_cost_usd,
                gas_by_product=gas_by_product,
                dex_quotes_by_product=dex_quotes,
            )
            if parsed_fill is not None:
                parsed_maker.append(parsed_fill)

        tick_counts = Counter(item.dedup_key for item in parsed_maker)
        emitted_this_tick: Counter[tuple[Any, ...]] = Counter()
        for parsed_fill in parsed_maker:
            key = parsed_fill.dedup_key
            already_seen = self._seen_maker_key_counts[key]
            target_total = max(already_seen, tick_counts[key])
            if already_seen + emitted_this_tick[key] < target_total:
                emitted_this_tick[key] += 1
                maker_trade = RealizedOosTrade(
                    trade_id=self._next_trade_id("maker", step_seq, parsed_fill.product_id),
                    strategy="maker",
                    product_id=parsed_fill.product_id,
                    direction=parsed_fill.direction,
                    decision_step_seq=parsed_fill.decision_step_seq,
                    settlement_step_seq=step_seq,
                    observed_at_utc=parsed_fill.observed_at_utc,
                    notional_usd=parsed_fill.notional_usd,
                    quoted_net_edge_bps=parsed_fill.quoted_net_edge_bps,
                    realized_net_edge_bps=parsed_fill.realized_net_edge_bps,
                    realized_net_profit_usd=parsed_fill.realized_net_profit_usd,
                    gas_cost_usd=parsed_fill.gas_cost_usd,
                    fee_usd=parsed_fill.fee_usd,
                )
                settled_maker.append(maker_trade)
                self._realized_trades.append(maker_trade)
        for key, added in emitted_this_tick.items():
            self._seen_maker_key_counts[key] += added

        # Step C: Update per-product persistence streaks on tick t's opportunities.
        best_qualifying_by_product: dict[str, _ParsedTakerOpportunity] = {}
        for item in cex_dex_opportunities:
            pid, parsed_opp = _extract_taker_opportunity(
                item,
                min_signal_edge_bps=self._config.min_signal_edge_bps,
                dex_quotes_by_product=dex_quotes,
            )
            if parsed_opp is None:
                continue
            current_best = best_qualifying_by_product.get(pid)
            if current_best is None or (
                parsed_opp.quoted_net_edge_bps,
                parsed_opp.quoted_net_profit_usd,
            ) > (
                current_best.quoted_net_edge_bps,
                current_best.quoted_net_profit_usd,
            ):
                best_qualifying_by_product[pid] = parsed_opp

        rejected_flickers_this_step = 0
        for pid in tuple(self._streak_by_product):
            if pid not in best_qualifying_by_product:
                prev_streak, _ = self._streak_by_product.pop(pid)
                if 1 <= prev_streak < self._config.taker_persistence_ticks:
                    rejected_flickers_this_step += 1
                    self._rejected_one_tick_flickers += 1

        new_intents: list[PendingTakerIntent] = []
        for pid in sorted(best_qualifying_by_product):
            opp = best_qualifying_by_product[pid]
            prev_entry = self._streak_by_product.get(pid)
            if prev_entry is None:
                new_streak = 1
            else:
                prev_streak, prev_dir = prev_entry
                if self._config.require_same_direction and prev_dir != opp.direction:
                    if 1 <= prev_streak < self._config.taker_persistence_ticks:
                        rejected_flickers_this_step += 1
                        self._rejected_one_tick_flickers += 1
                    new_streak = 1
                else:
                    new_streak = prev_streak + 1

            self._streak_by_product[pid] = (new_streak, opp.direction)
            if new_streak >= self._config.taker_persistence_ticks:
                intent = PendingTakerIntent(
                    step_seq=step_seq,
                    observed_at_utc=observed_at_utc,
                    observed_at_ms=obs_ms,
                    product_id=pid,
                    direction=opp.direction,
                    notional_usdc_micros=opp.notional_usdc_micros,
                    notional_token_base=opp.notional_token_base,
                    token_decimals=opp.token_decimals,
                    quoted_net_edge_bps=opp.quoted_net_edge_bps,
                    quoted_net_profit_usd=opp.quoted_net_profit_usd,
                )
                self._pending_intents.append(intent)
                new_intents.append(intent)

        self._last_step_seq = step_seq
        self._last_observed_at_ms = obs_ms

        return OosStepResult(
            step_seq=step_seq,
            settled_taker_trades=tuple(settled_taker),
            settled_maker_trades=tuple(settled_maker),
            new_pending_intents=tuple(new_intents),
            expired_pending_intents=expired_this_step,
            rejected_flickers=rejected_flickers_this_step,
        )

    def summary(self) -> dict[str, Any]:
        """Compute exact conservative OOS execution metrics across all realized trades."""
        n_trades = len(self._realized_trades)
        taker_count = sum(1 for t in self._realized_trades if t.strategy == "taker")
        maker_count = sum(1 for t in self._realized_trades if t.strategy == "maker")
        positive_trades = sum(1 for t in self._realized_trades if t.realized_net_profit_usd > 0)

        if n_trades == 0:
            return {
                "oos_trades_count": 0,
                "oos_taker_trades_count": 0,
                "oos_maker_trades_count": 0,
                "oos_positive_trades": 0,
                "oos_win_rate": "0.0000",
                "oos_total_net_profit_usd": "0.000000",
                "oos_mean_net_edge_bps": "0.000000",
                "oos_std_net_edge_bps": "0",
                "oos_lcb_net_edge_bps_exact": "0",
                "oos_lcb_net_edge_bps": 0,
                "oos_max_drawdown_usd": "0.000000",
                "oos_tail_loss_p5_bps": "0.000000",
                "pending_unsettled_intents": len(self._pending_intents),
                "expired_pending_intents": self._expired_pending_intents,
                "rejected_one_tick_flickers": self._rejected_one_tick_flickers,
                "execution_eligible": False,
            }

        win_rate = (Decimal(positive_trades) / Decimal(n_trades)).quantize(_RATE_QUANT)
        total_profit = sum((t.realized_net_profit_usd for t in self._realized_trades), Decimal(0))
        edges = [t.realized_net_edge_bps for t in self._realized_trades]
        mean_edge = sum(edges, Decimal(0)) / Decimal(n_trades)

        if n_trades >= 2:
            sq_err_sum = sum(((e - mean_edge) ** 2 for e in edges), Decimal(0))
            variance = sq_err_sum / Decimal(n_trades - 1)
            std_edge = variance.sqrt()
            std_error = std_edge / Decimal(n_trades).sqrt()
            lcb_exact = mean_edge - _LCB_Z_SCORE * std_error
            std_str = str(std_edge.quantize(_BPS_QUANT))
        else:
            std_str = "0"
            lcb_exact = min(Decimal(0), mean_edge)

        lcb_quantized = lcb_exact.quantize(_BPS_QUANT, rounding=ROUND_FLOOR)
        lcb_int = int(lcb_quantized.to_integral_value(rounding=ROUND_FLOOR))

        equity = Decimal(0)
        peak = Decimal(0)
        max_drawdown = Decimal(0)
        for trade in self._realized_trades:
            equity += trade.realized_net_profit_usd
            if equity > peak:
                peak = equity
            drawdown = peak - equity
            if drawdown > max_drawdown:
                max_drawdown = drawdown

        sorted_edges = sorted(edges)
        p5_index = max(0, (5 * n_trades + 99) // 100 - 1)
        tail_p5_bps = sorted_edges[p5_index]

        return {
            "oos_trades_count": n_trades,
            "oos_taker_trades_count": taker_count,
            "oos_maker_trades_count": maker_count,
            "oos_positive_trades": positive_trades,
            "oos_win_rate": str(win_rate),
            "oos_total_net_profit_usd": str(total_profit.quantize(_USD_QUANT)),
            "oos_mean_net_edge_bps": str(mean_edge.quantize(_BPS_QUANT)),
            "oos_std_net_edge_bps": std_str,
            "oos_lcb_net_edge_bps_exact": str(lcb_quantized),
            "oos_lcb_net_edge_bps": lcb_int,
            "oos_max_drawdown_usd": str(max_drawdown.quantize(_USD_QUANT)),
            "oos_tail_loss_p5_bps": str(tail_p5_bps.quantize(_BPS_QUANT)),
            "pending_unsettled_intents": len(self._pending_intents),
            "expired_pending_intents": self._expired_pending_intents,
            "rejected_one_tick_flickers": self._rejected_one_tick_flickers,
            "execution_eligible": False,
        }


__all__ = [
    "OosStepResult",
    "OosStrategyExecutionTracker",
    "OosTrackerConfig",
    "PendingTakerIntent",
    "RealizedOosTrade",
]
