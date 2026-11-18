"""Keyless dual-provider Base RPC quorum verifier.

Verifies chain ID, a lag-pinned block header (hash, parentHash, stateRoot,
timestamp), BaseTime predeploy consistency, and identical non-empty contract
runtime code hashes across two distinct HTTPS JSON-RPC providers.

Read-only diagnostic only (`execution_eligible` is permanently `False`).
"""

import argparse
import http.client
import ipaddress
import json
import math
import os
import re
import socket
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal, cast

import certifi

from ..arbitrage.event_quote import V3
from ..errors import RpcError
from ..evidence.base_denim import (
    BASE_CHAIN_ID,
    BASE_TIME,
    TIMESTAMP_MILLIS_PART_SELECTOR,
    TIMESTAMP_MS_SELECTOR,
    parse_base_time,
    validate_header_timestamp_ms,
)
from ..evidence.rpc_validation import (
    rpc_quantity,
    strict_json_object,
    strict_object,
    strict_string,
)
from ..models.common import canonical_json, normalize_address, normalize_hash32, sha256_hex
from ..sniping.venues import WETH

SCHEMA_VERSION: Final[str] = "oasis.dual-rpc-quorum.v1"
DEFAULT_PRIMARY_URL: Final[str] = "https://base-rpc.publicnode.com"
DEFAULT_PRIMARY_PROVIDER: Final[str] = "publicnode_base"
DEFAULT_SECONDARY_URL: Final[str] = "https://mainnet.base.org"
DEFAULT_SECONDARY_PROVIDER: Final[str] = "base_official_mainnet"
DEFAULT_BLOCK_LAG: Final[int] = 2
DEFAULT_TIMEOUT_S: Final[float] = 10.0
MAX_RESPONSE_BYTES: Final[int] = 1_000_000

CODE_TARGETS: Final[tuple[str, ...]] = (
    normalize_address(WETH),
    normalize_address(V3.address),
    normalize_address(BASE_TIME),
)

_LOCALHOST_NAMES: Final[frozenset[str]] = frozenset({"localhost", "localhost.localdomain"})
_PLACEHOLDER_MARKERS: Final[tuple[str, ...]] = (
    "unused.invalid",
    "example.invalid",
    "placeholder",
)
_FORBIDDEN_LABEL_MARKERS: Final[tuple[str, ...]] = (
    "127.0.0.1",
    "localhost",
    "::1",
    "unused.invalid",
    "example.invalid",
    "placeholder",
    "@",
)
_TUN_FAKE_IPV4_NET: Final[ipaddress.IPv4Network] = ipaddress.IPv4Network("198.18.0.0/15")
_TUN_FAKE_IPV6_NET: Final[ipaddress.IPv6Network] = ipaddress.IPv6Network("2001:2::/48")
_GLOBAL_DEFAULT_TIMEOUT: Final[object] = getattr(socket, "_GLOBAL_DEFAULT_TIMEOUT", object())
_SHA256_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")
_RAW_HEX_KEY_RE: Final[re.Pattern[str]] = re.compile(r"0x[0-9a-fA-F]{64}")
_HEX_BYTES_RE: Final[re.Pattern[str]] = re.compile(r"^0x(?:[0-9a-fA-F]{2})+$")
_USER_AGENT: Final[str] = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)

_AddrInfoEntry = tuple[
    socket.AddressFamily,
    socket.SocketKind,
    int,
    str,
    tuple[Any, ...],
]


class DualRpcQuorumError(ValueError):
    """Fail-closed validation or cross-provider disagreement error."""


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        with suppress(OSError):
            fp.close()
        raise RpcError("Dual RPC quorum probe redirects are forbidden")


