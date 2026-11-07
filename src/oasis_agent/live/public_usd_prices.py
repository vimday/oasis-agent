"""Explicit Kraken/Bitstamp USD trade quorum; HTTPS observations, not signed oracles.

Both price timestamps are exchange trade times at or before the valuation block.
The receipt clock never refreshes an old trade. No key, registration or RPC is used.
"""

import asyncio
import json
import time
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from ..models.common import BlockRef, canonical_json
from .config import BASE_USDC, LiveConfig
from .nav_errors import NavRefusal

POLICY = "kraken-bitstamp-usd-v1"
SCHEMA = "oasis.public-usd-trades.v1"
MAX_BYTES = 512_000
MAX_BUNDLE_BYTES = 4 * MAX_BYTES + 8_000
MAX_FETCH_MS = 15_000
URLS = {
    "kraken:ETH": "https://api.kraken.com/0/public/Trades?pair=ETHUSD&count=100",
    "kraken:USDC": "https://api.kraken.com/0/public/Trades?pair=USDCUSD&count=100",
    "bitstamp:ETH": "https://www.bitstamp.net/api/v2/transactions/ethusd/?time=hour",
    "bitstamp:USDC": "https://www.bitstamp.net/api/v2/transactions/usdcusd/?time=hour",
}
KRAKEN_PAIRS = {"ETH": "XETHZUSD", "USDC": "USDCUSD"}
ASSETS = {"ETH": "0x" + "00" * 20, "USDC": BASE_USDC}


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise NavRefusal("duplicate public price JSON key")
        result[key] = value
    return result


def _reject(_: str) -> None:
    raise NavRefusal("nonfinite public price JSON")


def _json(raw: bytes | str) -> Any:
    return json.loads(raw, object_pairs_hook=_unique, parse_float=Decimal, parse_constant=_reject)


def _number(value: object) -> Decimal:
    if type(value) not in (str, int, Decimal):
        raise NavRefusal("exact public price number required")
    text = str(value)
    if len(text) > 48 or not text or any(c not in "0123456789." for c in text):
        raise NavRefusal("bounded unsigned public price number required")
    try:
        number = Decimal(text)
    except InvalidOperation:
        raise NavRefusal("invalid public price decimal") from None
    if not number.is_finite() or not 0 < number < 10**12:
        raise NavRefusal("public price number outside bounds")
    return number


def _trade(source: str, feed: str, body: str, block_ms: int) -> tuple[int, int]:
    data = _json(body)
    if source == "kraken":
        pair = KRAKEN_PAIRS[feed]
        if (
            not isinstance(data, dict)
            or set(data) != {"error", "result"}
            or data["error"] != []
            or not isinstance(data["result"], dict)
            or set(data["result"]) != {pair, "last"}
        ):
            raise NavRefusal("Kraken USD trade identity or error differs")
        rows = data["result"][pair]
    else:
        rows = data
    if not isinstance(rows, list) or not 1 <= len(rows) <= 5_000:
        raise NavRefusal("bounded nonempty USD trade history required")
    eligible = []
    for row in rows:
        if source == "kraken":
            if not isinstance(row, list) or len(row) != 7:
                raise NavRefusal("Kraken trade shape differs")
            price, volume, stamp = row[:3]
            identity = row[6]
            if type(identity) is not int or identity < 0:
                raise NavRefusal("Kraken trade identity missing")
        else:
            if not isinstance(row, dict) or not {"price", "amount", "date", "tid"} <= set(row):
                raise NavRefusal("Bitstamp trade shape differs")
            price, volume, stamp, identity = row["price"], row["amount"], row["date"], row["tid"]
            if (
                isinstance(identity, str)
                and identity.isascii()
                and identity.isdigit()
                and len(identity) <= 30
            ):
                identity = int(identity)
            if type(identity) is not int or identity < 0:
                raise NavRefusal("Bitstamp trade identity missing")
        numerator, denominator = _number(price).as_integer_ratio()
        answer = numerator * 10**8 // denominator
        _number(volume)
        numerator, denominator = _number(stamp).as_integer_ratio()
        trade_ms = numerator * 1000 // denominator
        if answer <= 0:
            raise NavRefusal("positive USD trade required")
        if trade_ms <= block_ms:
            eligible.append((trade_ms, identity, answer))
    if not eligible:
        raise NavRefusal("no USD trade at or before NAV block")
    stamp, _, answer = max(eligible)
    return answer, stamp


