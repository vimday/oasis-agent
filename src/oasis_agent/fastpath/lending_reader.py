"""Keyless, read-only, block-pinned Base Aave V3 and Morpho Blue lending health reader.

Pins the Base block header first, discovers recent Aave V3 borrowers via bounded chunked
``Borrow`` event ``eth_getLogs`` queries merged with optional seed addresses, and reads
``Multicall3.aggregate3`` at the pinned block height to cross-check ``chain_id == 8453``
and ``BaseTime`` alongside Aave V3 reserve/user account data. Optionally invokes
``FlashLiquidationScanner.scan`` when liquidatable accounts and a ``pool_graph`` are
provided, and exposes ``read_morpho_blue_positions`` for pinned Morpho Blue auditing.

All reads are keyless and read-only (`execution_eligible = False` permanently).
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
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Final, Literal, cast

import certifi
from eth_abi.abi import decode, encode
from eth_abi.exceptions import DecodingError
from eth_hash.auto import keccak

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
    strict_list,
    strict_object,
    strict_string,
)
from ..models.common import (
    ZERO_ADDRESS,
    canonical_json,
    normalize_address,
    normalize_hash32,
    sha256_hex,
)
from ..sniping.venues import USDC
from .bridge import require_int
from .liquidation import (
    BorrowerPositionSnapshot,
    FlashLiquidationConfig,
    FlashLiquidationScanner,
    LiquidationCandidate,
)
from .live_shadow_runner import DEFAULT_BASE_RPC_URL, MAX_RESPONSE_BYTES
from .pool_graph import FastpathPoolGraph, PoolKind, PoolNode

RpcTransport = Callable[[str, bytes, float], bytes]

LENDING_HEALTH_SCHEMA_VERSION: Final[str] = "oasis.base-lending-health.v1"
MORPHO_AUDIT_SCHEMA_VERSION: Final[str] = "oasis.base-morpho-positions.v1"
DEFAULT_AT_RISK_HF_WAD: Final[int] = 1_080_000_000_000_000_000

_WAD: Final[int] = 10**18
_BPS_DENOM: Final[int] = 10_000
_USD_8DEC_SCALE: Final[int] = 10**8
_AAVE_CLOSE_FACTOR_HF_THRESHOLD: Final[int] = 95 * 10**16
_AAVE_DEFAULT_LIQUIDATION_BONUS_BPS: Final[int] = 500
_UINT128_LIMIT: Final[int] = 1 << 128
_UINT256_LIMIT: Final[int] = 1 << 256
_LOCALHOST_NAMES: Final[frozenset[str]] = frozenset({"localhost", "localhost.localdomain"})
_NAMED_BLOCK_TAGS: Final[frozenset[str]] = frozenset({"latest", "safe", "finalized"})
_TUN_FAKE_IPV4_NET: Final[ipaddress.IPv4Network] = ipaddress.IPv4Network("198.18.0.0/15")
_TUN_FAKE_IPV6_NET: Final[ipaddress.IPv6Network] = ipaddress.IPv6Network("2001:2::/48")
_SHA256_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")
_HEX_BYTES_RE: Final[re.Pattern[str]] = re.compile(r"^0x(?:[0-9a-fA-F]{2})*$")
_USER_AGENT: Final[str] = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)


def _addr20(hi80: int, lo80: int) -> str:
    return f"0x{hi80:020x}{lo80:020x}"


def _selector(signature: str) -> str:
    return "0x" + keccak(signature.encode("ascii"))[:4].hex()


def _topic0(signature: str) -> str:
    return "0x" + keccak(signature.encode("ascii")).hex()


MULTICALL3: Final[str] = _addr20(954245306222959992705383, 11960605330504318110225)
AAVE_V3_POOL: Final[str] = _addr20(766072348863779973267927, 1078586501037435580371397)
MORPHO_BLUE: Final[str] = _addr20(886545601050595088207419, 278440621000339517013963)

AGGREGATE3_SELECTOR: Final[str] = _selector("aggregate3((address,bool,bytes)[])")
MULTICALL3_GET_CHAIN_ID_SELECTOR: Final[str] = _selector("getChainId()")
AAVE_GET_RESERVES_LIST_SELECTOR: Final[str] = _selector("getReservesList()")
AAVE_GET_USER_ACCOUNT_DATA_SELECTOR: Final[str] = _selector("getUserAccountData(address)")
MORPHO_ID_TO_MARKET_PARAMS_SELECTOR: Final[str] = _selector("idToMarketParams(bytes32)")
MORPHO_MARKET_SELECTOR: Final[str] = _selector("market(bytes32)")
MORPHO_POSITION_SELECTOR: Final[str] = _selector("position(bytes32,address)")

AAVE_BORROW_TOPIC0: Final[str] = _topic0(
    "Borrow(address,address,address,uint256,uint8,uint256,uint16)"
)
AAVE_SUPPLY_TOPIC0: Final[str] = _topic0("Supply(address,address,address,uint256,uint16)")
AAVE_REPAY_TOPIC0: Final[str] = _topic0("Repay(address,address,address,uint256,bool)")
AAVE_LIQUIDATION_CALL_TOPIC0: Final[str] = _topic0(
    "LiquidationCall(address,address,address,uint256,uint256,address,bool)"
)


class LendingReaderError(ValueError):
    """Fail-closed validation, ABI, SSRF, or state-integrity error in lending reads."""


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        with suppress(OSError):
            fp.close()
        raise RpcError("Base lending RPC redirects are forbidden")


@dataclass(frozen=True, slots=True)
class AaveBorrowerAccountState:
    """Normalized Aave V3 ``getUserAccountData`` snapshot for one borrower."""

    borrower: str
    total_collateral_usd_8dec: int
    total_debt_usd_8dec: int
    available_borrows_usd_8dec: int
    current_liquidation_threshold_bps: int
    ltv_bps: int
    health_factor_wad: int
    distance_to_liquidation_bps: int
    liquidatable: bool
    total_debt_usd: float

    def __post_init__(self) -> None:
        normalized = normalize_address(self.borrower)
        if normalized == ZERO_ADDRESS:
            raise LendingReaderError("borrower cannot be zero address")
        object.__setattr__(self, "borrower", normalized)
        require_int(self.total_collateral_usd_8dec, "total_collateral_usd_8dec", non_negative=True)
        require_int(self.total_debt_usd_8dec, "total_debt_usd_8dec", non_negative=True)
        require_int(
            self.available_borrows_usd_8dec, "available_borrows_usd_8dec", non_negative=True
        )
        liq_thresh = require_int(
            self.current_liquidation_threshold_bps,
            "current_liquidation_threshold_bps",
            non_negative=True,
        )
        ltv = require_int(self.ltv_bps, "ltv_bps", non_negative=True)
        if liq_thresh > _BPS_DENOM or ltv > _BPS_DENOM:
            raise LendingReaderError("Aave threshold/LTV bps must be <= 10_000")
        require_int(self.health_factor_wad, "health_factor_wad", non_negative=True)
        require_int(self.distance_to_liquidation_bps, "distance_to_liquidation_bps")
        if type(self.liquidatable) is not bool:
            raise LendingReaderError("liquidatable must be a boolean")
        if (
            isinstance(self.total_debt_usd, bool)
            or not isinstance(self.total_debt_usd, (int, float))
            or not math.isfinite(float(self.total_debt_usd))
            or float(self.total_debt_usd) < 0.0
        ):
            raise LendingReaderError("total_debt_usd must be a non-negative finite number")
        object.__setattr__(self, "total_debt_usd", float(self.total_debt_usd))

    def to_dict(self) -> dict[str, object]:
        """Serialize account state into a JSON-compatible dictionary."""
        return {
            "borrower": self.borrower,
            "total_collateral_usd_8dec": self.total_collateral_usd_8dec,
            "total_debt_usd_8dec": self.total_debt_usd_8dec,
            "available_borrows_usd_8dec": self.available_borrows_usd_8dec,
            "current_liquidation_threshold_bps": self.current_liquidation_threshold_bps,
            "ltv_bps": self.ltv_bps,
            "health_factor_wad": self.health_factor_wad,
            "distance_to_liquidation_bps": self.distance_to_liquidation_bps,
            "liquidatable": self.liquidatable,
            "total_debt_usd": self.total_debt_usd,
        }


@dataclass(frozen=True, slots=True)
class MorphoMarketState:
    """Pinned Morpho Blue market parameters and supply/borrow totals."""

    market_id: str
    loan_token: str
    collateral_token: str
    oracle: str
    irm: str
    lltv_wad: int
    total_supply_assets: int
    total_borrow_assets: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "market_id", normalize_hash32(self.market_id, "market_id"))
        loan = normalize_address(self.loan_token)
        if loan == ZERO_ADDRESS:
            raise LendingReaderError("Morpho market loan_token cannot be zero address")
        object.__setattr__(self, "loan_token", loan)
        object.__setattr__(self, "collateral_token", normalize_address(self.collateral_token))
        object.__setattr__(self, "oracle", normalize_address(self.oracle))
        object.__setattr__(self, "irm", normalize_address(self.irm))
        lltv = require_int(self.lltv_wad, "lltv_wad", non_negative=True)
        if lltv > _WAD:
            raise LendingReaderError("Morpho market lltv_wad must be <= 10**18")
        sup = require_int(self.total_supply_assets, "total_supply_assets", non_negative=True)
        bor = require_int(self.total_borrow_assets, "total_borrow_assets", non_negative=True)
        if sup >= _UINT128_LIMIT or bor >= _UINT128_LIMIT:
            raise LendingReaderError("Morpho market assets must fit uint128")

    def to_dict(self) -> dict[str, object]:
        """Serialize market state into a JSON-compatible dictionary."""
        return {
            "market_id": self.market_id,
            "loan_token": self.loan_token,
            "collateral_token": self.collateral_token,
            "oracle": self.oracle,
            "irm": self.irm,
            "lltv_wad": self.lltv_wad,
            "total_supply_assets": self.total_supply_assets,
            "total_borrow_assets": self.total_borrow_assets,
        }


@dataclass(frozen=True, slots=True)
class MorphoBorrowerPosition:
    """Pinned Morpho Blue borrower share & collateral position for one market."""

    market_id: str
    borrower: str
    supply_shares: int
    borrow_shares: int
    collateral: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "market_id", normalize_hash32(self.market_id, "market_id"))
        borrower = normalize_address(self.borrower)
        if borrower == ZERO_ADDRESS:
            raise LendingReaderError("Morpho borrower cannot be zero address")
        object.__setattr__(self, "borrower", borrower)
        sup_shares = require_int(self.supply_shares, "supply_shares", non_negative=True)
        bor_shares = require_int(self.borrow_shares, "borrow_shares", non_negative=True)
        col = require_int(self.collateral, "collateral", non_negative=True)
        if sup_shares >= _UINT256_LIMIT:
            raise LendingReaderError("Morpho supply_shares must fit uint256")
        if bor_shares >= _UINT128_LIMIT or col >= _UINT128_LIMIT:
            raise LendingReaderError("Morpho borrow_shares and collateral must fit uint128")

    def to_dict(self) -> dict[str, object]:
        """Serialize borrower position into a JSON-compatible dictionary."""
        return {
            "market_id": self.market_id,
            "borrower": self.borrower,
            "supply_shares": self.supply_shares,
            "borrow_shares": self.borrow_shares,
            "collateral": self.collateral,
        }


@dataclass(frozen=True, slots=True)
class LendingHealthScanReport:
    """Block-pinned Base lending health & flash-liquidation scan report."""

    protocol: str
    block_number: int
    block_hash: str
    timestamp_ms: int
    rpc_origin: str
    reserves_count: int
    reserves: tuple[str, ...]
    lookback_blocks: int
    borrowers_discovered: int
    active_borrowers: tuple[AaveBorrowerAccountState, ...]
    liquidatable_accounts: tuple[AaveBorrowerAccountState, ...]
    at_risk_watchlist: tuple[AaveBorrowerAccountState, ...]
    min_health_factor_wad: int | None
    liquidation_candidates_count: int
    liquidation_candidates: tuple[LiquidationCandidate, ...]
    rpc_calls_used: int
    report_sha256: str = ""
    schema_version: str = LENDING_HEALTH_SCHEMA_VERSION
    chain_id: int = BASE_CHAIN_ID
    execution_eligible: Literal[False] = False

    def __post_init__(self) -> None:
        if self.schema_version != LENDING_HEALTH_SCHEMA_VERSION:
            raise LendingReaderError(f"unsupported schema_version: {self.schema_version!r}")
        if type(self.chain_id) is not int or self.chain_id != BASE_CHAIN_ID:
            raise LendingReaderError(f"chain_id must be Base ({BASE_CHAIN_ID})")
        if self.execution_eligible is not False:
            raise LendingReaderError(
                "LendingHealthScanReport is read-only (execution_eligible=False)"
            )
        if not isinstance(self.protocol, str) or not self.protocol.strip():
            raise LendingReaderError("protocol must be a non-empty string")
        require_int(self.block_number, "block_number", non_negative=True)
        object.__setattr__(self, "block_hash", normalize_hash32(self.block_hash, "block_hash"))
        require_int(self.timestamp_ms, "timestamp_ms", non_negative=True)
        if not isinstance(self.rpc_origin, str) or not self.rpc_origin.startswith("https://"):
            raise LendingReaderError("rpc_origin must be an https:// origin")
        res_count = require_int(self.reserves_count, "reserves_count", non_negative=True)
        if res_count != len(self.reserves):
            raise LendingReaderError("reserves_count must match len(reserves)")
        require_int(self.lookback_blocks, "lookback_blocks", non_negative=True)
        require_int(self.borrowers_discovered, "borrowers_discovered", non_negative=True)
        if self.min_health_factor_wad is not None:
            require_int(self.min_health_factor_wad, "min_health_factor_wad", non_negative=True)
        cand_count = require_int(
            self.liquidation_candidates_count, "liquidation_candidates_count", non_negative=True
        )
        if cand_count != len(self.liquidation_candidates):
            raise LendingReaderError(
                "liquidation_candidates_count must match len(liquidation_candidates)"
            )
        for cand in self.liquidation_candidates:
            if cand.execution_eligible is not False:
                raise LendingReaderError("liquidation candidate must have execution_eligible=False")
        require_int(self.rpc_calls_used, "rpc_calls_used", non_negative=True)

        expected_sha = sha256_hex(canonical_json(self._body_dict()))
        if not self.report_sha256:
            object.__setattr__(self, "report_sha256", expected_sha)
        else:
            if (
                not isinstance(self.report_sha256, str)
                or _SHA256_RE.fullmatch(self.report_sha256) is None
            ):
                raise LendingReaderError("report_sha256 must be a lowercase 64-hex SHA-256 digest")
            if self.report_sha256 != expected_sha:
                raise LendingReaderError("report_sha256 does not match canonical report body")

    def _body_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "chain_id": self.chain_id,
            "execution_eligible": self.execution_eligible,
            "protocol": self.protocol,
            "block_number": self.block_number,
            "block_hash": self.block_hash,
            "timestamp_ms": self.timestamp_ms,
            "rpc_origin": self.rpc_origin,
            "reserves_count": self.reserves_count,
            "reserves": list(self.reserves),
            "lookback_blocks": self.lookback_blocks,
            "borrowers_discovered": self.borrowers_discovered,
            "active_borrowers": [acct.to_dict() for acct in self.active_borrowers],
            "liquidatable_accounts": [acct.to_dict() for acct in self.liquidatable_accounts],
            "at_risk_watchlist": [acct.to_dict() for acct in self.at_risk_watchlist],
            "min_health_factor_wad": self.min_health_factor_wad,
            "liquidation_candidates_count": self.liquidation_candidates_count,
            "liquidation_candidates": [asdict(cand) for cand in self.liquidation_candidates],
            "rpc_calls_used": self.rpc_calls_used,
        }

    def to_dict(self) -> dict[str, Any]:
        """Serialize the lending health scan report to a JSON-compatible dictionary."""
        return {**self._body_dict(), "report_sha256": self.report_sha256}

    def to_json(self, *, indent: int | None = 2) -> str:
        """Serialize the lending health scan report to canonical-sorted JSON text."""
        return json.dumps(self.to_dict(), sort_keys=True, indent=indent)


@dataclass(frozen=True, slots=True)
class MorphoBlueAuditReport:
    """Block-pinned Morpho Blue market and borrower position snapshot."""

    protocol: str
    block_number: int
    block_hash: str
    timestamp_ms: int
    rpc_origin: str
    markets: tuple[MorphoMarketState, ...]
    positions: tuple[MorphoBorrowerPosition, ...]
    rpc_calls_used: int
    report_sha256: str = ""
    schema_version: str = MORPHO_AUDIT_SCHEMA_VERSION
    chain_id: int = BASE_CHAIN_ID
    execution_eligible: Literal[False] = False

    def __post_init__(self) -> None:
        if self.schema_version != MORPHO_AUDIT_SCHEMA_VERSION:
            raise LendingReaderError(f"unsupported schema_version: {self.schema_version!r}")
        if type(self.chain_id) is not int or self.chain_id != BASE_CHAIN_ID:
            raise LendingReaderError(f"chain_id must be Base ({BASE_CHAIN_ID})")
        if self.execution_eligible is not False:
            raise LendingReaderError(
                "MorphoBlueAuditReport is read-only (execution_eligible=False)"
            )
        require_int(self.block_number, "block_number", non_negative=True)
        object.__setattr__(self, "block_hash", normalize_hash32(self.block_hash, "block_hash"))
        require_int(self.timestamp_ms, "timestamp_ms", non_negative=True)
        if not isinstance(self.rpc_origin, str) or not self.rpc_origin.startswith("https://"):
            raise LendingReaderError("rpc_origin must be an https:// origin")
        require_int(self.rpc_calls_used, "rpc_calls_used", non_negative=True)

        expected_sha = sha256_hex(canonical_json(self._body_dict()))
        if not self.report_sha256:
            object.__setattr__(self, "report_sha256", expected_sha)
        else:
            if (
                not isinstance(self.report_sha256, str)
                or _SHA256_RE.fullmatch(self.report_sha256) is None
            ):
                raise LendingReaderError("report_sha256 must be a lowercase 64-hex SHA-256 digest")
            if self.report_sha256 != expected_sha:
                raise LendingReaderError("report_sha256 does not match canonical report body")

    def _body_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "chain_id": self.chain_id,
            "execution_eligible": self.execution_eligible,
            "protocol": self.protocol,
            "block_number": self.block_number,
            "block_hash": self.block_hash,
            "timestamp_ms": self.timestamp_ms,
            "rpc_origin": self.rpc_origin,
            "markets": [market.to_dict() for market in self.markets],
            "positions": [pos.to_dict() for pos in self.positions],
            "rpc_calls_used": self.rpc_calls_used,
        }

    def to_dict(self) -> dict[str, Any]:
        """Serialize the Morpho Blue audit report to a JSON-compatible dictionary."""
        return {**self._body_dict(), "report_sha256": self.report_sha256}

    def to_json(self, *, indent: int | None = 2) -> str:
        """Serialize the Morpho Blue audit report to canonical-sorted JSON text."""
        return json.dumps(self.to_dict(), sort_keys=True, indent=indent)


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


def _validate_public_ip_literal(hostname: str) -> None:
    lowered = hostname.lower()
    if lowered in _LOCALHOST_NAMES or lowered.endswith(".localhost"):
        raise LendingReaderError("Base lending RPC forbids localhost endpoints")
    try:
        ip_obj = ipaddress.ip_address(hostname)
    except ValueError:
        return
    if _is_forbidden_ip(ip_obj):
        raise LendingReaderError("Base lending RPC forbids private or loopback IP endpoints")


def _validate_rpc_url(rpc_url: str, *, enforce_public_host: bool) -> tuple[str, str, int]:
    if not isinstance(rpc_url, str) or not rpc_url or rpc_url != rpc_url.strip():
        raise LendingReaderError("rpc_url must be a nonempty string without outer whitespace")
    parsed = urllib.parse.urlsplit(rpc_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise LendingReaderError("Base lending RPC requires an https:// endpoint with host")
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise LendingReaderError("Base lending RPC forbids URL userinfo credentials")
    hostname = parsed.hostname
    if not hostname:
        raise LendingReaderError("Base lending RPC requires a nonempty hostname")
    try:
        port = parsed.port if parsed.port is not None else 443
    except ValueError as exc:
        raise LendingReaderError("Base lending RPC URL contains an invalid port") from exc
    if enforce_public_host:
        _validate_public_ip_literal(hostname)
    display_host = f"[{hostname}]" if ":" in hostname else hostname
    origin = f"https://{display_host}" + (f":{parsed.port}" if parsed.port is not None else "")
    return origin, hostname, port


def _validate_resolved_dns(
    hostname: str, port: int
) -> Sequence[tuple[socket.AddressFamily, socket.SocketKind, int, str, Any]]:
    _validate_public_ip_literal(hostname)
    try:
        addr_infos = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise RpcError("Base lending RPC DNS resolution failed") from exc
    if not addr_infos:
        raise RpcError("Base lending RPC DNS returned no addresses")
    for _, _, _, _, sockaddr in addr_infos:
        raw_ip = str(sockaddr[0]).split("%", 1)[0]
        ip_obj = ipaddress.ip_address(raw_ip)
        is_mapped_tun = (
            isinstance(ip_obj, ipaddress.IPv6Address)
            and ip_obj.ipv4_mapped is not None
            and _is_tun_fake_ip(ip_obj.ipv4_mapped)
        )
        if _is_forbidden_ip(ip_obj) and not _is_tun_fake_ip(ip_obj) and not is_mapped_tun:
            raise LendingReaderError("Base lending RPC DNS resolved to a private or loopback IP")
    return addr_infos


def _create_ipv4_preferred_connection(
    address: tuple[str, int],
    timeout: object = None,
    source_address: tuple[str, int] | None = None,
) -> socket.socket:
    host, port = address
    addr_infos = _validate_resolved_dns(host, port)
    ordered = [info for info in addr_infos if info[0] == socket.AF_INET] + [
        info for info in addr_infos if info[0] != socket.AF_INET
    ]
    last_exc: OSError | None = None
    for af, socktype, proto, _, sockaddr in ordered:
        sock: socket.socket | None = None
        try:
            sock = socket.socket(af, socktype, proto)
            if not isinstance(timeout, bool) and isinstance(timeout, (int, float)):
                sock.settimeout(float(timeout))
            if source_address is not None:
                sock.bind(source_address)
            sock.connect(sockaddr)
            return sock
        except OSError as exc:
            last_exc = exc
            if sock is not None:
                with suppress(OSError):
                    sock.close()
    if last_exc is not None:
        raise last_exc
    raise OSError("Base lending RPC DNS returned no connectable addresses")


class _IPv4PreferredHTTPSConnection(http.client.HTTPSConnection):
    """HTTPSConnection preferring IPv4 addresses while validating SSRF on every resolution."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._create_connection = _create_ipv4_preferred_connection