@dataclass(frozen=True, slots=True)
class DualRpcQuorumReport:
    """Immutable cross-provider quorum verification receipt for Base (8453)."""

    primary_rpc_provider: str
    secondary_rpc_provider: str
    pinned_block_number: int
    pinned_block_hash: str
    parent_hash: str
    state_root: str
    base_time_ms: int
    code_sha256_by_target: dict[str, str]
    probed_at_utc: str
    report_sha256: str
    schema_version: str = SCHEMA_VERSION
    chain_id: int = BASE_CHAIN_ID
    execution_eligible: Literal[False] = False
    secondary_rpc_verified: Literal[True] = True

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise DualRpcQuorumError("unsupported dual-rpc-quorum schema_version")
        if type(self.chain_id) is not int or self.chain_id != BASE_CHAIN_ID:
            raise DualRpcQuorumError("chain_id must be Base (8453)")
        if self.execution_eligible is not False:
            raise DualRpcQuorumError("dual-rpc quorum report cannot set execution_eligible=True")
        if self.secondary_rpc_verified is not True:
            raise DualRpcQuorumError("secondary_rpc_verified must be True")

        primary_clean = _validate_provider_label(self.primary_rpc_provider, "primary_rpc_provider")
        secondary_clean = _validate_provider_label(
            self.secondary_rpc_provider, "secondary_rpc_provider"
        )
        if primary_clean.lower() == secondary_clean.lower():
            raise DualRpcQuorumError("primary and secondary provider labels must be distinct")
        object.__setattr__(self, "primary_rpc_provider", primary_clean)
        object.__setattr__(self, "secondary_rpc_provider", secondary_clean)

        if type(self.pinned_block_number) is not int or self.pinned_block_number <= 0:
            raise DualRpcQuorumError("pinned_block_number must be a positive integer")
        object.__setattr__(
            self,
            "pinned_block_hash",
            normalize_hash32(self.pinned_block_hash, "pinned_block_hash"),
        )
        object.__setattr__(self, "parent_hash", normalize_hash32(self.parent_hash, "parent_hash"))
        object.__setattr__(self, "state_root", normalize_hash32(self.state_root, "state_root"))
        if type(self.base_time_ms) is not int or self.base_time_ms < 0:
            raise DualRpcQuorumError("base_time_ms must be a nonnegative integer")

        if not isinstance(self.code_sha256_by_target, dict) or not self.code_sha256_by_target:
            raise DualRpcQuorumError("code_sha256_by_target must be a non-empty mapping")
        normalized_codes: dict[str, str] = {}
        for addr, digest in self.code_sha256_by_target.items():
            norm_addr = normalize_address(addr)
            if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
                raise DualRpcQuorumError(f"invalid code sha256 digest for {addr}")
            normalized_codes[norm_addr] = digest
        if set(normalized_codes) != set(CODE_TARGETS):
            raise DualRpcQuorumError("code_sha256_by_target missing required targets")
        object.__setattr__(self, "code_sha256_by_target", normalized_codes)

        if not isinstance(self.probed_at_utc, str) or not self.probed_at_utc.strip():
            raise DualRpcQuorumError("probed_at_utc must be a non-empty ISO-8601 timestamp")
        try:
            parsed_dt = datetime.fromisoformat(self.probed_at_utc)
        except ValueError as exc:
            raise DualRpcQuorumError("probed_at_utc must be valid ISO-8601") from exc
        if parsed_dt.tzinfo is None or parsed_dt.utcoffset() is None:
            raise DualRpcQuorumError("probed_at_utc must include timezone info")

        if (
            not isinstance(self.report_sha256, str)
            or _SHA256_RE.fullmatch(self.report_sha256) is None
        ):
            raise DualRpcQuorumError("report_sha256 must be a lowercase 64-hex SHA-256 digest")
        expected_sha = sha256_hex(canonical_json(self._body_dict()))
        if self.report_sha256 != expected_sha:
            raise DualRpcQuorumError("report_sha256 does not match canonical report body")

    def _body_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "chain_id": self.chain_id,
            "execution_eligible": self.execution_eligible,
            "primary_rpc_provider": self.primary_rpc_provider,
            "secondary_rpc_provider": self.secondary_rpc_provider,
            "secondary_rpc_verified": self.secondary_rpc_verified,
            "pinned_block_number": self.pinned_block_number,
            "pinned_block_hash": self.pinned_block_hash,
            "parent_hash": self.parent_hash,
            "state_root": self.state_root,
            "base_time_ms": self.base_time_ms,
            "code_sha256_by_target": dict(self.code_sha256_by_target),
            "probed_at_utc": self.probed_at_utc,
        }

    def to_dict(self) -> dict[str, Any]:
        """Serialize quorum report into a JSON-compatible dictionary."""
        body = self._body_dict()
        body["report_sha256"] = self.report_sha256
        return body

    def to_json(self, *, indent: int | None = 2) -> str:
        """Render the quorum report as formatted JSON."""
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True) + "\n"


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


