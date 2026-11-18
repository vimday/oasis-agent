"""Bounded on-chain verification probe for the Base Denim BaseTime predeploy.

Reads one Base block header (`eth_getBlockByNumber`) and one pinned JSON-RPC batch
containing two `eth_call` requests (`timestampMs()` and `timestampMillisPart()`)
against `BASE_TIME` at `hex(block_number)`. Enforces HTTPS-only SSRF guards, explicit
request-budget accounting (3 calls total <= `max_requests`), SHA-256 request/response
retention, and cross-checks against `parse_base_time` and `validate_header_timestamp_ms`.
"""

import ipaddress
import json
import math
import re
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, cast

import certifi

from ..errors import RpcError
from ..models.common import normalize_address, normalize_hash32, sha256_hex
from .base_denim import (
    BASE_CHAIN_ID,
    BASE_TIME,
    TIMESTAMP_MILLIS_PART_SELECTOR,
    TIMESTAMP_MS_SELECTOR,
    parse_base_time,
    validate_header_timestamp_ms,
)
from .rpc_validation import rpc_quantity, strict_json_object, strict_object, strict_string

MAX_RESPONSE_BYTES: Final[int] = 1_000_000
_BATCH_CALL_COUNT: Final[int] = 2
_TOTAL_REQUIRED_CALLS: Final[int] = 3
_LOCALHOST_NAMES: Final[frozenset[str]] = frozenset({"localhost", "localhost.localdomain"})
_NAMED_BLOCK_TAGS: Final[frozenset[str]] = frozenset({"latest", "safe", "finalized"})
_USER_AGENT: Final[str] = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        with suppress(OSError):
            fp.close()
        raise RpcError("BaseTime probe redirects are forbidden")


@dataclass(frozen=True, slots=True)
class BaseTimeVerificationReceipt:
    """Retained, cross-checked verification receipt for a BaseTime predeploy read."""

    chain_id: int
    predeploy_address: str
    block_number: int
    block_hash: str
    header_timestamp: int
    header_timestamp_ms: int | None
    predeploy_timestamp_ms: int
    predeploy_millis_part: int
    requests_used: int
    max_requests: int
    header_request_sha256: str
    header_response_sha256: str
    batch_request_sha256: str
    batch_response_sha256: str
    rpc_origin: str
    verified_at_utc: str

    def __post_init__(self) -> None:
        if type(self.chain_id) is not int or self.chain_id != BASE_CHAIN_ID:
            raise ValueError("chain_id must be Base (8453)")
        if normalize_address(self.predeploy_address) != BASE_TIME:
            raise ValueError("predeploy_address must match BASE_TIME")
        if type(self.block_number) is not int or self.block_number < 0:
            raise ValueError("block_number must be a nonnegative integer")
        object.__setattr__(self, "block_hash", normalize_hash32(self.block_hash, "block_hash"))
        if type(self.header_timestamp) is not int or self.header_timestamp < 0:
            raise ValueError("header_timestamp must be a nonnegative integer")
        if self.header_timestamp_ms is not None and (
            type(self.header_timestamp_ms) is not int or self.header_timestamp_ms < 0
        ):
            raise ValueError("header_timestamp_ms must be a nonnegative integer or None")
        checked = parse_base_time(
            timestamp_ms=self.predeploy_timestamp_ms,
            millis_part=self.predeploy_millis_part,
            header_timestamp=self.header_timestamp,
        )
        if self.header_timestamp_ms is not None and self.header_timestamp_ms != checked:
            raise ValueError("base_time_header_timestamp_ms_mismatch")
        if type(self.max_requests) is not int or self.max_requests < _TOTAL_REQUIRED_CALLS:
            raise ValueError("max_requests must cover all BaseTime probe calls")
        if (
            type(self.requests_used) is not int
            or self.requests_used < _TOTAL_REQUIRED_CALLS
            or self.requests_used > self.max_requests
        ):
            raise ValueError("requests_used out of budget bounds")
        for digest_name, digest_val in (
            ("header_request_sha256", self.header_request_sha256),
            ("header_response_sha256", self.header_response_sha256),
            ("batch_request_sha256", self.batch_request_sha256),
            ("batch_response_sha256", self.batch_response_sha256),
        ):
            if not isinstance(digest_val, str) or re.fullmatch(r"[0-9a-f]{64}", digest_val) is None:
                raise ValueError(f"{digest_name} must be a lowercase SHA-256 hex digest")
        if not isinstance(self.rpc_origin, str) or not self.rpc_origin.startswith("https://"):
            raise ValueError("rpc_origin must be an https:// origin")
        if not isinstance(self.verified_at_utc, str) or not self.verified_at_utc.strip():
            raise ValueError("verified_at_utc must be a nonempty ISO-8601 timestamp")


