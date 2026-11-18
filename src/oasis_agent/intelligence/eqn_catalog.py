"""Equation News (`eqn.news`) channel registry and exact-integer domain models.

All external numeric fields are parsed via ``Decimal`` / ``Fraction`` into fixed-point
integers (basis points for percentages/ratios, USD micros for dollar amounts).
Binary floats are never stored on domain objects.
"""

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from types import MappingProxyType
from typing import Final

from ..models.common import sha256_hex

DEFAULT_MAX_FUTURE_SKEW_MS: Final[int] = 5_000
MAX_EQN_PAYLOAD_BYTES: Final[int] = 4 * 1024 * 1024
BPS_SCALE: Final[int] = 100  # percent value * 100 -> basis points
RATIO_BPS_SCALE: Final[int] = 10_000  # unit ratio * 10_000 -> basis points
USD_MICROS_SCALE: Final[int] = 1_000_000
RATE_E8_SCALE: Final[int] = 1_000_000  # percent * 1_000_000 -> 1e-8 fractional rate


@dataclass(frozen=True, slots=True)
class EqnChannelSpec:
    """Metadata for one public Equation News channel."""

    key: str
    telegram: str
    name_zh: str
    name_en: str
    is_main: bool = False

    def __post_init__(self) -> None:
        for name, value in (
            ("key", self.key),
            ("telegram", self.telegram),
            ("name_zh", self.name_zh),
            ("name_en", self.name_en),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"channel {name} must be a non-empty string")
        if type(self.is_main) is not bool:
            raise TypeError("channel is_main must be a bool")


_RAW_CHANNELS: tuple[EqnChannelSpec, ...] = (
    EqnChannelSpec(
        key="main",
        telegram="EquationNews",
        name_zh="方程式新闻",
        name_en="EquationNews",
        is_main=True,
    ),
    EqnChannelSpec(
        key="pricechange",
        telegram="eqn_pricechange",
        name_zh="价格异动",
        name_en="Price Moves",
    ),
    EqnChannelSpec(
        key="major",
        telegram="eqn_major",
        name_zh="大币异动",
        name_en="Big Moves",
    ),
    EqnChannelSpec(
        key="tier2",
        telegram="eqn_tier2",
        name_zh="二线上新",
        name_en="Tier-2 Listings",
    ),
    EqnChannelSpec(
        key="tradfi",
        telegram="eqn_tradfi",
        name_zh="方程式财经",
        name_en="Equation TradFi",
    ),
    EqnChannelSpec(
        key="oi",
        telegram="eqn_oi",
        name_zh="持仓与价格异动",
        name_en="OI & Price Moves",
    ),
    EqnChannelSpec(
        key="cz",
        telegram="eqn_cz",
        name_zh="CZ 与何一动态",
        name_en="CZ & Yi He Watch",
    ),
    EqnChannelSpec(
        key="earn",
        telegram="eqn_earn",
        name_zh="理财公告",
        name_en="Earn Alerts",
    ),
    EqnChannelSpec(
        key="tier3",
        telegram="eqn_tier3",
        name_zh="三线上新",
        name_en="Tier-3 Listings",
    ),
    EqnChannelSpec(
        key="binance",
        telegram="eqn_binance",
        name_zh="币安监控",
        name_en="Binance Monitor",
    ),
    EqnChannelSpec(
        key="korea",
        telegram="eqn_korea",
        name_zh="韩国交易所",
        name_en="Korean Exchanges",
    ),
    EqnChannelSpec(
        key="alpha",
        telegram="eqn_alpha",
        name_zh="币安 Alpha",
        name_en="Binance Alpha",
    ),
    EqnChannelSpec(
        key="hl",
        telegram="eqn_hl",
        name_zh="Hyperliquid",
        name_en="Hyperliquid",
    ),
    EqnChannelSpec(
        key="tfmoves",
        telegram="eqn_tfmoves",
        name_zh="美股大盘异动",
        name_en="US Stock Big Moves",
    ),
)

EQN_CHANNELS: Final[Mapping[str, EqnChannelSpec]] = MappingProxyType(
    {spec.key: spec for spec in _RAW_CHANNELS}
)
EQN_CHANNEL_KEYS: Final[frozenset[str]] = frozenset(EQN_CHANNELS)


def _unique_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"EQN JSON contains duplicate key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise ValueError(f"EQN JSON contains non-finite numeric constant: {value}")