def _validate_provider_label(label: object, role: str) -> str:
    if not isinstance(label, str) or not label.strip() or label != label.strip():
        raise DualRpcQuorumError(f"{role} must be a non-empty string without outer whitespace")
    lowered = label.lower()
    if any(marker in lowered for marker in _FORBIDDEN_LABEL_MARKERS):
        raise DualRpcQuorumError(
            f"{role} contains forbidden localhost/placeholder/credential marker"
        )
    if _RAW_HEX_KEY_RE.search(label) is not None:
        raise DualRpcQuorumError(f"{role} must not contain raw 32-byte hex material")
    return label


def _validate_public_ip_literal(hostname: str) -> None:
    lowered = hostname.lower()
    if lowered in _LOCALHOST_NAMES or lowered.endswith(".localhost"):
        raise DualRpcQuorumError("dual RPC probe forbids localhost endpoints")
    if any(marker in lowered for marker in _PLACEHOLDER_MARKERS):
        raise DualRpcQuorumError("dual RPC probe forbids placeholder endpoints")
    try:
        ip_obj = ipaddress.ip_address(hostname)
    except ValueError:
        return
    # Literal IP addresses must never bypass into TUN Fake-IP allowances.
    if _is_forbidden_ip(ip_obj) or _is_tun_fake_ip(ip_obj):
        raise DualRpcQuorumError(
            "dual RPC probe forbids private, loopback, or literal Fake-IP endpoints"
        )


def _validate_rpc_endpoint(url: str, *, role: str) -> tuple[str, str, int]:
    if not isinstance(url, str) or not url or url != url.strip():
        raise DualRpcQuorumError(f"{role} URL must be a non-empty string without outer whitespace")
    if any(ord(ch) < 32 or ch.isspace() for ch in url):
        raise DualRpcQuorumError(f"{role} URL must not contain whitespace or control characters")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise DualRpcQuorumError(f"{role} URL must use https:// with a valid host")
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise DualRpcQuorumError(f"{role} URL forbids userinfo credentials")
    if "@" in url:
        raise DualRpcQuorumError(f"{role} URL forbids '@' credential markers")
    if any(marker in parsed.netloc.lower() for marker in _PLACEHOLDER_MARKERS):
        raise DualRpcQuorumError(f"{role} URL forbids placeholder domains")
    hostname = parsed.hostname
    if not hostname:
        raise DualRpcQuorumError(f"{role} URL requires a non-empty hostname")
    try:
        port = parsed.port if parsed.port is not None else 443
    except ValueError as exc:
        raise DualRpcQuorumError(f"{role} URL contains an invalid port") from exc
    _validate_public_ip_literal(hostname)
    return parsed.netloc.lower(), hostname.lower(), port


def _resolve_and_validate_dns(hostname: str, port: int) -> list[_AddrInfoEntry]:
    _validate_public_ip_literal(hostname)
    try:
        addr_infos = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise RpcError("Dual RPC probe DNS resolution failed") from exc
    if not addr_infos:
        raise RpcError("Dual RPC probe DNS returned no addresses")
    validated: list[_AddrInfoEntry] = []
    for family, socktype, proto, canonname, sockaddr in addr_infos:
        raw_ip = str(sockaddr[0]).split("%", 1)[0]
        ip_obj = ipaddress.ip_address(raw_ip)
        is_mapped_tun = (
            isinstance(ip_obj, ipaddress.IPv6Address)
            and ip_obj.ipv4_mapped is not None
            and _is_tun_fake_ip(ip_obj.ipv4_mapped)
        )
        if _is_forbidden_ip(ip_obj) and not _is_tun_fake_ip(ip_obj) and not is_mapped_tun:
            raise DualRpcQuorumError("Dual RPC probe DNS resolved to a private or loopback IP")
        validated.append((family, socktype, proto, canonname, sockaddr))
    return validated


def _validate_resolved_dns(hostname: str, port: int) -> None:
    _resolve_and_validate_dns(hostname, port)


