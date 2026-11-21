"""Keyless Bybit Spot V5 public L2 orderbook and RFC-6455 TLS WebSocket trade-flow client.

Fetches public Bybit Spot V5 orderbooks (`GET /v5/market/orderbook`), recent trades
(`GET /v5/market/recent-trade`), and live `publicTrade.<SYMBOL>` WebSocket prints
(`wss://stream.bybit.com/v5/public/spot`) across Base ecosystem pairs, normalizing
USDT quotes to USD via a live fail-closed Coinbase `USDT-USD` peg (`0.95 <= bid < ask <= 1.05`).

All outputs are strictly read-only shadow market data (`execution_eligible = False`).
"""

import base64
import hmac
import json
import math
import os
import re
import select
import ssl
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final, Literal, Self, cast

import certifi

from ..models.common import sha256_hex
from .coinbase_trade_flow import (
    _WS_OP_BINARY,
    _WS_OP_CLOSE,
    _WS_OP_CONTINUATION,
    _WS_OP_PING,
    _WS_OP_PONG,
    _WS_OP_TEXT,
    CoinbaseTradeFlowError,
    CoinbaseTradePrint,
    _connect_tls_ipv4_preferred,
    _decode_json_strict,
    _resolve_and_validate_addrs,
    _to_strict_positive_decimal,
    build_websocket_frame,
    compute_websocket_accept,
)
from .live_cex_dex_shadow import (
    _USER_AGENT,
    CoinbaseL2Book,
    CoinbaseL2Level,
    LiveCexDexError,
    _IPv4PreferredHTTPSHandler,
    resolve_coinbase_product,
)

BYBIT_REST_ALLOWED_HOSTS: Final[frozenset[str]] = frozenset({"api.bybit.com"})
BYBIT_WS_HOST: Final[str] = "stream.bybit.com"
BYBIT_WS_PORT: Final[int] = 443
BYBIT_WS_PATH: Final[str] = "/v5/public/spot"
BYBIT_WS_ALLOWED_HOSTS: Final[frozenset[str]] = frozenset({BYBIT_WS_HOST})
USDT_PEG_ALLOWED_HOSTS: Final[frozenset[str]] = frozenset({"api.exchange.coinbase.com"})
USDT_PEG_URL: Final[str] = "https://api.exchange.coinbase.com/products/USDT-USD/book?level=1"
BYBIT_ORDERBOOK_URL_TEMPLATE: Final[str] = (
    "https://api.bybit.com/v5/market/orderbook?category=spot&symbol={symbol}&limit={limit}"
)
BYBIT_RECENT_TRADES_URL_TEMPLATE: Final[str] = (
    "https://api.bybit.com/v5/market/recent-trade?category=spot&symbol={symbol}&limit={limit}"
)
MAX_BYBIT_RESPONSE_BYTES: Final[int] = 1_048_576
MAX_BYBIT_WS_FRAME_BYTES: Final[int] = 1_048_576
USDT_PEG_MIN: Final[Decimal] = Decimal("0.95")
USDT_PEG_MAX: Final[Decimal] = Decimal("1.05")

_MAX_HANDSHAKE_BYTES: Final[int] = 16_384
_RECV_CHUNK_BYTES: Final[int] = 65_536
_PING_INTERVAL_S: Final[float] = 15.0
_DIGITS_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9]+$")
_SHA256_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")

BYBIT_SYMBOL_BY_PRODUCT: Final[dict[str, str]] = {
    "ETH-USD": "ETHUSDT",
    "BTC-USD": "BTCUSDT",
    "AERO-USD": "AEROUSDT",
    "VIRTUAL-USD": "VIRTUALUSDT",
    "VVV-USD": "VVVUSDT",
    "MORPHO-USD": "MORPHOUSDT",
    "DEGEN-USD": "DEGENUSDT",
}
PRODUCT_BY_BYBIT_SYMBOL: Final[dict[str, str]] = {
    sym: prod for prod, sym in BYBIT_SYMBOL_BY_PRODUCT.items()
}
SUPPORTED_BYBIT_PRODUCTS: Final[tuple[str, ...]] = tuple(BYBIT_SYMBOL_BY_PRODUCT.keys())


class BybitSpotFeedError(ValueError):
    """Fail-closed error on Bybit Spot V5 transport, peg validation, or payload parsing."""


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        if fp is not None:
            with suppress(OSError, AttributeError):
                fp.close()
        raise BybitSpotFeedError("Bybit spot feed redirects are forbidden")


def _validate_timeout(timeout_s: float) -> float:
    if (
        isinstance(timeout_s, bool)
        or not isinstance(timeout_s, (int, float))
        or not math.isfinite(float(timeout_s))
        or float(timeout_s) <= 0.0
    ):
        raise BybitSpotFeedError("timeout_s must be a positive finite number")
    return float(timeout_s)