class _IPv4PreferredHTTPSHandler(urllib.request.HTTPSHandler):
    """urllib HTTPSHandler opening connections via ``_IPv4PreferredHTTPSConnection``."""

    def __init__(self, *, context: ssl.SSLContext) -> None:
        super().__init__(context=context)
        self._ssl_context = context

    def https_open(self, req: urllib.request.Request) -> http.client.HTTPResponse:
        return self.do_open(
            _IPv4PreferredHTTPSConnection,
            req,
            context=self._ssl_context,
        )


def _default_https_transport(rpc_url: str, payload: bytes, timeout_s: float) -> bytes:
    parsed = urllib.parse.urlsplit(rpc_url)
    if parsed.scheme != "https" or not parsed.netloc or not parsed.hostname:
        raise LendingReaderError("Base lending RPC requires an https:// endpoint")
    port = parsed.port if parsed.port is not None else 443
    _validate_resolved_dns(parsed.hostname, port)

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
                raise RpcError("Base lending RPC response exceeds byte limit")
            return raw
    except urllib.error.HTTPError as exc:
        with suppress(OSError):
            exc.close()
        raise RpcError(f"Base lending RPC HTTP failure: status {exc.code}") from None
    except (urllib.error.URLError, OSError) as exc:
        if isinstance(exc, RpcError):
            raise
        raise RpcError("Base lending RPC transport failure") from None