class _IPv4PreferredHTTPSConnection(http.client.HTTPSConnection):
    """HTTPSConnection that validates DNS IPs and prefers AF_INET before AF_INET6."""

    _context: ssl.SSLContext
    source_address: tuple[str, int] | None

    def __init__(
        self,
        host: str,
        port: int | None = None,
        *,
        timeout: float | None = cast(float | None, _GLOBAL_DEFAULT_TIMEOUT),
        source_address: tuple[str, int] | None = None,
        context: ssl.SSLContext | None = None,
        blocksize: int = 8192,
    ) -> None:
        resolved_context = (
            context if context is not None else ssl.create_default_context(cafile=certifi.where())
        )
        super().__init__(
            host,
            port=port,
            timeout=timeout,
            source_address=source_address,
            context=resolved_context,
            blocksize=blocksize,
        )
        self._context = resolved_context

    def connect(self) -> None:
        addr_infos = _resolve_and_validate_dns(self.host, self.port)
        ordered = sorted(
            addr_infos,
            key=lambda entry: 0 if entry[0] == socket.AF_INET else 1,
        )
        last_err: OSError | None = None
        for family, socktype, proto, _canonname, sockaddr in ordered:
            raw_sock: socket.socket | None = None
            connected = False
            try:
                raw_sock = socket.socket(family, socktype, proto)
                if self.timeout is not _GLOBAL_DEFAULT_TIMEOUT:
                    raw_sock.settimeout(self.timeout)
                if self.source_address is not None:
                    raw_sock.bind(self.source_address)
                raw_sock.connect(sockaddr)
                if hasattr(raw_sock, "setsockopt"):
                    with suppress(OSError):
                        raw_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                self.sock = self._context.wrap_socket(raw_sock, server_hostname=self.host)
                connected = True
                return
            except OSError as exc:
                last_err = exc
                self.sock = None
            finally:
                if not connected and raw_sock is not None:
                    with suppress(OSError):
                        raw_sock.close()
        if last_err is not None:
            raise last_err
        raise OSError("no validated addresses available")


class _IPv4PreferredHTTPSHandler(urllib.request.HTTPSHandler):
    """urllib HTTPSHandler using _IPv4PreferredHTTPSConnection."""

    _context: ssl.SSLContext

    def __init__(
        self,
        debuglevel: int | None = None,
        context: ssl.SSLContext | None = None,
        check_hostname: bool | None = None,
    ) -> None:
        resolved_context = (
            context if context is not None else ssl.create_default_context(cafile=certifi.where())
        )
        super().__init__(
            debuglevel=debuglevel,
            context=resolved_context,
            check_hostname=check_hostname,
        )
        self._context = resolved_context

    def https_open(self, req: urllib.request.Request) -> http.client.HTTPResponse:
        return self.do_open(
            _IPv4PreferredHTTPSConnection,
            req,
            context=self._context,
        )


def _default_https_transport(rpc_url: str, payload: bytes, timeout_s: float) -> bytes:
    _, hostname, port = _validate_rpc_endpoint(rpc_url, role="rpc")
    _validate_resolved_dns(hostname, port)

    opener = urllib.request.build_opener(
        _IPv4PreferredHTTPSHandler(context=ssl.create_default_context(cafile=certifi.where())),
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
                raise RpcError("Dual RPC probe response exceeds byte limit")
            return raw
    except urllib.error.HTTPError as exc:
        with suppress(OSError):
            exc.close()
        raise RpcError(f"Dual RPC probe HTTP failure: status {exc.code}") from None
    except (urllib.error.URLError, OSError) as exc:
        if isinstance(exc, RpcError):
            raise
        raise RpcError("Dual RPC probe transport failure") from None


def _unique_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise DualRpcQuorumError("RPC JSON contains duplicate fields")
        result[key] = value
    return result


def _reject_constant(_value: str) -> object:
    raise DualRpcQuorumError("RPC JSON contains non-finite numbers")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise DualRpcQuorumError("RPC JSON number overflows to non-finite value")
    return number


def _single_rpc_call(
    send: Callable[[str, bytes, float], bytes],
    url: str,
    *,
    role: str,
    method: str,
    params: list[object],
    timeout_s: float,
) -> object:
    req_bytes = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        separators=(",", ":"),
    ).encode("utf-8")
    resp_bytes = send(url, req_bytes, timeout_s)
    if not isinstance(resp_bytes, bytes) or len(resp_bytes) > MAX_RESPONSE_BYTES:
        raise RpcError(f"{role} {method} response must be bounded bytes")
    try:
        envelope = strict_json_object(resp_bytes)
    except ValueError as exc:
        raise DualRpcQuorumError(f"{role} {method} returned invalid JSON: {exc}") from exc
    if (
        envelope.get("jsonrpc") != "2.0"
        or type(envelope.get("id")) is not int
        or envelope["id"] != 1
    ):
        raise DualRpcQuorumError(f"{role} {method} JSON-RPC version or ID mismatch")
    if ("result" in envelope) == ("error" in envelope):
        raise DualRpcQuorumError(f"{role} {method} must contain exactly one result or error")
    if "error" in envelope:
        raise RpcError(f"{role} {method} returned JSON-RPC error")
    return envelope["result"]


