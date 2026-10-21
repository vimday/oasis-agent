"""Offline RedStone price-candidate verification, never live NAV authorization.

Protocol and PrimaryProd signer policy are pinned to upstream commit
ee55521d0949a9f944d9568bd34671ab14c56118. Only single-feed ETH/USDC packages
with 32-byte numeric values and eight decimals are supported. Gateway metadata
is not signed; signer identity is always recovered from the package bytes.
"""

import base64
import json
from dataclasses import asdict, dataclass
from decimal import ROUND_HALF_UP, Decimal, localcontext
from typing import Any

from eth_hash.auto import keccak
from eth_keys.constants import SECPK1_N
from eth_keys.datatypes import Signature

from ..models.common import canonical_json, normalize_address, sha256_hex

UPSTREAM_COMMIT = "ee55521d0949a9f944d9568bd34671ab14c56118"
SERVICE = "redstone-primary-prod"
SIGNERS = frozenset(
    {
        "0x8bb8f32df04c8b654987daaed53d6b6091e3b774",
        "0xdeb22f54738d54976c4c0fe5ce6d408e40d88499",
        "0x51ce04be4b3e32572c4ec9135221d0691ba7d202",
        "0xdd682daec5a90dd295d14da4b0bec9281017b5be",
        "0x9c5ae89c4af6aa32ce58588dbaf90d18a855b6de",
    }
)
FEEDS = ("ETH", "USDC")
MAX_RAW_BYTES = 1_000_000


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("duplicate price JSON key")
        result[name] = value
    return result


def _invalid_constant(_: str) -> None:
    raise ValueError("non-finite price JSON")


def _base64(value: object, size: int) -> bytes:
    if not isinstance(value, str) or len(value) > 100:
        raise ValueError("bounded canonical base64 required")
    decoded = base64.b64decode(value, validate=True)
    if len(decoded) != size or base64.b64encode(decoded).decode() != value:
        raise ValueError("canonical base64 size mismatch")
    return decoded


def _value(point: dict[str, Any]) -> int:
    if set(point) - {"dataFeedId", "value", "decimals", "valueByteSize", "metadata"}:
        raise ValueError("unsupported price point fields")
    if type(point.get("decimals", 8)) is not int or point.get("decimals", 8) != 8:
        raise ValueError("eight price decimals required")
    if type(point.get("valueByteSize", 32)) is not int or point.get("valueByteSize", 32) != 32:
        raise ValueError("32-byte numeric price required")
    value = point.get("value")
    if isinstance(value, str):
        # A protocol string is base64 bytes, never an unsigned decimal assertion.
        answer = int.from_bytes(_base64(value, 32), "big")
    elif type(value) is int or isinstance(value, Decimal):
        if not 0 < value <= 10**12:
            raise ValueError("numeric price outside supported bounds")
        with localcontext() as context:
            context.prec = 100
            answer = int((Decimal(value) * 10**8).to_integral_value(rounding=ROUND_HALF_UP))
    else:
        raise ValueError("exact numeric price required")
    if not 0 < answer <= 10**20:
        raise ValueError("signed price outside supported bounds")
    return answer


@dataclass(frozen=True, slots=True)
class SignedPrice:
    feed: str
    answer: int
    timestamp_ms: int
    signer: str
    digest: str


