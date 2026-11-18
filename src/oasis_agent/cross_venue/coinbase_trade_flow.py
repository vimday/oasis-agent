"""Keyless Coinbase Exchange public trade-flow client (REST `/trades` + RFC-6455 TLS WS).

Provides strict DNS/SSRF-validated IPv4-preferred HTTPS REST trade polling (bypassing
local proxy variables via `urllib.request.ProxyHandler({})`) and a pure Python stdlib
RFC-6455 TLS WebSocket client for Coinbase Exchange's public `matches` channel.

All outputs are read-only shadow market-data prints (`CoinbaseTradePrint`).
"""

import base64
import errno
import hashlib
import hmac
import ipaddress
import json
import math
import os
import re
import select
import socket
import ssl
import struct
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Final, Literal, Self, cast

import certifi

from .live_cex_dex_shadow import (
    _USER_AGENT,
    COINBASE_ALLOWED_HOSTS,
    LiveCexDexError,
    _decimal_to_float,
    _IPv4PreferredHTTPSHandler,
    _is_forbidden_ip,
    _is_tun_fake_ip,
    resolve_coinbase_product,
)

MAX_TRADES_RESPONSE_BYTES: Final[int] = 1_048_576
MAX_WS_FRAME_BYTES: Final[int] = 1_048_576
COINBASE_TRADES_URL_TEMPLATE: Final[str] = (
    "https://api.exchange.coinbase.com/products/{product}/trades?limit={limit}"
)
COINBASE_WS_HOST: Final[str] = "ws-feed.exchange.coinbase.com"
COINBASE_WS_PORT: Final[int] = 443
COINBASE_WS_ALLOWED_HOSTS: Final[frozenset[str]] = frozenset({COINBASE_WS_HOST})
RFC6455_GUID: Final[str] = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

_MAX_HANDSHAKE_BYTES: Final[int] = 16_384
_RECV_CHUNK_BYTES: Final[int] = 65_536
_COINBASE_TRADES_PATH_RE: Final[re.Pattern[str]] = re.compile(r"^/products/([A-Za-z0-9-]+)/trades$")
_COINBASE_TRADES_QUERY_RE: Final[re.Pattern[str]] = re.compile(r"^limit=([1-9][0-9]*)$")

_WS_OP_CONTINUATION: Final[int] = 0x0
_WS_OP_TEXT: Final[int] = 0x1
_WS_OP_BINARY: Final[int] = 0x2
_WS_OP_CLOSE: Final[int] = 0x8
_WS_OP_PING: Final[int] = 0x9
_WS_OP_PONG: Final[int] = 0xA


class CoinbaseTradeFlowError(ValueError):
    """Fail-closed error on Coinbase trade-flow transport, framing, or payload parsing."""


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        if fp is not None:
            with suppress(OSError, AttributeError):
                fp.close()
        raise CoinbaseTradeFlowError("Coinbase trades redirects are forbidden")


def _resolve_supported_product(product_id: str) -> str:
    try:
        return resolve_coinbase_product(product_id).product_id
    except ValueError as exc:
        raise CoinbaseTradeFlowError(str(exc)) from exc


def _to_strict_positive_decimal(value: object, name: str) -> Decimal:
    if isinstance(value, bool):
        raise CoinbaseTradeFlowError(f"{name} must not be a boolean")
    if isinstance(value, float):
        raise CoinbaseTradeFlowError(f"{name} must be Decimal, int, or decimal string, not float")
    try:
        if isinstance(value, Decimal):
            dec = value
        elif isinstance(value, int):
            dec = Decimal(value)
        elif isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                raise CoinbaseTradeFlowError(f"{name} must not be empty")
            dec = Decimal(stripped)
        else:
            raise CoinbaseTradeFlowError(
                f"{name} has unsupported numeric type {type(value).__name__}"
            )
    except InvalidOperation as exc:
        raise CoinbaseTradeFlowError(f"{name} is not a valid decimal number") from exc

    if not dec.is_finite() or dec <= 0:
        raise CoinbaseTradeFlowError(f"{name} must be a positive finite decimal")
    return dec


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise CoinbaseTradeFlowError("Coinbase JSON contains duplicate fields")
        result[key] = value
    return result


def _reject_constant(_value: str) -> object:
    raise CoinbaseTradeFlowError("Coinbase JSON contains non-finite numbers")


def _finite_decimal_literal(value: str) -> Decimal:
    try:
        dec = Decimal(value)
    except InvalidOperation as exc:
        raise CoinbaseTradeFlowError("Coinbase JSON contains invalid decimal number") from exc
    if not dec.is_finite():
        raise CoinbaseTradeFlowError("Coinbase JSON contains non-finite decimal number")
    return dec


