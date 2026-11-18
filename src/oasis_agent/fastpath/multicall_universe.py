"""Keyless, block-pinned Base multi-asset Multicall3 universe discovery & QuoterV2 verifier.

Discovers active pools across Uniswap V3, Aerodrome Slipstream, and Aerodrome Volatile V2
over the five canonical Base assets (WETH, USDC, cbBTC, AERO, wstETH) and optional ecosystem
assets (VIRTUAL, VVV, MORPHO, cbETH, DEGEN, WELL) paired against WETH/USDC, filters out dust/dead
pools, fetches coherent 2-RPC-call snapshots with BaseTime validation and an anti-hallucination
concentrated-liquidity bracket cap, and verifies candidate routes against on-chain QuoterV2.

All network reads are strictly keyless, read-only, bounded in response size, and carry
`execution_eligible = False`.
"""

import http.client
import ipaddress
import json
import math
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Final, cast

import certifi
from eth_abi.abi import decode, encode
from eth_abi.exceptions import DecodingError
from eth_hash.auto import keccak

from ..arbitrage.event_quote import ORACLE, V3
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
from ..models.common import ZERO_ADDRESS, normalize_address, normalize_hash32
from ..sniping.venues import (
    AERO_FACTORY,
    AERO_USDC_WETH,
    SLIPSTREAM,
    SLIPSTREAM_USDC_WETH,
    USDC,
    WETH,
)
from ..uniswap_v4.liquidity_math import (
    MAX_SQRT_PRICE,
    MAX_TICK,
    MIN_SQRT_PRICE,
    MIN_TICK,
    amount0_delta,
    amount1_delta,
    sqrt_price_at_tick,
)
from .bridge import FastpathBridge, FastpathCallError, require_int
from .live_shadow_runner import (
    ACTIVE_TICK_HALF_WIDTH,
    DEFAULT_BASE_RPC_URL,
    FEE_SELECTOR,
    GET_FEE_SELECTOR,
    GET_L1_FEE_CALLDATA,
    GET_RESERVES_SELECTOR,
    LIQUIDITY_SELECTOR,
    MAX_RESPONSE_BYTES,
    SLOT0_SELECTOR,
    decode_aero_factory_fee_pips,
    decode_aero_get_reserves,
    decode_l1_fee_wei,
    decode_liquidity_word,
    decode_slipstream_fee_pips,
    decode_slot0_words,
    resolve_fastpath_bridge,
)
from .pool_graph import CycleHop, FastpathPoolGraph, PoolKind, PoolNode, TickLiquidity

RpcTransport = Callable[[str, bytes, float], bytes]


def _addr20(hi80: int, lo80: int) -> str:
    return f"0x{hi80:020x}{lo80:020x}"


def _selector(signature: str) -> str:
    return "0x" + keccak(signature.encode("ascii"))[:4].hex()


MULTICALL3: Final[str] = _addr20(954245306222959992705383, 11960605330504318110225)
UNIV3_FACTORY: Final[str] = _addr20(241182716431300492361166, 495498167475622741736957)
UNIV3_QUOTER_V2: Final[str] = _addr20(289508167577201351124465, 871012247504659137541994)
SLIPSTREAM_FACTORY: Final[str] = _addr20(446184154468680566683266, 806398540398612560904346)
CBBTC: Final[str] = _addr20(962029985257869835451167, 429691464623657203872703)
AERO_TOKEN: Final[str] = _addr20(698938029289277408845381, 196638687667187760072241)
WSTETH: Final[str] = _addr20(915173236817837272783139, 736850604702865302676562)
VIRTUAL_TOKEN: Final[str] = _addr20(53093369572835308006302, 299957124776707463478811)
MORPHO_TOKEN: Final[str] = _addr20(881418587902261807008815, 468385840514299800979522)
WELL_TOKEN: Final[str] = _addr20(795821710285406097221955, 586745419114598753277614)
DEGEN_TOKEN: Final[str] = _addr20(372272040501209052850519, 54728650980430820929517)
VVV_TOKEN: Final[str] = _addr20(816939432874901296838581, 41323956922079427436991)
CBETH_TOKEN: Final[str] = _addr20(202544235633287275663338, 831585851150422141430818)

UNIV3_FEE_TIERS: Final[tuple[int, ...]] = (100, 500, 3000, 10000)
SLIPSTREAM_TICK_SPACINGS: Final[tuple[int, ...]] = (1, 50, 100, 200)

AGGREGATE3_SELECTOR: Final[str] = _selector("aggregate3((address,bool,bytes)[])")
UNIV3_GET_POOL_SELECTOR: Final[str] = _selector("getPool(address,address,uint24)")
SLIPSTREAM_GET_POOL_SELECTOR: Final[str] = _selector("getPool(address,address,int24)")
AERO_GET_POOL_SELECTOR: Final[str] = _selector("getPool(address,address,bool)")
BALANCE_OF_SELECTOR: Final[str] = _selector("balanceOf(address)")
UNIV3_QUOTE_SINGLE_SELECTOR: Final[str] = _selector(
    "quoteExactInputSingle((address,address,uint256,uint24,uint160))"
)
SLIPSTREAM_QUOTE_SINGLE_SELECTOR: Final[str] = _selector(
    "quoteExactInputSingle((address,address,uint256,int24,uint160))"
)

_ANTI_HALLUCINATION_RESERVE_NUM: Final[int] = 4  # 80% = 4 / 5 of actual balanceOf(pool)
_ANTI_HALLUCINATION_RESERVE_DEN: Final[int] = 5
_INT128_LIMIT: Final[int] = 1 << 127
_RESERVE_LIMIT: Final[int] = 1 << 160
_FEE_PIPS_DENOM: Final[int] = 1_000_000
_LOCALHOST_NAMES: Final[frozenset[str]] = frozenset({"localhost", "localhost.localdomain"})
_NAMED_BLOCK_TAGS: Final[frozenset[str]] = frozenset({"latest", "safe", "finalized"})
_TUN_FAKE_IPV4_NET: Final[ipaddress.IPv4Network] = ipaddress.IPv4Network("198.18.0.0/15")
_TUN_FAKE_IPV6_NET: Final[ipaddress.IPv6Network] = ipaddress.IPv6Network("2001:2::/48")
_GLOBAL_DEFAULT_TIMEOUT: Final[object] = getattr(socket, "_GLOBAL_DEFAULT_TIMEOUT", object())
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


class MulticallUniverseError(ValueError):
    """Fail-closed validation, state integrity, or ABI error in multicall universe reads."""


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        with suppress(OSError):
            fp.close()
        raise RpcError("Base multicall RPC redirects are forbidden")


@dataclass(frozen=True, slots=True)
class BaseTokenMeta:
    """Canonical Base token metadata and minimum pool reserve threshold."""

    symbol: str
    address: str
    decimals: int
    min_pool_reserve: int

    def __post_init__(self) -> None:
        if not self.symbol or not isinstance(self.symbol, str):
            raise MulticallUniverseError("token symbol must be non-empty")
        object.__setattr__(self, "address", normalize_address(self.address))
        require_int(self.decimals, "decimals", non_negative=True)
        if self.decimals > 36:
            raise MulticallUniverseError("token decimals must be <= 36")
        require_int(self.min_pool_reserve, "min_pool_reserve", non_negative=True)
        if self.min_pool_reserve <= 0:
            raise MulticallUniverseError("min_pool_reserve must be positive")