def resolve_bybit_symbol(product_or_symbol: str) -> tuple[str, str]:
    """Resolve a canonical product ID or Base token alias to `(product_id, bybit_symbol)`."""
    try:
        spec = resolve_coinbase_product(product_or_symbol)
    except ValueError as exc:
        raise BybitSpotFeedError(str(exc)) from exc
    bybit_sym = BYBIT_SYMBOL_BY_PRODUCT.get(spec.product_id)
    if bybit_sym is None:
        raise BybitSpotFeedError(f"product {spec.product_id!r} is not supported on Bybit Spot")
    return spec.product_id, bybit_sym


def _validate_https_url(url: str, *, allowed_hosts: frozenset[str], label: str) -> str:
    if not isinstance(url, str) or not url.strip():
        raise BybitSpotFeedError(f"{label} URL must be a non-empty string")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https":
        raise BybitSpotFeedError(f"{label} URL must use https")
    if parsed.username or parsed.password:
        raise BybitSpotFeedError(f"{label} URL must not embed credentials")
    if parsed.fragment:
        raise BybitSpotFeedError(f"{label} URL must not contain fragments")
    host = (parsed.hostname or "").lower()
    if host not in allowed_hosts:
        raise BybitSpotFeedError(f"{label} host {host!r} is not allowlisted")
    if parsed.port not in (None, 443):
        raise BybitSpotFeedError(f"{label} URL must use default HTTPS port 443")
    return url


def _default_https_get(
    url: str,
    timeout_s: float,
    *,
    allowed_hosts: frozenset[str],
    label: str,
) -> bytes:
    _validate_https_url(url, allowed_hosts=allowed_hosts, label=label)
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or "").lower()
    try:
        _resolve_and_validate_addrs(host, 443)
    except CoinbaseTradeFlowError as exc:
        raise BybitSpotFeedError(str(exc)) from exc

    tls_ctx = ssl.create_default_context(cafile=certifi.where())
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _NoRedirectHandler(),
        _IPv4PreferredHTTPSHandler(context=tls_ctx),
    )
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": _USER_AGENT},
        method="GET",
    )
    try:
        with opener.open(req, timeout=timeout_s) as resp:
            status = getattr(resp, "status", None)
            if status != 200:
                raise BybitSpotFeedError(f"{label} returned HTTP status {status}")
            raw = resp.read(MAX_BYBIT_RESPONSE_BYTES + 1)
    except BybitSpotFeedError:
        raise
    except (urllib.error.URLError, OSError, ssl.SSLError, TimeoutError) as exc:
        raise BybitSpotFeedError(f"{label} transport failed: {exc}") from exc
    if not raw or len(raw) > MAX_BYBIT_RESPONSE_BYTES:
        raise BybitSpotFeedError(f"{label} response exceeds {MAX_BYBIT_RESPONSE_BYTES} byte limit")
    return bytes(raw)