def _decode_json_strict(raw_bytes: bytes) -> object:
    try:
        return json.loads(
            raw_bytes.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
            parse_float=_finite_decimal_literal,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise CoinbaseTradeFlowError("Coinbase payload is not valid JSON") from exc


def _validate_addr_infos(addr_infos: list[Any]) -> list[Any]:
    if not addr_infos:
        raise CoinbaseTradeFlowError("Coinbase DNS returned no addresses")
    for _, _, _, _, sockaddr in addr_infos:
        raw_ip = str(sockaddr[0]).split("%", 1)[0]
        ip_obj = ipaddress.ip_address(raw_ip)
        is_mapped_tun = (
            isinstance(ip_obj, ipaddress.IPv6Address)
            and ip_obj.ipv4_mapped is not None
            and _is_tun_fake_ip(ip_obj.ipv4_mapped)
        )
        if _is_forbidden_ip(ip_obj) and not _is_tun_fake_ip(ip_obj) and not is_mapped_tun:
            raise CoinbaseTradeFlowError("Coinbase DNS resolved to a non-public IP")
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
        raise CoinbaseTradeFlowError("Coinbase endpoint forbids IP literals")

    try:
        if family == socket.AF_UNSPEC:
            addr_infos = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
        else:
            addr_infos = socket.getaddrinfo(hostname, port, family=family, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise CoinbaseTradeFlowError("Coinbase DNS resolution failed") from exc
    return _validate_addr_infos(addr_infos)


def _validate_coinbase_trades_url(url: str) -> str:
    if not isinstance(url, str) or not url or url != url.strip():
        raise CoinbaseTradeFlowError("Coinbase trades URL must be a non-empty string")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise CoinbaseTradeFlowError("Coinbase trades client requires an https:// endpoint")
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise CoinbaseTradeFlowError("Coinbase trades URL forbids userinfo credentials")
    hostname = parsed.hostname
    if not hostname or hostname.lower() not in COINBASE_ALLOWED_HOSTS:
        raise CoinbaseTradeFlowError("Coinbase trades URL host is not allowlisted")
    try:
        if parsed.port is not None:
            raise CoinbaseTradeFlowError("Coinbase trades URL forbids explicit custom ports")
    except ValueError as exc:
        raise CoinbaseTradeFlowError("Coinbase trades URL contains an invalid port") from exc
    if parsed.fragment:
        raise CoinbaseTradeFlowError("Coinbase trades URL forbids fragments")
    path_match = _COINBASE_TRADES_PATH_RE.fullmatch(parsed.path)
    if path_match is None:
        raise CoinbaseTradeFlowError(
            "Coinbase trades URL path must target /products/<supported-product>/trades"
        )
    _resolve_supported_product(path_match.group(1))
    query_match = _COINBASE_TRADES_QUERY_RE.fullmatch(parsed.query)
    if query_match is None:
        raise CoinbaseTradeFlowError("Coinbase trades URL query must be limit=<1..1000>")
    limit_val = int(query_match.group(1))
    if not 1 <= limit_val <= 1000:
        raise CoinbaseTradeFlowError("Coinbase trades URL limit must be in 1..1000")
    return url


@dataclass(frozen=True, slots=True)
class CoinbaseTradePrint:
    """Validated single Coinbase Exchange public execution print.

    In Coinbase Exchange's public REST `/trades` and WebSocket `matches` schemas,
    `side` indicates the **maker** side resting on the orderbook (`"sell"` means
    a resting ask was lifted by a buyer; `"buy"` means a resting bid was hit by
    a seller).
    """

    product_id: str
    trade_id: int
    price: Decimal
    size: Decimal
    maker_side: Literal["buy", "sell"]
    traded_at_utc: str

    def __post_init__(self) -> None:
        canonical_pid = _resolve_supported_product(self.product_id)
        if self.product_id != canonical_pid:
            object.__setattr__(self, "product_id", canonical_pid)
        if type(self.trade_id) is not int or self.trade_id <= 0:
            raise CoinbaseTradeFlowError("trade_id must be a positive integer")
        price_dec = _to_strict_positive_decimal(self.price, "price")
        size_dec = _to_strict_positive_decimal(self.size, "size")
        if self.maker_side not in ("buy", "sell"):
            raise CoinbaseTradeFlowError(f"invalid maker_side: {self.maker_side!r}")
        if not isinstance(self.traded_at_utc, str) or not self.traded_at_utc.strip():
            raise CoinbaseTradeFlowError("traded_at_utc must be a non-empty timestamp")
        object.__setattr__(self, "price", price_dec)
        object.__setattr__(self, "size", size_dec)

    @property
    def notional_usd(self) -> Decimal:
        """Quote USD notional of the trade print (`price * size`)."""
        return self.price * self.size

    def to_dict(self) -> dict[str, Any]:
        """Serialize trade print to a JSON-compatible dictionary."""
        return {
            "product_id": self.product_id,
            "trade_id": self.trade_id,
            "price": str(self.price),
            "size": str(self.size),
            "maker_side": self.maker_side,
            "traded_at_utc": self.traded_at_utc,
            "notional_usd": _decimal_to_float(self.notional_usd),
        }


def _parse_single_trade_mapping(
    raw_entry: object,
    *,
    expected_product_id: str,
    context: str,
) -> CoinbaseTradePrint:
    if not isinstance(raw_entry, dict):
        raise CoinbaseTradeFlowError(f"{context} must be a JSON object")

    raw_pid = raw_entry.get("product_id")
    if raw_pid is not None:
        if not isinstance(raw_pid, str) or not raw_pid.strip():
            raise CoinbaseTradeFlowError(f"{context}.product_id must be a non-empty string")
        entry_pid = _resolve_supported_product(raw_pid)
        if entry_pid != expected_product_id:
            raise CoinbaseTradeFlowError(
                f"{context}.product_id {entry_pid} does not match {expected_product_id}"
            )

    raw_side = raw_entry.get("side")
    if raw_side not in ("buy", "sell"):
        raise CoinbaseTradeFlowError(f"{context}.side must be 'buy' or 'sell'")

    raw_id = raw_entry.get("trade_id")
    if type(raw_id) is not int or raw_id <= 0:
        raise CoinbaseTradeFlowError(f"{context}.trade_id must be a positive integer")

    raw_time = raw_entry.get("time")
    if not isinstance(raw_time, str) or not raw_time.strip():
        raise CoinbaseTradeFlowError(f"{context}.time must be a non-empty string")

    return CoinbaseTradePrint(
        product_id=expected_product_id,
        trade_id=raw_id,
        price=_to_strict_positive_decimal(raw_entry.get("price"), f"{context}.price"),
        size=_to_strict_positive_decimal(raw_entry.get("size"), f"{context}.size"),
        maker_side=cast(Literal["buy", "sell"], raw_side),
        traded_at_utc=raw_time,
    )


def parse_coinbase_trades_payload(
    product_id: str,
    raw_bytes: bytes,
    *,
    since_trade_id: int | None = None,
) -> tuple[CoinbaseTradePrint, ...]:
    """Strictly parse Coinbase `/products/<product>/trades` JSON bytes into sorted prints."""
    canonical_pid = _resolve_supported_product(product_id)
    if since_trade_id is not None and (type(since_trade_id) is not int or since_trade_id < 0):
        raise CoinbaseTradeFlowError("since_trade_id must be a non-negative integer or None")
    if not isinstance(raw_bytes, bytes):
        raise TypeError("raw_bytes must be bytes")
    if len(raw_bytes) > MAX_TRADES_RESPONSE_BYTES:
        raise CoinbaseTradeFlowError("Coinbase trades payload exceeds MAX_TRADES_RESPONSE_BYTES")

    decoded = _decode_json_strict(raw_bytes)
    if not isinstance(decoded, list):
        raise CoinbaseTradeFlowError("Coinbase trades payload must be a JSON array")

    by_trade_id: dict[int, CoinbaseTradePrint] = {}
    for idx, entry in enumerate(decoded):
        trade_print = _parse_single_trade_mapping(
            entry,
            expected_product_id=canonical_pid,
            context=f"trades[{idx}]",
        )
        existing = by_trade_id.get(trade_print.trade_id)
        if existing is not None:
            if existing != trade_print:
                raise CoinbaseTradeFlowError(
                    f"conflicting trade print for duplicate trade_id {trade_print.trade_id}"
                )
            continue
        by_trade_id[trade_print.trade_id] = trade_print

    filtered = [
        item for tid, item in by_trade_id.items() if since_trade_id is None or tid > since_trade_id
    ]
    filtered.sort(key=lambda item: item.trade_id)
    return tuple(filtered)


def _default_trades_https_get(url: str, timeout_s: float) -> bytes:
    _validate_coinbase_trades_url(url)
    parsed = urllib.parse.urlsplit(url)
    if not parsed.hostname:
        raise CoinbaseTradeFlowError("Coinbase trades URL missing hostname")
    _resolve_and_validate_addrs(parsed.hostname, 443)

    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
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
            raw = cast(bytes, response.read(MAX_TRADES_RESPONSE_BYTES + 1))
            if len(raw) > MAX_TRADES_RESPONSE_BYTES:
                raise CoinbaseTradeFlowError("Coinbase trades response exceeds byte limit")
            return raw
    except urllib.error.HTTPError as exc:
        with suppress(OSError):
            exc.close()
        raise CoinbaseTradeFlowError(f"Coinbase trades HTTP failure: status {exc.code}") from None
    except LiveCexDexError as exc:
        raise CoinbaseTradeFlowError(str(exc)) from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        if isinstance(exc, CoinbaseTradeFlowError):
            raise
        raise CoinbaseTradeFlowError(f"Coinbase trades HTTPS transport failure: {exc}") from None


def fetch_coinbase_recent_trades(
    product_id: str,
    *,
    limit: int = 50,
    since_trade_id: int | None = None,
    timeout_s: float = 8.0,
    transport: Callable[[str, float], bytes] | None = None,
) -> tuple[CoinbaseTradePrint, ...]:
    """Fetch and strictly parse recent Coinbase Exchange public trade prints."""
    canonical_pid = _resolve_supported_product(product_id)
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise CoinbaseTradeFlowError("limit must be an integer in 1..1000")
    if since_trade_id is not None and (type(since_trade_id) is not int or since_trade_id < 0):
        raise CoinbaseTradeFlowError("since_trade_id must be a non-negative integer or None")
    if (
        isinstance(timeout_s, bool)
        or not isinstance(timeout_s, (int, float))
        or not math.isfinite(float(timeout_s))
        or float(timeout_s) <= 0
    ):
        raise CoinbaseTradeFlowError("timeout_s must be a positive finite number")

    url = _validate_coinbase_trades_url(
        COINBASE_TRADES_URL_TEMPLATE.format(product=canonical_pid, limit=limit)
    )
    getter = _default_trades_https_get if transport is None else transport
    raw_bytes = getter(url, float(timeout_s))
    return parse_coinbase_trades_payload(
        canonical_pid,
        raw_bytes,
        since_trade_id=since_trade_id,
    )


def compute_websocket_accept(sec_websocket_key: str) -> str:
    """Compute the RFC-6455 `Sec-WebSocket-Accept` digest for a client handshake key."""
    if not isinstance(sec_websocket_key, str) or not sec_websocket_key:
        raise CoinbaseTradeFlowError("sec_websocket_key must be a non-empty string")
    digest = hashlib.sha1(
        (sec_websocket_key + RFC6455_GUID).encode("ascii"),
        usedforsecurity=False,
    ).digest()
    return base64.b64encode(digest).decode("ascii")


def build_websocket_frame(
    opcode: int,
    payload: bytes = b"",
    *,
    masked: bool = True,
    fin: bool = True,
    mask_key: bytes | None = None,
) -> bytes:
    """Encode a single RFC-6455 WebSocket frame."""
    if type(opcode) is not int or not 0 <= opcode <= 0xF:
        raise CoinbaseTradeFlowError("WebSocket opcode must be in 0..15")
    if not isinstance(payload, bytes):
        raise TypeError("payload must be bytes")
    if opcode >= 0x8:
        if not fin:
            raise CoinbaseTradeFlowError("WebSocket control frame must set fin=True")
        if len(payload) > 125:
            raise CoinbaseTradeFlowError("WebSocket control frame payload exceeds 125 bytes")

    b0 = (0x80 if fin else 0x00) | opcode
    mask_bit = 0x80 if masked else 0x00
    payload_len = len(payload)
    if payload_len < 126:
        header = bytes((b0, mask_bit | payload_len))
    elif payload_len <= 0xFFFF:
        header = struct.pack("!BBH", b0, mask_bit | 126, payload_len)
    else:
        header = struct.pack("!BBQ", b0, mask_bit | 127, payload_len)

    if not masked:
        return header + payload

    active_mask = os.urandom(4) if mask_key is None else mask_key
    if not isinstance(active_mask, bytes) or len(active_mask) != 4:
        raise CoinbaseTradeFlowError("WebSocket mask_key must be 4 bytes")
    masked_payload = bytes(byte ^ active_mask[i & 3] for i, byte in enumerate(payload))
    return header + active_mask + masked_payload


def _try_connect_tls_candidates(
    candidates: list[Any],
    server_hostname: str,
    timeout_s: float,
    tls_ctx: ssl.SSLContext,
) -> tuple[ssl.SSLSocket | None, OSError | None, bool]:
    last_exc: OSError | None = None
    saw_ipv6_ssl_eof = False
    for af, socktype, proto, _canon, sockaddr in candidates:
        raw_sock: socket.socket | None = None
        try:
            raw_sock = socket.socket(af, socktype, proto)
            raw_sock.settimeout(timeout_s)
            raw_sock.connect(sockaddr)
            try:
                raw_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError as opt_exc:
                if opt_exc.errno != errno.ENOPROTOOPT:
                    raise
            wrapped = tls_ctx.wrap_socket(raw_sock, server_hostname=server_hostname)
            return wrapped, None, saw_ipv6_ssl_eof
        except OSError as exc:
            last_exc = exc
            if af == socket.AF_INET6 and isinstance(exc, ssl.SSLEOFError):
                saw_ipv6_ssl_eof = True
            if raw_sock is not None:
                with suppress(OSError):
                    raw_sock.close()
    return None, last_exc, saw_ipv6_ssl_eof


def _connect_tls_ipv4_preferred(host: str, port: int, timeout_s: float) -> ssl.SSLSocket:
    ordered_addrs = _resolve_and_validate_addrs(host, port)
    tls_ctx = ssl.create_default_context(cafile=certifi.where())
    wrapped, last_exc, saw_ipv6_ssl_eof = _try_connect_tls_candidates(
        ordered_addrs, host, timeout_s, tls_ctx
    )
    if wrapped is not None:
        return wrapped

    has_ipv4 = any(info[0] == socket.AF_INET for info in ordered_addrs)
    if saw_ipv6_ssl_eof and not has_ipv4:
        try:
            ipv4_candidates = [
                info
                for info in _resolve_and_validate_addrs(host, port, family=socket.AF_INET)
                if info[0] == socket.AF_INET
            ]
        except CoinbaseTradeFlowError as dns_exc:
            if "non-public" in str(dns_exc) or "forbids" in str(dns_exc):
                raise
            ipv4_candidates = []
        if ipv4_candidates:
            wrapped_v4, ipv4_exc, _ = _try_connect_tls_candidates(
                ipv4_candidates, host, timeout_s, tls_ctx
            )
            if wrapped_v4 is not None:
                return wrapped_v4
            last_exc = ipv4_exc

    raise CoinbaseTradeFlowError(f"Coinbase WebSocket TLS connection failed: {last_exc}") from None


def _parse_ws_frame_from_buffer(
    buf: bytearray,
    *,
    max_frame_bytes: int,
) -> tuple[bool, int, bytes] | None:
    buf_len = len(buf)
    if buf_len < 2:
        return None
    b0 = buf[0]
    b1 = buf[1]
    fin = bool(b0 & 0x80)
    rsv = (b0 >> 4) & 0x07
    opcode = b0 & 0x0F
    masked = bool(b1 & 0x80)
    length_code = b1 & 0x7F
    offset = 2

    if rsv != 0:
        raise CoinbaseTradeFlowError(f"unsupported WebSocket RSV bits: {rsv}")

    if length_code == 126:
        if buf_len < 4:
            return None
        payload_len = int.from_bytes(buf[2:4], "big")
        offset = 4
    elif length_code == 127:
        if buf_len < 10:
            return None
        payload_len = int.from_bytes(buf[2:10], "big")
        offset = 10
        if payload_len > (1 << 63) - 1:
            raise CoinbaseTradeFlowError("WebSocket frame payload length MSB must be zero")
    else:
        payload_len = length_code

    if payload_len > max_frame_bytes:
        raise CoinbaseTradeFlowError(
            f"WebSocket frame payload length {payload_len} exceeds limit {max_frame_bytes}"
        )

    if opcode >= 0x8:
        if not fin:
            raise CoinbaseTradeFlowError("WebSocket control frame must not be fragmented")
        if payload_len > 125:
            raise CoinbaseTradeFlowError("WebSocket control frame payload exceeds 125 bytes")

    if masked:
        if buf_len < offset + 4:
            return None
        mask_key = bytes(buf[offset : offset + 4])
        offset += 4
    else:
        mask_key = b""

    if buf_len < offset + payload_len:
        return None

    raw_payload = bytes(buf[offset : offset + payload_len])
    del buf[: offset + payload_len]
    if masked and payload_len > 0:
        payload = bytes(byte ^ mask_key[i & 3] for i, byte in enumerate(raw_payload))
    else:
        payload = raw_payload
    return fin, opcode, payload


def _parse_upgrade_response(head_bytes: bytes, expected_accept: str) -> None:
    try:
        head_text = head_bytes.decode("latin-1")
    except UnicodeDecodeError as exc:
        raise CoinbaseTradeFlowError("invalid WebSocket handshake response encoding") from exc
    lines = head_text.split("\r\n")
    if not lines:
        raise CoinbaseTradeFlowError("empty WebSocket handshake response")
    status_parts = lines[0].split(" ", 2)
    if len(status_parts) < 2 or status_parts[0].upper() != "HTTP/1.1" or status_parts[1] != "101":
        raise CoinbaseTradeFlowError(
            f"WebSocket handshake failed: expected 101 Switching Protocols, got {lines[0]!r}"
        )

    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line:
            continue
        name, sep, value = line.partition(":")
        key = name.strip().lower()
        if not sep or not key:
            raise CoinbaseTradeFlowError(f"malformed WebSocket handshake header: {line!r}")
        val = value.strip()
        if key in headers:
            if key in ("sec-websocket-accept", "upgrade"):
                raise CoinbaseTradeFlowError(f"duplicate WebSocket handshake header: {key}")
            headers[key] = f"{headers[key]}, {val}"
        else:
            headers[key] = val

    if headers.get("upgrade", "").lower() != "websocket":
        raise CoinbaseTradeFlowError("WebSocket handshake missing Upgrade: websocket header")
    conn_tokens = {tok.strip().lower() for tok in headers.get("connection", "").split(",")}
    if "upgrade" not in conn_tokens:
        raise CoinbaseTradeFlowError("WebSocket handshake missing Connection: Upgrade header")

    actual_accept = headers.get("sec-websocket-accept")
    if actual_accept is None or not hmac.compare_digest(actual_accept, expected_accept):
        raise CoinbaseTradeFlowError("WebSocket handshake Sec-WebSocket-Accept digest mismatch")


def _socket_has_readable_data(sock: Any) -> bool:
    pending_fn = getattr(sock, "pending", None)
    if callable(pending_fn):
        pending_count = pending_fn()
        if isinstance(pending_count, int) and pending_count > 0:
            return True

    fileno_fn = getattr(sock, "fileno", None)
    if callable(fileno_fn):
        try:
            fd = fileno_fn()
        except (OSError, ValueError):
            fd = -1
        if isinstance(fd, int) and fd >= 0:
            readable, _, _ = select.select([sock], [], [], 0.0)
            return bool(readable)

    return not callable(pending_fn)


class CoinbaseWebSocketFeed:
    """Pure stdlib RFC-6455 TLS client for Coinbase Exchange `matches` trade prints."""

    def __init__(
        self,
        product_ids: Sequence[str],
        *,
        host: str = COINBASE_WS_HOST,
        port: int = COINBASE_WS_PORT,
        path: str = "/",
        timeout_s: float = 8.0,
        max_frame_bytes: int = MAX_WS_FRAME_BYTES,
        auto_connect: bool = True,
        socket_connector: Callable[[str, int, float], Any] | None = None,
        key_bytes: bytes | None = None,
        mask_key_factory: Callable[[], bytes] | None = None,
    ) -> None:
        if isinstance(product_ids, (str, bytes)) or not product_ids:
            raise CoinbaseTradeFlowError("product_ids must be a non-empty sequence of products")
        resolved_pids = tuple(dict.fromkeys(_resolve_supported_product(pid) for pid in product_ids))
        if not isinstance(host, str) or host.lower() not in COINBASE_WS_ALLOWED_HOSTS:
            raise CoinbaseTradeFlowError("WebSocket host is not allowlisted")
        if type(port) is not int or port != COINBASE_WS_PORT:
            raise CoinbaseTradeFlowError("WebSocket port must be 443")
        if (
            not isinstance(path, str)
            or not path.startswith("/")
            or any(ch in path for ch in ("\r", "\n", " ", "\t", "#"))
        ):
            raise CoinbaseTradeFlowError("WebSocket path is invalid")
        if (
            isinstance(timeout_s, bool)
            or not isinstance(timeout_s, (int, float))
            or not math.isfinite(float(timeout_s))
            or float(timeout_s) <= 0
        ):
            raise CoinbaseTradeFlowError("timeout_s must be a positive finite number")
        if type(max_frame_bytes) is not int or not 1 <= max_frame_bytes <= MAX_WS_FRAME_BYTES:
            raise CoinbaseTradeFlowError(
                f"max_frame_bytes must be an integer in 1..{MAX_WS_FRAME_BYTES}"
            )
        if key_bytes is not None and (not isinstance(key_bytes, bytes) or len(key_bytes) != 16):
            raise CoinbaseTradeFlowError("key_bytes must be exactly 16 bytes when provided")
        if type(auto_connect) is not bool:
            raise CoinbaseTradeFlowError("auto_connect must be a boolean")

        self._product_ids: tuple[str, ...] = resolved_pids
        self._product_id_set: frozenset[str] = frozenset(resolved_pids)
        self._host: str = host.lower()
        self._port: int = port
        self._path: str = path
        self._timeout_s: float = float(timeout_s)
        self._max_frame_bytes: int = max_frame_bytes
        self._socket_connector = socket_connector
        self._key_bytes: bytes = os.urandom(16) if key_bytes is None else key_bytes
        self._mask_key_factory: Callable[[], bytes] = (
            (lambda: os.urandom(4)) if mask_key_factory is None else mask_key_factory
        )

        self._sock: Any | None = None
        self._connected: bool = False
        self._closed: bool = False
        self._recv_buffer: bytearray = bytearray()
        self._fragment_opcode: int | None = None
        self._fragment_buffer: bytearray = bytearray()
        self._last_trade_id_by_product: dict[str, int] = {}

        if auto_connect:
            self.connect()

    @property
    def product_ids(self) -> tuple[str, ...]:
        """Canonical subscribed Coinbase product IDs."""
        return self._product_ids

    @property
    def is_connected(self) -> bool:
        """Return True if handshake + subscribe completed and feed has not closed."""
        return self._connected and not self._closed

    @property
    def last_trade_id_by_product(self) -> dict[str, int]:
        """Return defensive copy of last seen `trade_id` watermark per product."""
        return dict(self._last_trade_id_by_product)

    def _close_socket_quietly(self) -> None:
        sock = self._sock
        self._sock = None
        if sock is not None:
            close_fn = getattr(sock, "close", None)
            if callable(close_fn):
                with suppress(OSError):
                    close_fn()

    def _set_socket_timeout(self, timeout_s: float) -> None:
        sock = self._sock
        if sock is None:
            return
        settimeout_fn = getattr(sock, "settimeout", None)
        if callable(settimeout_fn):
            settimeout_fn(timeout_s)

    def _send_frame(self, opcode: int, payload: bytes = b"") -> None:
        sock = self._sock
        if sock is None:
            raise CoinbaseTradeFlowError("WebSocket socket is closed")
        frame = build_websocket_frame(
            opcode,
            payload,
            masked=True,
            fin=True,
            mask_key=self._mask_key_factory(),
        )
        self._set_socket_timeout(self._timeout_s)
        sock.sendall(frame)

    def connect(self) -> None:
        """Open TLS socket, verify RFC-6455 HTTP 101 handshake, and subscribe to `matches`."""
        if self._closed:
            raise CoinbaseTradeFlowError("cannot connect a closed CoinbaseWebSocketFeed")
        if self._connected:
            return

        sec_key = base64.b64encode(self._key_bytes).decode("ascii")
        expected_accept = compute_websocket_accept(sec_key)
        handshake_req = (
            f"GET {self._path} HTTP/1.1\r\n"
            f"Host: {self._host}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {sec_key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            f"User-Agent: {_USER_AGENT}\r\n"
            "\r\n"
        ).encode("ascii")

        try:
            if self._socket_connector is None:
                self._sock = _connect_tls_ipv4_preferred(self._host, self._port, self._timeout_s)
            else:
                self._sock = self._socket_connector(self._host, self._port, self._timeout_s)
            self._set_socket_timeout(self._timeout_s)
            self._sock.sendall(handshake_req)

            header_buf = bytearray()
            while b"\r\n\r\n" not in header_buf:
                if len(header_buf) >= _MAX_HANDSHAKE_BYTES:
                    raise CoinbaseTradeFlowError(
                        "WebSocket handshake response exceeds header limit"
                    )
                chunk = self._sock.recv(4096)
                if not chunk:
                    raise CoinbaseTradeFlowError(
                        "WebSocket connection closed during HTTP upgrade handshake"
                    )
                header_buf.extend(chunk)
                if len(header_buf) > _MAX_HANDSHAKE_BYTES and b"\r\n\r\n" not in header_buf:
                    raise CoinbaseTradeFlowError(
                        "WebSocket handshake response exceeds header limit"
                    )

            head_bytes, trailing = bytes(header_buf).split(b"\r\n\r\n", 1)
            _parse_upgrade_response(head_bytes, expected_accept)
            self._recv_buffer.extend(trailing)

            subscribe_payload = json.dumps(
                {
                    "type": "subscribe",
                    "product_ids": list(self._product_ids),
                    "channels": ["matches"],
                },
                separators=(",", ":"),
            ).encode("utf-8")
            self._send_frame(_WS_OP_TEXT, subscribe_payload)
            self._connected = True
        except CoinbaseTradeFlowError:
            self._close_socket_quietly()
            self._closed = True
            raise
        except (OSError, ssl.SSLError, TimeoutError) as exc:
            self._close_socket_quietly()
            self._closed = True
            raise CoinbaseTradeFlowError(
                f"Coinbase WebSocket handshake/subscribe failed: {exc}"
            ) from None

    def _try_read_one_frame(self) -> tuple[tuple[bool, int, bytes] | None, bool]:
        sock = self._sock
        if sock is None:
            return None, True

        while True:
            parsed = _parse_ws_frame_from_buffer(
                self._recv_buffer,
                max_frame_bytes=self._max_frame_bytes,
            )
            if parsed is not None:
                return parsed, False
            if not _socket_has_readable_data(sock):
                return None, False

            self._set_socket_timeout(0.0)
            try:
                chunk = sock.recv(_RECV_CHUNK_BYTES)
            except (ssl.SSLWantReadError, BlockingIOError):
                return None, False
            finally:
                with suppress(OSError):
                    self._set_socket_timeout(self._timeout_s)

            if not chunk:
                return None, True
            self._recv_buffer.extend(chunk)
            if len(self._recv_buffer) > self._max_frame_bytes + 16:
                # Re-parse immediately to trigger frame cap check if header already arrived.
                parsed_after = _parse_ws_frame_from_buffer(
                    self._recv_buffer,
                    max_frame_bytes=self._max_frame_bytes,
                )
                if parsed_after is not None:
                    return parsed_after, False
                raise CoinbaseTradeFlowError("WebSocket receive buffer exceeds max_frame_bytes")

    def drain_events(
        self,
        max_frames: int = 200,
    ) -> tuple[tuple[CoinbaseTradePrint, ...], bool]:
        """Non-blockingly drain WebSocket frames and return deduplicated prints + gap flag."""
        if type(max_frames) is not int or max_frames <= 0:
            raise CoinbaseTradeFlowError("max_frames must be a positive integer")
        if not self._connected:
            raise CoinbaseTradeFlowError("WebSocket feed is not connected")
        if self._closed or self._sock is None:
            return (), True

        gap_detected = False
        batch_by_key: dict[tuple[str, int], CoinbaseTradePrint] = {}

        for _ in range(max_frames):
            try:
                frame, eof = self._try_read_one_frame()
            except CoinbaseTradeFlowError:
                self._close_socket_quietly()
                self._closed = True
                raise
            except (OSError, ssl.SSLError, TimeoutError):
                self._close_socket_quietly()
                self._closed = True
                gap_detected = True
                break

            if eof:
                self._close_socket_quietly()
                self._closed = True
                gap_detected = True
                break
            if frame is None:
                break

            fin, opcode, payload = frame
            if opcode == _WS_OP_PING:
                try:
                    self._send_frame(_WS_OP_PONG, payload)
                except (OSError, ssl.SSLError, TimeoutError):
                    self._close_socket_quietly()
                    self._closed = True
                    gap_detected = True
                    break
                continue
            if opcode == _WS_OP_PONG:
                continue
            if opcode == _WS_OP_CLOSE:
                close_ack = payload[:2] if len(payload) >= 2 else b""
                with suppress(OSError, ssl.SSLError, TimeoutError):
                    self._send_frame(_WS_OP_CLOSE, close_ack)
                self._close_socket_quietly()
                self._closed = True
                gap_detected = True
                break

            if opcode in (_WS_OP_TEXT, _WS_OP_BINARY):
                if self._fragment_opcode is not None:
                    self._close_socket_quietly()
                    self._closed = True
                    raise CoinbaseTradeFlowError(
                        "received new WebSocket data frame while fragmented message was open"
                    )
                if not fin:
                    self._fragment_opcode = opcode
                    self._fragment_buffer = bytearray(payload)
                    continue
                msg_opcode = opcode
                msg_payload = payload
            elif opcode == _WS_OP_CONTINUATION:
                if self._fragment_opcode is None:
                    self._close_socket_quietly()
                    self._closed = True
                    raise CoinbaseTradeFlowError("unexpected WebSocket continuation frame")
                if len(self._fragment_buffer) + len(payload) > self._max_frame_bytes:
                    self._close_socket_quietly()
                    self._closed = True
                    raise CoinbaseTradeFlowError(
                        "WebSocket fragmented message exceeds max_frame_bytes"
                    )
                self._fragment_buffer.extend(payload)
                if not fin:
                    continue
                msg_opcode = self._fragment_opcode
                msg_payload = bytes(self._fragment_buffer)
                self._fragment_opcode = None
                self._fragment_buffer.clear()
            else:
                self._close_socket_quietly()
                self._closed = True
                raise CoinbaseTradeFlowError(f"unsupported WebSocket opcode: 0x{opcode:x}")

            if msg_opcode != _WS_OP_TEXT:
                self._close_socket_quietly()
                self._closed = True
                raise CoinbaseTradeFlowError("unexpected binary WebSocket message from Coinbase")

            try:
                decoded = _decode_json_strict(msg_payload)
            except CoinbaseTradeFlowError:
                self._close_socket_quietly()
                self._closed = True
                raise
            if not isinstance(decoded, dict):
                self._close_socket_quietly()
                self._closed = True
                raise CoinbaseTradeFlowError("WebSocket message payload must be a JSON object")

            msg_type = decoded.get("type")
            if msg_type == "error":
                gap_detected = True
                continue
            if msg_type not in ("match", "last_match"):
                continue

            raw_msg_pid = decoded.get("product_id")
            try:
                if not isinstance(raw_msg_pid, str) or not raw_msg_pid.strip():
                    raise CoinbaseTradeFlowError("WebSocket match message missing product_id")
                msg_pid = _resolve_supported_product(raw_msg_pid)
                if msg_pid not in self._product_id_set:
                    continue

                trade_print = _parse_single_trade_mapping(
                    decoded,
                    expected_product_id=msg_pid,
                    context=f"ws.{msg_type}",
                )
            except CoinbaseTradeFlowError:
                self._close_socket_quietly()
                self._closed = True
                raise
            watermark = self._last_trade_id_by_product.get(msg_pid, 0)
            if trade_print.trade_id <= watermark:
                continue

            key = (msg_pid, trade_print.trade_id)
            existing = batch_by_key.get(key)
            if existing is not None:
                if existing != trade_print:
                    self._close_socket_quietly()
                    self._closed = True
                    raise CoinbaseTradeFlowError(
                        f"conflicting WebSocket print for {msg_pid} trade_id {trade_print.trade_id}"
                    )
                continue
            batch_by_key[key] = trade_print

        for pid, tid in batch_by_key:
            prev = self._last_trade_id_by_product.get(pid, 0)
            if tid > prev:
                self._last_trade_id_by_product[pid] = tid

        prints_sorted = tuple(
            sorted(batch_by_key.values(), key=lambda item: (item.trade_id, item.product_id))
        )
        return prints_sorted, gap_detected

    def close(self) -> None:
        """Close the WebSocket feed cleanly and idempotently."""
        if self._closed:
            return
        if self._connected and self._sock is not None:
            with suppress(OSError, ssl.SSLError, TimeoutError, CoinbaseTradeFlowError):
                self._send_frame(_WS_OP_CLOSE, struct.pack("!H", 1000))
        self._close_socket_quietly()
        self._closed = True

    def __enter__(self) -> Self:
        if not self._connected and not self._closed:
            self.connect()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


__all__ = [
    "COINBASE_TRADES_URL_TEMPLATE",
    "COINBASE_WS_ALLOWED_HOSTS",
    "COINBASE_WS_HOST",
    "COINBASE_WS_PORT",
    "MAX_TRADES_RESPONSE_BYTES",
    "MAX_WS_FRAME_BYTES",
    "RFC6455_GUID",
    "CoinbaseTradeFlowError",
    "CoinbaseTradePrint",
    "CoinbaseWebSocketFeed",
    "build_websocket_frame",
    "compute_websocket_accept",
    "fetch_coinbase_recent_trades",
    "parse_coinbase_trades_payload",
]