def _is_forbidden_ip(ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        ip_obj.is_private
        or ip_obj.is_loopback
        or ip_obj.is_link_local
        or ip_obj.is_multicast
        or ip_obj.is_reserved
        or ip_obj.is_unspecified
    )


def _validate_public_ip_literal(hostname: str) -> None:
    lowered = hostname.lower()
    if lowered in _LOCALHOST_NAMES or lowered.endswith(".localhost"):
        raise ValueError("BaseTime probe forbids localhost RPC endpoints")
    try:
        ip_obj = ipaddress.ip_address(hostname)
    except ValueError:
        return
    if _is_forbidden_ip(ip_obj):
        raise ValueError("BaseTime probe forbids private or loopback IP endpoints")


def _validate_rpc_url(rpc_url: str, *, enforce_public_host: bool) -> tuple[str, str, int]:
    if not isinstance(rpc_url, str) or not rpc_url or rpc_url != rpc_url.strip():
        raise ValueError("rpc_url must be a nonempty string without leading/trailing whitespace")
    parsed = urllib.parse.urlsplit(rpc_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("BaseTime probe requires an https:// endpoint with host")
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise ValueError("BaseTime probe forbids URL userinfo credentials")
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("BaseTime probe requires a nonempty hostname")
    try:
        port = parsed.port if parsed.port is not None else 443
    except ValueError as exc:
        raise ValueError("BaseTime probe URL contains an invalid port") from exc
    if enforce_public_host:
        _validate_public_ip_literal(hostname)
    display_host = f"[{hostname}]" if ":" in hostname else hostname
    origin = f"https://{display_host}" + (f":{parsed.port}" if parsed.port is not None else "")
    return origin, hostname, port


def _validate_resolved_dns(hostname: str, port: int) -> None:
    _validate_public_ip_literal(hostname)
    try:
        addr_infos = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise RpcError("BaseTime probe DNS resolution failed") from exc
    for _, _, _, _, sockaddr in addr_infos:
        raw_ip = str(sockaddr[0]).split("%", 1)[0]
        ip_obj = ipaddress.ip_address(raw_ip)
        if _is_forbidden_ip(ip_obj):
            raise ValueError("BaseTime probe DNS resolved to a private or loopback IP")


def _default_https_transport(rpc_url: str, payload: bytes, timeout_s: float) -> bytes:
    parsed = urllib.parse.urlsplit(rpc_url)
    if parsed.scheme != "https" or not parsed.netloc or not parsed.hostname:
        raise ValueError("BaseTime probe requires an https:// endpoint")
    port = parsed.port if parsed.port is not None else 443
    _validate_resolved_dns(parsed.hostname, port)

    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=certifi.where())),
        _NoRedirectHandler(),
    )
    request = urllib.request.Request(
        rpc_url,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": _USER_AGENT},
        method="POST",
    )
    try:
        with opener.open(request, timeout=timeout_s) as response:
            raw = cast(bytes, response.read(MAX_RESPONSE_BYTES + 1))
            if len(raw) > MAX_RESPONSE_BYTES:
                raise RpcError("BaseTime probe response exceeds byte limit")
            return raw
    except urllib.error.HTTPError as exc:
        with suppress(OSError):
            exc.close()
        raise RpcError(f"BaseTime probe HTTP failure: status {exc.code}") from None
    except (urllib.error.URLError, OSError) as exc:
        if isinstance(exc, RpcError):
            raise
        raise RpcError("BaseTime probe transport failure") from None