CANONICAL_BASE_TOKENS: Final[tuple[BaseTokenMeta, ...]] = (
    BaseTokenMeta("WETH", WETH, 18, 3 * 10**18),
    BaseTokenMeta("USDC", USDC, 6, 5_000 * 10**6),
    BaseTokenMeta("cbBTC", CBBTC, 8, 10_000_000),
    BaseTokenMeta("AERO", AERO_TOKEN, 18, 10_000 * 10**18),
    BaseTokenMeta("wstETH", WSTETH, 18, 2 * 10**18),
)

ECOSYSTEM_BASE_TOKENS: Final[tuple[BaseTokenMeta, ...]] = (
    BaseTokenMeta("VIRTUAL", VIRTUAL_TOKEN, 18, 25_000 * 10**18),
    BaseTokenMeta("VVV", VVV_TOKEN, 18, 700 * 10**18),
    BaseTokenMeta("MORPHO", MORPHO_TOKEN, 18, 8_000 * 10**18),
    BaseTokenMeta("cbETH", CBETH_TOKEN, 18, 5 * 10**18),
    BaseTokenMeta("DEGEN", DEGEN_TOKEN, 18, 15_000_000 * 10**18),
    BaseTokenMeta("WELL", WELL_TOKEN, 18, 8_000_000 * 10**18),
)

EXTENDED_BASE_TOKENS: Final[tuple[BaseTokenMeta, ...]] = (
    CANONICAL_BASE_TOKENS + ECOSYSTEM_BASE_TOKENS
)


@dataclass(frozen=True, slots=True)
class DiscoveredPool:
    """Normalized, reserve-filtered Base DEX pool discovered on-chain."""

    pool_id: str
    label: str
    venue: str
    kind: PoolKind
    token0: str
    token1: str
    decimals0: int
    decimals1: int
    fixed_fee_pips: int | None
    tick_spacing: int | None
    factory: str
    reserve0: int
    reserve1: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "pool_id", normalize_address(self.pool_id))
        object.__setattr__(self, "token0", normalize_address(self.token0))
        object.__setattr__(self, "token1", normalize_address(self.token1))
        object.__setattr__(self, "factory", normalize_address(self.factory))
        if self.pool_id == ZERO_ADDRESS or self.factory == ZERO_ADDRESS:
            raise MulticallUniverseError("pool_id and factory must be non-zero")
        if self.token0 >= self.token1:
            raise MulticallUniverseError("DiscoveredPool requires canonical token0 < token1")
        if not self.label or not self.venue:
            raise MulticallUniverseError("pool label and venue must be non-empty")
        require_int(self.decimals0, "decimals0", non_negative=True)
        require_int(self.decimals1, "decimals1", non_negative=True)
        if self.decimals0 > 36 or self.decimals1 > 36:
            raise MulticallUniverseError("token decimals must be <= 36")
        if self.fixed_fee_pips is not None:
            require_int(self.fixed_fee_pips, "fixed_fee_pips", non_negative=True)
            if not 0 < self.fixed_fee_pips < _FEE_PIPS_DENOM:
                raise MulticallUniverseError("fixed_fee_pips out of (0, 1_000_000) range")
        if self.tick_spacing is not None:
            require_int(self.tick_spacing, "tick_spacing", non_negative=True)
            if self.tick_spacing <= 0:
                raise MulticallUniverseError("tick_spacing must be positive")
        require_int(self.reserve0, "reserve0", non_negative=True)
        require_int(self.reserve1, "reserve1", non_negative=True)
        if self.reserve0 <= 0 or self.reserve1 <= 0:
            raise MulticallUniverseError("DiscoveredPool reserves must be positive")


@dataclass(frozen=True, slots=True)
class PinnedUniverseSnapshot:
    """Coherent block-pinned multi-pool universe snapshot with BaseTime & balance proof."""

    chain_id: int
    block_number: int
    block_hash: str
    parent_hash: str
    header_timestamp_ms: int
    base_time_ms: int
    base_fee_per_gas_wei: int
    l1_fee_wei: int
    pools: tuple[DiscoveredPool, ...]
    pool_nodes: tuple[PoolNode, ...]
    balances_by_pool: dict[str, tuple[int, int]]
    rpc_calls_used: int
    execution_eligible: bool = False

    def __post_init__(self) -> None:
        if self.chain_id != BASE_CHAIN_ID:
            raise MulticallUniverseError(f"chain_id must be {BASE_CHAIN_ID}")
        require_int(self.block_number, "block_number", non_negative=True)
        object.__setattr__(self, "block_hash", normalize_hash32(self.block_hash, "block_hash"))
        object.__setattr__(self, "parent_hash", normalize_hash32(self.parent_hash, "parent_hash"))
        if self.block_hash == self.parent_hash:
            raise MulticallUniverseError("block_hash cannot equal parent_hash")
        require_int(self.header_timestamp_ms, "header_timestamp_ms", non_negative=True)
        require_int(self.base_time_ms, "base_time_ms", non_negative=True)
        if self.header_timestamp_ms != self.base_time_ms:
            raise MulticallUniverseError("header_timestamp_ms must match base_time_ms")
        require_int(self.base_fee_per_gas_wei, "base_fee_per_gas_wei", non_negative=True)
        require_int(self.l1_fee_wei, "l1_fee_wei", non_negative=True)
        require_int(self.rpc_calls_used, "rpc_calls_used", non_negative=True)
        if self.execution_eligible is not False:
            raise MulticallUniverseError("PinnedUniverseSnapshot execution_eligible must be False")


@dataclass(frozen=True, slots=True)
class QuoterVerificationResult:
    """Result of pinned on-chain QuoterV2 / reserve-bounded route verification."""

    verified: bool
    simulated_amount_out: int
    simulated_net_profit: int
    hop_outputs: tuple[int, ...]
    reason: str
    rpc_calls_used: int
    execution_eligible: bool = False

    def __post_init__(self) -> None:
        if type(self.verified) is not bool:
            raise MulticallUniverseError("verified must be a boolean")
        require_int(self.simulated_amount_out, "simulated_amount_out", non_negative=True)
        require_int(self.simulated_net_profit, "simulated_net_profit")
        for idx, out in enumerate(self.hop_outputs):
            require_int(out, f"hop_outputs[{idx}]", non_negative=True)
        if not isinstance(self.reason, str) or not self.reason:
            raise MulticallUniverseError("reason must be a non-empty string")
        require_int(self.rpc_calls_used, "rpc_calls_used", non_negative=True)
        if self.execution_eligible is not False:
            raise MulticallUniverseError(
                "QuoterVerificationResult execution_eligible must be False"
            )


def _validate_canonical_wiring() -> None:
    if normalize_address(V3.factory or "") != UNIV3_FACTORY:
        raise MulticallUniverseError("V3.factory disagrees with UNIV3_FACTORY")
    if normalize_address(V3.quoter or "") != UNIV3_QUOTER_V2:
        raise MulticallUniverseError("V3.quoter disagrees with UNIV3_QUOTER_V2")
    if normalize_address(AERO_USDC_WETH[0]) == ZERO_ADDRESS:
        raise MulticallUniverseError("AERO_USDC_WETH pool must be non-zero")


_validate_canonical_wiring()


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
        raise MulticallUniverseError("Base multicall RPC forbids localhost endpoints")
    try:
        ip_obj = ipaddress.ip_address(hostname)
    except ValueError:
        return
    if _is_forbidden_ip(ip_obj):
        raise MulticallUniverseError("Base multicall RPC forbids private or loopback IP endpoints")