def verify_package(package: object, feed: str) -> SignedPrice:
    if feed not in FEEDS or not isinstance(package, dict):
        raise ValueError("supported signed price package required")
    allowed = {
        "dataPoints",
        "timestampMilliseconds",
        "signature",
        "dataPackageId",
        "dataServiceId",
        "dataFeedId",
        "signerAddress",
        "isSignatureValid",
    }
    if set(package) - allowed:
        raise ValueError("unsupported signed price fields")
    if (
        package.get("dataPackageId") != feed
        or package.get("dataServiceId", SERVICE) != SERVICE
        or package.get("dataFeedId", feed) != feed
    ):
        raise ValueError("price package label mismatch")
    points = package.get("dataPoints")
    if (
        not isinstance(points, list)
        or len(points) != 1
        or not isinstance(points[0], dict)
        or points[0].get("dataFeedId") != feed
    ):
        raise ValueError("one exact signed feed required")
    timestamp = package.get("timestampMilliseconds")
    if type(timestamp) is not int or not 0 < timestamp < 2**48:
        raise ValueError("canonical signed millisecond timestamp required")
    answer = _value(points[0])
    payload = (
        feed.encode().ljust(32, b"\0")
        + answer.to_bytes(32, "big")
        + timestamp.to_bytes(6, "big")
        + (32).to_bytes(4, "big")
        + (1).to_bytes(3, "big")
    )
    digest = keccak(payload)
    signature = _base64(package.get("signature"), 65)
    r, s, v = (
        int.from_bytes(signature[:32], "big"),
        int.from_bytes(signature[32:64], "big"),
        signature[64],
    )
    if not 0 < r < SECPK1_N or not 0 < s <= SECPK1_N // 2 or v not in (27, 28):
        raise ValueError("canonical low-s ECDSA signature required")
    signer = (
        Signature(vrs=(v - 27, r, s))
        .recover_public_key_from_msg_hash(digest)
        .to_checksum_address()
        .lower()
    )
    if signer not in SIGNERS:
        raise ValueError("price signer is not authorized by pinned policy")
    if "signerAddress" in package and normalize_address(package["signerAddress"]) != signer:
        raise ValueError("declared price signer differs from recovered identity")
    return SignedPrice(feed, answer, timestamp, signer, "0x" + digest.hex())


def verify_prices(
    raw: bytes, *, observed_at_ms: int, max_age_ms: int = 1_800_000, max_spread_bps: int = 40
) -> dict[str, Any]:
    """Verify both feeds as of one explicit observation clock; no network or keys."""
    if (
        type(observed_at_ms) is not int
        or not 0 < observed_at_ms < 2**48
        or type(max_age_ms) is not int
        or not 0 < max_age_ms <= 1_800_000
        or type(max_spread_bps) is not int
        or not 0 < max_spread_bps <= 40
        or not isinstance(raw, bytes)
        or len(raw) > MAX_RAW_BYTES
    ):
        raise ValueError("bounded observation and price policy required")
    packages = json.loads(
        raw, parse_float=Decimal, parse_constant=_invalid_constant, object_pairs_hook=_unique
    )
    if not isinstance(packages, dict) or set(packages) != set(FEEDS):
        raise ValueError("exact ETH and USDC price package sets required")
    results, timestamps = {}, set()
    for feed in FEEDS:
        rows = packages[feed]
        if not isinstance(rows, list) or not 3 <= len(rows) <= 5:
            raise ValueError("three to five distinct authorized signers required per feed")
        verified = [verify_package(row, feed) for row in rows]
        if len({p.signer for p in verified}) != len(verified):
            raise ValueError("duplicate signer cannot count toward price quorum")
        for p in verified:
            if not 0 <= observed_at_ms - p.timestamp_ms <= max_age_ms:
                raise ValueError("signed price stale or future")
            timestamps.add(p.timestamp_ms)
        values = sorted(p.answer for p in verified)
        if (values[-1] - values[0]) * 10_000 > values[0] * max_spread_bps:
            raise ValueError("signed price quorum spread exceeded")
        mid = len(values) // 2
        median = values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) // 2
        results[feed] = {
            "decimals": 8,
            "median": median,
            "minimum": values[0],
            "maximum": values[-1],
            "witnesses": [asdict(p) for p in sorted(verified, key=lambda p: p.signer)],
        }
    if len(timestamps) != 1:
        raise ValueError("one signed timestamp required across both price quorums")
    result = {
        "schema": "oasis.signed-price-candidate.v1",
        "upstream_commit": UPSTREAM_COMMIT,
        "service": SERVICE,
        "signer_policy_hash": sha256_hex(canonical_json(sorted(SIGNERS))),
        "raw_hash": sha256_hex(raw),
        "observed_at_ms": observed_at_ms,
        "timestamp_ms": next(iter(timestamps)),
        "max_age_ms": max_age_ms,
        "max_spread_bps": max_spread_bps,
        "prices": results,
        "execution_eligible": False,
        "scope": "off-chain signed price candidate; not block-bound NAV or live admission",
    }
    result["evidence_hash"] = sha256_hex(canonical_json(result))
    return result