def _strict_batch_response(
    payload: bytes, expected_ids: tuple[int, ...], *, role: str
) -> dict[int, object]:
    try:
        decoded = json.loads(
            payload,
            object_pairs_hook=_unique_fields,
            parse_constant=_reject_constant,
            parse_float=_finite_float,
        )
    except (json.JSONDecodeError, UnicodeError, RecursionError) as exc:
        raise DualRpcQuorumError(f"{role} batch response is invalid JSON") from exc
    if not isinstance(decoded, list) or len(decoded) != len(expected_ids):
        raise DualRpcQuorumError(f"{role} batch response length mismatch")

    results: dict[int, object] = {}
    expected_set = set(expected_ids)
    for item in decoded:
        try:
            envelope = strict_object(item, f"{role} batch item")
        except ValueError as exc:
            raise DualRpcQuorumError(str(exc)) from exc
        item_id = envelope.get("id")
        if (
            envelope.get("jsonrpc") != "2.0"
            or type(item_id) is not int
            or item_id not in expected_set
        ):
            raise DualRpcQuorumError(f"{role} batch item version or ID mismatch")
        if item_id in results:
            raise DualRpcQuorumError(f"{role} batch response contains duplicate ID")
        if ("result" in envelope) == ("error" in envelope):
            raise DualRpcQuorumError(f"{role} batch item must contain exactly one result or error")
        if "error" in envelope:
            raise RpcError(f"{role} batch call returned JSON-RPC error")
        results[item_id] = envelope["result"]
    if set(results) != expected_set:
        raise DualRpcQuorumError(f"{role} batch response missing expected IDs")
    return results


def _decode_uint256_word(value: object, name: str) -> int:
    try:
        raw = strict_string(value, name)
        normalized = normalize_hash32(raw, name)
    except ValueError as exc:
        raise DualRpcQuorumError(f"invalid uint256 word for {name}: {exc}") from exc
    return int(normalized, 16)


def _decode_code_sha256(value: object, *, target: str, role: str) -> str:
    try:
        raw_hex = strict_string(value, f"{role}.eth_getCode({target})")
    except ValueError as exc:
        raise DualRpcQuorumError(str(exc)) from exc
    if raw_hex == "0x":
        raise DualRpcQuorumError(f"{role} returned empty bytecode for {target}")
    if _HEX_BYTES_RE.fullmatch(raw_hex) is None:
        raise DualRpcQuorumError(f"{role} returned malformed bytecode hex for {target}")
    code_bytes = bytes.fromhex(raw_hex[2:])
    if not code_bytes:
        raise DualRpcQuorumError(f"{role} returned empty bytecode for {target}")
    return sha256_hex(code_bytes)


@dataclass(frozen=True, slots=True)
class _ProviderBlockSnapshot:
    block_hash: str
    parent_hash: str
    state_root: str
    timestamp_s: int
    base_time_ms: int
    millis_part: int
    code_hashes: dict[str, str]


