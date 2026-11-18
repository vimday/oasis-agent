"""Keyless Coinbase <-> Base live CEX-DEX lead-lag evaluator.

Fetches public Coinbase Exchange L2 orderbooks across the 9 verified Base-Coinbase
products (`ETH-USD`, `BTC-USD`, `AERO-USD`, `VIRTUAL-USD`, `VVV-USD`, `MORPHO-USD`,
`DEGEN-USD`, `WELL-USD`, `CBETH-USD`) with strict HTTPS/DNS/JSON hardening, IPv4-first
TLS fallback, and automatic level-2 -> level-1 BBO fallback on >1 MB books. Evaluates
full-depth VWAP execution against pinned-block Base DEX quotes in both directions
(`buy_dex_sell_cex` and `buy_cex_sell_dex`), attributes taker fees, latency haircut,
and Base L2+L1 gas, and degrades fail-soft per tick when Coinbase is unavailable.

All outputs are read-only shadow measurements (`execution_eligible = False`).
"""

import errno
import http.client
import ipaddress
import json
import math
import re
import socket
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Final, Literal, cast

import certifi

from ..models.common import canonical_json, sha256_hex

COINBASE_ALLOWED_HOSTS: Final[frozenset[str]] = frozenset({"api.exchange.coinbase.com"})
COINBASE_BOOK_URL_TEMPLATE: Final[str] = (
    "https://api.exchange.coinbase.com/products/{product}/book?level=2"
)
COINBASE_BOOK_LEVEL1_URL_TEMPLATE: Final[str] = (
    "https://api.exchange.coinbase.com/products/{product}/book?level={level}"
)
SUPPORTED_PRODUCTS: Final[tuple[str, ...]] = (
    "ETH-USD",
    "BTC-USD",
    "AERO-USD",
    "VIRTUAL-USD",
    "VVV-USD",
    "MORPHO-USD",
    "DEGEN-USD",
    "WELL-USD",
    "CBETH-USD",
)
MAX_RESPONSE_BYTES: Final[int] = 1_000_000
DEFAULT_CEX_TAKER_FEE_BPS: Final[Decimal] = Decimal("3.5")
DEFAULT_LATENCY_HAIRCUT_BPS: Final[Decimal] = Decimal("1.5")
DEFAULT_TRADE_NOTIONAL_USDC: Final[int] = 500_000_000
DEFAULT_TIMEOUT_S: Final[float] = 5.0

_BPS_DIVISOR: Final[Decimal] = Decimal(10_000)
_USDC_SCALE: Final[Decimal] = Decimal(1_000_000)
_WEI_SCALE: Final[Decimal] = Decimal(10**18)
_TUN_FAKE_IPV4_NET: Final[ipaddress.IPv4Network] = ipaddress.IPv4Network("198.18.0.0/15")
_TUN_FAKE_IPV6_NET: Final[ipaddress.IPv6Network] = ipaddress.IPv6Network("2001:2::/48")
_SHA256_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")
_COINBASE_BOOK_PATH_RE: Final[re.Pattern[str]] = re.compile(r"^/products/([A-Za-z0-9-]+)/book$")
_USER_AGENT: Final[str] = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)


class LiveCexDexError(ValueError):
    """Fail-closed error on Coinbase transport, book parsing, or input validation."""


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        with suppress(OSError):
            fp.close()
        raise LiveCexDexError("Coinbase book redirects are forbidden")


@dataclass(frozen=True, slots=True)
class CoinbaseProductSpec:
    """Mapping between Coinbase Exchange product ID and Base token metadata."""

    product_id: str
    token_symbol: str
    token_decimals: int


_PRODUCT_SPECS: Final[dict[str, CoinbaseProductSpec]] = {
    "ETH-USD": CoinbaseProductSpec(product_id="ETH-USD", token_symbol="WETH", token_decimals=18),
    "BTC-USD": CoinbaseProductSpec(product_id="BTC-USD", token_symbol="cbBTC", token_decimals=8),
    "AERO-USD": CoinbaseProductSpec(product_id="AERO-USD", token_symbol="AERO", token_decimals=18),
    "VIRTUAL-USD": CoinbaseProductSpec(
        product_id="VIRTUAL-USD", token_symbol="VIRTUAL", token_decimals=18
    ),
    "VVV-USD": CoinbaseProductSpec(product_id="VVV-USD", token_symbol="VVV", token_decimals=18),
    "MORPHO-USD": CoinbaseProductSpec(
        product_id="MORPHO-USD", token_symbol="MORPHO", token_decimals=18
    ),
    "DEGEN-USD": CoinbaseProductSpec(
        product_id="DEGEN-USD", token_symbol="DEGEN", token_decimals=18
    ),
    "WELL-USD": CoinbaseProductSpec(product_id="WELL-USD", token_symbol="WELL", token_decimals=18),
    "CBETH-USD": CoinbaseProductSpec(
        product_id="CBETH-USD", token_symbol="cbETH", token_decimals=18
    ),
}
_SYMBOL_ALIASES: Final[dict[str, str]] = {
    "ETH-USD": "ETH-USD",
    "ETH": "ETH-USD",
    "WETH": "ETH-USD",
    "BTC-USD": "BTC-USD",
    "BTC": "BTC-USD",
    "CBBTC": "BTC-USD",
    "AERO-USD": "AERO-USD",
    "AERO": "AERO-USD",
    "VIRTUAL-USD": "VIRTUAL-USD",
    "VIRTUAL": "VIRTUAL-USD",
    "VVV-USD": "VVV-USD",
    "VVV": "VVV-USD",
    "MORPHO-USD": "MORPHO-USD",
    "MORPHO": "MORPHO-USD",
    "DEGEN-USD": "DEGEN-USD",
    "DEGEN": "DEGEN-USD",
    "WELL-USD": "WELL-USD",
    "WELL": "WELL-USD",
    "CBETH-USD": "CBETH-USD",
    "CBETH": "CBETH-USD",
}


def resolve_coinbase_product(symbol_or_product: str) -> CoinbaseProductSpec:
    """Resolve any supported Coinbase product ID or Base token symbol alias."""
    if not isinstance(symbol_or_product, str) or not symbol_or_product.strip():
        raise ValueError("symbol_or_product must be a non-empty string")
    key = symbol_or_product.strip().upper()
    product_id = _SYMBOL_ALIASES.get(key)
    if product_id is None:
        raise ValueError(f"unsupported Coinbase product or token symbol: {symbol_or_product!r}")
    return _PRODUCT_SPECS[product_id]