def strict_eqn_json(payload: bytes | str) -> dict[str, object]:
    """Decode EQN JSON strictly, preserving decimals as ``Decimal`` without floats."""
    raw_bytes = payload.encode("utf-8") if isinstance(payload, str) else payload
    if not isinstance(raw_bytes, bytes):
        raise TypeError("EQN payload must be bytes or str")
    if len(raw_bytes) > MAX_EQN_PAYLOAD_BYTES:
        raise ValueError("EQN payload exceeds maximum allowed size")
    try:
        decoded = json.loads(
            raw_bytes,
            parse_float=Decimal,
            parse_constant=_reject_constant,
            object_pairs_hook=_unique_fields,
        )
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ValueError("EQN payload is invalid or ambiguous JSON") from exc
    if not isinstance(decoded, dict):
        raise ValueError("EQN JSON root must be an object")
    return decoded


def to_fraction(value: object, *, name: str = "value") -> Fraction:
    """Convert an int, Decimal, Fraction, decimal string, or finite float into Fraction."""
    if isinstance(value, bool):
        raise ValueError(f"{name} must not be a boolean")
    if isinstance(value, Fraction):
        return value
    if type(value) is int:
        if abs(value) > 10**30:
            raise ValueError(f"{name} integer magnitude out of bounds")
        return Fraction(value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError(f"{name} must be finite")
        if value != 0 and abs(value.adjusted()) > 30:
            raise ValueError(f"{name} decimal exponent out of bounds")
        return Fraction(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            raise ValueError(f"{name} must not be empty")
        try:
            parsed = Decimal(stripped)
        except InvalidOperation as exc:
            raise ValueError(f"{name} is not a valid decimal string") from exc
        return to_fraction(parsed, name=name)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{name} float must be finite")
        return to_fraction(Decimal(str(value)), name=name)
    raise ValueError(f"{name} must be numeric, got {type(value).__name__}")


def _scale_round_half_up(value: object, scale: int, *, name: str) -> int:
    fraction = to_fraction(value, name=name)
    scaled = fraction * scale
    quotient, remainder = divmod(abs(scaled.numerator), scaled.denominator)
    if 2 * remainder >= scaled.denominator:
        quotient += 1
    return -quotient if scaled.numerator < 0 else quotient


def pct_to_bps(value: object, *, name: str = "pct") -> int:
    """Convert a percentage value (e.g. 30.013 -> 3001 bps) using exact rational arithmetic."""
    return _scale_round_half_up(value, BPS_SCALE, name=name)


def ratio_to_bps(value: object, *, name: str = "ratio") -> int:
    """Convert a unit ratio (e.g. 0.4216 -> 4216 bps) using exact rational arithmetic."""
    return _scale_round_half_up(value, RATIO_BPS_SCALE, name=name)


def usd_to_micros(value: object, *, name: str = "usd") -> int:
    """Convert a USD amount (e.g. 17041250.7 -> 17041250700000 micros) to non-negative micros."""
    micros = _scale_round_half_up(value, USD_MICROS_SCALE, name=name)
    if micros < 0:
        raise ValueError(f"{name} must be non-negative")
    return micros


def pct_to_e8(value: object, *, name: str = "pct_e8") -> int:
    """Convert a percent value into signed 1e-8 integer units (preserving sub-bps precision)."""
    return _scale_round_half_up(value, RATE_E8_SCALE, name=name)


def _validate_digest(digest: str, name: str = "raw_sha256") -> str:
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(ch not in "0123456789abcdef" for ch in digest)
    ):
        raise ValueError(f"{name} must be a lowercase 64-char SHA-256 hex digest")
    return digest


def _require_positive_int(value: object, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _require_non_negative_int(value: object, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _require_optional_int(value: object, name: str, *, non_negative: bool = False) -> int | None:
    if value is None:
        return None
    if type(value) is not int:
        raise TypeError(f"{name} must be an integer or None")
    if non_negative and value < 0:
        raise ValueError(f"{name} must be non-negative")
    return value


def _validate_timestamps(
    published_ts_ms: int,
    received_ts_ms: int,
    max_future_skew_ms: int,
) -> int:
    _require_positive_int(published_ts_ms, "published_ts_ms")
    _require_positive_int(received_ts_ms, "received_ts_ms")
    _require_non_negative_int(max_future_skew_ms, "max_future_skew_ms")
    skew_ms = published_ts_ms - received_ts_ms
    if skew_ms > max_future_skew_ms:
        raise ValueError(f"excessive future clock skew: {skew_ms} ms > {max_future_skew_ms} ms")
    return skew_ms


@dataclass(frozen=True, slots=True)
class EqnStructuredData:
    """Normalized integer-only structured signal fields from EQN `data` payloads."""

    signal: str = ""
    symbol: str = ""
    base: str = ""
    exchange: str = ""
    market: str = ""
    direction: str = ""
    window_s: int | None = None
    pct_bps: int | None = None
    price_pct_bps: int | None = None
    oi_pct_bps: int | None = None
    oi_usd_micros: int | None = None
    market_cap_usd_micros: int | None = None
    oi_mc_bps: int | None = None
    vol_24h_usd_micros: int | None = None
    funding_rate_bps: int | None = None
    funding_rate_e8: int | None = None
    funding_interval_h: int | None = None
    funding_next_ts_ms: int | None = None
    price_usd_micros: int | None = None
    base_price_usd_micros: int | None = None
    liquidity_rank: int | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("signal", self.signal),
            ("symbol", self.symbol),
            ("base", self.base),
            ("exchange", self.exchange),
            ("market", self.market),
            ("direction", self.direction),
        ):
            if not isinstance(value, str):
                raise TypeError(f"{name} must be a str")
        _require_optional_int(self.window_s, "window_s", non_negative=True)
        _require_optional_int(self.pct_bps, "pct_bps")
        _require_optional_int(self.price_pct_bps, "price_pct_bps")
        _require_optional_int(self.oi_pct_bps, "oi_pct_bps")
        _require_optional_int(self.oi_usd_micros, "oi_usd_micros", non_negative=True)
        _require_optional_int(
            self.market_cap_usd_micros, "market_cap_usd_micros", non_negative=True
        )
        _require_optional_int(self.oi_mc_bps, "oi_mc_bps", non_negative=True)
        _require_optional_int(self.vol_24h_usd_micros, "vol_24h_usd_micros", non_negative=True)
        _require_optional_int(self.funding_rate_bps, "funding_rate_bps")
        _require_optional_int(self.funding_rate_e8, "funding_rate_e8")
        _require_optional_int(self.funding_interval_h, "funding_interval_h", non_negative=True)
        _require_optional_int(self.funding_next_ts_ms, "funding_next_ts_ms", non_negative=True)
        _require_optional_int(self.price_usd_micros, "price_usd_micros", non_negative=True)
        _require_optional_int(
            self.base_price_usd_micros, "base_price_usd_micros", non_negative=True
        )
        _require_optional_int(self.liquidity_rank, "liquidity_rank", non_negative=True)
        if self.pct_bps is None and self.price_pct_bps is not None:
            object.__setattr__(self, "pct_bps", self.price_pct_bps)
        elif self.price_pct_bps is None and self.pct_bps is not None:
            object.__setattr__(self, "price_pct_bps", self.pct_bps)
        elif (
            self.pct_bps is not None
            and self.price_pct_bps is not None
            and self.pct_bps != self.price_pct_bps
        ):
            raise ValueError("conflicting pct_bps and price_pct_bps")

    @property
    def window(self) -> int | None:
        return self.window_s


@dataclass(frozen=True, slots=True)
class EqnNewsItem:
    """Validated Equation News REST item carrying raw SHA-256 and clock skew."""

    item_id: str
    published_ts_ms: int
    received_ts_ms: int
    raw_sha256: str
    channels: tuple[str, ...] = ()
    kind: str = "news"
    no: int | None = None
    path: str = ""
    source_slug: str = ""
    title_en: str = ""
    title_zh: str = ""
    coins: tuple[str, ...] = ()
    data: EqnStructuredData | None = None
    max_future_skew_ms: int = DEFAULT_MAX_FUTURE_SKEW_MS
    clock_skew_ms: int = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.item_id, str) or not self.item_id.strip():
            raise ValueError("item_id must be a non-empty string")
        _validate_digest(self.raw_sha256)
        if not isinstance(self.kind, str) or not self.kind.strip():
            raise ValueError("kind must be a non-empty string")
        if self.no is not None:
            _require_non_negative_int(self.no, "no")
        if not isinstance(self.path, str) or not isinstance(self.source_slug, str):
            raise TypeError("path and source_slug must be strings")
        if not isinstance(self.title_en, str) or not isinstance(self.title_zh, str):
            raise TypeError("titles must be strings")
        if not isinstance(self.channels, tuple) or any(
            not isinstance(ch, str) or not ch.strip() for ch in self.channels
        ):
            raise ValueError("channels must be a tuple of non-empty strings")
        if not isinstance(self.coins, tuple) or any(
            not isinstance(sym, str) or not sym.strip() for sym in self.coins
        ):
            raise ValueError("coins must be a tuple of non-empty strings")
        if self.data is not None and not isinstance(self.data, EqnStructuredData):
            raise TypeError("data must be EqnStructuredData or None")
        skew_ms = _validate_timestamps(
            self.published_ts_ms, self.received_ts_ms, self.max_future_skew_ms
        )
        object.__setattr__(self, "clock_skew_ms", skew_ms)

    @property
    def id(self) -> str:
        return self.item_id

    @property
    def title(self) -> str:
        return self.title_en or self.title_zh


@dataclass(frozen=True, slots=True)
class EqnWsPushFrame:
    """Validated WebSocket push message from `wss://api.eqn.news/public/ws`."""

    frame_id: str
    source_name: str
    news_title: str
    coins_included: tuple[str, ...]
    url: str
    published_ts_ms: int
    received_ts_ms: int
    raw_sha256: str
    max_future_skew_ms: int = DEFAULT_MAX_FUTURE_SKEW_MS
    clock_skew_ms: int = field(init=False)

    def __post_init__(self) -> None:
        for name, value in (
            ("frame_id", self.frame_id),
            ("source_name", self.source_name),
            ("news_title", self.news_title),
            ("url", self.url),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"WS frame {name} must be a non-empty string")
        _validate_digest(self.raw_sha256)
        if not isinstance(self.coins_included, tuple) or any(
            not isinstance(sym, str) or not sym.strip() for sym in self.coins_included
        ):
            raise ValueError("coins_included must be a tuple of non-empty strings")
        skew_ms = _validate_timestamps(
            self.published_ts_ms, self.received_ts_ms, self.max_future_skew_ms
        )
        object.__setattr__(self, "clock_skew_ms", skew_ms)


@dataclass(frozen=True, slots=True)
class EqnBatchCapture:
    """Immutable EQN REST news batch bound to raw payload SHA-256 and observation clock."""

    capture_id: str
    channel: str
    request_url: str
    items: tuple[EqnNewsItem, ...]
    published_ts_ms: int
    received_ts_ms: int
    raw_sha256: str
    next_cursor: str | None = None
    max_future_skew_ms: int = DEFAULT_MAX_FUTURE_SKEW_MS
    clock_skew_ms: int = field(init=False)

    def __post_init__(self) -> None:
        for name, value in (
            ("capture_id", self.capture_id),
            ("channel", self.channel),
            ("request_url", self.request_url),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"batch {name} must be a non-empty string")
        _validate_digest(self.raw_sha256)
        if not isinstance(self.items, tuple) or any(
            not isinstance(item, EqnNewsItem) for item in self.items
        ):
            raise TypeError("items must be a tuple of EqnNewsItem")
        if self.next_cursor is not None and not isinstance(self.next_cursor, str):
            raise TypeError("next_cursor must be str or None")
        skew_ms = _validate_timestamps(
            self.published_ts_ms, self.received_ts_ms, self.max_future_skew_ms
        )
        object.__setattr__(self, "clock_skew_ms", skew_ms)


def _optional_str(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"EQN field {key} must be a string")
    return value


def _optional_int_field(
    mapping: Mapping[str, object], key: str, *, non_negative: bool = False
) -> int | None:
    value = mapping.get(key)
    if value is None:
        return None
    if type(value) is not int:
        raise ValueError(f"EQN field {key} must be an integer")
    if non_negative and value < 0:
        raise ValueError(f"EQN field {key} must be non-negative")
    return value


def parse_structured_data(raw_data: Mapping[str, object]) -> EqnStructuredData:
    """Parse EQN item `data` object into exact fixed-point integer fields."""
    if not isinstance(raw_data, Mapping):
        raise ValueError("EQN data field must be an object")

    raw_price_pct = raw_data.get("price_pct")
    raw_pct = raw_data.get("pct")
    price_pct_bps = (
        pct_to_bps(raw_price_pct, name="price_pct") if raw_price_pct is not None else None
    )
    pct_bps = pct_to_bps(raw_pct, name="pct") if raw_pct is not None else None

    raw_oi_pct = raw_data.get("oi_pct")
    oi_pct_bps = pct_to_bps(raw_oi_pct, name="oi_pct") if raw_oi_pct is not None else None

    raw_oi_usd = raw_data.get("oi_usd")
    oi_usd_micros = usd_to_micros(raw_oi_usd, name="oi_usd") if raw_oi_usd is not None else None

    raw_mcap = raw_data.get("market_cap")
    market_cap_usd_micros = (
        usd_to_micros(raw_mcap, name="market_cap") if raw_mcap is not None else None
    )

    raw_ratio = raw_data.get("oi_mc_ratio")
    oi_mc_bps = ratio_to_bps(raw_ratio, name="oi_mc_ratio") if raw_ratio is not None else None
    if oi_mc_bps is not None and oi_mc_bps < 0:
        raise ValueError("oi_mc_ratio must be non-negative")

    raw_vol = raw_data.get("vol_24h_usd")
    vol_24h_usd_micros = usd_to_micros(raw_vol, name="vol_24h_usd") if raw_vol is not None else None

    raw_funding = raw_data.get("funding_rate_pct")
    funding_rate_bps = (
        pct_to_bps(raw_funding, name="funding_rate_pct") if raw_funding is not None else None
    )
    funding_rate_e8 = (
        pct_to_e8(raw_funding, name="funding_rate_pct") if raw_funding is not None else None
    )

    raw_price = raw_data.get("price")
    price_usd_micros = usd_to_micros(raw_price, name="price") if raw_price is not None else None

    raw_base_price = raw_data.get("base_price")
    base_price_usd_micros = (
        usd_to_micros(raw_base_price, name="base_price") if raw_base_price is not None else None
    )

    return EqnStructuredData(
        signal=_optional_str(raw_data, "signal"),
        symbol=_optional_str(raw_data, "symbol"),
        base=_optional_str(raw_data, "base"),
        exchange=_optional_str(raw_data, "exchange"),
        market=_optional_str(raw_data, "market"),
        direction=_optional_str(raw_data, "direction"),
        window_s=_optional_int_field(raw_data, "window", non_negative=True),
        pct_bps=pct_bps,
        price_pct_bps=price_pct_bps,
        oi_pct_bps=oi_pct_bps,
        oi_usd_micros=oi_usd_micros,
        market_cap_usd_micros=market_cap_usd_micros,
        oi_mc_bps=oi_mc_bps,
        vol_24h_usd_micros=vol_24h_usd_micros,
        funding_rate_bps=funding_rate_bps,
        funding_rate_e8=funding_rate_e8,
        funding_interval_h=_optional_int_field(raw_data, "funding_interval_h", non_negative=True),
        funding_next_ts_ms=_optional_int_field(raw_data, "funding_next_ts", non_negative=True),
        price_usd_micros=price_usd_micros,
        base_price_usd_micros=base_price_usd_micros,
        liquidity_rank=_optional_int_field(raw_data, "liquidity_rank", non_negative=True),
    )


def _extract_coins(
    raw_item: Mapping[str, object], data: EqnStructuredData | None
) -> tuple[str, ...]:
    raw_coins = raw_item.get("coins")
    seen: dict[str, None] = {}
    if raw_coins is not None:
        if not isinstance(raw_coins, list):
            raise ValueError("EQN coins field must be a list")
        for entry in raw_coins:
            if isinstance(entry, str):
                symbol = entry.strip().upper()
            elif isinstance(entry, Mapping):
                raw_sym = entry.get("symbol")
                if not isinstance(raw_sym, str):
                    raise ValueError("EQN coin entry requires string symbol")
                symbol = raw_sym.strip().upper()
            else:
                raise ValueError("EQN coin entry must be str or object")
            if symbol:
                seen.setdefault(symbol, None)
    if data is not None and data.base.strip():
        seen.setdefault(data.base.strip().upper(), None)
    return tuple(seen)


def parse_news_item(
    raw_item: Mapping[str, object],
    *,
    received_ts_ms: int,
    raw_sha256: str,
    max_future_skew_ms: int = DEFAULT_MAX_FUTURE_SKEW_MS,
) -> EqnNewsItem:
    """Parse and validate a single EQN news item object."""
    if not isinstance(raw_item, Mapping):
        raise ValueError("EQN item must be a JSON object")
    raw_id = raw_item.get("id")
    if isinstance(raw_id, int) and not isinstance(raw_id, bool):
        item_id = str(raw_id)
    elif isinstance(raw_id, str):
        item_id = raw_id.strip()
    else:
        raise ValueError("EQN item id must be a non-empty string or integer")

    published_ts_ms = raw_item.get("published_ts")
    if type(published_ts_ms) is not int or published_ts_ms <= 0:
        raise ValueError("EQN item published_ts must be a positive integer")

    raw_channels = raw_item.get("channels")
    if raw_channels is None:
        channels: tuple[str, ...] = ()
    else:
        if not isinstance(raw_channels, list):
            raise ValueError("EQN item channels must be a list")
        parsed_channels: list[str] = []
        for ch in raw_channels:
            if not isinstance(ch, str) or not ch.strip():
                raise ValueError("EQN channel key must be a non-empty string")
            parsed_channels.append(ch.strip())
        channels = tuple(parsed_channels)

    kind_raw = raw_item.get("kind", "news")
    if not isinstance(kind_raw, str) or not kind_raw.strip():
        raise ValueError("EQN item kind must be a non-empty string")

    raw_source = raw_item.get("source")
    source_slug = ""
    if isinstance(raw_source, Mapping):
        source_slug = _optional_str(raw_source, "slug")
    elif isinstance(raw_source, str):
        source_slug = raw_source
    elif raw_source is not None:
        raise ValueError("EQN item source must be an object or string")

    raw_i18n = raw_item.get("i18n")
    title_en = ""
    title_zh = ""
    if raw_i18n is not None:
        if not isinstance(raw_i18n, Mapping):
            raise ValueError("EQN item i18n must be an object")
        title_en = _optional_str(raw_i18n, "en")
        title_zh = _optional_str(raw_i18n, "zh")
    if not title_en:
        title_en = _optional_str(raw_item, "title")

    raw_data = raw_item.get("data")
    data: EqnStructuredData | None = None
    if raw_data is not None:
        if not isinstance(raw_data, Mapping):
            raise ValueError("EQN item data must be an object")
        data = parse_structured_data(raw_data)

    coins = _extract_coins(raw_item, data)
    return EqnNewsItem(
        item_id=item_id,
        no=_optional_int_field(raw_item, "no", non_negative=True),
        path=_optional_str(raw_item, "path"),
        published_ts_ms=published_ts_ms,
        received_ts_ms=received_ts_ms,
        raw_sha256=raw_sha256,
        channels=channels,
        kind=kind_raw.strip(),
        source_slug=source_slug,
        title_en=title_en,
        title_zh=title_zh,
        coins=coins,
        data=data,
        max_future_skew_ms=max_future_skew_ms,
    )


def parse_news_payload(
    raw_bytes: bytes | str,
    *,
    channel: str = "all",
    request_url: str = "https://eqn.news/api/v1/news",
    received_ts_ms: int,
    max_future_skew_ms: int = DEFAULT_MAX_FUTURE_SKEW_MS,
) -> EqnBatchCapture:
    """Parse an EQN REST `/api/v1/news` payload into an immutable ``EqnBatchCapture``."""
    payload_bytes = raw_bytes.encode("utf-8") if isinstance(raw_bytes, str) else raw_bytes
    raw_sha256 = sha256_hex(payload_bytes)
    envelope = strict_eqn_json(payload_bytes)
    raw_items = envelope.get("items")
    if not isinstance(raw_items, list):
        raise ValueError("EQN response requires an items array")
    items = tuple(
        parse_news_item(
            raw_item,
            received_ts_ms=received_ts_ms,
            raw_sha256=raw_sha256,
            max_future_skew_ms=max_future_skew_ms,
        )
        for raw_item in raw_items
    )
    next_cursor_raw = envelope.get("next_cursor")
    if next_cursor_raw is not None and not isinstance(next_cursor_raw, str):
        raise ValueError("EQN next_cursor must be a string or null")
    published_ts_ms = max((item.published_ts_ms for item in items), default=received_ts_ms)
    capture_id = f"eqn:{channel}:{received_ts_ms}:{raw_sha256[:12]}"
    return EqnBatchCapture(
        capture_id=capture_id,
        channel=channel,
        request_url=request_url,
        items=items,
        published_ts_ms=published_ts_ms,
        received_ts_ms=received_ts_ms,
        raw_sha256=raw_sha256,
        next_cursor=next_cursor_raw,
        max_future_skew_ms=max_future_skew_ms,
    )