def _fetch_provider_pinned_snapshot(
    send: Callable[[str, bytes, float], bytes],
    url: str,
    *,
    role: str,
    target_block: int,
    timeout_s: float,
) -> _ProviderBlockSnapshot:
    target_hex = hex(target_block)
    batch_payload: list[dict[str, object]] = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_getBlockByNumber",
            "params": [target_hex, False],
        },
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "eth_call",
            "params": [{"to": BASE_TIME, "data": TIMESTAMP_MS_SELECTOR}, target_hex],
        },
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "eth_call",
            "params": [{"to": BASE_TIME, "data": TIMESTAMP_MILLIS_PART_SELECTOR}, target_hex],
        },
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "eth_getCode",
            "params": [CODE_TARGETS[0], target_hex],
        },
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "eth_getCode",
            "params": [CODE_TARGETS[1], target_hex],
        },
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "eth_getCode",
            "params": [CODE_TARGETS[2], target_hex],
        },
    ]
    req_bytes = json.dumps(batch_payload, separators=(",", ":")).encode("utf-8")
    resp_bytes = send(url, req_bytes, timeout_s)
    if not isinstance(resp_bytes, bytes) or len(resp_bytes) > MAX_RESPONSE_BYTES:
        raise RpcError(f"{role} pinned batch response must be bounded bytes")

    results = _strict_batch_response(resp_bytes, (1, 2, 3, 4, 5, 6), role=role)
    try:
        header = strict_object(results[1], f"{role} block header")
        header_num = rpc_quantity(header.get("number"))
        if header_num != target_block:
            raise DualRpcQuorumError(
                f"{role} returned block {header_num}, expected pinned block {target_block}"
            )
        block_hash = normalize_hash32(strict_string(header.get("hash"), "hash"), "hash")
        parent_hash = normalize_hash32(
            strict_string(header.get("parentHash"), "parentHash"), "parentHash"
        )
        state_root = normalize_hash32(
            strict_string(header.get("stateRoot"), "stateRoot"), "stateRoot"
        )
        header_ts_s = rpc_quantity(header.get("timestamp"))
        header_ts_ms = validate_header_timestamp_ms(header, chain_id=BASE_CHAIN_ID)

        predeploy_ms = _decode_uint256_word(results[2], f"{role}.timestampMs")
        millis_part = _decode_uint256_word(results[3], f"{role}.timestampMillisPart")
        checked_ms = parse_base_time(
            timestamp_ms=predeploy_ms,
            millis_part=millis_part,
            header_timestamp=header_ts_s,
        )
    except ValueError as exc:
        if isinstance(exc, DualRpcQuorumError):
            raise
        raise DualRpcQuorumError(
            f"{role} pinned block or BaseTime validation failed: {exc}"
        ) from exc

    if header_ts_ms is not None and header_ts_ms != checked_ms:
        raise DualRpcQuorumError(f"{role} base_time_header_timestamp_ms_mismatch")

    code_hashes = {
        target: _decode_code_sha256(results[idx], target=target, role=role)
        for idx, target in zip((4, 5, 6), CODE_TARGETS, strict=True)
    }
    return _ProviderBlockSnapshot(
        block_hash=block_hash,
        parent_hash=parent_hash,
        state_root=state_root,
        timestamp_s=header_ts_s,
        base_time_ms=checked_ms,
        millis_part=millis_part,
        code_hashes=code_hashes,
    )


