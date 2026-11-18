"""Read-only keyless client and strict parser for the UniswapX Base Dutch_V3 orderbook."""

import ipaddress
import json
import math
import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import Final, cast

import certifi

from ..models.common import ZERO_ADDRESS, normalize_address, sha256_hex
from .dutch_v3 import BASE_CHAIN_ID, DutchV3Curve, DutchV3Order, DutchV3Output

UNISWAPX_ORDERS_URL: Final[str] = "https://api.uniswap.org/v2/orders"
ALLOWED_HOSTS: Final[frozenset[str]] = frozenset({"api.uniswap.org"})
MIN_RATE_LIMIT_INTERVAL_S: Final[float] = 0.25
MAX_RESPONSE_BYTES: Final[int] = 2_000_000
_INT_STR_RE: Final[re.Pattern[str]] = re.compile(r"^-?(?:0|[1-9][0-9]*)$")
_USER_AGENT: Final[str] = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)


class UniswapXClientError(ValueError):
    """Fail-closed error raised on UniswapX URL, budget, transport, or payload failure."""


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        with suppress(OSError):
            fp.close()
        raise UniswapXClientError("UniswapX redirects are forbidden")


@dataclass(frozen=True, slots=True)
class UniswapXPollResult:
    """Retained result from one bounded poll of the UniswapX orderbook."""

    orders: tuple[DutchV3Order, ...]
    raw_sha256: str
    request_url: str
    polled_at_ms: int

    def __post_init__(self) -> None:
        if not isinstance(self.orders, tuple) or any(
            not isinstance(order, DutchV3Order) for order in self.orders
        ):
            raise TypeError("orders must be a tuple of DutchV3Order")
        if (
            not isinstance(self.raw_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", self.raw_sha256) is None
        ):
            raise ValueError("raw_sha256 must be a lowercase SHA-256 hex digest")
        _validate_uniswapx_url(self.request_url)
        if type(self.polled_at_ms) is not int or self.polled_at_ms <= 0:
            raise ValueError("polled_at_ms must be a positive integer")


def _validate_uniswapx_url(url: str) -> str:
    if not isinstance(url, str) or not url or url != url.strip():
        raise UniswapXClientError("UniswapX URL must be a nonempty string")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise UniswapXClientError("UniswapX client requires an https:// endpoint")
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise UniswapXClientError("UniswapX URL forbids userinfo credentials")
    hostname = parsed.hostname
    if not hostname or hostname.lower() not in ALLOWED_HOSTS:
        raise UniswapXClientError("UniswapX URL host is not allowlisted")
    try:
        if parsed.port is not None:
            raise UniswapXClientError("UniswapX URL forbids explicit custom ports")
    except ValueError as exc:
        raise UniswapXClientError("UniswapX URL contains an invalid port") from exc
    return url


_TUN_FAKE_IPV4_NET: Final[ipaddress.IPv4Network] = ipaddress.IPv4Network("198.18.0.0/15")
_TUN_FAKE_IPV6_NET: Final[ipaddress.IPv6Network] = ipaddress.IPv6Network("2001:2::/48")


def _is_tun_fake_ip(ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Allow local TUN Fake-IP benchmarking ranges only for allowlisted domain DNS."""
    if isinstance(ip_obj, ipaddress.IPv4Address):
        return ip_obj in _TUN_FAKE_IPV4_NET
    return ip_obj in _TUN_FAKE_IPV6_NET


def _is_forbidden_ip(ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        ip_obj.is_private
        or ip_obj.is_loopback
        or ip_obj.is_link_local
        or ip_obj.is_multicast
        or ip_obj.is_reserved
        or ip_obj.is_unspecified
    )


def _validate_resolved_public_dns(hostname: str) -> None:
    try:
        addr_infos = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise UniswapXClientError("UniswapX DNS resolution failed") from exc
    for _, _, _, _, sockaddr in addr_infos:
        raw_ip = str(sockaddr[0]).split("%", 1)[0]
        ip_obj = ipaddress.ip_address(raw_ip)
        if _is_forbidden_ip(ip_obj) and not _is_tun_fake_ip(ip_obj):
            raise UniswapXClientError("UniswapX DNS resolved to a non-public IP")


def _default_https_get(url: str, timeout_s: float) -> bytes:
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or not parsed.hostname
        or parsed.hostname.lower() not in ALLOWED_HOSTS
    ):
        raise UniswapXClientError("UniswapX URL failed SSRF validation")
    _validate_resolved_public_dns(parsed.hostname)

    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=certifi.where())),
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
                raise UniswapXClientError("UniswapX response exceeds byte limit")
            return raw
    except urllib.error.HTTPError as exc:
        with suppress(OSError):
            exc.close()
        raise UniswapXClientError(f"UniswapX HTTP failure: status {exc.code}") from None
    except (urllib.error.URLError, OSError) as exc:
        if isinstance(exc, UniswapXClientError):
            raise
        raise UniswapXClientError("UniswapX HTTPS transport failure") from None


def _unique_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("UniswapX JSON contains duplicate fields")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise ValueError("UniswapX JSON contains non-finite numbers")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("UniswapX JSON number overflows to non-finite value")
    return number


def _strict_json_object(raw_bytes: bytes) -> dict[str, object]:
    if not isinstance(raw_bytes, bytes):
        raise TypeError("raw_bytes must be bytes")
    if len(raw_bytes) > MAX_RESPONSE_BYTES:
        raise ValueError("UniswapX payload exceeds MAX_RESPONSE_BYTES")
    try:
        decoded = json.loads(
            raw_bytes,
            object_pairs_hook=_unique_fields,
            parse_constant=_reject_constant,
            parse_float=_finite_float,
        )
    except (json.JSONDecodeError, UnicodeError, RecursionError) as exc:
        raise ValueError("UniswapX payload is not valid JSON") from exc
    if not isinstance(decoded, dict):
        raise ValueError("UniswapX payload must be a JSON object")
    return cast(dict[str, object], decoded)


def _strict_mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a JSON object")
    return value


def _strict_str(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return value.strip()


def _strict_int(value: object, name: str) -> int:
    if type(value) is int:
        return value
    if isinstance(value, str) and _INT_STR_RE.fullmatch(value) is not None:
        return int(value, 10)
    raise ValueError(f"{name} must be an integer or decimal integer string")


def _parse_curve(raw_curve: object, name: str) -> DutchV3Curve:
    curve = _strict_mapping(raw_curve, name)
    raw_blocks = curve.get("relativeBlocks")
    raw_amounts = curve.get("relativeAmounts")
    if not isinstance(raw_blocks, list) or not isinstance(raw_amounts, list):
        raise ValueError(f"{name} relativeBlocks and relativeAmounts must be lists")
    blocks = tuple(_strict_int(val, f"{name}.relativeBlocks") for val in raw_blocks)
    amounts = tuple(_strict_int(val, f"{name}.relativeAmounts") for val in raw_amounts)
    return DutchV3Curve(relative_blocks=blocks, relative_amounts=amounts)


def _parse_single_order(raw_item: object, *, chain_id: int) -> DutchV3Order:
    item = _strict_mapping(raw_item, "order")

    order_type = item.get("type") if "type" in item else item.get("orderType")
    if order_type is not None and order_type != "Dutch_V3":
        raise ValueError(f"unsupported UniswapX order type: {order_type!r}")

    status = item.get("orderStatus")
    if status is not None and status != "open":
        raise ValueError(f"unexpected UniswapX orderStatus: {status!r}")

    item_chain_id = _strict_int(item.get("chainId"), "chainId")
    if item_chain_id != chain_id:
        raise ValueError(f"order chainId {item_chain_id} does not match expected {chain_id}")

    order_hash = _strict_str(item.get("orderHash"), "orderHash")
    reactor_raw = item.get("reactor") if "reactor" in item else item.get("reactorAddress")
    reactor_address = _strict_str(reactor_raw, "reactor")
    swapper = _strict_str(item.get("swapper"), "swapper")
    nonce = _strict_int(item.get("nonce"), "nonce")

    cosigner_data_raw = item.get("cosignerData")
    cosigner_data: Mapping[str, object] = (
        _strict_mapping(cosigner_data_raw, "cosignerData") if cosigner_data_raw is not None else {}
    )

    decay_start_raw = (
        item["decayStartBlock"]
        if "decayStartBlock" in item
        else cosigner_data.get("decayStartBlock")
    )
    decay_start_block = _strict_int(decay_start_raw, "decayStartBlock")
    decay_end_block = _strict_int(item.get("decayEndBlock"), "decayEndBlock")
    deadline_raw = item["deadlineBlock"] if "deadlineBlock" in item else item.get("deadline")
    deadline_block = _strict_int(deadline_raw, "deadlineBlock")

    exclusive_filler_raw = (
        item["exclusiveFiller"]
        if item.get("exclusiveFiller") is not None
        else cosigner_data.get("exclusiveFiller")
    )
    exclusive_filler = (
        ZERO_ADDRESS
        if exclusive_filler_raw is None
        else normalize_address(_strict_str(exclusive_filler_raw, "exclusiveFiller"))
    )

    override_raw = (
        item["exclusivityOverrideBps"]
        if item.get("exclusivityOverrideBps") is not None
        else cosigner_data.get("exclusivityOverrideBps")
    )
    exclusivity_override_bps = (
        0 if override_raw is None else _strict_int(override_raw, "exclusivityOverrideBps")
    )

    exclusivity_end_raw = (
        item["exclusivityEndBlock"]
        if item.get("exclusivityEndBlock") is not None
        else cosigner_data.get("exclusivityEndBlock")
    )
    if exclusivity_end_raw is not None:
        exclusivity_end_block = _strict_int(exclusivity_end_raw, "exclusivityEndBlock")
    elif exclusive_filler == ZERO_ADDRESS:
        exclusivity_end_block = 0
    else:
        exclusivity_end_block = decay_start_block

    raw_input = _strict_mapping(item.get("input"), "input")
    input_token = _strict_str(raw_input.get("token"), "input.token")
    input_start_amount = _strict_int(raw_input.get("startAmount"), "input.startAmount")
    input_end_amount = _strict_int(raw_input.get("endAmount"), "input.endAmount")
    raw_input_curve = raw_input.get("curve")
    input_curve = None if raw_input_curve is None else _parse_curve(raw_input_curve, "input.curve")

    raw_outputs = item.get("outputs")
    if not isinstance(raw_outputs, list) or not raw_outputs:
        raise ValueError("order outputs must be a nonempty list")
    outputs: list[DutchV3Output] = []
    for idx, raw_out in enumerate(raw_outputs):
        out_map = _strict_mapping(raw_out, f"outputs[{idx}]")
        raw_curve = out_map.get("curve")
        curve = (
            DutchV3Curve(relative_blocks=(), relative_amounts=())
            if raw_curve is None
            else _parse_curve(raw_curve, f"outputs[{idx}].curve")
        )
        outputs.append(
            DutchV3Output(
                token=_strict_str(out_map.get("token"), f"outputs[{idx}].token"),
                start_amount=_strict_int(out_map.get("startAmount"), f"outputs[{idx}].startAmount"),
                end_amount=_strict_int(out_map.get("endAmount"), f"outputs[{idx}].endAmount"),
                recipient=_strict_str(out_map.get("recipient"), f"outputs[{idx}].recipient"),
                curve=curve,
            )
        )

    return DutchV3Order(
        order_hash=order_hash,
        chain_id=chain_id,
        reactor_address=reactor_address,
        swapper=swapper,
        nonce=nonce,
        decay_start_block=decay_start_block,
        decay_end_block=decay_end_block,
        deadline_block=deadline_block,
        exclusivity_end_block=exclusivity_end_block,
        exclusive_filler=exclusive_filler,
        exclusivity_override_bps=exclusivity_override_bps,
        input_token=input_token,
        input_start_amount=input_start_amount,
        input_end_amount=input_end_amount,
        outputs=tuple(outputs),
        input_curve=input_curve,
    )


def parse_uniswapx_v2_orders(
    raw_bytes: bytes, *, chain_id: int = BASE_CHAIN_ID
) -> tuple[DutchV3Order, ...]:
    """Strictly parse a UniswapX `/v2/orders` payload into validated `DutchV3Order` tuples."""
    if type(chain_id) is not int or chain_id != BASE_CHAIN_ID:
        raise ValueError(f"UniswapX Dutch_V3 parser requires Base chain_id {BASE_CHAIN_ID}")
    payload = _strict_json_object(raw_bytes)
    raw_orders = payload.get("orders")
    if not isinstance(raw_orders, list):
        raise ValueError("UniswapX response must contain an 'orders' list")
    if not raw_orders:
        return ()
    return tuple(_parse_single_order(item, chain_id=chain_id) for item in raw_orders)


class UniswapXOrderbookClient:
    """Bounded, rate-limited, keyless HTTPS client for UniswapX `/v2/orders`."""

    def __init__(
        self,
        *,
        base_url: str = UNISWAPX_ORDERS_URL,
        chain_id: int = BASE_CHAIN_ID,
        min_interval_s: float = MIN_RATE_LIMIT_INTERVAL_S,
        max_requests: int = 16,
        timeout_s: float = 8.0,
        transport: Callable[[str, float], bytes] | None = None,
        clock: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        if type(chain_id) is not int or chain_id != BASE_CHAIN_ID:
            raise ValueError(f"UniswapXOrderbookClient requires Base chain_id {BASE_CHAIN_ID}")
        if (
            isinstance(min_interval_s, bool)
            or not isinstance(min_interval_s, (int, float))
            or not math.isfinite(float(min_interval_s))
            or float(min_interval_s) < MIN_RATE_LIMIT_INTERVAL_S
        ):
            raise ValueError(f"min_interval_s must be finite and >= {MIN_RATE_LIMIT_INTERVAL_S}")
        if type(max_requests) is not int or max_requests <= 0:
            raise ValueError("max_requests must be a positive integer")
        if (
            isinstance(timeout_s, bool)
            or not isinstance(timeout_s, (int, float))
            or not math.isfinite(float(timeout_s))
            or float(timeout_s) <= 0
        ):
            raise ValueError("timeout_s must be a positive finite number")

        self.base_url = _validate_uniswapx_url(base_url)
        self.chain_id = chain_id
        self.min_interval_s = float(min_interval_s)
        self.max_requests = max_requests
        self.timeout_s = float(timeout_s)
        self.requests_used = 0
        self._transport = _default_https_get if transport is None else transport
        self._clock = time.monotonic if clock is None else clock
        self._sleeper = time.sleep if sleeper is None else sleeper
        self._last_request_monotonic: float | None = None

    @property
    def requests_remaining(self) -> int:
        return max(0, self.max_requests - self.requests_used)

    def _build_orders_url(self, *, limit: int | None) -> str:
        params: list[tuple[str, str]] = [
            ("chainId", str(self.chain_id)),
            ("orderType", "Dutch_V3"),
            ("orderStatus", "open"),
        ]
        if limit is not None:
            if type(limit) is not int or not 1 <= limit <= 100:
                raise UniswapXClientError("limit must be an integer in [1, 100]")
            params.append(("limit", str(limit)))
        parsed = urllib.parse.urlsplit(self.base_url)
        query = urllib.parse.urlencode(params)
        url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, ""))
        return _validate_uniswapx_url(url)

    def poll_open_orders(
        self,
        *,
        limit: int | None = None,
        polled_at_ms: int | None = None,
    ) -> UniswapXPollResult:
        """Poll open Base Dutch_V3 orders under rate-limit and request-budget constraints."""
        request_url = self._build_orders_url(limit=limit)
        if self.requests_used >= self.max_requests:
            raise UniswapXClientError("UniswapX request budget exhausted")

        if self._last_request_monotonic is not None:
            elapsed = self._clock() - self._last_request_monotonic
            if elapsed < self.min_interval_s:
                self._sleeper(self.min_interval_s - elapsed)

        self.requests_used += 1
        self._last_request_monotonic = self._clock()

        raw_bytes = self._transport(request_url, self.timeout_s)
        if not isinstance(raw_bytes, bytes) or len(raw_bytes) > MAX_RESPONSE_BYTES:
            raise UniswapXClientError("UniswapX response must be bounded bytes")
        raw_sha256 = sha256_hex(raw_bytes)
        try:
            orders = parse_uniswapx_v2_orders(raw_bytes, chain_id=self.chain_id)
        except (ValueError, TypeError) as exc:
            raise UniswapXClientError(f"UniswapX orderbook parse failed: {exc}") from exc

        resolved_polled_at_ms = int(time.time() * 1000) if polled_at_ms is None else polled_at_ms
        if type(resolved_polled_at_ms) is not int or resolved_polled_at_ms <= 0:
            raise UniswapXClientError("polled_at_ms must be a positive integer")

        return UniswapXPollResult(
            orders=orders,
            raw_sha256=raw_sha256,
            request_url=request_url,
            polled_at_ms=resolved_polled_at_ms,
        )


__all__ = [
    "ALLOWED_HOSTS",
    "MAX_RESPONSE_BYTES",
    "MIN_RATE_LIMIT_INTERVAL_S",
    "UNISWAPX_ORDERS_URL",
    "UniswapXClientError",
    "UniswapXOrderbookClient",
    "UniswapXPollResult",
    "parse_uniswapx_v2_orders",
]