def verify_public_price_bundle(
    raw: bytes, c: LiveConfig, *, as_of_ms: int, now_ms: int
) -> dict[str, Any]:
    """Replay both sources; reported HTTP clocks are retained observations, not attestations."""
    if (
        not isinstance(raw, bytes)
        or len(raw) > MAX_BUNDLE_BYTES
        or type(now_ms) is not int
        or type(as_of_ms) is not int
        or not 0 < as_of_ms <= now_ms
        or not 0 <= now_ms - as_of_ms <= 120_000
    ):
        raise NavRefusal("bounded current Base price observation required")
    data = _json(raw)
    if (
        not isinstance(data, dict)
        or set(data) != {"schema", "observations"}
        or data["schema"] != SCHEMA
    ):
        raise NavRefusal("public USD price bundle differs")
    rows = data["observations"]
    if not isinstance(rows, list) or len(rows) != 4:
        raise NavRefusal("four exact public USD observations required")
    observed = {}
    for key, row in zip(URLS, rows, strict=True):
        if (
            not isinstance(row, dict)
            or set(row) != {"key", "url", "started_at_ms", "received_at_ms", "body"}
            or row["key"] != key
            or row["url"] != URLS[key]
            or type(row["started_at_ms"]) is not int
            or type(row["received_at_ms"]) is not int
            or not isinstance(row["body"], str)
            or len(row["body"].encode()) > MAX_BYTES
            or not 0 <= now_ms - row["started_at_ms"] <= 120_000
            or not row["started_at_ms"] <= row["received_at_ms"] <= now_ms
        ):
            raise NavRefusal("public USD observation scope or clock differs")
        source, feed = key.split(":")
        answer, stamp = _trade(source, feed, row["body"], as_of_ms)
        maximum = (
            c.oracle_max_age_seconds if feed == "ETH" else c.usdc_oracle_max_age_seconds
        ) * 1000
        if not 0 < maximum <= 1_800_000 or not 0 <= now_ms - stamp <= maximum:
            raise NavRefusal("public USD trade stale or future")
        if feed == "USDC" and abs(answer - 10**8) * 10_000 > 200 * 10**8:
            raise NavRefusal("NAV USDC / USD answer outside stable band")
        observed[key] = {"answer": answer, "timestamp_ms": stamp, "valid_until_ms": stamp + maximum}
    started = min(row["started_at_ms"] for row in rows)
    ended = max(row["received_at_ms"] for row in rows)
    if ended - started > MAX_FETCH_MS:
        raise NavRefusal("public USD acquisition deadline exceeded")
    prices = {}
    for feed in ("ETH", "USDC"):
        a, b = (observed[f"{s}:{feed}"]["answer"] for s in ("kraken", "bitstamp"))
        if abs(a - b) * 10_000 > min(a, b) * min(40, c.max_oracle_deviation_bps):
            raise NavRefusal("independent USD prices disagree")
        prices[feed] = (a + b) // 2
    return {
        "policy": POLICY,
        "quote_currency": "USD",
        "asset_mapping": ASSETS,
        "prices": prices,
        "witnesses": observed,
        "valid_until": min(
            as_of_ms // 1000 + 120, min(r["valid_until_ms"] for r in observed.values()) // 1000
        ),
        "trust": "explicit_exchange_https_observations_not_signed_oracles",
    }


def verify_public_prices(
    raw: bytes, c: LiveConfig, block: BlockRef, *, now_ms: int
) -> dict[str, Any]:
    if block.chain_id != 8453 or type(block.timestamp) is not int:
        raise NavRefusal("public USD NAV requires a timestamped Base block")
    return verify_public_price_bundle(raw, c, as_of_ms=block.timestamp * 1000, now_ms=now_ms)


async def _fetch(on_request: Callable[[str], None] | None = None) -> bytes:
    rows = []
    async with (
        asyncio.timeout(MAX_FETCH_MS / 1000),
        httpx.AsyncClient(follow_redirects=False, trust_env=False, timeout=5) as client,
    ):
        for key, url in URLS.items():
            if on_request is not None:
                on_request(key)
            started = time.time_ns() // 1_000_000
            async with client.stream(
                "GET", url, headers={"Accept-Encoding": "identity"}
            ) as response:
                if (
                    response.status_code != 200
                    or response.headers.get("content-encoding", "identity") != "identity"
                ):
                    raise NavRefusal("public USD HTTP status or encoding refused")
                raw = bytearray()
                async for chunk in response.aiter_raw():
                    raw.extend(chunk)
                    if len(raw) > MAX_BYTES:
                        raise NavRefusal("public USD HTTP response exceeds bounds")
            rows.append(
                {
                    "key": key,
                    "url": url,
                    "started_at_ms": started,
                    "received_at_ms": time.time_ns() // 1_000_000,
                    "body": raw.decode("utf-8"),
                }
            )
    return canonical_json({"schema": SCHEMA, "observations": rows})


def fetch_public_prices(*, on_request: Callable[[str], None] | None = None) -> bytes:
    """Exactly four credential-free GETs, fail-fast, no retries or redirects."""
    try:
        return asyncio.run(_fetch(on_request))
    except Exception:  # noqa: BLE001 - provider diagnostics are never published
        raise NavRefusal("public USD acquisition failed (details redacted)") from None
