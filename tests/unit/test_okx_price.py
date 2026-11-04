"""OKX price source: exact parsing, freshness, scope binding and credential redaction."""

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime
from fractions import Fraction

import pytest

from oasis_agent.live.okx_budget import OkxPriceReadBudget, OkxPriceReadGrant
from oasis_agent.live.okx_price import (
    NATIVE,
    OKX_NATIVE,
    OkxError,
    OkxPriceSource,
    parse_price,
    parse_prices,
    signed_headers,
)
from oasis_agent.live.price_guard import check_price_deviation
from oasis_agent.models.common import sha256_hex
from oasis_agent.rpc.budget import RpcBudget

CREDS = {"OKX_API_KEY": "key-a", "OKX_SECRET_KEY": "secret-b", "OKX_PASSPHRASE": "pass-c"}
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
NOW = 1_793_628_625.0


def grant(**changes):
    values = {
        "schema": "oasis.okx-price-free-read-grant.v1",
        "endpoint": "https://web3.okx.com/api/v6/dex/market/price",
        "chain_id": 8453,
        "currencies": (NATIVE, USDC),
        "api_key_hash": sha256_hex(CREDS["OKX_API_KEY"].encode()),
        "valid_from_ms": int(NOW * 1000) - 1000,
        "valid_until_ms": int(NOW * 1000) + 60_000,
        "max_requests": 4,
        "min_interval_ms": 100,
        "billing_mode": "free_quota_only",
        "free_requests_remaining": 4,
        "pricing_evidence_hash": "22" * 32,
        "authorization_evidence_hash": "33" * 32,
    }
    return OkxPriceReadGrant(**(values | changes))


def budget(tmp_path, **changes):
    policy = grant(**changes)
    path = tmp_path / "okx-budget.db"
    with RpcBudget(path, policy.policies()):
        pass
    return OkxPriceReadBudget(path, policy)


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
        row(USDC, price="1e999999999"),
        row(USDC, price="1e-999999999"),
        row(USDC, price="1" * 129),
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


def test_native_currency_is_queried_as_okx_native_address(tmp_path) -> None:
    seen: list[bytes] = []

    def transport(url: str, headers: dict[str, str], body: bytes) -> bytes:
        seen.append(body)
        return row(OKX_NATIVE)

    source = OkxPriceSource(CREDS, budget=budget(tmp_path), transport=transport, clock=lambda: NOW)
    assert source.fetch_usd_price(8453, NATIVE) == Fraction("2290.4896")
    assert json.loads(seen[0]) == [{"chainIndex": "8453", "tokenContractAddress": OKX_NATIVE}]


def test_transport_failure_is_unavailable_and_never_leaks_credentials(tmp_path) -> None:
    def transport(url: str, headers: dict[str, str], body: bytes) -> bytes:
        raise RuntimeError(headers["OK-ACCESS-PASSPHRASE"])

    source = OkxPriceSource(CREDS, budget=budget(tmp_path), transport=transport, clock=lambda: NOW)
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
    tmp_path, elapsed: int, price_age: int, accepted: bool
) -> None:
    clock = [NOW]
    requests = []

    def transport(url: str, headers: dict[str, str], body: bytes) -> bytes:
        requests.append(headers["OK-ACCESS-TIMESTAMP"])
        clock[0] += elapsed
        return row(USDC, "1", at=int((NOW - price_age) * 1000))

    source = OkxPriceSource(
        CREDS, budget=budget(tmp_path), transport=transport, clock=lambda: clock[0]
    )
    result = source.fetch_usd_price(8453, USDC)
    assert result == (Fraction(1) if accepted else None)
    # Authentication still names request time; freshness must name response time.
    assert requests == [
        datetime.fromtimestamp(NOW, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    ]


def test_no_grant_never_contacts_provider():
    calls = []
    source = OkxPriceSource(CREDS, transport=lambda *args: calls.append(args), clock=lambda: NOW)
    assert source.fetch_usd_prices(8453, (NATIVE, USDC)) is None
    assert calls == []


def batch(*rows):
    return json.dumps({"code": "0", "data": [json.loads(r)["data"][0] for r in rows]}).encode()


@pytest.mark.parametrize("fault", [None, "duplicate", "missing", "foreign", "stale"])
def test_pair_uses_one_admitted_response_at_one_receive_time(tmp_path, fault):
    calls, clock = [], [NOW]
    rows = [row(USDC, "1"), row(OKX_NATIVE, "2500")]
    if fault == "duplicate":
        rows[1] = rows[0]
    elif fault == "missing":
        rows.pop()
    elif fault == "foreign":
        rows[1] = row("0x" + "11" * 20)
    elif fault == "stale":
        rows[1] = row(OKX_NATIVE, "2500", at=int(NOW * 1000) - 299_000)

    def transport(url, headers, body):
        calls.append(json.loads(body))
        clock[0] += 2
        assert not any("payment" in k.lower() for k in headers)
        return batch(*rows)

    source = OkxPriceSource(
        CREDS, budget=budget(tmp_path), transport=transport, clock=lambda: clock[0]
    )
    passed, _ = check_price_deviation(8453, NATIVE, USDC, Fraction(2500), Fraction(1), source, 50)
    assert passed is (fault is None)
    assert calls == [
        [
            {"chainIndex": "8453", "tokenContractAddress": OKX_NATIVE},
            {"chainIndex": "8453", "tokenContractAddress": USDC},
        ]
    ]


def test_owner_ceiling_and_response_bound():
    with pytest.raises(OkxError, match="1800"):
        OkxPriceSource(CREDS, max_age_seconds=1801)
    with pytest.raises(OkxError, match="bounded"):
        parse_prices(b" " * 100_001, 8453, (USDC,), int(NOW * 1000), 300_000)