def _to_finite_decimal(
    value: object,
    name: str,
    *,
    require_positive: bool = False,
    require_nonneg: bool = False,
) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{name} must not be a boolean")
    try:
        if isinstance(value, Decimal):
            dec = value
        elif isinstance(value, int):
            dec = Decimal(value)
        elif isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError(f"{name} must be finite")
            dec = Decimal(str(value))
        elif isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                raise ValueError(f"{name} must not be empty")
            dec = Decimal(stripped)
        else:
            raise ValueError(f"{name} has unsupported numeric type {type(value).__name__}")
    except InvalidOperation as exc:
        raise ValueError(f"{name} is not a valid decimal number") from exc

    if not dec.is_finite():
        raise ValueError(f"{name} must be a finite decimal")
    if require_positive and dec <= 0:
        raise ValueError(f"{name} must be positive")
    if require_nonneg and dec < 0:
        raise ValueError(f"{name} must be non-negative")
    return dec


def _decimal_to_float(value: Decimal, *, places: int = 6) -> float:
    rounded = round(float(value), places)
    if not math.isfinite(rounded):
        raise ValueError("decimal value overflows finite float representation")
    return rounded


def gas_wei_to_usd(gas_wei: int, eth_usd_price: Decimal | int | float | str) -> Decimal:
    """Convert Base L2+L1 gas in wei to USD using a finite positive ETH/USD reference."""
    if type(gas_wei) is not int or gas_wei < 0:
        raise ValueError("gas_wei must be a non-negative integer")
    price = _to_finite_decimal(eth_usd_price, "eth_usd_price", require_positive=True)
    return (Decimal(gas_wei) * price) / _WEI_SCALE


@dataclass(frozen=True, slots=True)
class CoinbaseL2Level:
    """Single validated Coinbase Exchange L2 price/size depth level."""

    price: Decimal
    size: Decimal
    num_orders: int = 1

    def __post_init__(self) -> None:
        p = _to_finite_decimal(self.price, "level.price", require_positive=True)
        s = _to_finite_decimal(self.size, "level.size", require_positive=True)
        if type(self.num_orders) is not int or self.num_orders <= 0:
            raise LiveCexDexError("level.num_orders must be a positive integer")
        object.__setattr__(self, "price", p)
        object.__setattr__(self, "size", s)


@dataclass(frozen=True, slots=True)
class VwapFillResult:
    """Full-depth VWAP fill summary across L2 book levels."""

    qty_tokens: Decimal
    total_usd: Decimal
    vwap_price_usd: Decimal
    levels_consumed: int


@dataclass(frozen=True, slots=True)
class CoinbaseL2Book:
    """Validated non-crossed Coinbase Exchange L2 orderbook snapshot."""

    product_id: str
    bids: tuple[CoinbaseL2Level, ...]
    asks: tuple[CoinbaseL2Level, ...]
    sequence: int | None
    retrieved_at_utc: str
    raw_sha256: str
    book_level: Literal[1, 2] = 2

    def __post_init__(self) -> None:
        spec = resolve_coinbase_product(self.product_id)
        if self.product_id != spec.product_id:
            object.__setattr__(self, "product_id", spec.product_id)
        if not self.bids or not self.asks:
            raise LiveCexDexError(f"{self.product_id} book must contain non-empty bids and asks")
        for idx in range(len(self.bids) - 1):
            if self.bids[idx].price < self.bids[idx + 1].price:
                raise LiveCexDexError(f"{self.product_id} bids are not sorted descending")
        for idx in range(len(self.asks) - 1):
            if self.asks[idx].price > self.asks[idx + 1].price:
                raise LiveCexDexError(f"{self.product_id} asks are not sorted ascending")
        if self.bids[0].price >= self.asks[0].price:
            raise LiveCexDexError(
                f"{self.product_id} book is crossed: "
                f"bid={self.bids[0].price} >= ask={self.asks[0].price}"
            )
        if self.sequence is not None and (type(self.sequence) is not int or self.sequence < 0):
            raise LiveCexDexError("book sequence must be a non-negative integer or None")
        if not isinstance(self.raw_sha256, str) or _SHA256_RE.fullmatch(self.raw_sha256) is None:
            raise LiveCexDexError("raw_sha256 must be a lowercase 64-hex SHA-256 digest")
        if not isinstance(self.retrieved_at_utc, str) or not self.retrieved_at_utc.strip():
            raise LiveCexDexError("retrieved_at_utc must be a non-empty timestamp")
        if type(self.book_level) is not int or self.book_level not in (1, 2):
            raise LiveCexDexError("book_level must be 1 or 2")

    @property
    def best_bid(self) -> Decimal:
        return self.bids[0].price

    @property
    def best_ask(self) -> Decimal:
        return self.asks[0].price

    @property
    def mid_price(self) -> Decimal:
        return (self.best_bid + self.best_ask) / Decimal(2)

    def _walk_levels(
        self, levels: tuple[CoinbaseL2Level, ...], qty_tokens: Decimal
    ) -> VwapFillResult | None:
        remaining = _to_finite_decimal(qty_tokens, "qty_tokens", require_positive=True)
        total_usd = Decimal(0)
        for consumed, level in enumerate(levels, start=1):
            take = min(remaining, level.size)
            total_usd += take * level.price
            remaining -= take
            if remaining == 0:
                return VwapFillResult(
                    qty_tokens=qty_tokens,
                    total_usd=total_usd,
                    vwap_price_usd=total_usd / qty_tokens,
                    levels_consumed=consumed,
                )
        return None

    def vwap_sell_bids(self, qty_tokens: Decimal) -> VwapFillResult | None:
        """Walk bids descending to compute total USD proceeds for selling `qty_tokens`."""
        return self._walk_levels(self.bids, qty_tokens)

    def vwap_buy_asks(self, qty_tokens: Decimal) -> VwapFillResult | None:
        """Walk asks ascending to compute total USD cost for buying `qty_tokens`."""
        return self._walk_levels(self.asks, qty_tokens)


@dataclass(frozen=True, slots=True)
class CexDexDirectionResult:
    """Attributed CEX-DEX comparison for a single execution direction."""

    direction: Literal["buy_dex_sell_cex", "buy_cex_sell_dex"]
    feasible: bool
    error_reason: str | None
    qty_tokens: str
    dex_notional_usd: float
    cex_vwap_price_usd: float | None
    dex_effective_price_usd: float | None
    raw_spread_bps: float | None
    net_bps: float | None
    net_profit_usd: float | None
    maker_zero_fee_net_bps_diagnostic: float | None
    levels_consumed: int

    def to_dict(self) -> dict[str, Any]:
        """Serialize direction result to a JSON-compatible dictionary."""
        return {
            "direction": self.direction,
            "feasible": self.feasible,
            "error_reason": self.error_reason,
            "qty_tokens": self.qty_tokens,
            "dex_notional_usd": self.dex_notional_usd,
            "cex_vwap_price_usd": self.cex_vwap_price_usd,
            "dex_effective_price_usd": self.dex_effective_price_usd,
            "raw_spread_bps": self.raw_spread_bps,
            "net_bps": self.net_bps,
            "net_profit_usd": self.net_profit_usd,
            "maker_zero_fee_net_bps_diagnostic": self.maker_zero_fee_net_bps_diagnostic,
            "maker_zero_fee_diagnostic_only": True,
            "levels_consumed": self.levels_consumed,
        }