def probe_dual_base_rpc_quorum(
    *,
    primary_url: str = DEFAULT_PRIMARY_URL,
    primary_provider: str = DEFAULT_PRIMARY_PROVIDER,
    secondary_url: str = DEFAULT_SECONDARY_URL,
    secondary_provider: str = DEFAULT_SECONDARY_PROVIDER,
    block_lag: int = DEFAULT_BLOCK_LAG,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    transport: Callable[[str, bytes, float], bytes] | None = None,
) -> DualRpcQuorumReport:
    """Verify Base block header, BaseTime, and contract code agreement across two RPC providers."""
    if type(block_lag) is not int or block_lag < 0:
        raise DualRpcQuorumError("block_lag must be a nonnegative integer")
    if (
        isinstance(timeout_s, bool)
        or not isinstance(timeout_s, (int, float))
        or not math.isfinite(float(timeout_s))
        or float(timeout_s) <= 0
    ):
        raise DualRpcQuorumError("timeout_s must be a positive finite number")

    primary_label = _validate_provider_label(primary_provider, "primary_provider")
    secondary_label = _validate_provider_label(secondary_provider, "secondary_provider")
    if primary_label.lower() == secondary_label.lower():
        raise DualRpcQuorumError("primary_provider and secondary_provider must be distinct")

    p_netloc, p_host, p_port = _validate_rpc_endpoint(primary_url, role="primary")
    s_netloc, s_host, s_port = _validate_rpc_endpoint(secondary_url, role="secondary")
    if p_netloc == s_netloc or (p_host, p_port) == (s_host, s_port):
        raise DualRpcQuorumError("primary_url and secondary_url must have distinct netlocs")

    send = _default_https_transport if transport is None else transport
    timeout_val = float(timeout_s)

    # 1. Verify eth_chainId == 8453 on both providers.
    for role, url in (("primary", primary_url), ("secondary", secondary_url)):
        raw_chain = _single_rpc_call(
            send,
            url,
            role=role,
            method="eth_chainId",
            params=[],
            timeout_s=timeout_val,
        )
        try:
            chain_id = rpc_quantity(raw_chain)
        except ValueError as exc:
            raise DualRpcQuorumError(f"{role} invalid eth_chainId response: {exc}") from exc
        if chain_id != BASE_CHAIN_ID:
            raise DualRpcQuorumError(
                f"{role} eth_chainId mismatch: expected {BASE_CHAIN_ID}, got {chain_id}"
            )

    # 2. Fetch eth_blockNumber on primary and pin target_block = max(1, latest_block - block_lag).
    raw_block_num = _single_rpc_call(
        send,
        primary_url,
        role="primary",
        method="eth_blockNumber",
        params=[],
        timeout_s=timeout_val,
    )
    try:
        latest_block = rpc_quantity(raw_block_num)
    except ValueError as exc:
        raise DualRpcQuorumError(f"primary invalid eth_blockNumber response: {exc}") from exc
    if latest_block <= 0:
        raise DualRpcQuorumError("primary latest block number must be positive")
    target_block = max(1, latest_block - block_lag)

    # 3. Fetch pinned block header, BaseTime predeploy pair, and runtime bytecodes on both.
    primary_snap = _fetch_provider_pinned_snapshot(
        send,
        primary_url,
        role="primary",
        target_block=target_block,
        timeout_s=timeout_val,
    )
    secondary_snap = _fetch_provider_pinned_snapshot(
        send,
        secondary_url,
        role="secondary",
        target_block=target_block,
        timeout_s=timeout_val,
    )

    if primary_snap.block_hash != secondary_snap.block_hash:
        raise DualRpcQuorumError(
            "dual RPC quorum mismatch on block hash at "
            f"block {target_block}: {primary_snap.block_hash} != {secondary_snap.block_hash}"
        )
    if primary_snap.parent_hash != secondary_snap.parent_hash:
        raise DualRpcQuorumError(f"dual RPC quorum mismatch on parentHash at block {target_block}")
    if primary_snap.state_root != secondary_snap.state_root:
        raise DualRpcQuorumError(f"dual RPC quorum mismatch on stateRoot at block {target_block}")
    if primary_snap.timestamp_s != secondary_snap.timestamp_s:
        raise DualRpcQuorumError(f"dual RPC quorum mismatch on timestamp at block {target_block}")
    if (
        primary_snap.base_time_ms != secondary_snap.base_time_ms
        or primary_snap.millis_part != secondary_snap.millis_part
    ):
        raise DualRpcQuorumError(
            f"dual RPC quorum mismatch on BaseTime predeploy at block {target_block}"
        )
    for target in CODE_TARGETS:
        if primary_snap.code_hashes[target] != secondary_snap.code_hashes[target]:
            raise DualRpcQuorumError(f"dual RPC quorum mismatch on eth_getCode sha256 for {target}")

    probed_at_utc = datetime.now(UTC).isoformat()
    body: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "chain_id": BASE_CHAIN_ID,
        "execution_eligible": False,
        "primary_rpc_provider": primary_label,
        "secondary_rpc_provider": secondary_label,
        "secondary_rpc_verified": True,
        "pinned_block_number": target_block,
        "pinned_block_hash": primary_snap.block_hash,
        "parent_hash": primary_snap.parent_hash,
        "state_root": primary_snap.state_root,
        "base_time_ms": primary_snap.base_time_ms,
        "code_sha256_by_target": dict(primary_snap.code_hashes),
        "probed_at_utc": probed_at_utc,
    }
    report_sha256 = sha256_hex(canonical_json(body))
    return DualRpcQuorumReport(
        primary_rpc_provider=primary_label,
        secondary_rpc_provider=secondary_label,
        pinned_block_number=target_block,
        pinned_block_hash=primary_snap.block_hash,
        parent_hash=primary_snap.parent_hash,
        state_root=primary_snap.state_root,
        base_time_ms=primary_snap.base_time_ms,
        code_sha256_by_target=dict(primary_snap.code_hashes),
        probed_at_utc=probed_at_utc,
        report_sha256=report_sha256,
    )