def _validate_block_tag(block_tag: str) -> str:
    if not isinstance(block_tag, str) or not block_tag:
        raise ValueError("block_tag must be a nonempty string")
    if block_tag in _NAMED_BLOCK_TAGS:
        return block_tag
    rpc_quantity(block_tag)
    return block_tag


def _unique_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("RPC JSON contains duplicate fields")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise ValueError("RPC JSON contains non-finite numbers")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("RPC JSON number overflows to non-finite value")
    return number


def _strict_batch_response(payload: bytes, expected_ids: tuple[int, ...]) -> dict[int, object]:
    try:
        decoded = json.loads(
            payload,
            object_pairs_hook=_unique_fields,
            parse_constant=_reject_constant,
            parse_float=_finite_float,
        )
    except (json.JSONDecodeError, UnicodeError, RecursionError) as exc:
        raise ValueError("BaseTime batch response is invalid JSON") from exc
    if not isinstance(decoded, list) or len(decoded) != len(expected_ids):
        raise ValueError("BaseTime batch response length mismatch")

    results: dict[int, object] = {}
    expected_set = set(expected_ids)
    for item in decoded:
        envelope = strict_object(item, "batch item")
        item_id = envelope.get("id")
        if (
            envelope.get("jsonrpc") != "2.0"
            or type(item_id) is not int
            or item_id not in expected_set
        ):
            raise ValueError("BaseTime batch item version or ID mismatch")
        if item_id in results:
            raise ValueError("BaseTime batch response contains duplicate ID")
        if ("result" in envelope) == ("error" in envelope):
            raise ValueError("BaseTime batch item must contain exactly one result or error")
        if "error" in envelope:
            raise RpcError("BaseTime batch eth_call returned JSON-RPC error")
        results[item_id] = envelope["result"]
    if set(results) != expected_set:
        raise ValueError("BaseTime batch response missing expected IDs")
    return results


def _decode_uint256_word(value: object, name: str) -> int:
    raw = strict_string(value, name)
    normalized = normalize_hash32(raw, name)
    return int(normalized, 16)