def _validate_timeout(timeout_s: float) -> float:
    if (
        isinstance(timeout_s, bool)
        or not isinstance(timeout_s, (int, float))
        or not math.isfinite(float(timeout_s))
        or float(timeout_s) <= 0
    ):
        raise LendingReaderError("timeout_s must be a positive finite number")
    return float(timeout_s)


def _validate_block_tag(block_tag: str) -> str:
    if not isinstance(block_tag, str) or not block_tag:
        raise LendingReaderError("block_tag must be a nonempty string")
    if block_tag in _NAMED_BLOCK_TAGS:
        return block_tag
    rpc_quantity(block_tag)
    return block_tag


def _rpc_call_envelope(
    rpc_url: str,
    send: RpcTransport,
    timeout_s: float,
    req_id: int,
    method: str,
    params: list[object],
) -> dict[str, object]:
    req_bytes = json.dumps(
        {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params},
        separators=(",", ":"),
    ).encode("utf-8")
    resp_bytes = send(rpc_url, req_bytes, timeout_s)
    if not isinstance(resp_bytes, bytes) or len(resp_bytes) > MAX_RESPONSE_BYTES:
        raise RpcError("Base lending RPC response must be bounded bytes")
    try:
        envelope = strict_json_object(resp_bytes)
    except ValueError as exc:
        raise LendingReaderError(f"Base lending RPC malformed JSON envelope: {exc}") from exc
    if (
        envelope.get("jsonrpc") != "2.0"
        or type(envelope.get("id")) is not int
        or envelope["id"] != req_id
    ):
        raise LendingReaderError("Base lending RPC JSON-RPC version or ID mismatch")
    if ("result" in envelope) == ("error" in envelope):
        raise LendingReaderError("Base lending RPC response must contain result xor error")
    return envelope


