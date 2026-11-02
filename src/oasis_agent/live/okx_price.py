"""OKX OnchainOS DEX price as an independent, read-only external price source.

Only `POST /api/v6/dex/market/price` is called. Credentials are passed in explicitly and
never logged; every failure is reduced to a fixed category so provider text (which may
echo request headers) never reaches a log or state file. Nothing here signs or trades.
"""

import base64
import hashlib
import hmac
import json
import ssl
import time
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from fractions import Fraction

import certifi

from ..evidence.rpc_validation import strict_json_object
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
    payload = strict_json_object(raw)
    data = payload.get("data")
    if payload.get("code") != "0" or not isinstance(data, list) or len(data) != 1:
        raise OkxError("price response refused")
    row = data[0]
    if (
        not isinstance(row, dict)
        or row.get("chainIndex") != str(chain_id)
        or not isinstance(row.get("tokenContractAddress"), str)
        or row["tokenContractAddress"].lower() != token
        or not isinstance(row.get("price"), str)
        or not isinstance(row.get("time"), str)
        or not row["time"].isdigit()
    ):
        raise OkxError("price row refused")
    observed = int(row["time"])
    if not -MAX_CLOCK_SKEW_MS <= now_ms - observed <= max_age_ms:
        raise OkxError("price stale")
    try:
        value = Decimal(row["price"])
    except InvalidOperation:
        raise OkxError("price not decimal") from None
    if not value.is_finite() or value <= 0:
        raise OkxError("price not positive")
    return Fraction(value)


class OkxPriceSource(ExternalPriceSource):
    def __init__(
        self,
        credentials: dict[str, str],
        *,
        max_age_seconds: int = 300,
        transport: Transport = https_post,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if set(credentials) != set(CREDENTIAL_NAMES) or not all(credentials.values()):
            raise OkxError("OKX credentials incomplete")
        if type(max_age_seconds) is not int or not 1 <= max_age_seconds <= 3600:
            raise OkxError("max age must be 1-3600s")
        self._credentials = dict(credentials)
        self._max_age_ms = max_age_seconds * 1000
        self._transport, self._clock = transport, clock

    def __repr__(self) -> str:
        return "OkxPriceSource(<credentials redacted>)"

    def fetch_usd_price(self, chain_id: int, currency: str) -> Fraction | None:
        token = currency.lower()
        token = OKX_NATIVE if token == NATIVE else token
        body = json.dumps(
            [{"chainIndex": str(chain_id), "tokenContractAddress": token}], separators=(",", ":")
        )
        now = self._clock()
        headers = signed_headers(
            self._credentials, "POST", PRICE_PATH, body, datetime.fromtimestamp(now, UTC)
        )
        try:
            raw = self._transport(HOST + PRICE_PATH, headers, body.encode())
        except Exception:  # noqa: BLE001 - transport errors may echo headers
            return None
        try:
            return parse_price(raw, chain_id, token, int(now * 1000), self._max_age_ms)
        except Exception:  # noqa: BLE001 - malformed/stale/foreign rows are unavailable
            return None