@dataclass(frozen=True, slots=True)
class CexDexTickEvaluation:
    """Combined bidirectional CEX-DEX lead-lag evaluation for one product on one tick."""

    product_id: str
    token_symbol: str
    status: Literal["ok", "degraded"]
    error_reason: str | None
    cex_taker_fee_bps: float
    latency_haircut_bps: float
    gas_cost_usd: float
    best_bid_usd: float | None
    best_ask_usd: float | None
    cex_mid_usd: float | None
    book_sequence: int | None
    book_sha256: str | None
    buy_dex_sell_cex: CexDexDirectionResult | None
    buy_cex_sell_dex: CexDexDirectionResult | None
    best_direction: str | None
    best_net_bps: float | None
    best_raw_spread_bps: float | None
    maker_zero_fee_net_bps: float | None
    maker_zero_fee_diagnostic_only: bool = True
    execution_eligible: Literal[False] = False
    book_level: Literal[1, 2] = 2

    def __post_init__(self) -> None:
        if self.execution_eligible is not False:
            raise LiveCexDexError("CEX-DEX shadow evaluation cannot set execution_eligible=True")
        if type(self.book_level) is not int or self.book_level not in (1, 2):
            raise LiveCexDexError("book_level must be 1 or 2")

    def to_dict(self) -> dict[str, Any]:
        """Serialize tick evaluation to a JSON-compatible dictionary."""
        return {
            "product_id": self.product_id,
            "token_symbol": self.token_symbol,
            "status": self.status,
            "error_reason": self.error_reason,
            "cex_taker_fee_bps": self.cex_taker_fee_bps,
            "latency_haircut_bps": self.latency_haircut_bps,
            "gas_cost_usd": self.gas_cost_usd,
            "best_bid_usd": self.best_bid_usd,
            "best_ask_usd": self.best_ask_usd,
            "cex_mid_usd": self.cex_mid_usd,
            "book_sequence": self.book_sequence,
            "book_sha256": self.book_sha256,
            "book_level": self.book_level,
            "buy_dex_sell_cex": (
                self.buy_dex_sell_cex.to_dict() if self.buy_dex_sell_cex is not None else None
            ),
            "buy_cex_sell_dex": (
                self.buy_cex_sell_dex.to_dict() if self.buy_cex_sell_dex is not None else None
            ),
            "best_direction": self.best_direction,
            "best_net_bps": self.best_net_bps,
            "best_raw_spread_bps": self.best_raw_spread_bps,
            "maker_zero_fee_net_bps": self.maker_zero_fee_net_bps,
            "maker_zero_fee_diagnostic_only": self.maker_zero_fee_diagnostic_only,
            "execution_eligible": self.execution_eligible,
        }


def _is_tun_fake_ip(ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip_obj, ipaddress.IPv4Address):
        return ip_obj in _TUN_FAKE_IPV4_NET
    return ip_obj in _TUN_FAKE_IPV6_NET