def _rpc_call(
    rpc_url: str,
    send: RpcTransport,
    timeout_s: float,
    req_id: int,
    method: str,
    params: list[object],
) -> object:
    envelope = _rpc_call_envelope(rpc_url, send, timeout_s, req_id, method, params)
    if "error" in envelope:
        err = envelope["error"]
        raise RpcError(f"Base lending RPC error on {method}: {err!r}")
    return envelope["result"]


def _decode_hex_bytes(value: object, name: str) -> bytes:
    raw_hex = strict_string(value, name)
    if _HEX_BYTES_RE.fullmatch(raw_hex) is None:
        raise LendingReaderError(f"{name} must be 0x-prefixed even-length hex bytes")
    return bytes.fromhex(raw_hex[2:])


def _encode_aggregate3_calldata(calls: Sequence[tuple[str, bool, bytes]]) -> str:
    return (
        AGGREGATE3_SELECTOR
        + encode(
            ["(address,bool,bytes)[]"],
            [[(normalize_address(target), allow_fail, data) for target, allow_fail, data in calls]],
        ).hex()
    )


def _decode_aggregate3_results(raw: bytes, expected_count: int) -> tuple[tuple[bool, bytes], ...]:
    try:
        (decoded,) = decode(["(bool,bytes)[]"], raw, strict=True)
    except DecodingError as exc:
        raise LendingReaderError("invalid Multicall3.aggregate3 ABI return data") from exc
    if len(decoded) != expected_count:
        raise LendingReaderError(
            f"Multicall3.aggregate3 result count mismatch: {len(decoded)} != {expected_count}"
        )
    out: list[tuple[bool, bytes]] = []
    for item in decoded:
        success, ret = item
        if type(success) is not bool or not isinstance(ret, bytes):
            raise LendingReaderError("invalid Multicall3.aggregate3 element types")
        out.append((success, ret))
    return tuple(out)


def _decode_uint256(raw: bytes, name: str, *, limit: int | None = None) -> int:
    if len(raw) != 32:
        raise LendingReaderError(f"{name} uint256 word must be 32 bytes, got {len(raw)}")
    value = int.from_bytes(raw, "big")
    if limit is not None and not 0 <= value < limit:
        raise LendingReaderError(f"{name} value {value} out of range [0, {limit})")
    return value


def _pin_block_header(
    rpc_url: str,
    send: RpcTransport,
    timeout_s: float,
    block_tag: str,
    req_id: int,
) -> tuple[int, str, int, int | None]:
    result = _rpc_call(rpc_url, send, timeout_s, req_id, "eth_getBlockByNumber", [block_tag, False])
    header = strict_object(result, "block header")
    block_number = rpc_quantity(header.get("number"))
    block_hash = normalize_hash32(strict_string(header.get("hash"), "block hash"), "block hash")
    raw_parent = header.get("parentHash")
    if raw_parent is not None:
        parent_hash = normalize_hash32(strict_string(raw_parent, "parentHash"), "parentHash")
        if parent_hash == block_hash:
            raise LendingReaderError("block header hash cannot equal parentHash")
    header_timestamp = rpc_quantity(header.get("timestamp"))
    header_timestamp_ms = validate_header_timestamp_ms(header, chain_id=BASE_CHAIN_ID)
    return block_number, block_hash, header_timestamp, header_timestamp_ms


def _validate_base_time_and_chain(
    chain_raw: bytes,
    ts_ms_raw: bytes,
    millis_raw: bytes,
    *,
    header_timestamp: int,
    header_timestamp_ms: int | None,
) -> int:
    chain_id = _decode_uint256(chain_raw, "Multicall3.getChainId")
    if chain_id != BASE_CHAIN_ID:
        raise LendingReaderError(f"chain_id mismatch: expected {BASE_CHAIN_ID}, got {chain_id}")
    ts_ms = _decode_uint256(ts_ms_raw, "BaseTime.timestampMs", limit=1 << 64)
    millis_part = _decode_uint256(millis_raw, "BaseTime.timestampMillisPart", limit=1000)
    checked_ms = parse_base_time(
        timestamp_ms=ts_ms,
        millis_part=millis_part,
        header_timestamp=header_timestamp,
    )
    if header_timestamp_ms is not None and checked_ms != header_timestamp_ms:
        raise LendingReaderError("base_time_header_timestamp_ms_mismatch")
    return checked_ms


def _decode_reserves_list(raw: bytes) -> tuple[str, ...]:
    try:
        (reserves_raw,) = decode(["address[]"], raw, strict=True)
    except DecodingError as exc:
        raise LendingReaderError("invalid Aave V3 getReservesList() ABI return data") from exc
    out: list[str] = []
    seen: set[str] = set()
    for item in reserves_raw:
        addr = normalize_address(str(item))
        if addr == ZERO_ADDRESS:
            raise LendingReaderError("Aave V3 getReservesList() contains zero address")
        if addr not in seen:
            seen.add(addr)
            out.append(addr)
    return tuple(out)