def _decode_json_object(raw_bytes: bytes, label: str) -> dict[str, object]:
    if not isinstance(raw_bytes, bytes) or not raw_bytes:
        raise BybitSpotFeedError(f"{label} payload must be non-empty bytes")
    if len(raw_bytes) > MAX_BYBIT_RESPONSE_BYTES:
        raise BybitSpotFeedError(f"{label} payload exceeds {MAX_BYBIT_RESPONSE_BYTES} bytes")
    try:
        parsed = _decode_json_strict(raw_bytes)
    except CoinbaseTradeFlowError as exc:
        raise BybitSpotFeedError(f"{label}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise BybitSpotFeedError(f"{label} JSON root must be an object")
    return cast(dict[str, object], parsed)


def _strict_pos_decimal(value: object, name: str) -> Decimal:
    try:
        return _to_strict_positive_decimal(value, name)
    except CoinbaseTradeFlowError as exc:
        raise BybitSpotFeedError(str(exc)) from exc


def _parse_positive_int_id(value: object, name: str) -> int:
    if type(value) is int:
        if value <= 0:
            raise BybitSpotFeedError(f"{name} must be a positive integer")
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if not _DIGITS_RE.fullmatch(stripped):
            raise BybitSpotFeedError(f"{name} must be a positive base-10 integer string")
        parsed = int(stripped)
        if parsed <= 0:
            raise BybitSpotFeedError(f"{name} must be a positive integer")
        return parsed
    raise BybitSpotFeedError(f"{name} must be a positive integer or numeric string")


def _ms_to_utc_iso(raw_ms: object, name: str) -> str:
    ms_int = _parse_positive_int_id(raw_ms, name)
    dt = datetime.fromtimestamp(ms_int / 1000.0, tz=UTC)
    return dt.isoformat().replace("+00:00", "Z")


def _bybit_taker_side_to_maker_side(raw_side: object, context: str) -> Literal["buy", "sell"]:
    """Convert Bybit taker side (`Buy`/`Sell`) to resting maker side (`sell`/`buy`)."""
    if raw_side == "Buy":
        return "sell"
    if raw_side == "Sell":
        return "buy"
    raise BybitSpotFeedError(f"{context}.side must be 'Buy' or 'Sell', got {raw_side!r}")


@dataclass(frozen=True, slots=True)
class UsdtUsdPeg:
    """Validated live USDT/USD conversion peg from Coinbase `USDT-USD` BBO."""

    bid: Decimal
    ask: Decimal
    mid: Decimal
    retrieved_at_utc: str
    raw_sha256: str
    execution_eligible: Literal[False] = False

    def __post_init__(self) -> None:
        if self.execution_eligible is not False:
            raise BybitSpotFeedError("UsdtUsdPeg cannot set execution_eligible=True")
        bid_dec = _strict_pos_decimal(self.bid, "usdt_peg.bid")
        ask_dec = _strict_pos_decimal(self.ask, "usdt_peg.ask")
        mid_dec = _strict_pos_decimal(self.mid, "usdt_peg.mid")
        if bid_dec < USDT_PEG_MIN or ask_dec > USDT_PEG_MAX:
            raise BybitSpotFeedError(
                f"USDT-USD peg outside safety band [{USDT_PEG_MIN}, {USDT_PEG_MAX}]: "
                f"bid={bid_dec}, ask={ask_dec}"
            )
        if bid_dec >= ask_dec:
            raise BybitSpotFeedError(
                f"USDT-USD peg book is crossed: bid={bid_dec} >= ask={ask_dec}"
            )
        if not (bid_dec <= mid_dec <= ask_dec):
            raise BybitSpotFeedError("usdt_peg.mid must lie between bid and ask")
        if not isinstance(self.retrieved_at_utc, str) or not self.retrieved_at_utc.strip():
            raise BybitSpotFeedError("usdt_peg.retrieved_at_utc must be non-empty")
        if not isinstance(self.raw_sha256, str) or _SHA256_RE.fullmatch(self.raw_sha256) is None:
            raise BybitSpotFeedError("usdt_peg.raw_sha256 must be a lowercase 64-hex SHA-256")
        object.__setattr__(self, "bid", bid_dec)
        object.__setattr__(self, "ask", ask_dec)
        object.__setattr__(self, "mid", mid_dec)


def parse_usdt_usd_peg_payload(
    raw_bytes: bytes,
    *,
    retrieved_at_utc: str | None = None,
) -> UsdtUsdPeg:
    """Parse Coinbase `USDT-USD` level-1 book payload into a fail-closed `UsdtUsdPeg`."""
    obj = _decode_json_object(raw_bytes, "USDT-USD peg")
    bids = obj.get("bids")
    asks = obj.get("asks")
    if not isinstance(bids, list) or not bids or not isinstance(asks, list) or not asks:
        raise BybitSpotFeedError("USDT-USD peg payload missing bids or asks")
    top_bid = bids[0]
    top_ask = asks[0]
    if (
        not isinstance(top_bid, list)
        or len(top_bid) < 2
        or not isinstance(top_ask, list)
        or len(top_ask) < 2
    ):
        raise BybitSpotFeedError("USDT-USD peg top level malformed")
    bid_dec = _strict_pos_decimal(top_bid[0], "usdt_peg.bid")
    ask_dec = _strict_pos_decimal(top_ask[0], "usdt_peg.ask")
    mid_dec = (bid_dec + ask_dec) / Decimal(2)
    ts = (
        retrieved_at_utc
        if retrieved_at_utc is not None
        else datetime.now(UTC).isoformat().replace("+00:00", "Z")
    )
    return UsdtUsdPeg(
        bid=bid_dec,
        ask=ask_dec,
        mid=mid_dec,
        retrieved_at_utc=ts,
        raw_sha256=sha256_hex(raw_bytes),
    )


def fetch_usdt_usd_peg(
    *,
    timeout_s: float = 5.0,
    transport: Callable[[str, float], bytes] | None = None,
) -> UsdtUsdPeg:
    """Fetch live Coinbase `USDT-USD` level-1 peg with fail-closed depeg guard."""
    checked_timeout = _validate_timeout(timeout_s)
    if transport is not None:
        raw_bytes = transport(USDT_PEG_URL, checked_timeout)
    else:
        raw_bytes = _default_https_get(
            USDT_PEG_URL,
            checked_timeout,
            allowed_hosts=USDT_PEG_ALLOWED_HOSTS,
            label="USDT-USD peg",
        )
    return parse_usdt_usd_peg_payload(raw_bytes)


def parse_bybit_orderbook_payload(
    product_or_symbol: str,
    raw_bytes: bytes,
    *,
    usdt_peg: UsdtUsdPeg,
    retrieved_at_utc: str | None = None,
) -> CoinbaseL2Book:
    """Parse Bybit V5 `/v5/market/orderbook` JSON into a USD-normalized `CoinbaseL2Book`."""
    if not isinstance(usdt_peg, UsdtUsdPeg):
        raise BybitSpotFeedError("usdt_peg must be a UsdtUsdPeg instance")
    canonical_pid, bybit_sym = resolve_bybit_symbol(product_or_symbol)
    obj = _decode_json_object(raw_bytes, f"Bybit orderbook {bybit_sym}")
    if obj.get("retCode") != 0:
        raise BybitSpotFeedError(
            f"Bybit orderbook {bybit_sym} error retCode={obj.get('retCode')!r}"
        )
    result = obj.get("result")
    if not isinstance(result, dict):
        raise BybitSpotFeedError(f"Bybit orderbook {bybit_sym} missing result object")
    if result.get("s") != bybit_sym:
        raise BybitSpotFeedError(
            f"Bybit orderbook symbol mismatch: expected {bybit_sym}, got {result.get('s')!r}"
        )
    raw_bids = result.get("b")
    raw_asks = result.get("a")
    if (
        not isinstance(raw_bids, list)
        or not raw_bids
        or not isinstance(raw_asks, list)
        or not raw_asks
    ):
        raise BybitSpotFeedError(f"Bybit orderbook {bybit_sym} has empty bids or asks")

    bids: list[CoinbaseL2Level] = []
    for idx, entry in enumerate(raw_bids):
        if not isinstance(entry, list) or len(entry) < 2:
            raise BybitSpotFeedError(f"Bybit orderbook {bybit_sym} bid[{idx}] malformed")
        px_usdt = _strict_pos_decimal(entry[0], f"bid[{idx}].price")
        sz = _strict_pos_decimal(entry[1], f"bid[{idx}].size")
        bids.append(CoinbaseL2Level(price=px_usdt * usdt_peg.mid, size=sz, num_orders=1))

    asks: list[CoinbaseL2Level] = []
    for idx, entry in enumerate(raw_asks):
        if not isinstance(entry, list) or len(entry) < 2:
            raise BybitSpotFeedError(f"Bybit orderbook {bybit_sym} ask[{idx}] malformed")
        px_usdt = _strict_pos_decimal(entry[0], f"ask[{idx}].price")
        sz = _strict_pos_decimal(entry[1], f"ask[{idx}].size")
        asks.append(CoinbaseL2Level(price=px_usdt * usdt_peg.mid, size=sz, num_orders=1))

    raw_seq = result.get("u")
    seq: int | None
    if raw_seq is None:
        seq = None
    elif type(raw_seq) is int and raw_seq >= 0:
        seq = raw_seq
    else:
        raise BybitSpotFeedError(f"Bybit orderbook {bybit_sym} invalid sequence {raw_seq!r}")

    ts = (
        retrieved_at_utc
        if retrieved_at_utc is not None
        else (
            _ms_to_utc_iso(result["ts"], "orderbook.ts")
            if "ts" in result and result["ts"] is not None
            else datetime.now(UTC).isoformat().replace("+00:00", "Z")
        )
    )
    try:
        return CoinbaseL2Book(
            product_id=canonical_pid,
            bids=tuple(bids),
            asks=tuple(asks),
            sequence=seq,
            retrieved_at_utc=ts,
            raw_sha256=sha256_hex(raw_bytes),
            book_level=2,
        )
    except LiveCexDexError as exc:
        raise BybitSpotFeedError(str(exc)) from exc


def fetch_bybit_l2_book(
    product_or_symbol: str,
    *,
    usdt_peg: UsdtUsdPeg,
    limit: int = 25,
    timeout_s: float = 5.0,
    transport: Callable[[str, float], bytes] | None = None,
) -> CoinbaseL2Book:
    """Fetch and normalize a Bybit Spot V5 L2 orderbook into USD terms."""
    checked_timeout = _validate_timeout(timeout_s)
    if type(limit) is not int or not 1 <= limit <= 50:
        raise BybitSpotFeedError("limit must be an integer in 1..50")
    canonical_pid, bybit_sym = resolve_bybit_symbol(product_or_symbol)
    url = BYBIT_ORDERBOOK_URL_TEMPLATE.format(symbol=bybit_sym, limit=limit)
    if transport is not None:
        raw_bytes = transport(url, checked_timeout)
    else:
        raw_bytes = _default_https_get(
            url,
            checked_timeout,
            allowed_hosts=BYBIT_REST_ALLOWED_HOSTS,
            label=f"Bybit orderbook {bybit_sym}",
        )
    return parse_bybit_orderbook_payload(canonical_pid, raw_bytes, usdt_peg=usdt_peg)


def parse_bybit_recent_trades_payload(
    product_or_symbol: str,
    raw_bytes: bytes,
    *,
    usdt_peg: UsdtUsdPeg,
    since_trade_id: int | None = None,
) -> tuple[CoinbaseTradePrint, ...]:
    """Parse Bybit V5 `/v5/market/recent-trade` JSON into USD-normalized trade prints."""
    if not isinstance(usdt_peg, UsdtUsdPeg):
        raise BybitSpotFeedError("usdt_peg must be a UsdtUsdPeg instance")
    if since_trade_id is not None and (type(since_trade_id) is not int or since_trade_id < 0):
        raise BybitSpotFeedError("since_trade_id must be a non-negative integer or None")
    canonical_pid, bybit_sym = resolve_bybit_symbol(product_or_symbol)
    obj = _decode_json_object(raw_bytes, f"Bybit recent-trade {bybit_sym}")
    if obj.get("retCode") != 0:
        raise BybitSpotFeedError(
            f"Bybit recent-trade {bybit_sym} error retCode={obj.get('retCode')!r}"
        )
    result = obj.get("result")
    if not isinstance(result, dict):
        raise BybitSpotFeedError(f"Bybit recent-trade {bybit_sym} missing result object")
    rows = result.get("list")
    if not isinstance(rows, list):
        raise BybitSpotFeedError(f"Bybit recent-trade {bybit_sym} missing list")

    by_id: dict[int, CoinbaseTradePrint] = {}
    for idx, row in enumerate(rows):
        if not isinstance(row, dict):
            raise BybitSpotFeedError(f"Bybit recent-trade[{idx}] must be an object")
        if row.get("symbol") != bybit_sym:
            raise BybitSpotFeedError(
                f"Bybit recent-trade[{idx}] symbol mismatch: {row.get('symbol')!r}"
            )
        tid = _parse_positive_int_id(row.get("execId"), f"recent-trade[{idx}].execId")
        if since_trade_id is not None and tid <= since_trade_id:
            continue
        px_usd = _strict_pos_decimal(row.get("price"), f"recent-trade[{idx}].price") * usdt_peg.mid
        sz = _strict_pos_decimal(row.get("size"), f"recent-trade[{idx}].size")
        maker_side = _bybit_taker_side_to_maker_side(row.get("side"), f"recent-trade[{idx}]")
        traded_at = _ms_to_utc_iso(row.get("time"), f"recent-trade[{idx}].time")
        tp = CoinbaseTradePrint(
            product_id=canonical_pid,
            trade_id=tid,
            price=px_usd,
            size=sz,
            maker_side=maker_side,
            traded_at_utc=traded_at,
        )
        existing = by_id.get(tid)
        if existing is not None and existing != tp:
            raise BybitSpotFeedError(f"conflicting Bybit trade_id {tid} for {bybit_sym}")
        by_id[tid] = tp

    return tuple(sorted(by_id.values(), key=lambda item: item.trade_id))


def fetch_bybit_recent_trades(
    product_or_symbol: str,
    *,
    usdt_peg: UsdtUsdPeg,
    limit: int = 60,
    since_trade_id: int | None = None,
    timeout_s: float = 5.0,
    transport: Callable[[str, float], bytes] | None = None,
) -> tuple[CoinbaseTradePrint, ...]:
    """Fetch recent public Bybit Spot V5 trades for REST gap fallback."""
    checked_timeout = _validate_timeout(timeout_s)
    if type(limit) is not int or not 1 <= limit <= 60:
        raise BybitSpotFeedError("limit must be an integer in 1..60")
    canonical_pid, bybit_sym = resolve_bybit_symbol(product_or_symbol)
    url = BYBIT_RECENT_TRADES_URL_TEMPLATE.format(symbol=bybit_sym, limit=limit)
    if transport is not None:
        raw_bytes = transport(url, checked_timeout)
    else:
        raw_bytes = _default_https_get(
            url,
            checked_timeout,
            allowed_hosts=BYBIT_REST_ALLOWED_HOSTS,
            label=f"Bybit recent-trade {bybit_sym}",
        )
    return parse_bybit_recent_trades_payload(
        canonical_pid,
        raw_bytes,
        usdt_peg=usdt_peg,
        since_trade_id=since_trade_id,
    )


class BybitSpotWebSocketFeed:
    """Pure-stdlib RFC-6455 TLS WebSocket client for Bybit Spot `publicTrade` prints."""

    def __init__(
        self,
        products: Sequence[str],
        *,
        usdt_peg: UsdtUsdPeg,
        timeout_s: float = 10.0,
        socket_factory: Callable[[str, int, float], Any] | None = None,
    ) -> None:
        if isinstance(products, (str, bytes)) or not products:
            raise BybitSpotFeedError("products must be a non-empty sequence of product IDs")
        if not isinstance(usdt_peg, UsdtUsdPeg):
            raise BybitSpotFeedError("usdt_peg must be a UsdtUsdPeg instance")
        self._timeout_s = _validate_timeout(timeout_s)
        self._usdt_peg = usdt_peg
        self._socket_factory = socket_factory

        resolved_pids: list[str] = []
        resolved_syms: list[str] = []
        seen: set[str] = set()
        for raw_pid in products:
            pid, sym = resolve_bybit_symbol(raw_pid)
            if pid not in seen:
                seen.add(pid)
                resolved_pids.append(pid)
                resolved_syms.append(sym)
        self._products: tuple[str, ...] = tuple(resolved_pids)
        self._bybit_symbols: tuple[str, ...] = tuple(resolved_syms)
        self._sock: Any | None = None
        self._recv_buf = bytearray()
        self._frag_opcode: int | None = None
        self._frag_chunks: list[bytes] = []
        self._watermarks: dict[str, int] = {}
        self._last_ping_mono: float = 0.0
        self._connected: bool = False

    @property
    def products(self) -> tuple[str, ...]:
        """Canonical USD product IDs subscribed on this WebSocket."""
        return self._products

    @property
    def is_connected(self) -> bool:
        """Whether the underlying TLS WebSocket is currently open."""
        return self._connected and self._sock is not None

    def update_usdt_peg(self, usdt_peg: UsdtUsdPeg) -> None:
        """Update the live USDT/USD peg applied to subsequent WebSocket trade prints."""
        if not isinstance(usdt_peg, UsdtUsdPeg):
            raise BybitSpotFeedError("usdt_peg must be a UsdtUsdPeg instance")
        self._usdt_peg = usdt_peg

    def __enter__(self) -> Self:
        self.connect()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def connect(self) -> None:
        """Open TLS socket, complete RFC-6455 handshake, and subscribe to `publicTrade`."""
        self.close()
        if BYBIT_WS_HOST not in BYBIT_WS_ALLOWED_HOSTS:
            raise BybitSpotFeedError(f"Bybit WS host {BYBIT_WS_HOST!r} is not allowlisted")

        sock: Any
        if self._socket_factory is not None:
            sock = self._socket_factory(BYBIT_WS_HOST, BYBIT_WS_PORT, self._timeout_s)
        else:
            try:
                sock = _connect_tls_ipv4_preferred(BYBIT_WS_HOST, BYBIT_WS_PORT, self._timeout_s)
            except (CoinbaseTradeFlowError, OSError, ssl.SSLError, TimeoutError) as exc:
                raise BybitSpotFeedError(f"Bybit WS connect failed: {exc}") from exc

        try:
            ws_key = base64.b64encode(os.urandom(16)).decode("ascii")
            expected_accept = compute_websocket_accept(ws_key)
            handshake = (
                f"GET {BYBIT_WS_PATH} HTTP/1.1\r\n"
                f"Host: {BYBIT_WS_HOST}\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {ws_key}\r\n"
                "Sec-WebSocket-Version: 13\r\n"
                f"User-Agent: {_USER_AGENT}\r\n\r\n"
            ).encode("ascii")
            sock.settimeout(self._timeout_s)
            sock.sendall(handshake)

            raw_resp = bytearray()
            while b"\r\n\r\n" not in raw_resp:
                chunk = sock.recv(4096)
                if not chunk:
                    raise BybitSpotFeedError("Bybit WS closed during handshake")
                raw_resp.extend(chunk)
                if len(raw_resp) > _MAX_HANDSHAKE_BYTES:
                    raise BybitSpotFeedError("Bybit WS handshake header exceeds limit")

            header_end = raw_resp.index(b"\r\n\r\n")
            header_block = bytes(raw_resp[:header_end]).decode("iso-8859-1")
            leftover = bytes(raw_resp[header_end + 4 :])
            lines = header_block.split("\r\n")
            if not lines or "101" not in lines[0]:
                raise BybitSpotFeedError(
                    f"Bybit WS handshake rejected: {lines[0] if lines else 'empty'}"
                )
            headers: dict[str, str] = {}
            for line in lines[1:]:
                if ":" in line:
                    k, v = line.split(":", 1)
                    headers[k.strip().lower()] = v.strip()
            accept_hdr = headers.get("sec-websocket-accept", "")
            if not hmac.compare_digest(accept_hdr, expected_accept):
                raise BybitSpotFeedError("Bybit WS Sec-WebSocket-Accept mismatch")

            sub_payload = json.dumps(
                {
                    "op": "subscribe",
                    "args": [f"publicTrade.{sym}" for sym in self._bybit_symbols],
                },
                separators=(",", ":"),
            ).encode("utf-8")
            sock.sendall(build_websocket_frame(_WS_OP_TEXT, sub_payload, masked=True))
            sock.setblocking(False)
        except Exception as exc:
            with suppress(Exception):
                sock.close()
            if isinstance(exc, BybitSpotFeedError):
                raise
            raise BybitSpotFeedError(f"Bybit WS handshake failed: {exc}") from exc

        self._sock = sock
        self._recv_buf = bytearray(leftover)
        self._frag_opcode = None
        self._frag_chunks = []
        self._last_ping_mono = time.monotonic()
        self._connected = True

    def close(self) -> None:
        """Close the underlying WebSocket connection cleanly."""
        sock = self._sock
        self._sock = None
        self._connected = False
        self._recv_buf.clear()
        self._frag_opcode = None
        self._frag_chunks.clear()
        if sock is not None:
            with suppress(Exception):
                sock.sendall(build_websocket_frame(_WS_OP_CLOSE, b"", masked=True))
            with suppress(Exception):
                sock.close()

    def _send_control_or_text(self, opcode: int, payload: bytes) -> bool:
        if self._sock is None:
            return False
        try:
            self._sock.sendall(build_websocket_frame(opcode, payload, masked=True))
            return True
        except OSError, ssl.SSLError, TimeoutError:
            return False

    def _read_available(self) -> bool:
        """Non-blocking read into `_recv_buf`; returns False if EOF or transport error."""
        if self._sock is None:
            return False
        for _ in range(16):
            pending_fn = getattr(self._sock, "pending", None)
            has_ssl_pending = bool(callable(pending_fn) and pending_fn() > 0)
            if not has_ssl_pending:
                try:
                    fileno = self._sock.fileno()
                except AttributeError, OSError, ValueError:
                    fileno = -1
                if fileno >= 0:
                    try:
                        readable, _, _ = select.select([self._sock], [], [], 0.0)
                    except OSError, ValueError:
                        return False
                    if not readable:
                        break
            try:
                chunk = self._sock.recv(_RECV_CHUNK_BYTES)
            except BlockingIOError, ssl.SSLWantReadError, ssl.SSLWantWriteError:
                break
            except OSError, ssl.SSLError, TimeoutError:
                return False
            if not chunk:
                return False
            self._recv_buf.extend(chunk)
            if len(self._recv_buf) > MAX_BYBIT_WS_FRAME_BYTES * 4:
                return False
        return True

    def _pop_frame(self) -> tuple[bool, int, bytes] | None:
        if len(self._recv_buf) < 2:
            return None
        b0 = self._recv_buf[0]
        b1 = self._recv_buf[1]
        fin = bool(b0 & 0x80)
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        ln = b1 & 0x7F
        pos = 2
        if ln == 126:
            if len(self._recv_buf) < 4:
                return None
            ln = struct.unpack("!H", self._recv_buf[2:4])[0]
            pos = 4
        elif ln == 127:
            if len(self._recv_buf) < 10:
                return None
            ln = struct.unpack("!Q", self._recv_buf[2:10])[0]
            pos = 10
        if ln > MAX_BYBIT_WS_FRAME_BYTES:
            raise BybitSpotFeedError(f"Bybit WS frame exceeds {MAX_BYBIT_WS_FRAME_BYTES} bytes")
        mask_len = 4 if masked else 0
        if len(self._recv_buf) < pos + mask_len + ln:
            return None
        mask_key = bytes(self._recv_buf[pos : pos + 4]) if masked else b""
        payload_start = pos + mask_len
        payload = bytes(self._recv_buf[payload_start : payload_start + ln])
        del self._recv_buf[: payload_start + ln]
        if masked:
            payload = bytes(byte ^ mask_key[i & 3] for i, byte in enumerate(payload))
        return fin, opcode, payload

    def _handle_text_message(
        self,
        raw_payload: bytes,
        collected: list[CoinbaseTradePrint],
    ) -> bool:
        """Parse one Bybit WS JSON message; returns True if gap/error occurred."""
        try:
            obj = _decode_json_object(raw_payload, "Bybit WS message")
        except BybitSpotFeedError:
            return True

        op = obj.get("op")
        if op in ("pong", "ping"):
            return False
        if op == "subscribe":
            return obj.get("success") is not True

        topic = obj.get("topic")
        if not isinstance(topic, str) or not topic.startswith("publicTrade."):
            return False
        bybit_sym = topic.split(".", 1)[1]
        canonical_pid = PRODUCT_BY_BYBIT_SYMBOL.get(bybit_sym)
        if canonical_pid is None:
            return False

        rows = obj.get("data")
        if not isinstance(rows, list):
            return True
        for idx, row in enumerate(rows):
            if not isinstance(row, dict):
                return True
            if row.get("s") != bybit_sym:
                return True
            try:
                tid = _parse_positive_int_id(row.get("i"), f"ws.data[{idx}].i")
                px_usd = _strict_pos_decimal(row.get("p"), f"ws.data[{idx}].p") * self._usdt_peg.mid
                sz = _strict_pos_decimal(row.get("v"), f"ws.data[{idx}].v")
                maker_side = _bybit_taker_side_to_maker_side(row.get("S"), f"ws.data[{idx}]")
                traded_at = _ms_to_utc_iso(row.get("T"), f"ws.data[{idx}].T")
            except BybitSpotFeedError:
                return True
            prev_tid = self._watermarks.get(canonical_pid, 0)
            if tid <= prev_tid:
                continue
            self._watermarks[canonical_pid] = tid
            collected.append(
                CoinbaseTradePrint(
                    product_id=canonical_pid,
                    trade_id=tid,
                    price=px_usd,
                    size=sz,
                    maker_side=maker_side,
                    traded_at_utc=traded_at,
                )
            )
        return False

    def drain_events(self, *, max_frames: int = 256) -> tuple[tuple[CoinbaseTradePrint, ...], bool]:
        """Drain buffered Bybit `publicTrade` prints without blocking; return `(prints, gap)`."""
        if not self.is_connected:
            return (), True
        if type(max_frames) is not int or max_frames <= 0:
            raise BybitSpotFeedError("max_frames must be a positive integer")

        now_mono = time.monotonic()
        if now_mono - self._last_ping_mono >= _PING_INTERVAL_S:
            if not self._send_control_or_text(_WS_OP_TEXT, b'{"op":"ping"}'):
                self.close()
                return (), True
            self._last_ping_mono = now_mono

        if not self._read_available():
            self.close()
            return (), True

        collected: list[CoinbaseTradePrint] = []
        gap_detected = False
        frames_processed = 0
        while frames_processed < max_frames:
            try:
                frame = self._pop_frame()
            except BybitSpotFeedError:
                gap_detected = True
                self.close()
                break
            if frame is None:
                break
            frames_processed += 1
            fin, opcode, payload = frame

            if opcode == _WS_OP_PING:
                if not self._send_control_or_text(_WS_OP_PONG, payload):
                    gap_detected = True
                    self.close()
                    break
                continue
            if opcode == _WS_OP_PONG:
                continue
            if opcode == _WS_OP_CLOSE:
                gap_detected = True
                self.close()
                break
            if opcode == _WS_OP_BINARY:
                gap_detected = True
                continue
            if opcode == _WS_OP_TEXT:
                if fin:
                    if self._handle_text_message(payload, collected):
                        gap_detected = True
                else:
                    self._frag_opcode = _WS_OP_TEXT
                    self._frag_chunks = [payload]
                continue
            if opcode == _WS_OP_CONTINUATION:
                if self._frag_opcode != _WS_OP_TEXT:
                    gap_detected = True
                    self.close()
                    break
                self._frag_chunks.append(payload)
                if sum(len(c) for c in self._frag_chunks) > MAX_BYBIT_WS_FRAME_BYTES:
                    gap_detected = True
                    self.close()
                    break
                if fin:
                    assembled = b"".join(self._frag_chunks)
                    self._frag_opcode = None
                    self._frag_chunks = []
                    if self._handle_text_message(assembled, collected):
                        gap_detected = True
                continue
            gap_detected = True

        return tuple(collected), gap_detected


__all__ = [
    "BYBIT_ORDERBOOK_URL_TEMPLATE",
    "BYBIT_RECENT_TRADES_URL_TEMPLATE",
    "BYBIT_REST_ALLOWED_HOSTS",
    "BYBIT_SYMBOL_BY_PRODUCT",
    "BYBIT_WS_ALLOWED_HOSTS",
    "BYBIT_WS_HOST",
    "BYBIT_WS_PATH",
    "BYBIT_WS_PORT",
    "MAX_BYBIT_RESPONSE_BYTES",
    "MAX_BYBIT_WS_FRAME_BYTES",
    "PRODUCT_BY_BYBIT_SYMBOL",
    "SUPPORTED_BYBIT_PRODUCTS",
    "USDT_PEG_ALLOWED_HOSTS",
    "USDT_PEG_MAX",
    "USDT_PEG_MIN",
    "USDT_PEG_URL",
    "BybitSpotFeedError",
    "BybitSpotWebSocketFeed",
    "UsdtUsdPeg",
    "fetch_bybit_l2_book",
    "fetch_bybit_recent_trades",
    "fetch_usdt_usd_peg",
    "parse_bybit_orderbook_payload",
    "parse_bybit_recent_trades_payload",
    "parse_usdt_usd_peg_payload",
    "resolve_bybit_symbol",
]
