"""OKX OnchainOS DEX price as an independent, read-only external price source.

Only `POST /api/v6/dex/market/price` is called. Credentials are passed in explicitly and
never logged; every failure is reduced to a fixed category so provider text (which may
echo request headers) never reaches a log or state file. Nothing here signs or trades.
"""

import base64
import hashlib
import hmac
import json
import math
import ssl
import time
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from fractions import Fraction

import certifi

from ..evidence.rpc_validation import strict_json_object
from ..models.common import normalize_address
from .okx_budget import OkxPriceReadBudget
from .price_guard import ExternalPriceSource

HOST = "https://web3.okx.com"
PRICE_PATH = "/api/v6/dex/market/price"
NATIVE = "0x0000000000000000000000000000000000000000"
OKX_NATIVE = "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
MAX_BYTES = 100_000
CREDENTIAL_NAMES = ("OKX_API_KEY", "OKX_SECRET_KEY", "OKX_PASSPHRASE")
# OKX row times were observed ~1s ahead of the local clock; tolerate bounded skew only.
MAX_CLOCK_SKEW_MS = 5_000

Transport = Callable[[str, dict[str, str], bytes], bytes]


class OkxError(ValueError):
    """Fixed category only; never provider or exception text."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        raise OkxError("authenticated redirect refused")


def https_request(
    method: str, url: str, headers: dict[str, str], body: bytes | None, max_bytes: int
) -> bytes:
    opener = urllib.request.build_opener(
        _NoRedirect(),
        urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=certifi.where())),
    )
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    with opener.open(request, timeout=15) as response:
        raw: bytes = response.read(max_bytes + 1)
        if response.status != 200 or len(raw) > max_bytes:
            raise OkxError("invalid bounded response")
    return raw


def https_post(url: str, headers: dict[str, str], body: bytes) -> bytes:
    return https_request("POST", url, headers, body, MAX_BYTES)


def signed_headers(
    credentials: dict[str, str], method: str, path: str, body: str, now: datetime
) -> dict[str, str]:
    timestamp = now.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    signature = base64.b64encode(
        hmac.new(
            credentials["OKX_SECRET_KEY"].encode(),
            (timestamp + method + path + body).encode(),
            hashlib.sha256,
        ).digest()
    ).decode()
    return {
        "OK-ACCESS-KEY": credentials["OKX_API_KEY"],
        "OK-ACCESS-SIGN": signature,
        "OK-ACCESS-PASSPHRASE": credentials["OKX_PASSPHRASE"],
        "OK-ACCESS-TIMESTAMP": timestamp,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "OasisAgent/0.1",
    }


def parse_price(raw: bytes, chain_id: int, token: str, now_ms: int, max_age_ms: int) -> Fraction:
    """One exact, fresh, positive USD price for exactly the requested token."""
    return parse_prices(raw, chain_id, (token,), now_ms, max_age_ms)[0]


def parse_prices(
    raw: bytes, chain_id: int, tokens: tuple[str, ...], now_ms: int, max_age_ms: int
) -> tuple[Fraction, ...]:
    """Validate an exact token set from one bounded response at its receive time."""
    if (
        len(raw) > MAX_BYTES
        or not 1 <= len(tokens) <= 2
        or len(set(tokens)) != len(tokens)
        or type(chain_id) is not int
        or chain_id <= 0
        or type(now_ms) is not int
        or now_ms < 0
        or type(max_age_ms) is not int
        or not 1 <= max_age_ms <= 1_800_000
    ):
        raise OkxError("bounded price request required")
    payload = strict_json_object(raw)
    data = payload.get("data")
    if payload.get("code") != "0" or not isinstance(data, list) or len(data) != len(tokens):
        raise OkxError("price response refused")
    prices: dict[str, Fraction] = {}
    for row in data:
        if not isinstance(row, dict) or not isinstance(row.get("tokenContractAddress"), str):
            raise OkxError("price row refused")
        token = row["tokenContractAddress"].lower()
        if token not in tokens or token in prices:
            raise OkxError("duplicate or foreign price row")
        prices[token] = _parse_row(row, chain_id, now_ms, max_age_ms)
    return tuple(prices[token] for token in tokens)


def _parse_row(row: dict[str, object], chain_id: int, now_ms: int, max_age_ms: int) -> Fraction:
    price, timestamp = row.get("price"), row.get("time")
    if (
        row.get("chainIndex") != str(chain_id)
        or not isinstance(price, str)
        or len(price) > 128
        or not isinstance(timestamp, str)
        or len(timestamp) > 19
        or not timestamp.isascii()
        or not timestamp.isdigit()
    ):
        raise OkxError("price row refused")
    observed = int(timestamp)
    if not -MAX_CLOCK_SKEW_MS <= now_ms - observed <= max_age_ms:
        raise OkxError("price stale")
    try:
        value = Decimal(price)
    except InvalidOperation:
        raise OkxError("price not decimal") from None
    if not value.is_finite() or value <= 0:
        raise OkxError("price not positive")
    # Decimal accepts enormous exponents cheaply; Fraction would then allocate
    # enormous integers. Bound representation before conversion, not afterward.
    parts = value.as_tuple()
    if len(parts.digits) > 78 or not isinstance(parts.exponent, int) or abs(parts.exponent) > 78:
        raise OkxError("price precision out of bounds")
    return Fraction(value)


class OkxPriceSource(ExternalPriceSource):
    def __init__(
        self,
        credentials: dict[str, str],
        *,
        max_age_seconds: int = 300,
        budget: OkxPriceReadBudget | None = None,
        transport: Transport = https_post,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if set(credentials) != set(CREDENTIAL_NAMES) or not all(
            isinstance(v, str) and v.strip() and "\n" not in v and "\r" not in v
            for v in credentials.values()
        ):
            raise OkxError("OKX credentials incomplete")
        if type(max_age_seconds) is not int or not 1 <= max_age_seconds <= 1800:
            raise OkxError("max age must be 1-1800s")
        self._credentials = dict(credentials)
        self._max_age_ms = max_age_seconds * 1000
        self._transport, self._clock = transport, clock
        self._budget, self._failed = budget, False

    def __repr__(self) -> str:
        return "OkxPriceSource(<credentials redacted>)"

    def fetch_usd_price(self, chain_id: int, currency: str) -> Fraction | None:
        prices = self._request(chain_id, (currency,))
        return prices[0] if prices is not None else None

    def fetch_usd_prices(
        self, chain_id: int, currencies: tuple[str, str]
    ) -> tuple[Fraction, Fraction] | None:
        prices = self._request(chain_id, currencies)
        return (prices[0], prices[1]) if prices is not None else None

    def _request(self, chain_id: int, currencies: tuple[str, ...]) -> tuple[Fraction, ...] | None:
        if self._failed or self._budget is None:
            return None
        try:
            normalized = tuple(normalize_address(c) for c in currencies)
            tokens = tuple(OKX_NATIVE if c == NATIVE else c for c in normalized)
            body = json.dumps(
                [{"chainIndex": str(chain_id), "tokenContractAddress": t} for t in tokens],
                separators=(",", ":"),
            )
            now = self._clock()
            if not math.isfinite(now):
                raise OkxError("invalid request clock")
            headers = signed_headers(
                self._credentials, "POST", PRICE_PATH, body, datetime.fromtimestamp(now, UTC)
            )
            self._budget.reserve(
                self._credentials["OKX_API_KEY"], chain_id, normalized, int(now * 1000)
            )
            admitted_at = self._clock()
            if not math.isfinite(admitted_at) or admitted_at < now:
                raise OkxError("invalid admission clock")
            self._budget.grant.require_scope(
                self._credentials["OKX_API_KEY"], chain_id, normalized, int(admitted_at * 1000)
            )
            # Spend before I/O; failures are never refunded. No retry, redirect
            # or x402 payment negotiation is performed.
            raw = self._transport(HOST + PRICE_PATH, headers, body.encode())
            received_at = self._clock()
            if not math.isfinite(received_at) or received_at < admitted_at:
                raise OkxError("invalid response clock")
            self._budget.grant.require_scope(
                self._credentials["OKX_API_KEY"], chain_id, normalized, int(received_at * 1000)
            )
            # Transport latency consumes the price's age budget. Authentication
            # uses request time, but freshness is checked when the response arrives.
            return parse_prices(raw, chain_id, tokens, int(received_at * 1000), self._max_age_ms)
        except Exception:  # noqa: BLE001 - malformed/stale/foreign rows are unavailable
            self._failed = True
            return None