def _decode_aave_user_account_data(borrower: str, raw: bytes) -> AaveBorrowerAccountState:
    if len(raw) != 192:
        raise LendingReaderError(f"getUserAccountData ABI length must be 192 bytes, got {len(raw)}")
    try:
        col_8dec, debt_8dec, avail_8dec, liq_thresh_bps, ltv_bps, hf_wad = decode(
            ["uint256", "uint256", "uint256", "uint256", "uint256", "uint256"],
            raw,
            strict=True,
        )
    except DecodingError as exc:
        raise LendingReaderError("invalid Aave V3 getUserAccountData ABI return data") from exc

    col_int = int(col_8dec)
    debt_int = int(debt_8dec)
    avail_int = int(avail_8dec)
    liq_thresh_int = int(liq_thresh_bps)
    ltv_int = int(ltv_bps)
    hf_int = int(hf_wad)

    if hf_int >= _WAD:
        distance_bps = ((hf_int - _WAD) * _BPS_DENOM) // _WAD
    else:
        distance_bps = -(((_WAD - hf_int) * _BPS_DENOM) // _WAD)

    liquidatable = debt_int > 0 and 0 < hf_int < _WAD
    total_debt_usd = round(debt_int / float(_USD_8DEC_SCALE), 8)
    return AaveBorrowerAccountState(
        borrower=borrower,
        total_collateral_usd_8dec=col_int,
        total_debt_usd_8dec=debt_int,
        available_borrows_usd_8dec=avail_int,
        current_liquidation_threshold_bps=liq_thresh_int,
        ltv_bps=ltv_int,
        health_factor_wad=hf_int,
        distance_to_liquidation_bps=distance_bps,
        liquidatable=liquidatable,
        total_debt_usd=total_debt_usd,
    )


def _decode_morpho_market(
    market_id: str, raw_params: bytes, raw_market: bytes
) -> MorphoMarketState:
    if len(raw_params) != 160:
        raise LendingReaderError(
            f"Morpho idToMarketParams ABI length must be 160 bytes, got {len(raw_params)}"
        )
    if len(raw_market) != 192:
        raise LendingReaderError(
            f"Morpho market ABI length must be 192 bytes, got {len(raw_market)}"
        )
    try:
        loan_tok, col_tok, oracle, irm, lltv = decode(
            ["address", "address", "address", "address", "uint256"],
            raw_params,
            strict=True,
        )
        total_supply_assets, _sup_shares, total_borrow_assets, _bor_shares, _last_upd, _fee = (
            decode(
                ["uint128", "uint128", "uint128", "uint128", "uint128", "uint128"],
                raw_market,
                strict=True,
            )
        )
    except DecodingError as exc:
        raise LendingReaderError("invalid Morpho Blue market ABI return data") from exc

    return MorphoMarketState(
        market_id=market_id,
        loan_token=str(loan_tok),
        collateral_token=str(col_tok),
        oracle=str(oracle),
        irm=str(irm),
        lltv_wad=int(lltv),
        total_supply_assets=int(total_supply_assets),
        total_borrow_assets=int(total_borrow_assets),
    )


def _decode_morpho_position(
    market_id: str, borrower: str, raw_pos: bytes
) -> MorphoBorrowerPosition:
    if len(raw_pos) != 96:
        raise LendingReaderError(f"Morpho position ABI length must be 96 bytes, got {len(raw_pos)}")
    try:
        supply_shares, borrow_shares, collateral = decode(
            ["uint256", "uint128", "uint128"],
            raw_pos,
            strict=True,
        )
    except DecodingError as exc:
        raise LendingReaderError("invalid Morpho Blue position ABI return data") from exc
    return MorphoBorrowerPosition(
        market_id=market_id,
        borrower=borrower,
        supply_shares=int(supply_shares),
        borrow_shares=int(borrow_shares),
        collateral=int(collateral),
    )


def _is_block_range_rpc_error(error_obj: object) -> bool:
    if not isinstance(error_obj, Mapping):
        return False
    code = error_obj.get("code")
    message = str(error_obj.get("message", "")).lower()
    if code in (-32005, -32602):
        return any(
            marker in message
            for marker in ("range", "block", "too many", "limit", "exceed", "10000", "logs")
        )
    return any(
        marker in message
        for marker in ("block range", "eth_getlogs", "exceed maximum", "too many results")
    )


def _extract_borrower_from_borrow_log(log_obj: object, *, chunk_start: int, chunk_end: int) -> str:
    record = strict_object(log_obj, "eth_getLogs item")
    removed = record.get("removed")
    if removed is not None:
        if type(removed) is not bool:
            raise LendingReaderError("eth_getLogs item 'removed' must be a boolean")
        if removed:
            raise LendingReaderError("eth_getLogs returned a removed/reorged log")
    raw_addr = record.get("address")
    if raw_addr is not None:
        log_addr = normalize_address(strict_string(raw_addr, "log.address"))
        if log_addr != AAVE_V3_POOL:
            raise LendingReaderError(f"unexpected eth_getLogs address: {log_addr}")
    raw_bn = record.get("blockNumber")
    if raw_bn is not None:
        bn = rpc_quantity(raw_bn)
        if not chunk_start <= bn <= chunk_end:
            raise LendingReaderError(
                f"eth_getLogs log blockNumber {bn} outside [{chunk_start}, {chunk_end}]"
            )
    topics = strict_list(record.get("topics"), "log.topics")
    if len(topics) < 3:
        raise LendingReaderError("Aave Borrow log requires at least 3 topics")
    topic0 = normalize_hash32(strict_string(topics[0], "topics[0]"), "topics[0]")
    if topic0 != AAVE_BORROW_TOPIC0:
        raise LendingReaderError(f"unexpected log topic0: {topic0}")
    topic1_bytes = bytes.fromhex(
        normalize_hash32(strict_string(topics[1], "topics[1]"), "topics[1]")[2:]
    )
    if topic1_bytes[:12] != b"\x00" * 12:
        raise LendingReaderError("non-canonical address padding in Borrow topics[1]")
    topic2_bytes = bytes.fromhex(
        normalize_hash32(strict_string(topics[2], "topics[2]"), "topics[2]")[2:]
    )
    if topic2_bytes[:12] != b"\x00" * 12:
        raise LendingReaderError("non-canonical address padding in Borrow topics[2]")
    borrower = "0x" + topic2_bytes[12:].hex()
    if borrower == ZERO_ADDRESS:
        raise LendingReaderError("Aave Borrow log onBehalfOf cannot be zero address")
    return borrower


def _discover_aave_borrowers(
    *,
    rpc_url: str,
    send: RpcTransport,
    timeout_s: float,
    block_number: int,
    lookback_blocks: int,
    chunk_blocks: int,
    max_borrowers: int,
    seed_borrowers: Sequence[str],
    start_req_id: int,
) -> tuple[tuple[str, ...], int, int]:
    candidates: list[str] = []
    seen: set[str] = set()
    for raw_seed in seed_borrowers:
        seed = normalize_address(raw_seed)
        if seed == ZERO_ADDRESS:
            raise LendingReaderError("seed_borrowers cannot contain zero address")
        if seed not in seen:
            seen.add(seed)
            candidates.append(seed)
            if len(candidates) >= max_borrowers:
                break

    if lookback_blocks == 0 or len(candidates) >= max_borrowers:
        return tuple(candidates), 0, start_req_id

    from_block = max(0, block_number - lookback_blocks + 1)
    chunk_stack: list[tuple[int, int]] = []
    cur = from_block
    while cur <= block_number:
        end = min(block_number, cur + chunk_blocks - 1)
        chunk_stack.append((cur, end))
        cur = end + 1

    max_log_calls = min(32, max(16, (lookback_blocks // max(1, min(chunk_blocks, 10))) + 8))
    effective_max_span = chunk_blocks
    log_calls = 0
    req_id = start_req_id

    while chunk_stack and len(candidates) < max_borrowers:
        chunk_start, chunk_end = chunk_stack.pop()
        span = chunk_end - chunk_start + 1
        if span > effective_max_span:
            sub_cur = chunk_start
            while sub_cur <= chunk_end:
                sub_end = min(chunk_end, sub_cur + effective_max_span - 1)
                chunk_stack.append((sub_cur, sub_end))
                sub_cur = sub_end + 1
            continue

        if log_calls >= max_log_calls:
            raise RpcError(f"Aave eth_getLogs call budget exhausted ({max_log_calls})")

        log_calls += 1
        req_id += 1
        envelope = _rpc_call_envelope(
            rpc_url,
            send,
            timeout_s,
            req_id,
            "eth_getLogs",
            [
                {
                    "address": AAVE_V3_POOL,
                    "fromBlock": hex(chunk_start),
                    "toBlock": hex(chunk_end),
                    "topics": [AAVE_BORROW_TOPIC0],
                }
            ],
        )
        if "error" in envelope:
            err_obj = envelope["error"]
            if span > 1 and _is_block_range_rpc_error(err_obj):
                mid = (chunk_start + chunk_end) // 2
                effective_max_span = max(1, min(effective_max_span, mid - chunk_start + 1))
                chunk_stack.append((chunk_start, mid))
                chunk_stack.append((mid + 1, chunk_end))
                continue
            raise RpcError(f"Base lending eth_getLogs RPC error: {err_obj!r}")

        logs = strict_list(envelope["result"], "eth_getLogs result")
        for log_obj in reversed(logs):
            borrower = _extract_borrower_from_borrow_log(
                log_obj, chunk_start=chunk_start, chunk_end=chunk_end
            )
            if borrower not in seen:
                seen.add(borrower)
                candidates.append(borrower)
                if len(candidates) >= max_borrowers:
                    break

    return tuple(candidates), log_calls, req_id


def _graph_pools(pool_graph: FastpathPoolGraph) -> tuple[PoolNode, ...]:
    public_pools = getattr(pool_graph, "pools", None)
    if isinstance(public_pools, Mapping):
        return tuple(public_pools.values())
    if isinstance(public_pools, Sequence):
        return tuple(public_pools)
    return tuple(pool_graph._pools.values())


def _pool_spot_rate_wad(pool: PoolNode, token_in: str, token_out: str) -> int | None:
    t0 = normalize_address(pool.token0)
    t1 = normalize_address(pool.token1)
    if {token_in, token_out} != {t0, t1} or token_in == token_out:
        return None
    if pool.kind in (PoolKind.V3, PoolKind.SLIPSTREAM):
        if pool.sqrt_price_x96 is None or pool.sqrt_price_x96 <= 0:
            return None
        sq = pool.sqrt_price_x96 * pool.sqrt_price_x96
        if token_in == t0:
            return (sq * _WAD) >> 192
        return ((1 << 192) * _WAD) // sq
    if pool.kind == PoolKind.V2:
        if pool.reserve0 is None or pool.reserve1 is None:
            return None
        if pool.reserve0 <= 0 or pool.reserve1 <= 0:
            return None
        if token_in == t0:
            return (pool.reserve1 * _WAD) // pool.reserve0
        return (pool.reserve0 * _WAD) // pool.reserve1
    return None


def _derive_liquidatable_positions(
    liquidatable_accounts: Sequence[AaveBorrowerAccountState],
    *,
    reserves: Sequence[str],
    pool_graph: FastpathPoolGraph,
    block_number: int,
    block_hash: str,
) -> list[BorrowerPositionSnapshot]:
    pools = _graph_pools(pool_graph)
    if not pools or not liquidatable_accounts:
        return []

    usdc_addr = normalize_address(USDC)
    usd_8dec_per_raw_wad: dict[str, int] = {}
    has_usdc = any(
        normalize_address(p.token0) == usdc_addr or normalize_address(p.token1) == usdc_addr
        for p in pools
    )
    if has_usdc:
        usd_8dec_per_raw_wad[usdc_addr] = (_USD_8DEC_SCALE * _WAD) // (10**6)
    else:
        six_dec_tokens = sorted(
            {
                tok
                for p in pools
                for tok, dec in (
                    (normalize_address(p.token0), p.decimals0),
                    (normalize_address(p.token1), p.decimals1),
                )
                if dec == 6
            }
        )
        if six_dec_tokens:
            usd_8dec_per_raw_wad[six_dec_tokens[0]] = (_USD_8DEC_SCALE * _WAD) // (10**6)

    if not usd_8dec_per_raw_wad:
        return []

    # Propagate 1 hop from USD anchor token across pools so non-USDC pairs can price debt.
    for pool in pools:
        t0 = normalize_address(pool.token0)
        t1 = normalize_address(pool.token1)
        if t0 in usd_8dec_per_raw_wad and t1 not in usd_8dec_per_raw_wad:
            rate_0_per_1 = _pool_spot_rate_wad(pool, t1, t0)
            if rate_0_per_1 is not None and rate_0_per_1 > 0:
                usd_8dec_per_raw_wad[t1] = (rate_0_per_1 * usd_8dec_per_raw_wad[t0]) // _WAD
        elif t1 in usd_8dec_per_raw_wad and t0 not in usd_8dec_per_raw_wad:
            rate_1_per_0 = _pool_spot_rate_wad(pool, t0, t1)
            if rate_1_per_0 is not None and rate_1_per_0 > 0:
                usd_8dec_per_raw_wad[t0] = (rate_1_per_0 * usd_8dec_per_raw_wad[t1]) // _WAD

    reserves_set = {normalize_address(r) for r in reserves}
    anchor_tokens = set(usd_8dec_per_raw_wad)

    pair_oracle: dict[tuple[str, str], tuple[int, int]] = {}
    ordered_pools = sorted(
        pools,
        key=lambda p: (
            0
            if (
                normalize_address(p.token0) in reserves_set
                and normalize_address(p.token1) in reserves_set
            )
            else 1,
            p.pool_id,
        ),
    )
    for pool in ordered_pools:
        t0 = normalize_address(pool.token0)
        t1 = normalize_address(pool.token1)
        if t1 in anchor_tokens and (t1 == usdc_addr or (pool.decimals1 == 6 and t0 != usdc_addr)):
            col_tok, debt_tok = t0, t1
        elif t0 in anchor_tokens and (t0 == usdc_addr or (pool.decimals0 == 6 and t1 != usdc_addr)):
            col_tok, debt_tok = t1, t0
        elif t1 in usd_8dec_per_raw_wad:
            col_tok, debt_tok = t0, t1
        elif t0 in usd_8dec_per_raw_wad:
            col_tok, debt_tok = t1, t0
        else:
            continue

        if (col_tok, debt_tok) in pair_oracle:
            continue
        oracle_wad = _pool_spot_rate_wad(pool, col_tok, debt_tok)
        debt_usd_wad = usd_8dec_per_raw_wad.get(debt_tok)
        if oracle_wad is None or oracle_wad <= 0 or debt_usd_wad is None or debt_usd_wad <= 0:
            continue
        pair_oracle[(col_tok, debt_tok)] = (oracle_wad, debt_usd_wad)

    snapshots: list[BorrowerPositionSnapshot] = []
    for acct in liquidatable_accounts:
        if acct.total_debt_usd_8dec <= 0 or acct.total_collateral_usd_8dec <= 0:
            continue
        close_factor_bps = (
            _BPS_DENOM
            if acct.health_factor_wad < _AAVE_CLOSE_FACTOR_HF_THRESHOLD
            else _BPS_DENOM // 2
        )
        for (col_tok, debt_tok), (oracle_wad, debt_usd_wad) in pair_oracle.items():
            debt_amount_raw = (acct.total_debt_usd_8dec * _WAD) // debt_usd_wad
            collateral_value_in_debt_raw = (acct.total_collateral_usd_8dec * _WAD) // debt_usd_wad
            collateral_amount_raw = (collateral_value_in_debt_raw * _WAD) // oracle_wad
            if debt_amount_raw <= 0 or collateral_amount_raw <= 0:
                continue
            snapshots.append(
                BorrowerPositionSnapshot(
                    protocol="aave_v3",
                    market_id=AAVE_V3_POOL,
                    borrower=acct.borrower,
                    debt_token=debt_tok,
                    collateral_token=col_tok,
                    debt_amount_raw=debt_amount_raw,
                    collateral_amount_raw=collateral_amount_raw,
                    health_factor_wad=acct.health_factor_wad,
                    close_factor_bps=close_factor_bps,
                    liquidation_bonus_bps=_AAVE_DEFAULT_LIQUIDATION_BONUS_BPS,
                    oracle_price_collateral_in_debt_wad=oracle_wad,
                    block_number=block_number,
                    block_hash=block_hash,
                )
            )
    return snapshots


def scan_aave_v3_base_health(
    *,
    rpc_url: str = DEFAULT_BASE_RPC_URL,
    lookback_blocks: int = 120,
    chunk_blocks: int = 50,
    max_borrowers: int = 60,
    seed_borrowers: Sequence[str] = (),
    at_risk_hf_wad: int = DEFAULT_AT_RISK_HF_WAD,
    pool_graph: FastpathPoolGraph | None = None,
    transport: RpcTransport | None = None,
    timeout_s: float = 15.0,
    block_tag: str = "latest",
    liquidation_config: FlashLiquidationConfig | None = None,
    position_snapshots: Sequence[BorrowerPositionSnapshot] | None = None,
) -> LendingHealthScanReport:
    """Scan pinned Base Aave V3 borrower health and optional flash-liquidation candidates."""
    checked_timeout = _validate_timeout(timeout_s)
    checked_lookback = require_int(lookback_blocks, "lookback_blocks", non_negative=True)
    checked_chunk = require_int(chunk_blocks, "chunk_blocks", non_negative=True)
    if checked_chunk <= 0:
        raise LendingReaderError("chunk_blocks must be >= 1")
    checked_max_borrowers = require_int(max_borrowers, "max_borrowers", non_negative=True)
    if checked_max_borrowers <= 0:
        raise LendingReaderError("max_borrowers must be >= 1")
    checked_at_risk_hf = require_int(at_risk_hf_wad, "at_risk_hf_wad", non_negative=True)
    if checked_at_risk_hf < _WAD:
        raise LendingReaderError("at_risk_hf_wad must be >= 10**18")

    normalized_tag = _validate_block_tag(block_tag)
    origin, _, _ = _validate_rpc_url(rpc_url, enforce_public_host=transport is None)
    send = _default_https_transport if transport is None else transport

    block_number, block_hash, header_timestamp, header_timestamp_ms = _pin_block_header(
        rpc_url, send, checked_timeout, normalized_tag, req_id=1
    )
    rpc_calls_used = 1

    candidate_borrowers, log_calls, last_req_id = _discover_aave_borrowers(
        rpc_url=rpc_url,
        send=send,
        timeout_s=checked_timeout,
        block_number=block_number,
        lookback_blocks=checked_lookback,
        chunk_blocks=checked_chunk,
        max_borrowers=checked_max_borrowers,
        seed_borrowers=seed_borrowers,
        start_req_id=1,
    )
    rpc_calls_used += log_calls

    calls: list[tuple[str, bool, bytes]] = [
        (MULTICALL3, False, bytes.fromhex(MULTICALL3_GET_CHAIN_ID_SELECTOR[2:])),
        (BASE_TIME, False, bytes.fromhex(TIMESTAMP_MS_SELECTOR[2:])),
        (BASE_TIME, False, bytes.fromhex(TIMESTAMP_MILLIS_PART_SELECTOR[2:])),
        (AAVE_V3_POOL, False, bytes.fromhex(AAVE_GET_RESERVES_LIST_SELECTOR[2:])),
    ]
    for borrower in candidate_borrowers:
        call_data = bytes.fromhex(
            AAVE_GET_USER_ACCOUNT_DATA_SELECTOR[2:] + encode(["address"], [borrower]).hex()
        )
        calls.append((AAVE_V3_POOL, False, call_data))

    multicall_data = _encode_aggregate3_calldata(calls)
    rpc_calls_used += 1
    raw_mc_resp = _rpc_call(
        rpc_url,
        send,
        checked_timeout,
        last_req_id + 1,
        "eth_call",
        [{"to": MULTICALL3, "data": multicall_data}, hex(block_number)],
    )
    results = _decode_aggregate3_results(
        _decode_hex_bytes(raw_mc_resp, "aave.multicall3"),
        len(calls),
    )
    for idx, (success, _) in enumerate(results):
        if not success:
            raise LendingReaderError(f"Aave Multicall3 subcall {idx} failed")

    checked_ms = _validate_base_time_and_chain(
        results[0][1],
        results[1][1],
        results[2][1],
        header_timestamp=header_timestamp,
        header_timestamp_ms=header_timestamp_ms,
    )
    reserves = _decode_reserves_list(results[3][1])

    active_list: list[AaveBorrowerAccountState] = []
    for borrower, (_, ret_bytes) in zip(candidate_borrowers, results[4:], strict=True):
        state = _decode_aave_user_account_data(borrower, ret_bytes)
        if state.total_debt_usd_8dec > 0:
            active_list.append(state)

    active_list.sort(key=lambda a: (a.health_factor_wad, -a.total_debt_usd_8dec, a.borrower))
    active_borrowers = tuple(active_list)
    liquidatable_accounts = tuple(a for a in active_borrowers if 0 < a.health_factor_wad < _WAD)
    at_risk_watchlist = tuple(
        a for a in active_borrowers if _WAD <= a.health_factor_wad <= checked_at_risk_hf
    )
    min_hf = active_borrowers[0].health_factor_wad if active_borrowers else None

    liquidation_candidates: tuple[LiquidationCandidate, ...] = ()
    if liquidatable_accounts and pool_graph is not None:
        if position_snapshots is not None:
            liq_addrs = {a.borrower for a in liquidatable_accounts}
            scan_positions = [
                pos
                for pos in position_snapshots
                if pos.borrower in liq_addrs
                and pos.block_number == block_number
                and pos.block_hash == block_hash
            ]
        else:
            scan_positions = _derive_liquidatable_positions(
                liquidatable_accounts,
                reserves=reserves,
                pool_graph=pool_graph,
                block_number=block_number,
                block_hash=block_hash,
            )
        if scan_positions:
            liquidation_candidates = tuple(
                FlashLiquidationScanner.scan(scan_positions, pool_graph, config=liquidation_config)
            )

    return LendingHealthScanReport(
        protocol="aave_v3",
        block_number=block_number,
        block_hash=block_hash,
        timestamp_ms=checked_ms,
        rpc_origin=origin,
        reserves_count=len(reserves),
        reserves=reserves,
        lookback_blocks=checked_lookback,
        borrowers_discovered=len(candidate_borrowers),
        active_borrowers=active_borrowers,
        liquidatable_accounts=liquidatable_accounts,
        at_risk_watchlist=at_risk_watchlist,
        min_health_factor_wad=min_hf,
        liquidation_candidates_count=len(liquidation_candidates),
        liquidation_candidates=liquidation_candidates,
        rpc_calls_used=rpc_calls_used,
    )


def read_morpho_blue_positions(
    rpc_url: str = DEFAULT_BASE_RPC_URL,
    *,
    market_ids: Sequence[str],
    borrowers: Sequence[str],
    block_tag: str = "latest",
    transport: RpcTransport | None = None,
    timeout_s: float = 15.0,
) -> MorphoBlueAuditReport:
    """Read pinned Morpho Blue market state and borrower positions via 1 Multicall3 call."""
    checked_timeout = _validate_timeout(timeout_s)
    normalized_tag = _validate_block_tag(block_tag)
    origin, _, _ = _validate_rpc_url(rpc_url, enforce_public_host=transport is None)
    send = _default_https_transport if transport is None else transport

    unique_markets: list[str] = []
    seen_markets: set[str] = set()
    for raw_market_id in market_ids:
        norm_id = normalize_hash32(raw_market_id, "market_id")
        if norm_id not in seen_markets:
            seen_markets.add(norm_id)
            unique_markets.append(norm_id)
    if not unique_markets:
        raise LendingReaderError("market_ids must be non-empty")

    unique_borrowers: list[str] = []
    seen_borrowers: set[str] = set()
    for raw_borrower in borrowers:
        norm_borrower = normalize_address(raw_borrower)
        if norm_borrower == ZERO_ADDRESS:
            raise LendingReaderError("Morpho borrowers cannot contain zero address")
        if norm_borrower not in seen_borrowers:
            seen_borrowers.add(norm_borrower)
            unique_borrowers.append(norm_borrower)

    block_number, block_hash, header_timestamp, header_timestamp_ms = _pin_block_header(
        rpc_url, send, checked_timeout, normalized_tag, req_id=1
    )

    calls: list[tuple[str, bool, bytes]] = [
        (MULTICALL3, False, bytes.fromhex(MULTICALL3_GET_CHAIN_ID_SELECTOR[2:])),
        (BASE_TIME, False, bytes.fromhex(TIMESTAMP_MS_SELECTOR[2:])),
        (BASE_TIME, False, bytes.fromhex(TIMESTAMP_MILLIS_PART_SELECTOR[2:])),
    ]
    for market_id in unique_markets:
        market_bytes = bytes.fromhex(market_id[2:])
        params_cd = bytes.fromhex(
            MORPHO_ID_TO_MARKET_PARAMS_SELECTOR[2:] + encode(["bytes32"], [market_bytes]).hex()
        )
        market_cd = bytes.fromhex(
            MORPHO_MARKET_SELECTOR[2:] + encode(["bytes32"], [market_bytes]).hex()
        )
        calls.append((MORPHO_BLUE, False, params_cd))
        calls.append((MORPHO_BLUE, False, market_cd))

    pair_keys: list[tuple[str, str]] = []
    for market_id in unique_markets:
        market_bytes = bytes.fromhex(market_id[2:])
        for borrower in unique_borrowers:
            pos_cd = bytes.fromhex(
                MORPHO_POSITION_SELECTOR[2:]
                + encode(["bytes32", "address"], [market_bytes, borrower]).hex()
            )
            calls.append((MORPHO_BLUE, False, pos_cd))
            pair_keys.append((market_id, borrower))

    multicall_data = _encode_aggregate3_calldata(calls)
    raw_mc_resp = _rpc_call(
        rpc_url,
        send,
        checked_timeout,
        2,
        "eth_call",
        [{"to": MULTICALL3, "data": multicall_data}, hex(block_number)],
    )
    results = _decode_aggregate3_results(
        _decode_hex_bytes(raw_mc_resp, "morpho.multicall3"),
        len(calls),
    )
    for idx, (success, _) in enumerate(results):
        if not success:
            raise LendingReaderError(f"Morpho Multicall3 subcall {idx} failed")

    checked_ms = _validate_base_time_and_chain(
        results[0][1],
        results[1][1],
        results[2][1],
        header_timestamp=header_timestamp,
        header_timestamp_ms=header_timestamp_ms,
    )

    market_end = 3 + len(unique_markets) * 2
    markets: list[MorphoMarketState] = []
    for idx, market_id in enumerate(unique_markets):
        base = 3 + idx * 2
        markets.append(_decode_morpho_market(market_id, results[base][1], results[base + 1][1]))

    positions: list[MorphoBorrowerPosition] = [
        _decode_morpho_position(market_id, borrower, ret_bytes)
        for (market_id, borrower), (_, ret_bytes) in zip(
            pair_keys, results[market_end:], strict=True
        )
    ]

    return MorphoBlueAuditReport(
        protocol="morpho_blue",
        block_number=block_number,
        block_hash=block_hash,
        timestamp_ms=checked_ms,
        rpc_origin=origin,
        markets=tuple(markets),
        positions=tuple(positions),
        rpc_calls_used=2,
    )


def _validate_non_symlink_output(out_path: Path) -> None:
    if out_path.is_symlink() or out_path.parent.is_symlink():
        raise LendingReaderError(f"refusing symlink output path: {out_path}")
    if out_path.exists() and out_path.is_dir():
        raise LendingReaderError(f"output path is a directory: {out_path}")


def _write_json_atomic(out_path: Path, content: str) -> None:
    _validate_non_symlink_output(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    _validate_non_symlink_output(out_path)
    payload = content.encode("utf-8")
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=out_path.parent,
            prefix=".lending_reader_",
            suffix=".tmp",
            delete=False,
        ) as handle:
            tmp_path = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        _validate_non_symlink_output(out_path)
        os.replace(tmp_path, out_path)
        tmp_path = None
    finally:
        if tmp_path is not None:
            with suppress(OSError):
                tmp_path.unlink()


def build_parser() -> argparse.ArgumentParser:
    """Build CLI parser for keyless Base Aave V3 lending health reader."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--rpc-url",
        default=DEFAULT_BASE_RPC_URL,
        help=f"Base HTTPS JSON-RPC URL (default: {DEFAULT_BASE_RPC_URL}).",
    )
    parser.add_argument(
        "--lookback-blocks",
        type=int,
        default=120,
        help="Recent blocks to scan for Aave V3 Borrow events (default: 120).",
    )
    parser.add_argument(
        "--chunk-blocks",
        type=int,
        default=50,
        help="Maximum block span per eth_getLogs chunk (default: 50).",
    )
    parser.add_argument(
        "--max-borrowers",
        type=int,
        default=60,
        help="Maximum unique candidate borrowers to query (default: 60).",
    )
    parser.add_argument(
        "--seed-borrower",
        action="append",
        default=[],
        help="Optional seed borrower address to include in account health scan.",
    )
    parser.add_argument(
        "--at-risk-hf-wad",
        type=int,
        default=DEFAULT_AT_RISK_HF_WAD,
        help="Health factor upper threshold (WAD) for watchlist inclusion.",
    )
    parser.add_argument(
        "--block-tag",
        default="latest",
        help="Block tag or hex block number to pin (default: latest).",
    )
    parser.add_argument(
        "--timeout-s",
        type=float,
        default=15.0,
        help="Per-RPC request timeout in seconds (default: 15.0).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Optional output file path for LendingHealthScanReport JSON (symlinks refused).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Execute read-only Base Aave V3 health scan and print or write JSON report."""
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        if args.out is not None:
            _validate_non_symlink_output(args.out)
        report = scan_aave_v3_base_health(
            rpc_url=args.rpc_url,
            lookback_blocks=args.lookback_blocks,
            chunk_blocks=args.chunk_blocks,
            max_borrowers=args.max_borrowers,
            seed_borrowers=tuple(args.seed_borrower),
            at_risk_hf_wad=args.at_risk_hf_wad,
            block_tag=args.block_tag,
            timeout_s=args.timeout_s,
        )
        rendered = report.to_json(indent=2) + "\n"
        if args.out is not None:
            _write_json_atomic(args.out, rendered)
            print(
                json.dumps(
                    {
                        "schema_version": report.schema_version,
                        "chain_id": report.chain_id,
                        "protocol": report.protocol,
                        "block_number": report.block_number,
                        "block_hash": report.block_hash,
                        "borrowers_discovered": report.borrowers_discovered,
                        "active_borrowers": len(report.active_borrowers),
                        "liquidatable_accounts": len(report.liquidatable_accounts),
                        "at_risk_watchlist": len(report.at_risk_watchlist),
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
        print(f"lending-reader failed: {exc}", file=sys.stderr)
        return 2
    return 0


__all__ = [
    "AAVE_BORROW_TOPIC0",
    "AAVE_GET_RESERVES_LIST_SELECTOR",
    "AAVE_GET_USER_ACCOUNT_DATA_SELECTOR",
    "AAVE_LIQUIDATION_CALL_TOPIC0",
    "AAVE_REPAY_TOPIC0",
    "AAVE_SUPPLY_TOPIC0",
    "AAVE_V3_POOL",
    "AGGREGATE3_SELECTOR",
    "DEFAULT_AT_RISK_HF_WAD",
    "LENDING_HEALTH_SCHEMA_VERSION",
    "MORPHO_AUDIT_SCHEMA_VERSION",
    "MORPHO_BLUE",
    "MORPHO_ID_TO_MARKET_PARAMS_SELECTOR",
    "MORPHO_MARKET_SELECTOR",
    "MORPHO_POSITION_SELECTOR",
    "MULTICALL3",
    "MULTICALL3_GET_CHAIN_ID_SELECTOR",
    "AaveBorrowerAccountState",
    "LendingHealthScanReport",
    "LendingReaderError",
    "MorphoBlueAuditReport",
    "MorphoBorrowerPosition",
    "MorphoMarketState",
    "build_parser",
    "main",
    "read_morpho_blue_positions",
    "scan_aave_v3_base_health",
]


if __name__ == "__main__":
    raise SystemExit(main())
