"""OKX price source: exact parsing, freshness, scope binding and credential redaction."""

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime
from fractions import Fraction

import pytest

from oasis_agent.live.okx_price import (
    NATIVE,
    OKX_NATIVE,
    OkxError,
    OkxPriceSource,
    parse_price,
    signed_headers,
)

CREDS = {"OKX_API_KEY": "key-a", "OKX_SECRET_KEY": "secret-b", "OKX_PASSPHRASE": "pass-c"}
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
NOW = 1_793_628_625.0


def row(token: str, price: str = "2290.4896", at: int = int(NOW * 1000), chain: str = "8453"):
    item = {"chainIndex": chain, "price": price, "time": str(at), "tokenContractAddress": token}
    return json.dumps({"code": "0", "data": [item], "msg": ""}).encode()


def test_parses_exact_decimal_price() -> None:
    assert parse_price(row(USDC, "0.99987"), 8453, USDC, int(NOW * 1000), 300_000) == Fraction(
        99987, 100000
    )


@pytest.mark.parametrize(
    "raw",
    [
        row(USDC, chain="1"),
        row(OKX_NATIVE),
        row(USDC, price="0"),
        row(USDC, price="-1"),
        row(USDC, price="NaN"),
        row(USDC, price="abc"),
        row(USDC, at=int(NOW * 1000) - 300_001),
        row(USDC, at=int(NOW * 1000) + 5_001),
        json.dumps({"code": "50011", "data": [], "msg": "x"}).encode(),
        b'{"code":"0","code":"0","data":[]}',
    ],
)
def test_refuses_foreign_stale_nonpositive_or_malformed_rows(raw: bytes) -> None:
    with pytest.raises((OkxError, ValueError)):
        parse_price(raw, 8453, USDC, int(NOW * 1000), 300_000)


def test_signature_matches_okx_scheme() -> None:
    now = datetime.fromtimestamp(NOW, UTC)
    headers = signed_headers(CREDS, "POST", "/p", "[]", now)
    message = headers["OK-ACCESS-TIMESTAMP"] + "POST/p[]"
    expected = base64.b64encode(
        hmac.new(b"secret-b", message.encode(), hashlib.sha256).digest()
    ).decode()
    assert headers["OK-ACCESS-SIGN"] == expected
    assert headers["OK-ACCESS-TIMESTAMP"].endswith("Z")


def test_native_currency_is_queried_as_okx_native_address() -> None:
    seen: list[bytes] = []

    def transport(url: str, headers: dict[str, str], body: bytes) -> bytes:
        seen.append(body)
        return row(OKX_NATIVE)

    source = OkxPriceSource(CREDS, transport=transport, clock=lambda: NOW)
    assert source.fetch_usd_price(8453, NATIVE) == Fraction("2290.4896")
    assert json.loads(seen[0]) == [{"chainIndex": "8453", "tokenContractAddress": OKX_NATIVE}]


def test_transport_failure_is_unavailable_and_never_leaks_credentials() -> None:
    def transport(url: str, headers: dict[str, str], body: bytes) -> bytes:
        raise RuntimeError(headers["OK-ACCESS-PASSPHRASE"])

    source = OkxPriceSource(CREDS, transport=transport, clock=lambda: NOW)
    assert source.fetch_usd_price(8453, USDC) is None
    assert "pass-c" not in repr(source) and "secret-b" not in repr(source)


def test_incomplete_credentials_are_refused() -> None:
    with pytest.raises(OkxError, match="incomplete"):
        OkxPriceSource({**CREDS, "OKX_PASSPHRASE": ""})
    with pytest.raises(OkxError, match="incomplete"):
        OkxPriceSource({"OKX_API_KEY": "a"})


def test_bounded_provider_clock_skew_is_tolerated() -> None:
    raw = row(USDC, "1", at=int(NOW * 1000) + 938)
    assert parse_price(raw, 8453, USDC, int(NOW * 1000), 300_000) == Fraction(1)


@pytest.mark.parametrize(
    ("elapsed", "price_age", "accepted"),
    [(2, 299, False), (1, 299, True), (12, -12, True), (-1, 0, False)],
)
def test_freshness_uses_response_time_and_refuses_clock_regression(
    elapsed: int, price_age: int, accepted: bool
) -> None:
    clock = [NOW]
    requests = []

    def transport(url: str, headers: dict[str, str], body: bytes) -> bytes:
        requests.append(headers["OK-ACCESS-TIMESTAMP"])
        clock[0] += elapsed
        return row(USDC, "1", at=int((NOW - price_age) * 1000))

    source = OkxPriceSource(CREDS, transport=transport, clock=lambda: clock[0])
    result = source.fetch_usd_price(8453, USDC)
    assert result == (Fraction(1) if accepted else None)
    # Authentication still names request time; freshness must name response time.
    assert requests == [
        datetime.fromtimestamp(NOW, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    ]