def verify_base_time_predeploy(
    rpc_url: str,
    *,
    block_tag: str = "latest",
    timeout_s: float = 8.0,
    max_requests: int = 4,
    transport: Callable[[str, bytes, float], bytes] | None = None,
) -> BaseTimeVerificationReceipt:
    """Probe BaseTime `timestampMs()` and `timestampMillisPart()` pinned to one Base block."""
    if (
        isinstance(timeout_s, bool)
        or not isinstance(timeout_s, (int, float))
        or not math.isfinite(float(timeout_s))
        or float(timeout_s) <= 0
    ):
        raise ValueError("timeout_s must be a positive finite number")
    if type(max_requests) is not int or max_requests <= 0:
        raise ValueError("max_requests must be a positive integer")

    normalized_tag = _validate_block_tag(block_tag)
    origin, _, _ = _validate_rpc_url(rpc_url, enforce_public_host=transport is None)
    send = _default_https_transport if transport is None else transport
    requests_used = 0

    def _reserve(cost: int) -> None:
        nonlocal requests_used
        if requests_used + cost > max_requests:
            raise ValueError("BaseTime probe request budget exceeded")
        requests_used += cost

    _reserve(1)
    header_req_bytes = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_getBlockByNumber",
            "params": [normalized_tag, False],
        },
        separators=(",", ":"),
    ).encode("utf-8")
    header_req_sha256 = sha256_hex(header_req_bytes)
    header_resp_bytes = send(rpc_url, header_req_bytes, float(timeout_s))
    if not isinstance(header_resp_bytes, bytes) or len(header_resp_bytes) > MAX_RESPONSE_BYTES:
        raise RpcError("BaseTime header response must be bounded bytes")
    header_resp_sha256 = sha256_hex(header_resp_bytes)

    header_envelope = strict_json_object(header_resp_bytes)
    if (
        header_envelope.get("jsonrpc") != "2.0"
        or type(header_envelope.get("id")) is not int
        or header_envelope["id"] != 1
    ):
        raise ValueError("BaseTime header JSON-RPC version or ID mismatch")
    if ("result" in header_envelope) == ("error" in header_envelope):
        raise ValueError("BaseTime header response must contain exactly one result or error")
    if "error" in header_envelope:
        raise RpcError("BaseTime eth_getBlockByNumber returned JSON-RPC error")

    header = strict_object(header_envelope["result"], "block header")
    block_number = rpc_quantity(header.get("number"))
    block_hash = normalize_hash32(strict_string(header.get("hash"), "block hash"), "block hash")
    header_timestamp = rpc_quantity(header.get("timestamp"))
    header_timestamp_ms = validate_header_timestamp_ms(header, chain_id=BASE_CHAIN_ID)

    _reserve(_BATCH_CALL_COUNT)
    pinned_block = hex(block_number)
    batch_req_bytes = json.dumps(
        [
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "eth_call",
                "params": [{"to": BASE_TIME, "data": TIMESTAMP_MS_SELECTOR}, pinned_block],
            },
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "eth_call",
                "params": [{"to": BASE_TIME, "data": TIMESTAMP_MILLIS_PART_SELECTOR}, pinned_block],
            },
        ],
        separators=(",", ":"),
    ).encode("utf-8")
    batch_req_sha256 = sha256_hex(batch_req_bytes)
    batch_resp_bytes = send(rpc_url, batch_req_bytes, float(timeout_s))
    if not isinstance(batch_resp_bytes, bytes) or len(batch_resp_bytes) > MAX_RESPONSE_BYTES:
        raise RpcError("BaseTime batch response must be bounded bytes")
    batch_resp_sha256 = sha256_hex(batch_resp_bytes)

    batch_results = _strict_batch_response(batch_resp_bytes, (2, 3))
    predeploy_timestamp_ms = _decode_uint256_word(batch_results[2], "timestampMs")
    predeploy_millis_part = _decode_uint256_word(batch_results[3], "timestampMillisPart")

    checked_ms = parse_base_time(
        timestamp_ms=predeploy_timestamp_ms,
        millis_part=predeploy_millis_part,
        header_timestamp=header_timestamp,
    )
    if header_timestamp_ms is not None and header_timestamp_ms != checked_ms:
        raise ValueError("base_time_header_timestamp_ms_mismatch")

    return BaseTimeVerificationReceipt(
        chain_id=BASE_CHAIN_ID,
        predeploy_address=BASE_TIME,
        block_number=block_number,
        block_hash=block_hash,
        header_timestamp=header_timestamp,
        header_timestamp_ms=header_timestamp_ms,
        predeploy_timestamp_ms=checked_ms,
        predeploy_millis_part=predeploy_millis_part,
        requests_used=requests_used,
        max_requests=max_requests,
        header_request_sha256=header_req_sha256,
        header_response_sha256=header_resp_sha256,
        batch_request_sha256=batch_req_sha256,
        batch_response_sha256=batch_resp_sha256,
        rpc_origin=origin,
        verified_at_utc=datetime.now(UTC).isoformat(),
    )


__all__ = [
    "MAX_RESPONSE_BYTES",
    "BaseTimeVerificationReceipt",
    "verify_base_time_predeploy",
]