def _validate_rpc_url(rpc_url: str, *, enforce_public_host: bool) -> tuple[str, str, int]:
    if not isinstance(rpc_url, str) or not rpc_url or rpc_url != rpc_url.strip():
        raise MulticallUniverseError("rpc_url must be a nonempty string without outer whitespace")
    parsed = urllib.parse.urlsplit(rpc_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise MulticallUniverseError("Base multicall RPC requires an https:// endpoint with host")
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise MulticallUniverseError("Base multicall RPC forbids URL userinfo credentials")
    hostname = parsed.hostname
    if not hostname:
        raise MulticallUniverseError("Base multicall RPC requires a nonempty hostname")
    try:
        port = parsed.port if parsed.port is not None else 443
    except ValueError as exc:
        raise MulticallUniverseError("Base multicall RPC URL contains an invalid port") from exc
    if enforce_public_host:
        _validate_public_ip_literal(hostname)
    display_host = f"[{hostname}]" if ":" in hostname else hostname
    origin = f"https://{display_host}" + (f":{parsed.port}" if parsed.port is not None else "")
    return origin, hostname, port


def _resolve_and_validate_dns(hostname: str, port: int) -> list[_AddrInfoEntry]:
    _validate_public_ip_literal(hostname)
    try:
        addr_infos = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise RpcError("Base multicall RPC DNS resolution failed") from exc
    if not addr_infos:
        raise RpcError("Base multicall RPC DNS returned no addresses")
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
            raise MulticallUniverseError("Base multicall RPC DNS resolved to a private/loopback IP")
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
    parsed = urllib.parse.urlsplit(rpc_url)
    if parsed.scheme != "https" or not parsed.netloc or not parsed.hostname:
        raise MulticallUniverseError("Base multicall RPC requires an https:// endpoint")
    port = parsed.port if parsed.port is not None else 443
    _validate_resolved_dns(parsed.hostname, port)

    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
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
                raise RpcError("Base multicall RPC response exceeds byte limit")
            return raw
    except urllib.error.HTTPError as exc:
        with suppress(OSError):
            exc.close()
        raise RpcError(f"Base multicall RPC HTTP failure: status {exc.code}") from None
    except (urllib.error.URLError, OSError) as exc:
        if isinstance(exc, RpcError):
            raise
        raise RpcError("Base multicall RPC transport failure") from None


def _validate_timeout(timeout_s: float) -> float:
    if (
        isinstance(timeout_s, bool)
        or not isinstance(timeout_s, (int, float))
        or not math.isfinite(float(timeout_s))
        or float(timeout_s) <= 0
    ):
        raise MulticallUniverseError("timeout_s must be a positive finite number")
    return float(timeout_s)


def _validate_block_tag(block_tag: str) -> str:
    if not isinstance(block_tag, str) or not block_tag:
        raise MulticallUniverseError("block_tag must be a nonempty string")
    if block_tag in _NAMED_BLOCK_TAGS:
        return block_tag
    rpc_quantity(block_tag)
    return block_tag


def _rpc_call(
    rpc_url: str,
    send: RpcTransport,
    timeout_s: float,
    req_id: int,
    method: str,
    params: list[object],
) -> object:
    req_bytes = json.dumps(
        {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params},
        separators=(",", ":"),
    ).encode("utf-8")
    resp_bytes = send(rpc_url, req_bytes, timeout_s)
    if not isinstance(resp_bytes, bytes) or len(resp_bytes) > MAX_RESPONSE_BYTES:
        raise RpcError("Base multicall RPC response must be bounded bytes")
    envelope = strict_json_object(resp_bytes)
    if (
        envelope.get("jsonrpc") != "2.0"
        or type(envelope.get("id")) is not int
        or envelope["id"] != req_id
    ):
        raise MulticallUniverseError("Base multicall RPC JSON-RPC version or ID mismatch")
    if ("result" in envelope) == ("error" in envelope):
        raise MulticallUniverseError("Base multicall RPC response must have result xor error")
    if "error" in envelope:
        err = envelope["error"]
        raise RpcError(f"Base multicall RPC error on {method}: {err!r}")
    return envelope["result"]


def _decode_hex_bytes(value: object, name: str) -> bytes:
    raw_hex = strict_string(value, name)
    if not raw_hex.startswith("0x") or len(raw_hex) < 2 or len(raw_hex) % 2 != 0:
        raise MulticallUniverseError(f"{name} must be 0x-prefixed even-length hex bytes")
    try:
        return bytes.fromhex(raw_hex[2:])
    except ValueError as exc:
        raise MulticallUniverseError(f"{name} contains non-hex characters") from exc


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
        raise MulticallUniverseError("invalid Multicall3.aggregate3 ABI return data") from exc
    if len(decoded) != expected_count:
        raise MulticallUniverseError(
            f"Multicall3.aggregate3 result count mismatch: {len(decoded)} != {expected_count}"
        )
    out: list[tuple[bool, bytes]] = []
    for item in decoded:
        success, ret = item
        if type(success) is not bool or not isinstance(ret, bytes):
            raise MulticallUniverseError("invalid Multicall3.aggregate3 element types")
        out.append((success, ret))
    return tuple(out)


def _decode_abi_address(raw: bytes, name: str) -> str:
    if len(raw) != 32:
        raise MulticallUniverseError(f"{name} address word must be 32 bytes, got {len(raw)}")
    if raw[:12] != b"\x00" * 12:
        raise MulticallUniverseError(f"{name} address word has non-zero upper 12 bytes")
    return "0x" + raw[12:].hex()


def _decode_uint256_bounded(raw: bytes, name: str, *, limit: int = _RESERVE_LIMIT) -> int:
    if len(raw) != 32:
        raise MulticallUniverseError(f"{name} uint256 word must be 32 bytes, got {len(raw)}")
    value = int.from_bytes(raw, "big")
    if not 0 <= value < limit:
        raise MulticallUniverseError(f"{name} value {value} out of finite bound [0, {limit})")
    return value


def _pin_block_header(
    rpc_url: str,
    send: RpcTransport,
    timeout_s: float,
    block_tag: str,
    req_id: int,
    *,
    require_denim: bool = True,
) -> tuple[int, str, str, int, int, int]:
    result = _rpc_call(rpc_url, send, timeout_s, req_id, "eth_getBlockByNumber", [block_tag, False])
    header = strict_object(result, "block header")
    block_number = rpc_quantity(header.get("number"))
    block_hash = normalize_hash32(strict_string(header.get("hash"), "block hash"), "block hash")
    parent_hash = normalize_hash32(
        strict_string(header.get("parentHash"), "parentHash"), "parentHash"
    )
    if block_hash == parent_hash:
        raise MulticallUniverseError("block header hash cannot equal parentHash")
    header_timestamp = rpc_quantity(header.get("timestamp"))
    header_timestamp_ms = validate_header_timestamp_ms(
        header, chain_id=BASE_CHAIN_ID, require_denim=require_denim
    )
    if header_timestamp_ms is None:
        raise MulticallUniverseError("base_denim_timestamp_ms_missing")
    base_fee_per_gas = rpc_quantity(header.get("baseFeePerGas"))
    return (
        block_number,
        block_hash,
        parent_hash,
        header_timestamp,
        header_timestamp_ms,
        base_fee_per_gas,
    )


@dataclass(frozen=True, slots=True)
class _FactoryQuerySpec:
    label: str
    venue: str
    kind: PoolKind
    meta0: BaseTokenMeta
    meta1: BaseTokenMeta
    fixed_fee_pips: int | None
    tick_spacing: int | None
    factory: str
    calldata: bytes


def _append_pair_venue_queries(
    queries: list[_FactoryQuerySpec],
    seen_query_keys: set[tuple[str, bytes]],
    seen_slipstream_keys: set[tuple[str, str, str, int]],
    token_a: BaseTokenMeta,
    token_b: BaseTokenMeta,
) -> None:
    if token_a.address == token_b.address:
        return
    meta0, meta1 = (token_a, token_b) if token_a.address < token_b.address else (token_b, token_a)
    s0 = meta0.symbol.lower()
    s1 = meta1.symbol.lower()

    def _add(spec: _FactoryQuerySpec) -> None:
        key = (spec.factory, spec.calldata)
        if key not in seen_query_keys:
            seen_query_keys.add(key)
            queries.append(spec)

    for fee in UNIV3_FEE_TIERS:
        _add(
            _FactoryQuerySpec(
                label=f"uniswap_v3_{s0}_{s1}_{fee}",
                venue="uniswap_v3",
                kind=PoolKind.V3,
                meta0=meta0,
                meta1=meta1,
                fixed_fee_pips=fee,
                tick_spacing=None,
                factory=UNIV3_FACTORY,
                calldata=bytes.fromhex(
                    UNIV3_GET_POOL_SELECTOR[2:]
                    + encode(
                        ["address", "address", "uint24"],
                        [meta0.address, meta1.address, fee],
                    ).hex()
                ),
            )
        )

    for ts in SLIPSTREAM_TICK_SPACINGS:
        seen_slipstream_keys.add((SLIPSTREAM_FACTORY, meta0.address, meta1.address, ts))
        _add(
            _FactoryQuerySpec(
                label=f"slipstream_{s0}_{s1}_ts{ts}",
                venue="aerodrome_slipstream",
                kind=PoolKind.SLIPSTREAM,
                meta0=meta0,
                meta1=meta1,
                fixed_fee_pips=None,
                tick_spacing=ts,
                factory=SLIPSTREAM_FACTORY,
                calldata=bytes.fromhex(
                    SLIPSTREAM_GET_POOL_SELECTOR[2:]
                    + encode(
                        ["address", "address", "int24"],
                        [meta0.address, meta1.address, ts],
                    ).hex()
                ),
            )
        )

    _add(
        _FactoryQuerySpec(
            label=f"aerodrome_v2_{s0}_{s1}",
            venue="aerodrome_v2",
            kind=PoolKind.V2,
            meta0=meta0,
            meta1=meta1,
            fixed_fee_pips=None,
            tick_spacing=None,
            factory=AERO_FACTORY,
            calldata=bytes.fromhex(
                AERO_GET_POOL_SELECTOR[2:]
                + encode(
                    ["address", "address", "bool"],
                    [meta0.address, meta1.address, False],
                ).hex()
            ),
        )
    )


def _build_factory_queries(
    tokens: Sequence[BaseTokenMeta],
    *,
    ecosystem_tokens: Sequence[BaseTokenMeta] = (),
) -> tuple[_FactoryQuerySpec, ...]:
    queries: list[_FactoryQuerySpec] = []
    seen_query_keys: set[tuple[str, bytes]] = set()
    seen_slipstream_keys: set[tuple[str, str, str, int]] = set()
    by_symbol = {meta.symbol: meta for meta in tokens}

    for i in range(len(tokens)):
        for j in range(i + 1, len(tokens)):
            _append_pair_venue_queries(
                queries, seen_query_keys, seen_slipstream_keys, tokens[i], tokens[j]
            )

    if ecosystem_tokens:
        quote_tokens = tuple(by_symbol[sym] for sym in ("WETH", "USDC") if sym in by_symbol)
        for eco_tok in ecosystem_tokens:
            for quote_tok in quote_tokens:
                _append_pair_venue_queries(
                    queries, seen_query_keys, seen_slipstream_keys, eco_tok, quote_tok
                )

    # Include every SLIPSTREAM_USDC_WETH factory entry (e.g. second Slipstream v2 factory).
    if "WETH" in by_symbol and "USDC" in by_symbol:
        weth_meta = by_symbol["WETH"]
        usdc_meta = by_symbol["USDC"]
        uw0, uw1 = (
            (weth_meta, usdc_meta)
            if weth_meta.address < usdc_meta.address
            else (usdc_meta, weth_meta)
        )
        for raw_factory, (_pool_addr, spacing) in SLIPSTREAM_USDC_WETH.items():
            factory = normalize_address(raw_factory)
            key = (factory, uw0.address, uw1.address, spacing)
            if key in seen_slipstream_keys:
                continue
            seen_slipstream_keys.add(key)
            calldata = bytes.fromhex(
                SLIPSTREAM_GET_POOL_SELECTOR[2:]
                + encode(
                    ["address", "address", "int24"],
                    [uw0.address, uw1.address, spacing],
                ).hex()
            )
            query_key = (factory, calldata)
            if query_key in seen_query_keys:
                continue
            seen_query_keys.add(query_key)
            queries.append(
                _FactoryQuerySpec(
                    label=(
                        f"slipstream_{uw0.symbol.lower()}_{uw1.symbol.lower()}"
                        f"_ts{spacing}_{factory[2:8]}"
                    ),
                    venue="aerodrome_slipstream",
                    kind=PoolKind.SLIPSTREAM,
                    meta0=uw0,
                    meta1=uw1,
                    fixed_fee_pips=None,
                    tick_spacing=spacing,
                    factory=factory,
                    calldata=calldata,
                )
            )

    return tuple(queries)


def discover_base_universe_pools(
    rpc_url: str = DEFAULT_BASE_RPC_URL,
    *,
    transport: RpcTransport | None = None,
    timeout_s: float = 10.0,
    block_tag: str = "latest",
    include_ecosystem_tokens: bool = False,
) -> tuple[DiscoveredPool, ...]:
    """Discover and reserve-filter Base multi-asset pools in 3 pinned JSON-RPC calls."""
    if type(include_ecosystem_tokens) is not bool:
        raise MulticallUniverseError("include_ecosystem_tokens must be a boolean")
    checked_timeout = _validate_timeout(timeout_s)
    normalized_tag = _validate_block_tag(block_tag)
    _validate_rpc_url(rpc_url, enforce_public_host=transport is None)
    send = _default_https_transport if transport is None else transport

    block_number, _, _, _, _, _ = _pin_block_header(
        rpc_url, send, checked_timeout, normalized_tag, req_id=1
    )
    pinned_block = hex(block_number)

    factory_specs = _build_factory_queries(
        CANONICAL_BASE_TOKENS,
        ecosystem_tokens=ECOSYSTEM_BASE_TOKENS if include_ecosystem_tokens else (),
    )
    factory_calldata = _encode_aggregate3_calldata(
        [(spec.factory, True, spec.calldata) for spec in factory_specs]
    )
    raw_factory_resp = _rpc_call(
        rpc_url,
        send,
        checked_timeout,
        2,
        "eth_call",
        [{"to": MULTICALL3, "data": factory_calldata}, pinned_block],
    )
    factory_results = _decode_aggregate3_results(
        _decode_hex_bytes(raw_factory_resp, "discover.factory_multicall"),
        len(factory_specs),
    )
    if not any(success for success, _ in factory_results):
        raise MulticallUniverseError("all factory getPool calls failed in Multicall3 discovery")

    candidate_by_id: dict[str, tuple[_FactoryQuerySpec, str]] = {}
    for spec, (success, ret_bytes) in zip(factory_specs, factory_results, strict=True):
        if not success:
            continue
        pool_addr = _decode_abi_address(ret_bytes, f"{spec.label}.getPool")
        if pool_addr == ZERO_ADDRESS:
            continue
        if pool_addr not in candidate_by_id:
            candidate_by_id[pool_addr] = (spec, pool_addr)

    if not candidate_by_id:
        return ()

    candidates = list(candidate_by_id.values())
    probe_calls: list[tuple[str, bool, bytes]] = []
    for spec, pool_addr in candidates:
        bal_arg = bytes.fromhex(BALANCE_OF_SELECTOR[2:] + encode(["address"], [pool_addr]).hex())
        probe_calls.append((spec.meta0.address, True, bal_arg))
        probe_calls.append((spec.meta1.address, True, bal_arg))
        if spec.kind in (PoolKind.V3, PoolKind.SLIPSTREAM):
            probe_calls.append((pool_addr, True, bytes.fromhex(LIQUIDITY_SELECTOR[2:])))
        else:
            probe_calls.append((pool_addr, True, bytes.fromhex(GET_RESERVES_SELECTOR[2:])))

    probe_calldata = _encode_aggregate3_calldata(probe_calls)
    raw_probe_resp = _rpc_call(
        rpc_url,
        send,
        checked_timeout,
        3,
        "eth_call",
        [{"to": MULTICALL3, "data": probe_calldata}, pinned_block],
    )
    probe_results = _decode_aggregate3_results(
        _decode_hex_bytes(raw_probe_resp, "discover.reserve_multicall"),
        len(probe_calls),
    )

    discovered: list[DiscoveredPool] = []
    for idx, (spec, pool_addr) in enumerate(candidates):
        base = idx * 3
        ok0, raw0 = probe_results[base]
        ok1, raw1 = probe_results[base + 1]
        ok_state, raw_state = probe_results[base + 2]
        if not (ok0 and ok1 and ok_state):
            continue

        bal0 = _decode_uint256_bounded(raw0, f"{spec.label}.balance0")
        bal1 = _decode_uint256_bounded(raw1, f"{spec.label}.balance1")

        if spec.kind in (PoolKind.V3, PoolKind.SLIPSTREAM):
            if len(raw_state) != 32:
                raise MulticallUniverseError(
                    f"{spec.label}.liquidity() must be 32 bytes, got {len(raw_state)}"
                )
            liq = int.from_bytes(raw_state, "big")
            if liq <= 0 or liq >= _INT128_LIMIT:
                continue
            r0, r1 = bal0, bal1
        else:
            if len(raw_state) != 96:
                raise MulticallUniverseError(
                    f"{spec.label}.getReserves() must be 96 bytes, got {len(raw_state)}"
                )
            res0, res1, _ = decode(["uint256", "uint256", "uint256"], raw_state, strict=True)
            if res0 <= 0 or res1 <= 0 or res0 >= _RESERVE_LIMIT or res1 >= _RESERVE_LIMIT:
                continue
            # Use min(balanceOf, getReserves) so donated/unsynced dust cannot pass as AMM depth.
            r0 = min(bal0, int(res0))
            r1 = min(bal1, int(res1))

        if r0 < spec.meta0.min_pool_reserve or r1 < spec.meta1.min_pool_reserve:
            continue

        discovered.append(
            DiscoveredPool(
                pool_id=pool_addr,
                label=spec.label,
                venue=spec.venue,
                kind=spec.kind,
                token0=spec.meta0.address,
                token1=spec.meta1.address,
                decimals0=spec.meta0.decimals,
                decimals1=spec.meta1.decimals,
                fixed_fee_pips=spec.fixed_fee_pips,
                tick_spacing=spec.tick_spacing,
                factory=spec.factory,
                reserve0=r0,
                reserve1=r1,
            )
        )

    discovered.sort(key=lambda p: (p.label, p.pool_id))
    return tuple(discovered)


def compute_anti_hallucination_cl_bracket(
    *,
    sqrt_price_x96: int,
    tick: int,
    liquidity: int,
    balance0: int,
    balance1: int,
    max_half_width: int = ACTIVE_TICK_HALF_WIDTH,
) -> tuple[int, int, int, tuple[TickLiquidity, TickLiquidity]]:
    """Compute a conservative symmetric CL tick bracket bounded by 80% of pool token balances.

    Why this bound exists:
    When reading only ``slot0()`` and active ``liquidity()`` without querying the full
    on-chain tick bitmap, modeling a wide synthetic bracket ``[tick - H, tick + H]`` with
    full active liquidity ``L`` can overstate deliverable token0 / token1 if real LP positions
    only span a few ticks around the current tick (common in ``tickSpacing=1`` JIT pools).
    Any virtual single-band swap inside ``[lower_tick, upper_tick]`` moves ``sqrtP`` at most
    from ``sqrt_price_x96`` to ``sqrt_price_at_tick(upper_tick)`` (delivering token0) or down
    to ``sqrt_price_at_tick(lower_tick)`` (delivering token1). We binary-search the largest
    symmetric half-width ``h in [1, max_half_width]`` such that both exact integer
    ``amount0_delta`` and ``amount1_delta`` (with ``round_up=True``) stay <= 80% of actual
    on-chain ``balanceOf(pool)``. If even ``h = 1`` exceeds 80% of either balance (for
    example, a tight 1-tick range), ``h = 1`` is kept and ``effective_liquidity`` is
    conservatively scaled down so the invariant still holds strictly.
    """
    require_int(sqrt_price_x96, "sqrt_price_x96", non_negative=True)
    require_int(tick, "tick")
    require_int(liquidity, "liquidity", non_negative=True)
    require_int(balance0, "balance0", non_negative=True)
    require_int(balance1, "balance1", non_negative=True)
    require_int(max_half_width, "max_half_width", non_negative=True)
    if max_half_width < 1:
        raise MulticallUniverseError("max_half_width must be >= 1")
    if liquidity <= 0 or liquidity >= _INT128_LIMIT:
        raise MulticallUniverseError("CL liquidity must fit positive int128")
    if balance0 <= 0 or balance1 <= 0:
        raise MulticallUniverseError("CL pool token balances must be positive")
    if not MIN_TICK <= tick <= MAX_TICK:
        raise MulticallUniverseError(f"tick {tick} outside TickMath bounds")
    if not MIN_SQRT_PRICE <= sqrt_price_x96 <= MAX_SQRT_PRICE:
        raise MulticallUniverseError("sqrt_price_x96 outside TickMath bounds")

    max_h = min(max_half_width, tick - MIN_TICK, MAX_TICK - tick)
    if max_h < 1:
        raise MulticallUniverseError(f"tick {tick} too close to TickMath bounds")

    cap0 = (balance0 * _ANTI_HALLUCINATION_RESERVE_NUM) // _ANTI_HALLUCINATION_RESERVE_DEN
    cap1 = (balance1 * _ANTI_HALLUCINATION_RESERVE_NUM) // _ANTI_HALLUCINATION_RESERVE_DEN
    if cap0 <= 0 or cap1 <= 0:
        raise MulticallUniverseError("80% balance cap rounds to zero")

    def _deliverables(half: int, liq: int) -> tuple[int, int]:
        sqrt_lo = sqrt_price_at_tick(tick - half)
        sqrt_hi = sqrt_price_at_tick(tick + half)
        d0 = amount0_delta(sqrt_price_x96, sqrt_hi, liq, round_up=True)
        d1 = amount1_delta(sqrt_lo, sqrt_price_x96, liq, round_up=True)
        return d0, d1

    lo = 1
    hi = max_h
    best_h = 0
    while lo <= hi:
        mid = (lo + hi) // 2
        d0, d1 = _deliverables(mid, liquidity)
        if d0 <= cap0 and d1 <= cap1:
            best_h = mid
            lo = mid + 1
        else:
            hi = mid - 1

    eff_liq = liquidity
    if best_h == 0:
        best_h = 1
        d0_one, d1_one = _deliverables(1, liquidity)
        if d0_one > cap0:
            eff_liq = min(eff_liq, (liquidity * cap0) // d0_one)
        if d1_one > cap1:
            eff_liq = min(eff_liq, (liquidity * cap1) // d1_one)
        while eff_liq > 0:
            d0_chk, d1_chk = _deliverables(1, eff_liq)
            if d0_chk <= cap0 and d1_chk <= cap1:
                break
            eff_liq -= 1
        if eff_liq <= 0:
            raise MulticallUniverseError("pool reserve too small for even 1 wei of CL liquidity")

    lower_tick = tick - best_h
    upper_tick = tick + best_h
    return (
        lower_tick,
        upper_tick,
        eff_liq,
        (
            TickLiquidity(tick=lower_tick, liquidity_net=eff_liq),
            TickLiquidity(tick=upper_tick, liquidity_net=-eff_liq),
        ),
    )


def fetch_pinned_universe_snapshot(
    rpc_url: str = DEFAULT_BASE_RPC_URL,
    pools: Sequence[DiscoveredPool] = (),
    *,
    transport: RpcTransport | None = None,
    timeout_s: float = 10.0,
    block_tag: str = "latest",
    previous_snapshot: PinnedUniverseSnapshot | None = None,
) -> PinnedUniverseSnapshot:
    """Fetch a coherent block-pinned snapshot across all pools in exactly 2 RPC calls."""
    if not pools:
        raise MulticallUniverseError("pools must be non-empty")
    pool_list = tuple(pools)
    if len({p.pool_id for p in pool_list}) != len(pool_list):
        raise MulticallUniverseError("duplicate pool_id in pools")

    checked_timeout = _validate_timeout(timeout_s)
    normalized_tag = _validate_block_tag(block_tag)
    _validate_rpc_url(rpc_url, enforce_public_host=transport is None)
    send = _default_https_transport if transport is None else transport

    (
        block_number,
        block_hash,
        parent_hash,
        header_timestamp,
        header_timestamp_ms,
        base_fee_per_gas_wei,
    ) = _pin_block_header(rpc_url, send, checked_timeout, normalized_tag, req_id=1)

    if previous_snapshot is not None:
        if block_number < previous_snapshot.block_number:
            raise MulticallUniverseError(
                f"stale or regressed block {block_number} < {previous_snapshot.block_number}"
            )
        if (
            block_number == previous_snapshot.block_number
            and block_hash != previous_snapshot.block_hash
        ):
            raise MulticallUniverseError(f"reorg detected at block {block_number}: hash changed")
        if (
            block_number == previous_snapshot.block_number + 1
            and parent_hash != previous_snapshot.block_hash
        ):
            raise MulticallUniverseError(
                f"reorg detected at block {block_number}: parent_hash mismatch"
            )

    pinned_block = hex(block_number)
    calls: list[tuple[str, bool, bytes]] = [
        (BASE_TIME, False, bytes.fromhex(TIMESTAMP_MS_SELECTOR[2:])),
        (BASE_TIME, False, bytes.fromhex(TIMESTAMP_MILLIS_PART_SELECTOR[2:])),
        (ORACLE, False, bytes.fromhex(GET_L1_FEE_CALLDATA[2:])),
    ]
    pool_call_slices: list[tuple[DiscoveredPool, int, int]] = []
    for pool in pool_list:
        start_idx = len(calls)
        bal0_cd = bytes.fromhex(BALANCE_OF_SELECTOR[2:] + encode(["address"], [pool.pool_id]).hex())
        bal1_cd = bal0_cd
        if pool.kind in (PoolKind.V3, PoolKind.SLIPSTREAM):
            calls.extend(
                [
                    (pool.pool_id, False, bytes.fromhex(SLOT0_SELECTOR[2:])),
                    (pool.pool_id, False, bytes.fromhex(LIQUIDITY_SELECTOR[2:])),
                    (pool.pool_id, False, bytes.fromhex(FEE_SELECTOR[2:])),
                    (pool.token0, False, bal0_cd),
                    (pool.token1, False, bal1_cd),
                ]
            )
        elif pool.kind == PoolKind.V2:
            get_fee_cd = bytes.fromhex(
                GET_FEE_SELECTOR[2:] + encode(["address", "bool"], [pool.pool_id, False]).hex()
            )
            calls.extend(
                [
                    (pool.pool_id, False, bytes.fromhex(GET_RESERVES_SELECTOR[2:])),
                    (pool.factory, False, get_fee_cd),
                    (pool.token0, False, bal0_cd),
                    (pool.token1, False, bal1_cd),
                ]
            )
        else:
            raise MulticallUniverseError(f"unsupported pool kind {pool.kind!r}")
        pool_call_slices.append((pool, start_idx, len(calls)))

    multicall_data = _encode_aggregate3_calldata(calls)
    raw_mc_resp = _rpc_call(
        rpc_url,
        send,
        checked_timeout,
        2,
        "eth_call",
        [{"to": MULTICALL3, "data": multicall_data}, pinned_block],
    )
    results = _decode_aggregate3_results(
        _decode_hex_bytes(raw_mc_resp, "snapshot.multicall"),
        len(calls),
    )
    for idx, (success, _) in enumerate(results):
        if not success:
            raise MulticallUniverseError(f"snapshot Multicall3 subcall {idx} failed")

    ts_ms_word = _decode_uint256_bounded(results[0][1], "BaseTime.timestampMs", limit=1 << 64)
    millis_word = _decode_uint256_bounded(
        results[1][1], "BaseTime.timestampMillisPart", limit=1 << 64
    )
    base_time_ms = parse_base_time(
        timestamp_ms=ts_ms_word,
        millis_part=millis_word,
        header_timestamp=header_timestamp,
    )
    if base_time_ms != header_timestamp_ms:
        raise MulticallUniverseError("base_time_header_timestamp_ms_mismatch")

    l1_fee_wei = decode_l1_fee_wei(results[2][1])

    pool_nodes: list[PoolNode] = []
    balances_by_pool: dict[str, tuple[int, int]] = {}
    for pool, start_idx, end_idx in pool_call_slices:
        chunk = results[start_idx:end_idx]
        if pool.kind in (PoolKind.V3, PoolKind.SLIPSTREAM):
            sqrt_p, tick = decode_slot0_words(chunk[0][1])
            liq = decode_liquidity_word(chunk[1][1])
            fee_pips = decode_slipstream_fee_pips(chunk[2][1])
            if pool.fixed_fee_pips is not None and fee_pips != pool.fixed_fee_pips:
                raise MulticallUniverseError(
                    f"{pool.label} fee_pips mismatch: {fee_pips} != {pool.fixed_fee_pips}"
                )
            bal0 = _decode_uint256_bounded(chunk[3][1], f"{pool.label}.balance0")
            bal1 = _decode_uint256_bounded(chunk[4][1], f"{pool.label}.balance1")
            if bal0 <= 0 or bal1 <= 0:
                raise MulticallUniverseError(f"{pool.label} token balance must be positive")
            _, _, eff_liq, ticks = compute_anti_hallucination_cl_bracket(
                sqrt_price_x96=sqrt_p,
                tick=tick,
                liquidity=liq,
                balance0=bal0,
                balance1=bal1,
            )
            pool_nodes.append(
                PoolNode(
                    chain_id=BASE_CHAIN_ID,
                    pool_id=pool.pool_id,
                    kind=pool.kind,
                    token0=pool.token0,
                    token1=pool.token1,
                    decimals0=pool.decimals0,
                    decimals1=pool.decimals1,
                    fee_pips=fee_pips,
                    sqrt_price_x96=sqrt_p,
                    tick=tick,
                    liquidity=eff_liq,
                    ticks=ticks,
                    block_number=block_number,
                    timestamp_ms=base_time_ms,
                )
            )
            balances_by_pool[pool.pool_id] = (bal0, bal1)
        else:
            res0, res1, _ = decode_aero_get_reserves(chunk[0][1])
            fee_pips = decode_aero_factory_fee_pips(chunk[1][1])
            bal0 = _decode_uint256_bounded(chunk[2][1], f"{pool.label}.balance0")
            bal1 = _decode_uint256_bounded(chunk[3][1], f"{pool.label}.balance1")
            if bal0 <= 0 or bal1 <= 0:
                raise MulticallUniverseError(f"{pool.label} token balance must be positive")
            capped_r0 = min(res0, bal0)
            capped_r1 = min(res1, bal1)
            pool_nodes.append(
                PoolNode(
                    chain_id=BASE_CHAIN_ID,
                    pool_id=pool.pool_id,
                    kind=PoolKind.V2,
                    token0=pool.token0,
                    token1=pool.token1,
                    decimals0=pool.decimals0,
                    decimals1=pool.decimals1,
                    fee_pips=fee_pips,
                    reserve0=capped_r0,
                    reserve1=capped_r1,
                    block_number=block_number,
                    timestamp_ms=base_time_ms,
                )
            )
            balances_by_pool[pool.pool_id] = (bal0, bal1)

    return PinnedUniverseSnapshot(
        chain_id=BASE_CHAIN_ID,
        block_number=block_number,
        block_hash=block_hash,
        parent_hash=parent_hash,
        header_timestamp_ms=header_timestamp_ms,
        base_time_ms=base_time_ms,
        base_fee_per_gas_wei=base_fee_per_gas_wei,
        l1_fee_wei=l1_fee_wei,
        pools=pool_list,
        pool_nodes=tuple(pool_nodes),
        balances_by_pool=balances_by_pool,
        rpc_calls_used=2,
        execution_eligible=False,
    )


def _quoter_single_call(
    rpc_url: str,
    send: RpcTransport,
    timeout_s: float,
    req_id: int,
    quoter_addr: str,
    calldata_hex: str,
    pinned_block: str,
) -> tuple[int, int]:
    """Execute one pinned QuoterV2/Slipstream `quoteExactInputSingle` eth_call strictly."""
    raw_result = _rpc_call(
        rpc_url,
        send,
        timeout_s,
        req_id,
        "eth_call",
        [{"to": quoter_addr, "data": calldata_hex}, pinned_block],
    )
    raw_bytes = _decode_hex_bytes(raw_result, "quoter.quoteExactInputSingle")
    if len(raw_bytes) != 128:
        raise MulticallUniverseError(
            f"quoteExactInputSingle return must be 128 bytes, got {len(raw_bytes)}"
        )
    try:
        amount_out, sqrt_price_after, _ticks_crossed, _gas_est = decode(
            ["uint256", "uint160", "uint32", "uint256"], raw_bytes, strict=True
        )
    except DecodingError as exc:
        raise MulticallUniverseError("invalid quoteExactInputSingle ABI return") from exc
    out_int = int(amount_out)
    sqrt_int = int(sqrt_price_after)
    if not MIN_SQRT_PRICE <= sqrt_int <= MAX_SQRT_PRICE:
        raise MulticallUniverseError("quoter sqrtPriceX96After outside TickMath bounds")
    return out_int, sqrt_int


def _output_balance_cap(
    pool_node: PoolNode,
    token_out: str,
    balances_by_pool: Mapping[str, tuple[int, int]] | None,
) -> int | None:
    if balances_by_pool is not None:
        pair = balances_by_pool.get(pool_node.pool_id) or balances_by_pool.get(
            pool_node.pool_id.lower()
        )
        if pair is not None:
            return pair[0] if token_out == pool_node.token0 else pair[1]
    if pool_node.kind == PoolKind.V2:
        return pool_node.reserve0 if token_out == pool_node.token0 else pool_node.reserve1
    return None


def verify_candidate_with_onchain_quoter(
    rpc_url: str,
    hops: Sequence[CycleHop],
    amount_in: int,
    gas_cost_in_base: int,
    l1_fee_in_base: int,
    pools_by_id: Mapping[str, PoolNode],
    block_number_hex: str,
    *,
    transport: RpcTransport | None = None,
    timeout_s: float = 10.0,
    balances_by_pool: Mapping[str, tuple[int, int]] | None = None,
    discovered_pools_by_id: Mapping[str, DiscoveredPool] | None = None,
    bridge: FastpathBridge | None = None,
) -> QuoterVerificationResult:
    """Verify a multi-hop candidate sequentially at pinned block via QuoterV2 & bounded quotes."""
    checked_timeout = _validate_timeout(timeout_s)
    _validate_rpc_url(rpc_url, enforce_public_host=transport is None)
    pinned_num = rpc_quantity(block_number_hex)
    pinned_block = hex(pinned_num)
    require_int(amount_in, "amount_in", non_negative=True)
    require_int(gas_cost_in_base, "gas_cost_in_base", non_negative=True)
    require_int(l1_fee_in_base, "l1_fee_in_base", non_negative=True)

    if amount_in <= 0:
        return QuoterVerificationResult(
            verified=False,
            simulated_amount_out=0,
            simulated_net_profit=0,
            hop_outputs=(),
            reason="amount_in_must_be_positive",
            rpc_calls_used=0,
        )
    if not hops:
        return QuoterVerificationResult(
            verified=False,
            simulated_amount_out=0,
            simulated_net_profit=0,
            hop_outputs=(),
            reason="empty_hops",
            rpc_calls_used=0,
        )

    normalized_pools: dict[str, PoolNode] = {
        normalize_address(k): v for k, v in pools_by_id.items()
    }
    normalized_discovered: dict[str, DiscoveredPool] = (
        {normalize_address(k): v for k, v in discovered_pools_by_id.items()}
        if discovered_pools_by_id is not None
        else {}
    )
    normalized_balances: dict[str, tuple[int, int]] | None = (
        {normalize_address(k): v for k, v in balances_by_pool.items()}
        if balances_by_pool is not None
        else None
    )

    send = _default_https_transport if transport is None else transport
    local_graph: FastpathPoolGraph | None = None
    rpc_calls_used = 0
    hop_outputs: list[int] = []
    cur_amount = amount_in
    expected_in = normalize_address(hops[0].token_in)

    def _get_local_graph() -> FastpathPoolGraph:
        nonlocal local_graph
        if local_graph is None:
            active_bridge = resolve_fastpath_bridge(bridge)
            local_graph = FastpathPoolGraph(bridge=active_bridge)
            for node in normalized_pools.values():
                local_graph.upsert_pool(node)
        return local_graph

    for idx, hop in enumerate(hops):
        pool_id = normalize_address(hop.pool_id)
        token_in = normalize_address(hop.token_in)
        token_out = normalize_address(hop.token_out)
        if token_in != expected_in:
            return QuoterVerificationResult(
                verified=False,
                simulated_amount_out=0,
                simulated_net_profit=0,
                hop_outputs=tuple(hop_outputs),
                reason=f"hop_{idx}_token_in_mismatch",
                rpc_calls_used=rpc_calls_used,
            )
        pool_node = normalized_pools.get(pool_id)
        if pool_node is None:
            return QuoterVerificationResult(
                verified=False,
                simulated_amount_out=0,
                simulated_net_profit=0,
                hop_outputs=tuple(hop_outputs),
                reason=f"hop_{idx}_pool_missing",
                rpc_calls_used=rpc_calls_used,
            )
        pool_tokens = {normalize_address(pool_node.token0), normalize_address(pool_node.token1)}
        if token_in not in pool_tokens or token_out not in pool_tokens or token_in == token_out:
            return QuoterVerificationResult(
                verified=False,
                simulated_amount_out=0,
                simulated_net_profit=0,
                hop_outputs=tuple(hop_outputs),
                reason=f"hop_{idx}_token_not_in_pool",
                rpc_calls_used=rpc_calls_used,
            )

        out_amount = 0
        if pool_node.kind == PoolKind.V3:
            calldata_hex = (
                UNIV3_QUOTE_SINGLE_SELECTOR
                + encode(
                    ["(address,address,uint256,uint24,uint160)"],
                    [(token_in, token_out, cur_amount, pool_node.fee_pips, 0)],
                ).hex()
            )
            rpc_calls_used += 1
            try:
                out_amount, _ = _quoter_single_call(
                    rpc_url,
                    send,
                    checked_timeout,
                    rpc_calls_used,
                    UNIV3_QUOTER_V2,
                    calldata_hex,
                    pinned_block,
                )
            except (RpcError, MulticallUniverseError) as exc:
                return QuoterVerificationResult(
                    verified=False,
                    simulated_amount_out=0,
                    simulated_net_profit=0,
                    hop_outputs=tuple(hop_outputs),
                    reason=f"hop_{idx}_quoter_v2_failed: {exc}",
                    rpc_calls_used=rpc_calls_used,
                )
        elif pool_node.kind == PoolKind.SLIPSTREAM:
            disc = normalized_discovered.get(pool_id)
            quoter_entry = SLIPSTREAM.get(disc.factory) if disc is not None else None
            if disc is not None and disc.tick_spacing is not None and quoter_entry is not None:
                calldata_hex = (
                    SLIPSTREAM_QUOTE_SINGLE_SELECTOR
                    + encode(
                        ["(address,address,uint256,int24,uint160)"],
                        [(token_in, token_out, cur_amount, disc.tick_spacing, 0)],
                    ).hex()
                )
                rpc_calls_used += 1
                try:
                    out_amount, _ = _quoter_single_call(
                        rpc_url,
                        send,
                        checked_timeout,
                        rpc_calls_used,
                        normalize_address(quoter_entry[0]),
                        calldata_hex,
                        pinned_block,
                    )
                except (RpcError, MulticallUniverseError) as exc:
                    return QuoterVerificationResult(
                        verified=False,
                        simulated_amount_out=0,
                        simulated_net_profit=0,
                        hop_outputs=tuple(hop_outputs),
                        reason=f"hop_{idx}_slipstream_quoter_failed: {exc}",
                        rpc_calls_used=rpc_calls_used,
                    )
            else:
                try:
                    out_amount = (
                        _get_local_graph()
                        .quote_pool(pool_node.chain_id, pool_node.pool_id, token_in, cur_amount)
                        .amount_out
                    )
                except FastpathCallError as exc:
                    return QuoterVerificationResult(
                        verified=False,
                        simulated_amount_out=0,
                        simulated_net_profit=0,
                        hop_outputs=tuple(hop_outputs),
                        reason=f"hop_{idx}_local_slipstream_quote_failed: {exc}",
                        rpc_calls_used=rpc_calls_used,
                    )
        elif pool_node.kind == PoolKind.V2:
            try:
                out_amount = (
                    _get_local_graph()
                    .quote_pool(pool_node.chain_id, pool_node.pool_id, token_in, cur_amount)
                    .amount_out
                )
            except FastpathCallError as exc:
                return QuoterVerificationResult(
                    verified=False,
                    simulated_amount_out=0,
                    simulated_net_profit=0,
                    hop_outputs=tuple(hop_outputs),
                    reason=f"hop_{idx}_local_v2_quote_failed: {exc}",
                    rpc_calls_used=rpc_calls_used,
                )
        else:
            return QuoterVerificationResult(
                verified=False,
                simulated_amount_out=0,
                simulated_net_profit=0,
                hop_outputs=tuple(hop_outputs),
                reason=f"hop_{idx}_unsupported_kind_{pool_node.kind.value}",
                rpc_calls_used=rpc_calls_used,
            )

        if out_amount <= 0:
            return QuoterVerificationResult(
                verified=False,
                simulated_amount_out=0,
                simulated_net_profit=0,
                hop_outputs=tuple(hop_outputs),
                reason=f"hop_{idx}_zero_output",
                rpc_calls_used=rpc_calls_used,
            )

        bal_cap = _output_balance_cap(pool_node, token_out, normalized_balances)
        if bal_cap is not None and out_amount > bal_cap:
            return QuoterVerificationResult(
                verified=False,
                simulated_amount_out=0,
                simulated_net_profit=0,
                hop_outputs=tuple(hop_outputs),
                reason=f"hop_{idx}_output_exceeds_pool_balance",
                rpc_calls_used=rpc_calls_used,
            )

        hop_outputs.append(out_amount)
        cur_amount = out_amount
        expected_in = token_out

    if expected_in != normalize_address(hops[0].token_in):
        return QuoterVerificationResult(
            verified=False,
            simulated_amount_out=0,
            simulated_net_profit=0,
            hop_outputs=tuple(hop_outputs),
            reason="route_does_not_close_cycle",
            rpc_calls_used=rpc_calls_used,
        )

    simulated_net_profit = cur_amount - amount_in - gas_cost_in_base - l1_fee_in_base
    if simulated_net_profit <= 0:
        return QuoterVerificationResult(
            verified=False,
            simulated_amount_out=cur_amount,
            simulated_net_profit=simulated_net_profit,
            hop_outputs=tuple(hop_outputs),
            reason="non_positive_net_profit",
            rpc_calls_used=rpc_calls_used,
        )

    return QuoterVerificationResult(
        verified=True,
        simulated_amount_out=cur_amount,
        simulated_net_profit=simulated_net_profit,
        hop_outputs=tuple(hop_outputs),
        reason="ok",
        rpc_calls_used=rpc_calls_used,
    )


__all__ = [
    "AERO_GET_POOL_SELECTOR",
    "AERO_TOKEN",
    "AGGREGATE3_SELECTOR",
    "BALANCE_OF_SELECTOR",
    "CANONICAL_BASE_TOKENS",
    "CBBTC",
    "CBETH_TOKEN",
    "DEGEN_TOKEN",
    "ECOSYSTEM_BASE_TOKENS",
    "EXTENDED_BASE_TOKENS",
    "MORPHO_TOKEN",
    "MULTICALL3",
    "SLIPSTREAM_FACTORY",
    "SLIPSTREAM_GET_POOL_SELECTOR",
    "SLIPSTREAM_QUOTE_SINGLE_SELECTOR",
    "SLIPSTREAM_TICK_SPACINGS",
    "UNIV3_FACTORY",
    "UNIV3_FEE_TIERS",
    "UNIV3_GET_POOL_SELECTOR",
    "UNIV3_QUOTER_V2",
    "UNIV3_QUOTE_SINGLE_SELECTOR",
    "VIRTUAL_TOKEN",
    "VVV_TOKEN",
    "WELL_TOKEN",
    "WSTETH",
    "BaseTokenMeta",
    "DiscoveredPool",
    "MulticallUniverseError",
    "PinnedUniverseSnapshot",
    "QuoterVerificationResult",
    "compute_anti_hallucination_cl_bracket",
    "discover_base_universe_pools",
    "fetch_pinned_universe_snapshot",
    "verify_candidate_with_onchain_quoter",
]