def _is_forbidden_ip(ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if (
        isinstance(ip_obj, ipaddress.IPv6Address)
        and ip_obj.ipv4_mapped is not None
        and _is_forbidden_ip(ip_obj.ipv4_mapped)
    ):
        return True
    return (
        ip_obj.is_private
        or ip_obj.is_loopback
        or ip_obj.is_link_local
        or ip_obj.is_multicast
        or ip_obj.is_reserved
        or ip_obj.is_unspecified
    )


def _validate_coinbase_url(url: str) -> str:
    if not isinstance(url, str) or not url or url != url.strip():
        raise LiveCexDexError("Coinbase URL must be a non-empty string")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise LiveCexDexError("Coinbase client requires an https:// endpoint")
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise LiveCexDexError("Coinbase URL forbids userinfo credentials")
    hostname = parsed.hostname
    if not hostname or hostname.lower() not in COINBASE_ALLOWED_HOSTS:
        raise LiveCexDexError("Coinbase URL host is not allowlisted")
    try:
        if parsed.port is not None:
            raise LiveCexDexError("Coinbase URL forbids explicit custom ports")
    except ValueError as exc:
        raise LiveCexDexError("Coinbase URL contains an invalid port") from exc
    if parsed.fragment:
        raise LiveCexDexError("Coinbase URL forbids fragments")
    path_match = _COINBASE_BOOK_PATH_RE.fullmatch(parsed.path)
    if path_match is None or path_match.group(1) not in _PRODUCT_SPECS:
        raise LiveCexDexError("Coinbase URL path must target /products/<supported-product>/book")
    if parsed.query not in ("level=1", "level=2"):
        raise LiveCexDexError("Coinbase URL query must be level=1 or level=2")
    return url


def _validate_addr_infos(addr_infos: list[Any]) -> list[Any]:
    if not addr_infos:
        raise LiveCexDexError("Coinbase DNS returned no addresses")
    for _, _, _, _, sockaddr in addr_infos:
        raw_ip = str(sockaddr[0]).split("%", 1)[0]
        ip_obj = ipaddress.ip_address(raw_ip)
        is_mapped_tun = (
            isinstance(ip_obj, ipaddress.IPv6Address)
            and ip_obj.ipv4_mapped is not None
            and _is_tun_fake_ip(ip_obj.ipv4_mapped)
        )
        if _is_forbidden_ip(ip_obj) and not _is_tun_fake_ip(ip_obj) and not is_mapped_tun:
            raise LiveCexDexError("Coinbase DNS resolved to a non-public IP")
    return sorted(
        addr_infos,
        key=lambda item: (
            0 if item[0] == socket.AF_INET else (1 if item[0] == socket.AF_INET6 else 2)
        ),
    )


def _resolve_and_validate_addrs(
    hostname: str,
    port: int = 443,
    *,
    family: int = socket.AF_UNSPEC,
) -> list[Any]:
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        raise LiveCexDexError("Coinbase URL forbids IP literals")

    try:
        if family == socket.AF_UNSPEC:
            addr_infos = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
        else:
            addr_infos = socket.getaddrinfo(hostname, port, family=family, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise LiveCexDexError("Coinbase DNS resolution failed") from exc
    return _validate_addr_infos(addr_infos)


def _validate_resolved_dns(hostname: str) -> None:
    _resolve_and_validate_addrs(hostname, 443)


class _IPv4PreferredHTTPSConnection(http.client.HTTPSConnection):
    """HTTPSConnection sorting validated AF_INET first and falling back on IPv6 SSLEOFError."""

    def __init__(
        self,
        host: str,
        port: int | None = None,
        *,
        source_address: tuple[str, int] | None = None,
        context: ssl.SSLContext | None = None,
        **kwargs: Any,
    ) -> None:
        tls_ctx = (
            context if context is not None else ssl.create_default_context(cafile=certifi.where())
        )
        super().__init__(host, port, source_address=source_address, context=tls_ctx, **kwargs)
        self._tls_context: ssl.SSLContext = tls_ctx
        self._bind_address: tuple[str, int] | None = source_address

    def _try_connect_candidates(
        self, candidates: list[Any], server_hostname: str
    ) -> tuple[OSError | None, bool]:
        last_exc: OSError | None = None
        saw_ipv6_ssl_eof = False
        tunnel_host = getattr(self, "_tunnel_host", None)
        for af, socktype, proto, _canon, sockaddr in candidates:
            raw_sock: socket.socket | None = None
            try:
                raw_sock = socket.socket(af, socktype, proto)
                if isinstance(self.timeout, (int, float)) and not isinstance(self.timeout, bool):
                    raw_sock.settimeout(float(self.timeout))
                if self._bind_address is not None:
                    raw_sock.bind(self._bind_address)
                raw_sock.connect(sockaddr)
                try:
                    raw_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                except OSError as opt_exc:
                    if opt_exc.errno != errno.ENOPROTOOPT:
                        raise
                if tunnel_host:
                    self.sock = raw_sock
                    tunnel_fn = getattr(self, "_tunnel", None)
                    if callable(tunnel_fn):
                        tunnel_fn()
                    wrapped = self._tls_context.wrap_socket(
                        self.sock, server_hostname=server_hostname
                    )
                else:
                    wrapped = self._tls_context.wrap_socket(
                        raw_sock, server_hostname=server_hostname
                    )
                self.sock = wrapped
                return None, saw_ipv6_ssl_eof
            except OSError as exc:
                last_exc = exc
                if af == socket.AF_INET6 and isinstance(exc, ssl.SSLEOFError):
                    saw_ipv6_ssl_eof = True
                self.sock = None
                if raw_sock is not None:
                    with suppress(OSError):
                        raw_sock.close()
        return last_exc, saw_ipv6_ssl_eof

    def connect(self) -> None:
        sys.audit("http.client.connect", self, self.host, self.port)
        ordered_addrs = _resolve_and_validate_addrs(self.host, self.port)
        tunnel_host = getattr(self, "_tunnel_host", None)
        server_hostname = str(tunnel_host) if tunnel_host else self.host
        last_exc, saw_ipv6_ssl_eof = self._try_connect_candidates(ordered_addrs, server_hostname)
        if last_exc is None:
            return

        has_ipv4 = any(info[0] == socket.AF_INET for info in ordered_addrs)
        if saw_ipv6_ssl_eof and not has_ipv4:
            try:
                ipv4_candidates = [
                    info
                    for info in _resolve_and_validate_addrs(
                        self.host, self.port, family=socket.AF_INET
                    )
                    if info[0] == socket.AF_INET
                ]
            except LiveCexDexError as dns_exc:
                if "non-public" in str(dns_exc) or "forbids" in str(dns_exc):
                    raise
                ipv4_candidates = []
            if ipv4_candidates:
                ipv4_exc, _ = self._try_connect_candidates(ipv4_candidates, server_hostname)
                if ipv4_exc is None:
                    return
                last_exc = ipv4_exc
        raise last_exc


class _IPv4PreferredHTTPSHandler(urllib.request.HTTPSHandler):
    """HTTPSHandler opening connections via `_IPv4PreferredHTTPSConnection`."""

    def __init__(self, context: ssl.SSLContext | None = None) -> None:
        tls_ctx = (
            context if context is not None else ssl.create_default_context(cafile=certifi.where())
        )
        super().__init__(context=tls_ctx)
        self._context: ssl.SSLContext = tls_ctx

    def https_open(self, req: urllib.request.Request) -> http.client.HTTPResponse:
        return self.do_open(
            _IPv4PreferredHTTPSConnection,
            req,
            context=self._context,
        )


def _default_https_get(url: str, timeout_s: float) -> bytes:
    _validate_coinbase_url(url)
    parsed = urllib.parse.urlsplit(url)
    if not parsed.hostname:
        raise LiveCexDexError("Coinbase URL missing hostname")
    _validate_resolved_dns(parsed.hostname)

    opener = urllib.request.build_opener(
        _IPv4PreferredHTTPSHandler(context=ssl.create_default_context(cafile=certifi.where())),
        _NoRedirectHandler(),
    )
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": _USER_AGENT},
        method="GET",
    )
    try:
        with opener.open(request, timeout=timeout_s) as response:
            raw = cast(bytes, response.read(MAX_RESPONSE_BYTES + 1))
            if len(raw) > MAX_RESPONSE_BYTES:
                raise LiveCexDexError("Coinbase response exceeds byte limit")
            return raw
    except urllib.error.HTTPError as exc:
        with suppress(OSError):
            exc.close()
        raise LiveCexDexError(f"Coinbase HTTP failure: status {exc.code}") from None
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        if isinstance(exc, LiveCexDexError):
            raise
        raise LiveCexDexError(f"Coinbase HTTPS transport failure: {exc}") from None


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise LiveCexDexError("Coinbase JSON contains duplicate fields")
        result[key] = value
    return result


def _reject_constant(_value: str) -> object:
    raise LiveCexDexError("Coinbase JSON contains non-finite numbers")


def _finite_decimal_literal(value: str) -> Decimal:
    try:
        dec = Decimal(value)
    except InvalidOperation as exc:
        raise LiveCexDexError("Coinbase JSON contains invalid decimal number") from exc
    if not dec.is_finite():
        raise LiveCexDexError("Coinbase JSON contains non-finite decimal number")
    return dec


def _parse_book_levels(raw_levels: object, side: str) -> tuple[CoinbaseL2Level, ...]:
    if not isinstance(raw_levels, list) or not raw_levels:
        raise LiveCexDexError(f"Coinbase {side} must be a non-empty JSON array")
    levels: list[CoinbaseL2Level] = []
    for idx, entry in enumerate(raw_levels):
        if not isinstance(entry, list) or len(entry) < 2:
            raise LiveCexDexError(f"Coinbase {side}[{idx}] must be [price, size, ...]")
        price = _to_finite_decimal(entry[0], f"{side}[{idx}].price", require_positive=True)
        size = _to_finite_decimal(entry[1], f"{side}[{idx}].size", require_positive=True)
        num_orders = 1
        if len(entry) >= 3 and entry[2] is not None:
            raw_orders = entry[2]
            if type(raw_orders) is not int or raw_orders <= 0:
                raise LiveCexDexError(f"Coinbase {side}[{idx}].num_orders must be positive int")
            num_orders = raw_orders
        levels.append(CoinbaseL2Level(price=price, size=size, num_orders=num_orders))
    return tuple(levels)


def parse_coinbase_l2_book(
    raw_bytes: bytes,
    *,
    product_id: str,
    retrieved_at_utc: str | None = None,
    level: Literal[1, 2] = 2,
) -> CoinbaseL2Book:
    """Strictly parse a Coinbase Exchange `/products/{product}/book?level={1,2}` payload."""
    spec = resolve_coinbase_product(product_id)
    if type(level) is not int or level not in (1, 2):
        raise ValueError("level must be 1 or 2")
    if not isinstance(raw_bytes, bytes):
        raise TypeError("raw_bytes must be bytes")
    if len(raw_bytes) > MAX_RESPONSE_BYTES:
        raise LiveCexDexError("Coinbase payload exceeds MAX_RESPONSE_BYTES")
    try:
        decoded = json.loads(
            raw_bytes.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
            parse_float=_finite_decimal_literal,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise LiveCexDexError("Coinbase payload is not valid JSON") from exc
    if not isinstance(decoded, dict):
        raise LiveCexDexError("Coinbase payload must be a JSON object")

    if decoded.get("auction_mode") is True:
        raise LiveCexDexError(f"{spec.product_id} Coinbase book is in auction_mode")

    raw_seq = decoded.get("sequence")
    sequence: int | None = None
    if raw_seq is not None:
        if type(raw_seq) is not int or raw_seq < 0:
            raise LiveCexDexError("Coinbase book sequence must be a non-negative integer")
        sequence = raw_seq

    bids = _parse_book_levels(decoded.get("bids"), "bids")
    asks = _parse_book_levels(decoded.get("asks"), "asks")
    timestamp = datetime.now(UTC).isoformat() if retrieved_at_utc is None else retrieved_at_utc
    return CoinbaseL2Book(
        product_id=spec.product_id,
        bids=bids,
        asks=asks,
        sequence=sequence,
        retrieved_at_utc=timestamp,
        raw_sha256=sha256_hex(raw_bytes),
        book_level=level,
    )


def fetch_coinbase_l2_book(
    symbol_or_product: str,
    *,
    level: Literal[1, 2] = 2,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    transport: Callable[[str, float], bytes] | None = None,
) -> CoinbaseL2Book:
    """Fetch and strictly validate one keyless Coinbase Exchange level-2 or level-1 book."""
    if type(level) is not int or level not in (1, 2):
        raise ValueError("level must be 1 or 2")
    if (
        isinstance(timeout_s, bool)
        or not isinstance(timeout_s, (int, float))
        or not math.isfinite(float(timeout_s))
        or float(timeout_s) <= 0
    ):
        raise ValueError("timeout_s must be a positive finite number")
    spec = resolve_coinbase_product(symbol_or_product)
    url = _validate_coinbase_url(
        COINBASE_BOOK_LEVEL1_URL_TEMPLATE.format(product=spec.product_id, level=level)
    )
    getter = _default_https_get if transport is None else transport
    raw_bytes = getter(url, float(timeout_s))
    return parse_coinbase_l2_book(raw_bytes, product_id=spec.product_id, level=level)


def _evaluate_buy_dex_sell_cex(
    *,
    spec: CoinbaseProductSpec,
    book: CoinbaseL2Book,
    trade_notional_usdc: int,
    dex_buy_token_for_usdc_out: int | None,
    gas_usd: Decimal,
    taker_fee_bps: Decimal,
    haircut_bps: Decimal,
) -> CexDexDirectionResult:
    dex_cost_usd = Decimal(trade_notional_usdc) / _USDC_SCALE
    if dex_buy_token_for_usdc_out is None or dex_buy_token_for_usdc_out <= 0:
        return CexDexDirectionResult(
            direction="buy_dex_sell_cex",
            feasible=False,
            error_reason="dex_buy_quote_unavailable",
            qty_tokens="0",
            dex_notional_usd=_decimal_to_float(dex_cost_usd),
            cex_vwap_price_usd=None,
            dex_effective_price_usd=None,
            raw_spread_bps=None,
            net_bps=None,
            net_profit_usd=None,
            maker_zero_fee_net_bps_diagnostic=None,
            levels_consumed=0,
        )

    qty_tokens = Decimal(dex_buy_token_for_usdc_out) / Decimal(10**spec.token_decimals)
    dex_eff_price = dex_cost_usd / qty_tokens
    fill = book.vwap_sell_bids(qty_tokens)
    if fill is None:
        return CexDexDirectionResult(
            direction="buy_dex_sell_cex",
            feasible=False,
            error_reason="insufficient_coinbase_bid_depth",
            qty_tokens=str(qty_tokens.normalize()),
            dex_notional_usd=_decimal_to_float(dex_cost_usd),
            cex_vwap_price_usd=None,
            dex_effective_price_usd=_decimal_to_float(dex_eff_price),
            raw_spread_bps=None,
            net_bps=None,
            net_profit_usd=None,
            maker_zero_fee_net_bps_diagnostic=None,
            levels_consumed=len(book.bids),
        )

    gross_proceeds_usd = fill.total_usd
    raw_spread_bps = ((gross_proceeds_usd - dex_cost_usd) / dex_cost_usd) * _BPS_DIVISOR

    total_cex_cost_factor = (taker_fee_bps + haircut_bps) / _BPS_DIVISOR
    net_proceeds_cex = gross_proceeds_usd * (Decimal(1) - total_cex_cost_factor)
    net_profit_usd = net_proceeds_cex - dex_cost_usd - gas_usd
    net_bps = (net_profit_usd / dex_cost_usd) * _BPS_DIVISOR

    maker_factor = haircut_bps / _BPS_DIVISOR
    maker_net_usd = (gross_proceeds_usd * (Decimal(1) - maker_factor)) - dex_cost_usd - gas_usd
    maker_net_bps = (maker_net_usd / dex_cost_usd) * _BPS_DIVISOR

    return CexDexDirectionResult(
        direction="buy_dex_sell_cex",
        feasible=True,
        error_reason=None,
        qty_tokens=str(qty_tokens.normalize()),
        dex_notional_usd=_decimal_to_float(dex_cost_usd),
        cex_vwap_price_usd=_decimal_to_float(fill.vwap_price_usd),
        dex_effective_price_usd=_decimal_to_float(dex_eff_price),
        raw_spread_bps=_decimal_to_float(raw_spread_bps),
        net_bps=_decimal_to_float(net_bps),
        net_profit_usd=_decimal_to_float(net_profit_usd),
        maker_zero_fee_net_bps_diagnostic=_decimal_to_float(maker_net_bps),
        levels_consumed=fill.levels_consumed,
    )


def _evaluate_buy_cex_sell_dex(
    *,
    spec: CoinbaseProductSpec,
    book: CoinbaseL2Book,
    trade_notional_token: int,
    dex_sell_token_for_usdc_out: int | None,
    gas_usd: Decimal,
    taker_fee_bps: Decimal,
    haircut_bps: Decimal,
) -> CexDexDirectionResult:
    qty_tokens = Decimal(trade_notional_token) / Decimal(10**spec.token_decimals)
    if dex_sell_token_for_usdc_out is None or dex_sell_token_for_usdc_out <= 0:
        return CexDexDirectionResult(
            direction="buy_cex_sell_dex",
            feasible=False,
            error_reason="dex_sell_quote_unavailable",
            qty_tokens=str(qty_tokens.normalize()),
            dex_notional_usd=0.0,
            cex_vwap_price_usd=None,
            dex_effective_price_usd=None,
            raw_spread_bps=None,
            net_bps=None,
            net_profit_usd=None,
            maker_zero_fee_net_bps_diagnostic=None,
            levels_consumed=0,
        )

    dex_proceeds_usd = Decimal(dex_sell_token_for_usdc_out) / _USDC_SCALE
    dex_eff_price = dex_proceeds_usd / qty_tokens
    fill = book.vwap_buy_asks(qty_tokens)
    if fill is None:
        return CexDexDirectionResult(
            direction="buy_cex_sell_dex",
            feasible=False,
            error_reason="insufficient_coinbase_ask_depth",
            qty_tokens=str(qty_tokens.normalize()),
            dex_notional_usd=_decimal_to_float(dex_proceeds_usd),
            cex_vwap_price_usd=None,
            dex_effective_price_usd=_decimal_to_float(dex_eff_price),
            raw_spread_bps=None,
            net_bps=None,
            net_profit_usd=None,
            maker_zero_fee_net_bps_diagnostic=None,
            levels_consumed=len(book.asks),
        )

    gross_cost_cex_usd = fill.total_usd
    raw_spread_bps = ((dex_proceeds_usd - gross_cost_cex_usd) / gross_cost_cex_usd) * _BPS_DIVISOR

    total_cex_cost_factor = (taker_fee_bps + haircut_bps) / _BPS_DIVISOR
    effective_cost_cex = gross_cost_cex_usd * (Decimal(1) + total_cex_cost_factor)
    net_profit_usd = dex_proceeds_usd - effective_cost_cex - gas_usd
    net_bps = (net_profit_usd / gross_cost_cex_usd) * _BPS_DIVISOR

    maker_factor = haircut_bps / _BPS_DIVISOR
    maker_cost_cex = gross_cost_cex_usd * (Decimal(1) + maker_factor)
    maker_net_usd = dex_proceeds_usd - maker_cost_cex - gas_usd
    maker_net_bps = (maker_net_usd / gross_cost_cex_usd) * _BPS_DIVISOR

    return CexDexDirectionResult(
        direction="buy_cex_sell_dex",
        feasible=True,
        error_reason=None,
        qty_tokens=str(qty_tokens.normalize()),
        dex_notional_usd=_decimal_to_float(dex_proceeds_usd),
        cex_vwap_price_usd=_decimal_to_float(fill.vwap_price_usd),
        dex_effective_price_usd=_decimal_to_float(dex_eff_price),
        raw_spread_bps=_decimal_to_float(raw_spread_bps),
        net_bps=_decimal_to_float(net_bps),
        net_profit_usd=_decimal_to_float(net_profit_usd),
        maker_zero_fee_net_bps_diagnostic=_decimal_to_float(maker_net_bps),
        levels_consumed=fill.levels_consumed,
    )


def evaluate_cex_dex_opportunity(
    *,
    product_or_symbol: str,
    book: CoinbaseL2Book,
    trade_notional_usdc: int,
    dex_buy_token_for_usdc_out: int | None,
    trade_notional_token: int,
    dex_sell_token_for_usdc_out: int | None,
    gas_cost_usd: Decimal | int | float | str,
    cex_taker_fee_bps: Decimal | int | float | str = DEFAULT_CEX_TAKER_FEE_BPS,
    latency_haircut_bps: Decimal | int | float | str = DEFAULT_LATENCY_HAIRCUT_BPS,
) -> CexDexTickEvaluation:
    """Evaluate bidirectional Coinbase L2 VWAP vs pinned Base DEX quotes."""
    spec = resolve_coinbase_product(product_or_symbol)
    if book.product_id != spec.product_id:
        raise ValueError(
            f"book product_id {book.product_id} does not match requested {spec.product_id}"
        )
    if type(trade_notional_usdc) is not int or trade_notional_usdc <= 0:
        raise ValueError("trade_notional_usdc must be a positive integer")
    if type(trade_notional_token) is not int or trade_notional_token <= 0:
        raise ValueError("trade_notional_token must be a positive integer")
    if dex_buy_token_for_usdc_out is not None and type(dex_buy_token_for_usdc_out) is not int:
        raise TypeError("dex_buy_token_for_usdc_out must be an int or None")
    if dex_sell_token_for_usdc_out is not None and type(dex_sell_token_for_usdc_out) is not int:
        raise TypeError("dex_sell_token_for_usdc_out must be an int or None")

    gas_usd = _to_finite_decimal(gas_cost_usd, "gas_cost_usd", require_nonneg=True)
    taker_bps = _to_finite_decimal(cex_taker_fee_bps, "cex_taker_fee_bps", require_nonneg=True)
    haircut_bps = _to_finite_decimal(
        latency_haircut_bps, "latency_haircut_bps", require_nonneg=True
    )

    dir_buy_dex = _evaluate_buy_dex_sell_cex(
        spec=spec,
        book=book,
        trade_notional_usdc=trade_notional_usdc,
        dex_buy_token_for_usdc_out=dex_buy_token_for_usdc_out,
        gas_usd=gas_usd,
        taker_fee_bps=taker_bps,
        haircut_bps=haircut_bps,
    )
    dir_buy_cex = _evaluate_buy_cex_sell_dex(
        spec=spec,
        book=book,
        trade_notional_token=trade_notional_token,
        dex_sell_token_for_usdc_out=dex_sell_token_for_usdc_out,
        gas_usd=gas_usd,
        taker_fee_bps=taker_bps,
        haircut_bps=haircut_bps,
    )

    feasible_dirs = [d for d in (dir_buy_dex, dir_buy_cex) if d.feasible and d.net_bps is not None]
    if not feasible_dirs:
        reasons = ", ".join(
            f"{d.direction}:{d.error_reason}"
            for d in (dir_buy_dex, dir_buy_cex)
            if d.error_reason is not None
        )
        return CexDexTickEvaluation(
            product_id=spec.product_id,
            token_symbol=spec.token_symbol,
            status="degraded",
            error_reason=f"no_feasible_direction ({reasons})",
            cex_taker_fee_bps=_decimal_to_float(taker_bps),
            latency_haircut_bps=_decimal_to_float(haircut_bps),
            gas_cost_usd=_decimal_to_float(gas_usd),
            best_bid_usd=_decimal_to_float(book.best_bid),
            best_ask_usd=_decimal_to_float(book.best_ask),
            cex_mid_usd=_decimal_to_float(book.mid_price),
            book_sequence=book.sequence,
            book_sha256=book.raw_sha256,
            buy_dex_sell_cex=dir_buy_dex,
            buy_cex_sell_dex=dir_buy_cex,
            best_direction=None,
            best_net_bps=None,
            best_raw_spread_bps=None,
            maker_zero_fee_net_bps=None,
            book_level=book.book_level,
        )

    best_dir = max(feasible_dirs, key=lambda d: cast(float, d.net_bps))
    raw_spreads = [d.raw_spread_bps for d in feasible_dirs if d.raw_spread_bps is not None]
    maker_bps_list = [
        d.maker_zero_fee_net_bps_diagnostic
        for d in feasible_dirs
        if d.maker_zero_fee_net_bps_diagnostic is not None
    ]
    return CexDexTickEvaluation(
        product_id=spec.product_id,
        token_symbol=spec.token_symbol,
        status="ok",
        error_reason=None,
        cex_taker_fee_bps=_decimal_to_float(taker_bps),
        latency_haircut_bps=_decimal_to_float(haircut_bps),
        gas_cost_usd=_decimal_to_float(gas_usd),
        best_bid_usd=_decimal_to_float(book.best_bid),
        best_ask_usd=_decimal_to_float(book.best_ask),
        cex_mid_usd=_decimal_to_float(book.mid_price),
        book_sequence=book.sequence,
        book_sha256=book.raw_sha256,
        buy_dex_sell_cex=dir_buy_dex,
        buy_cex_sell_dex=dir_buy_cex,
        best_direction=best_dir.direction,
        best_net_bps=best_dir.net_bps,
        best_raw_spread_bps=max(raw_spreads) if raw_spreads else None,
        maker_zero_fee_net_bps=max(maker_bps_list) if maker_bps_list else None,
        book_level=book.book_level,
    )


def evaluate_live_cex_dex_shadow(
    *,
    product_or_symbol: str,
    trade_notional_usdc: int,
    dex_buy_token_for_usdc_out: int | None,
    trade_notional_token: int,
    dex_sell_token_for_usdc_out: int | None,
    gas_cost_usd: Decimal | int | float | str,
    cex_taker_fee_bps: Decimal | int | float | str = DEFAULT_CEX_TAKER_FEE_BPS,
    latency_haircut_bps: Decimal | int | float | str = DEFAULT_LATENCY_HAIRCUT_BPS,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    book: CoinbaseL2Book | None = None,
    transport: Callable[[str, float], bytes] | None = None,
) -> CexDexTickEvaluation:
    """Fetch Coinbase L2 book (falling back once to level=1 on >1MB byte limit) and evaluate."""
    spec = resolve_coinbase_product(product_or_symbol)
    if type(trade_notional_usdc) is not int or trade_notional_usdc <= 0:
        raise ValueError("trade_notional_usdc must be a positive integer")
    if type(trade_notional_token) is not int or trade_notional_token <= 0:
        raise ValueError("trade_notional_token must be a positive integer")
    gas_usd = _to_finite_decimal(gas_cost_usd, "gas_cost_usd", require_nonneg=True)
    taker_bps = _to_finite_decimal(cex_taker_fee_bps, "cex_taker_fee_bps", require_nonneg=True)
    haircut_bps = _to_finite_decimal(
        latency_haircut_bps, "latency_haircut_bps", require_nonneg=True
    )

    active_book = book
    if active_book is None:
        try:
            try:
                active_book = fetch_coinbase_l2_book(
                    spec.product_id,
                    level=2,
                    timeout_s=timeout_s,
                    transport=transport,
                )
            except LiveCexDexError as exc:
                msg = str(exc)
                if "byte limit" not in msg and "MAX_RESPONSE_BYTES" not in msg:
                    raise
                active_book = fetch_coinbase_l2_book(
                    spec.product_id,
                    level=1,
                    timeout_s=timeout_s,
                    transport=transport,
                )
        except (LiveCexDexError, OSError, TimeoutError, urllib.error.URLError, ValueError) as exc:
            return CexDexTickEvaluation(
                product_id=spec.product_id,
                token_symbol=spec.token_symbol,
                status="degraded",
                error_reason=str(exc) or exc.__class__.__name__,
                cex_taker_fee_bps=_decimal_to_float(taker_bps),
                latency_haircut_bps=_decimal_to_float(haircut_bps),
                gas_cost_usd=_decimal_to_float(gas_usd),
                best_bid_usd=None,
                best_ask_usd=None,
                cex_mid_usd=None,
                book_sequence=None,
                book_sha256=None,
                buy_dex_sell_cex=None,
                buy_cex_sell_dex=None,
                best_direction=None,
                best_net_bps=None,
                best_raw_spread_bps=None,
                maker_zero_fee_net_bps=None,
                book_level=2,
            )

    return evaluate_cex_dex_opportunity(
        product_or_symbol=spec.product_id,
        book=active_book,
        trade_notional_usdc=trade_notional_usdc,
        dex_buy_token_for_usdc_out=dex_buy_token_for_usdc_out,
        trade_notional_token=trade_notional_token,
        dex_sell_token_for_usdc_out=dex_sell_token_for_usdc_out,
        gas_cost_usd=gas_usd,
        cex_taker_fee_bps=taker_bps,
        latency_haircut_bps=haircut_bps,
    )


def _price_quant_for_mid(mid: Decimal) -> Decimal:
    if mid >= Decimal(100):
        return Decimal("0.01")
    if mid >= Decimal(1):
        return Decimal("0.0001")
    if mid >= Decimal("0.05"):
        return Decimal("0.00001")
    return Decimal("0.000001")


def build_synthetic_coinbase_l2_book(
    product_or_symbol: str = "ETH-USD",
    *,
    mid_price: Decimal | int | float | str = Decimal("3000.00"),
    spread_bps: Decimal | int | float | str = Decimal("1.0"),
    step_bps: Decimal | int | float | str = Decimal("2.0"),
    level_size: Decimal | int | float | str = Decimal("5.0"),
    levels: int = 5,
    sequence: int = 1000,
    retrieved_at_utc: str = "2026-11-18T00:00:00+00:00",
) -> CoinbaseL2Book:
    """Construct a deterministic non-crossed Coinbase L2 book for fixture/unit tests."""
    spec = resolve_coinbase_product(product_or_symbol)
    mid = _to_finite_decimal(mid_price, "mid_price", require_positive=True)
    half_spread = _to_finite_decimal(spread_bps, "spread_bps", require_positive=True) / Decimal(2)
    step = _to_finite_decimal(step_bps, "step_bps", require_positive=True)
    size = _to_finite_decimal(level_size, "level_size", require_positive=True)
    if type(levels) is not int or levels <= 0:
        raise ValueError("levels must be a positive integer")
    if type(sequence) is not int or sequence < 0:
        raise ValueError("sequence must be a non-negative integer")

    quant = _price_quant_for_mid(mid)
    bids_list: list[CoinbaseL2Level] = []
    asks_list: list[CoinbaseL2Level] = []
    for idx in range(levels):
        bid_offset = (half_spread + step * idx) / _BPS_DIVISOR
        ask_offset = (half_spread + step * idx) / _BPS_DIVISOR
        bid_p = (mid * (Decimal(1) - bid_offset)).quantize(quant)
        ask_p = (mid * (Decimal(1) + ask_offset)).quantize(quant)
        if bid_p <= 0:
            bid_p = quant
        if bid_p >= ask_p:
            ask_p = bid_p + quant
        if bids_list and bid_p >= bids_list[-1].price:
            bid_p = bids_list[-1].price - quant
        if bid_p <= 0:
            raise ValueError("synthetic book bid price quantized to non-positive value")
        if asks_list and ask_p <= asks_list[-1].price:
            ask_p = asks_list[-1].price + quant
        bids_list.append(CoinbaseL2Level(price=bid_p, size=size, num_orders=1))
        asks_list.append(CoinbaseL2Level(price=ask_p, size=size, num_orders=1))

    payload = canonical_json(
        {
            "sequence": sequence,
            "bids": [[str(lvl.price), str(lvl.size), lvl.num_orders] for lvl in bids_list],
            "asks": [[str(lvl.price), str(lvl.size), lvl.num_orders] for lvl in asks_list],
            "auction_mode": False,
        }
    )
    return CoinbaseL2Book(
        product_id=spec.product_id,
        bids=tuple(bids_list),
        asks=tuple(asks_list),
        sequence=sequence,
        retrieved_at_utc=retrieved_at_utc,
        raw_sha256=sha256_hex(payload),
    )


def serialize_coinbase_book_payload(book: CoinbaseL2Book, *, level: Literal[1, 2] = 2) -> bytes:
    """Serialize a `CoinbaseL2Book` into Coinbase Exchange level-1 or level-2 JSON bytes."""
    if type(level) is not int or level not in (1, 2):
        raise ValueError("level must be 1 or 2")
    bids_slice = book.bids[:1] if level == 1 else book.bids
    asks_slice = book.asks[:1] if level == 1 else book.asks
    return canonical_json(
        {
            "sequence": book.sequence if book.sequence is not None else 1,
            "bids": [[str(lvl.price), str(lvl.size), lvl.num_orders] for lvl in bids_slice],
            "asks": [[str(lvl.price), str(lvl.size), lvl.num_orders] for lvl in asks_slice],
            "auction_mode": False,
        }
    )


def build_synthetic_coinbase_transport(
    books_by_product: Mapping[str, CoinbaseL2Book] | None = None,
    *,
    default_mid_by_product: Mapping[str, Decimal | int | float | str] | None = None,
    fail_products: Sequence[str] = (),
) -> Callable[[str, float], bytes]:
    """Build deterministic offline Coinbase L2 GET transport for fixture mode and tests."""
    fail_set = {resolve_coinbase_product(item).product_id for item in fail_products}
    default_mids: dict[str, Decimal | int | float | str] = {
        "ETH-USD": Decimal("3000.00"),
        "BTC-USD": Decimal("90000.00"),
        "AERO-USD": Decimal("1.20"),
        "VIRTUAL-USD": Decimal("1.50"),
        "VVV-USD": Decimal("4.00"),
        "MORPHO-USD": Decimal("2.00"),
        "DEGEN-USD": Decimal("0.0050"),
        "WELL-USD": Decimal("0.0500"),
        "CBETH-USD": Decimal("3250.00"),
    }
    if default_mid_by_product is not None:
        for k, v in default_mid_by_product.items():
            default_mids[resolve_coinbase_product(k).product_id] = v

    resolved_books: dict[str, CoinbaseL2Book] = {}
    if books_by_product is not None:
        for k, b in books_by_product.items():
            resolved_books[resolve_coinbase_product(k).product_id] = b

    def _transport(url: str, _timeout_s: float) -> bytes:
        parsed = urllib.parse.urlsplit(url)
        parts = [seg for seg in parsed.path.split("/") if seg]
        if len(parts) != 3 or parts[0] != "products" or parts[2] != "book":
            raise LiveCexDexError(f"unexpected synthetic Coinbase URL path: {parsed.path}")
        product_id = resolve_coinbase_product(parts[1]).product_id
        if product_id in fail_set:
            raise LiveCexDexError(f"simulated Coinbase outage for {product_id}")
        book = resolved_books.get(product_id)
        if book is None:
            book = build_synthetic_coinbase_l2_book(product_id, mid_price=default_mids[product_id])
        level: Literal[1, 2] = 1 if parsed.query == "level=1" else 2
        return serialize_coinbase_book_payload(book, level=level)

    return _transport


__all__ = [
    "COINBASE_ALLOWED_HOSTS",
    "COINBASE_BOOK_LEVEL1_URL_TEMPLATE",
    "COINBASE_BOOK_URL_TEMPLATE",
    "DEFAULT_CEX_TAKER_FEE_BPS",
    "DEFAULT_LATENCY_HAIRCUT_BPS",
    "DEFAULT_TIMEOUT_S",
    "DEFAULT_TRADE_NOTIONAL_USDC",
    "MAX_RESPONSE_BYTES",
    "SUPPORTED_PRODUCTS",
    "CexDexDirectionResult",
    "CexDexTickEvaluation",
    "CoinbaseL2Book",
    "CoinbaseL2Level",
    "CoinbaseProductSpec",
    "LiveCexDexError",
    "VwapFillResult",
    "build_synthetic_coinbase_l2_book",
    "build_synthetic_coinbase_transport",
    "evaluate_cex_dex_opportunity",
    "evaluate_live_cex_dex_shadow",
    "fetch_coinbase_l2_book",
    "gas_wei_to_usd",
    "parse_coinbase_l2_book",
    "resolve_coinbase_product",
    "serialize_coinbase_book_payload",
]