def _write_immutable_json(out_path: Path, content: str) -> None:
    if out_path.is_symlink():
        raise DualRpcQuorumError(f"refusing symlink output path: {out_path}")
    if out_path.exists():
        raise FileExistsError(f"refusing to overwrite existing output file: {out_path}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("xb") as handle:
        handle.write(content.encode("utf-8"))
        handle.flush()
        os.fsync(handle.fileno())


def build_parser() -> argparse.ArgumentParser:
    """Build CLI parser for keyless dual-provider Base RPC quorum verification."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--primary-url",
        default=DEFAULT_PRIMARY_URL,
        help=f"Primary Base HTTPS JSON-RPC URL (default: {DEFAULT_PRIMARY_URL}).",
    )
    parser.add_argument(
        "--primary-provider",
        default=DEFAULT_PRIMARY_PROVIDER,
        help=f"Primary RPC provider identifier (default: {DEFAULT_PRIMARY_PROVIDER}).",
    )
    parser.add_argument(
        "--secondary-url",
        default=DEFAULT_SECONDARY_URL,
        help=f"Secondary Base HTTPS JSON-RPC URL (default: {DEFAULT_SECONDARY_URL}).",
    )
    parser.add_argument(
        "--secondary-provider",
        default=DEFAULT_SECONDARY_PROVIDER,
        help=f"Secondary RPC provider identifier (default: {DEFAULT_SECONDARY_PROVIDER}).",
    )
    parser.add_argument(
        "--block-lag",
        type=int,
        default=DEFAULT_BLOCK_LAG,
        help=f"Blocks behind primary head to pin (default: {DEFAULT_BLOCK_LAG}).",
    )
    parser.add_argument(
        "--timeout-s",
        type=float,
        default=DEFAULT_TIMEOUT_S,
        help=f"Per-request timeout in seconds (default: {DEFAULT_TIMEOUT_S}).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Optional output path to write immutable DualRpcQuorumReport JSON.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Execute dual-provider Base RPC quorum check and write/print the report."""
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        if args.out is not None and (args.out.is_symlink() or args.out.exists()):
            if args.out.is_symlink():
                raise DualRpcQuorumError(f"refusing symlink output path: {args.out}")
            raise FileExistsError(f"refusing to overwrite existing output file: {args.out}")
        report = probe_dual_base_rpc_quorum(
            primary_url=args.primary_url,
            primary_provider=args.primary_provider,
            secondary_url=args.secondary_url,
            secondary_provider=args.secondary_provider,
            block_lag=args.block_lag,
            timeout_s=args.timeout_s,
        )
        rendered = report.to_json(indent=2)
        if args.out is not None:
            _write_immutable_json(args.out, rendered)
            print(
                json.dumps(
                    {
                        "schema_version": report.schema_version,
                        "chain_id": report.chain_id,
                        "pinned_block_number": report.pinned_block_number,
                        "pinned_block_hash": report.pinned_block_hash,
                        "secondary_rpc_verified": report.secondary_rpc_verified,
                        "execution_eligible": report.execution_eligible,
                        "report_sha256": report.report_sha256,
                        "out": str(args.out),
                    },
                    sort_keys=True,
                )
            )
        else:
            sys.stdout.write(rendered)
    except (OSError, ValueError, TypeError, RpcError) as exc:
        print(f"dual-rpc-probe failed: {exc}", file=sys.stderr)
        return 2
    return 0


__all__ = [
    "CODE_TARGETS",
    "DEFAULT_BLOCK_LAG",
    "DEFAULT_PRIMARY_PROVIDER",
    "DEFAULT_PRIMARY_URL",
    "DEFAULT_SECONDARY_PROVIDER",
    "DEFAULT_SECONDARY_URL",
    "DEFAULT_TIMEOUT_S",
    "MAX_RESPONSE_BYTES",
    "SCHEMA_VERSION",
    "DualRpcQuorumError",
    "DualRpcQuorumReport",
    "main",
    "probe_dual_base_rpc_quorum",
]

if __name__ == "__main__":
    raise SystemExit(main())
