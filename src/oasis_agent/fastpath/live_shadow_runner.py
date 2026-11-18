"""Keyless, read-only, budget-bounded Base multi-DEX arbitrage & L1/L2 cost shadow runner.

Reads pinned Base mainnet block headers, four canonical WETH/USDC pools (Uniswap V3,
Aerodrome Volatile V2, and two Aerodrome Slipstream CL pools), GasPriceOracle L1 data
fees, and BaseTime predeploy timestamps in a single pinned JSON-RPC batch per tick.
Evaluates 2-hop cycles with the native Rust fastpath engine plus exhaustive pairwise
0.1 WETH direct quotes, optionally polls UniswapX open Dutch_V3 orders, and emits a
hash-linked `shadow_attribution.jsonl` audit log and `shadow_summary.json`.

All outputs are read-only shadow measurements with `execution_eligible = False`.
"""

import ipaddress
import json
import math
import os
import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, cast

import certifi
from eth_abi.abi import decode, encode
from eth_hash.auto import keccak

from ..arbitrage.event_quote import GAS_LIMIT_MODEL, ORACLE, V3
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
from ..intents.uniswapx_client import UniswapXClientError, UniswapXOrderbookClient
from ..models.common import canonical_json, normalize_address, normalize_hash32, sha256_hex
from ..sniping.venues import AERO_FACTORY, AERO_USDC_WETH, SLIPSTREAM_USDC_WETH, USDC, WETH
from ..uniswap_v4.liquidity_math import (
    MAX_SQRT_PRICE,
    MAX_TICK,
    MIN_SQRT_PRICE,
    MIN_TICK,
    sqrt_price_at_tick,
)
from .bridge import FastpathBridge, FastpathCallError, FastpathUnavailableError
from .pool_graph import FastpathPoolGraph, PoolKind, PoolNode, TickLiquidity

DEFAULT_BASE_RPC_URL: Final[str] = "https://base-rpc.publicnode.com"
DEFAULT_GAS_LIMIT_UNITS: Final[int] = GAS_LIMIT_MODEL
DEFAULT_PRIORITY_FEE_WEI: Final[int] = 2_000_000
MAX_RESPONSE_BYTES: Final[int] = 1_000_000
PROBE_AMOUNT_IN_WEI: Final[int] = 10**17  # 0.1 WETH
SCAN_MIN_AMOUNT_IN_WEI: Final[int] = 10**16  # 0.01 WETH
SCAN_MAX_AMOUNT_IN_WEI: Final[int] = 10**18  # 1.0 WETH
ACTIVE_TICK_HALF_WIDTH: Final[int] = 200
RPC_CALLS_PER_TICK: Final[int] = 15
_BATCH_CALL_COUNT: Final[int] = 14
_BPS_SCALE: Final[int] = 10_000
_LCB_Z_SCORE: Final[float] = 1.96
_DUMMY_EXECUTE_CYCLE_TX_BYTES: Final[bytes] = b"\xaa" * 680
_GENESIS_PREV_SHA256: Final[str] = "0" * 64
_MAX_SLIPSTREAM_FEE_PIPS: Final[int] = 100_000
_MAX_AERO_FEE_BPS: Final[int] = 1_000
_UINT128_LIMIT: Final[int] = 1 << 128
_UINT160_LIMIT: Final[int] = 1 << 160
_UINT24_LIMIT: Final[int] = 1 << 24
_UINT16_LIMIT: Final[int] = 1 << 16
_UINT8_LIMIT: Final[int] = 1 << 8
_LOCALHOST_NAMES: Final[frozenset[str]] = frozenset({"localhost", "localhost.localdomain"})
_NAMED_BLOCK_TAGS: Final[frozenset[str]] = frozenset({"latest", "safe", "finalized"})
_TUN_FAKE_IPV4_NET: Final[ipaddress.IPv4Network] = ipaddress.IPv4Network("198.18.0.0/15")
_TUN_FAKE_IPV6_NET: Final[ipaddress.IPv6Network] = ipaddress.IPv6Network("2001:2::/48")
_HEX_BYTES_RE: Final[re.Pattern[str]] = re.compile(r"0x(?:[0-9a-fA-F]{2})+")
_SHA256_RE: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{64}")
_USER_AGENT: Final[str] = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)


def _selector(signature: str) -> str:
    return "0x" + keccak(signature.encode("ascii"))[:4].hex()


SLOT0_SELECTOR: Final[str] = _selector("slot0()")
LIQUIDITY_SELECTOR: Final[str] = _selector("liquidity()")
FEE_SELECTOR: Final[str] = _selector("fee()")
GET_RESERVES_SELECTOR: Final[str] = _selector("getReserves()")
GET_FEE_SELECTOR: Final[str] = _selector("getFee(address,bool)")
GET_L1_FEE_SELECTOR: Final[str] = _selector("getL1Fee(bytes)")
GET_L1_FEE_CALLDATA: Final[str] = (
    GET_L1_FEE_SELECTOR + encode(["bytes"], [_DUMMY_EXECUTE_CYCLE_TX_BYTES]).hex()
)


class ShadowRunnerError(ValueError):
    """Fail-closed validation or state-integrity error in Base shadow runner."""


class ShadowBudgetError(ShadowRunnerError):
    """Raised before network I/O when request budget cannot cover the requested operation."""


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        with suppress(OSError):
            fp.close()
        raise RpcError("Base shadow RPC redirects are forbidden")


@dataclass(frozen=True, slots=True)
class BasePoolSpec:
    """Canonical Base mainnet WETH/USDC pool metadata."""

    name: str
    address: str
    kind: PoolKind
    token0: str
    token1: str
    decimals0: int
    decimals1: int
    fixed_fee_pips: int | None = None
    factory: str | None = None


@dataclass(frozen=True, slots=True)
class BaseShadowRunResult:
    """Completed Base shadow attribution run result and artifact paths."""

    summary: dict[str, Any]
    records: tuple[dict[str, Any], ...]
    summary_path: Path
    attribution_path: Path


def canonical_base_weth_usdc_pools() -> tuple[BasePoolSpec, ...]:
    """Return the four canonical Base WETH/USDC pools with enforced token0 < token1 ordering."""
    weth = normalize_address(WETH)
    usdc = normalize_address(USDC)
    if weth >= usdc:
        raise ShadowRunnerError("canonical Base WETH must sort before USDC")

    v3_addr = normalize_address(V3.address)
    if normalize_address(V3.token0) != weth or normalize_address(V3.token1) != usdc:
        raise ShadowRunnerError("Uniswap V3 canonical pool tokens mismatch WETH/USDC")
    if V3.fee is None or V3.fee <= 0 or V3.fee >= _MAX_SLIPSTREAM_FEE_PIPS:
        raise ShadowRunnerError("Uniswap V3 canonical fee out of range")

    aero_addr_raw, aero_stable = AERO_USDC_WETH
    if aero_stable is not False:
        raise ShadowRunnerError("Aerodrome canonical WETH/USDC pool must be volatile")
    aero_addr = normalize_address(aero_addr_raw)
    aero_factory = normalize_address(AERO_FACTORY)

    slipstream_entries = tuple(SLIPSTREAM_USDC_WETH.items())
    if len(slipstream_entries) != 2:
        raise ShadowRunnerError("expected exactly two canonical Slipstream WETH/USDC pools")
    ss1_factory, (ss1_addr_raw, _ss1_spacing) = slipstream_entries[0]
    ss2_factory, (ss2_addr_raw, _ss2_spacing) = slipstream_entries[1]
    ss1_addr = normalize_address(ss1_addr_raw)
    ss2_addr = normalize_address(ss2_addr_raw)

    specs = (
        BasePoolSpec(
            name="uniswap_v3_weth_usdc_500",
            address=v3_addr,
            kind=PoolKind.V3,
            token0=weth,
            token1=usdc,
            decimals0=18,
            decimals1=6,
            fixed_fee_pips=V3.fee,
        ),
        BasePoolSpec(
            name="aerodrome_volatile_weth_usdc",
            address=aero_addr,
            kind=PoolKind.V2,
            token0=weth,
            token1=usdc,
            decimals0=18,
            decimals1=6,
            fixed_fee_pips=None,
            factory=aero_factory,
        ),
        BasePoolSpec(
            name="slipstream_weth_usdc_1",
            address=ss1_addr,
            kind=PoolKind.SLIPSTREAM,
            token0=weth,
            token1=usdc,
            decimals0=18,
            decimals1=6,
            fixed_fee_pips=None,
            factory=normalize_address(ss1_factory),
        ),
        BasePoolSpec(
            name="slipstream_weth_usdc_2",
            address=ss2_addr,
            kind=PoolKind.SLIPSTREAM,
            token0=weth,
            token1=usdc,
            decimals0=18,
            decimals1=6,
            fixed_fee_pips=None,
            factory=normalize_address(ss2_factory),
        ),
    )
    if len({spec.address for spec in specs}) != 4:
        raise ShadowRunnerError("canonical Base WETH/USDC pool addresses must be distinct")
    return specs


def resolve_fastpath_bridge(bridge: FastpathBridge | None = None) -> FastpathBridge:
    """Resolve `FastpathBridge` from arg, `OASIS_CORE_LIB_PATH`, or `OASIS_FASTPATH_LIB`."""
    if bridge is not None:
        return bridge
    try:
        return FastpathBridge()
    except FastpathUnavailableError:
        alt_env = os.environ.get("OASIS_FASTPATH_LIB")
        if alt_env:
            return FastpathBridge(lib_path=Path(alt_env).expanduser())
        raise


def _is_tun_fake_ip(ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip_obj, ipaddress.IPv4Address):
        return ip_obj in _TUN_FAKE_IPV4_NET
    return ip_obj in _TUN_FAKE_IPV6_NET


def _is_forbidden_ip(ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip_obj, ipaddress.IPv6Address) and ip_obj.ipv4_mapped is not None:
        mapped = ip_obj.ipv4_mapped
        if _is_forbidden_ip(mapped):
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
        raise ShadowRunnerError("Base shadow RPC forbids localhost endpoints")
    try:
        ip_obj = ipaddress.ip_address(hostname)
    except ValueError:
        return
    if _is_forbidden_ip(ip_obj):
        raise ShadowRunnerError("Base shadow RPC forbids private or loopback IP endpoints")


def _validate_rpc_url(rpc_url: str, *, enforce_public_host: bool) -> tuple[str, str, int]:
    if not isinstance(rpc_url, str) or not rpc_url or rpc_url != rpc_url.strip():
        raise ShadowRunnerError("rpc_url must be a nonempty string without outer whitespace")
    parsed = urllib.parse.urlsplit(rpc_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ShadowRunnerError("Base shadow RPC requires an https:// endpoint with host")
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise ShadowRunnerError("Base shadow RPC forbids URL userinfo credentials")
    hostname = parsed.hostname
    if not hostname:
        raise ShadowRunnerError("Base shadow RPC requires a nonempty hostname")
    try:
        port = parsed.port if parsed.port is not None else 443
    except ValueError as exc:
        raise ShadowRunnerError("Base shadow RPC URL contains an invalid port") from exc
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
        raise RpcError("Base shadow RPC DNS resolution failed") from exc
    if not addr_infos:
        raise RpcError("Base shadow RPC DNS returned no addresses")
    for _, _, _, _, sockaddr in addr_infos:
        raw_ip = str(sockaddr[0]).split("%", 1)[0]
        ip_obj = ipaddress.ip_address(raw_ip)
        is_mapped_tun = (
            isinstance(ip_obj, ipaddress.IPv6Address)
            and ip_obj.ipv4_mapped is not None
            and _is_tun_fake_ip(ip_obj.ipv4_mapped)
        )
        if _is_forbidden_ip(ip_obj) and not _is_tun_fake_ip(ip_obj) and not is_mapped_tun:
            raise ShadowRunnerError("Base shadow RPC DNS resolved to a private or loopback IP")


def _default_https_transport(rpc_url: str, payload: bytes, timeout_s: float) -> bytes:
    parsed = urllib.parse.urlsplit(rpc_url)
    if parsed.scheme != "https" or not parsed.netloc or not parsed.hostname:
        raise ShadowRunnerError("Base shadow RPC requires an https:// endpoint")
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
                raise RpcError("Base shadow RPC response exceeds byte limit")
            return raw
    except urllib.error.HTTPError as exc:
        with suppress(OSError):
            exc.close()
        raise RpcError(f"Base shadow RPC HTTP failure: status {exc.code}") from None
    except (urllib.error.URLError, OSError) as exc:
        if isinstance(exc, RpcError):
            raise
        raise RpcError("Base shadow RPC transport failure") from None


def _validate_block_tag(block_tag: str) -> str:
    if not isinstance(block_tag, str) or not block_tag:
        raise ShadowRunnerError("block_tag must be a nonempty string")
    if block_tag in _NAMED_BLOCK_TAGS:
        return block_tag
    rpc_quantity(block_tag)
    return block_tag


def _unique_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ShadowRunnerError("RPC JSON contains duplicate fields")
        result[key] = value
    return result


def _reject_constant(_value: str) -> object:
    raise ShadowRunnerError("RPC JSON contains non-finite numbers")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ShadowRunnerError("RPC JSON number overflows to non-finite value")
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
        raise ShadowRunnerError("Base shadow batch response is invalid JSON") from exc
    if not isinstance(decoded, list) or len(decoded) != len(expected_ids):
        raise ShadowRunnerError("Base shadow batch response length mismatch")

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
            raise ShadowRunnerError("Base shadow batch item version or ID mismatch")
        if item_id in results:
            raise ShadowRunnerError("Base shadow batch response contains duplicate ID")
        if ("result" in envelope) == ("error" in envelope):
            raise ShadowRunnerError("Base shadow batch item must contain exactly one result/error")
        if "error" in envelope:
            raise RpcError("Base shadow batch call returned JSON-RPC error")
        results[item_id] = envelope["result"]
    if set(results) != expected_set:
        raise ShadowRunnerError("Base shadow batch response missing expected IDs")
    return results


def _decode_call_bytes(
    value: object,
    name: str,
    *,
    expected_lengths: tuple[int, ...],
) -> bytes:
    raw_hex = strict_string(value, name)
    if _HEX_BYTES_RE.fullmatch(raw_hex) is None:
        raise ShadowRunnerError(f"{name} must be nonempty 0x-prefixed hex bytes")
    raw = bytes.fromhex(raw_hex[2:])
    if len(raw) not in expected_lengths:
        raise ShadowRunnerError(
            f"{name} ABI byte length {len(raw)} not in expected {expected_lengths}"
        )
    return raw


def _decode_signed_word(word_bytes: bytes, bits: int, name: str) -> int:
    word = int.from_bytes(word_bytes, "big")
    mask = (1 << bits) - 1
    sign_bit = 1 << (bits - 1)
    lower = word & mask
    upper = word >> bits
    expected_upper = (1 << (256 - bits)) - 1 if (lower & sign_bit) else 0
    if upper != expected_upper:
        raise ShadowRunnerError(f"{name} signed int{bits} word is not canonically sign-extended")
    return lower - (1 << bits) if (lower & sign_bit) else lower


def _decode_uint_word(word_bytes: bytes, limit: int, name: str) -> int:
    value = int.from_bytes(word_bytes, "big")
    if not 0 <= value < limit:
        raise ShadowRunnerError(f"{name} out of canonical range")
    return value


def decode_slot0_words(raw: bytes) -> tuple[int, int]:
    """Decode 6-word (Slipstream) or 7-word (Uniswap V3) `slot0()` ABI output strictly."""
    if len(raw) not in (192, 224):
        raise ShadowRunnerError(f"slot0() ABI length must be 192 or 224 bytes, got {len(raw)}")
    sqrt_price_x96 = _decode_uint_word(raw[0:32], _UINT160_LIMIT, "sqrtPriceX96")
    tick = _decode_signed_word(raw[32:64], 24, "tick")
    _decode_uint_word(raw[64:96], _UINT16_LIMIT, "observationIndex")
    _decode_uint_word(raw[96:128], _UINT16_LIMIT, "observationCardinality")
    _decode_uint_word(raw[128:160], _UINT16_LIMIT, "observationCardinalityNext")
    if len(raw) == 224:
        _decode_uint_word(raw[160:192], _UINT8_LIMIT, "feeProtocol")
        unlocked_word = _decode_uint_word(raw[192:224], 2, "unlocked")
    else:
        unlocked_word = _decode_uint_word(raw[160:192], 2, "unlocked")
    if unlocked_word != 1:
        raise ShadowRunnerError("CL pool slot0 unlocked flag must be true")
    if not MIN_SQRT_PRICE <= sqrt_price_x96 <= MAX_SQRT_PRICE:
        raise ShadowRunnerError("slot0 sqrtPriceX96 outside canonical TickMath bounds")
    if not MIN_TICK <= tick <= MAX_TICK:
        raise ShadowRunnerError("slot0 tick outside canonical TickMath bounds")
    if sqrt_price_x96 < sqrt_price_at_tick(tick):
        raise ShadowRunnerError("slot0 sqrtPriceX96 below sqrt_price_at_tick(tick)")
    if tick < MAX_TICK and sqrt_price_x96 >= sqrt_price_at_tick(tick + 1):
        raise ShadowRunnerError("slot0 sqrtPriceX96 >= sqrt_price_at_tick(tick + 1)")
    return sqrt_price_x96, tick


def decode_liquidity_word(raw: bytes) -> int:
    """Decode CL `liquidity()` `uint128` ABI output strictly."""
    if len(raw) != 32:
        raise ShadowRunnerError(f"liquidity() must be 32 bytes, got {len(raw)}")
    liq = _decode_uint_word(raw, _UINT128_LIMIT, "liquidity")
    if liq <= 0:
        raise ShadowRunnerError("pool liquidity must be positive")
    return liq


def decode_slipstream_fee_pips(raw: bytes) -> int:
    """Decode Slipstream `fee()` `uint24` output and enforce `0 < fee_pips < 100_000`."""
    if len(raw) != 32:
        raise ShadowRunnerError(f"fee() must be 32 bytes, got {len(raw)}")
    fee_pips = _decode_uint_word(raw, _UINT24_LIMIT, "fee_pips")
    if not 0 < fee_pips < _MAX_SLIPSTREAM_FEE_PIPS:
        raise ShadowRunnerError(f"Slipstream fee_pips {fee_pips} out of range (0, 100_000)")
    return fee_pips


def decode_aero_get_reserves(raw: bytes) -> tuple[int, int, int]:
    """Decode Aerodrome V2 `getReserves()` `(uint256, uint256, uint256)` strictly."""
    if len(raw) != 96:
        raise ShadowRunnerError(f"getReserves() must be 96 bytes, got {len(raw)}")
    reserve0, reserve1, block_ts_last = decode(["uint256", "uint256", "uint256"], raw)
    if reserve0 <= 0 or reserve1 <= 0:
        raise ShadowRunnerError("Aerodrome pool reserves must be positive")
    return int(reserve0), int(reserve1), int(block_ts_last)


def decode_aero_factory_fee_pips(raw: bytes) -> int:
    """Decode `AERO_FACTORY.getFee(pool, False)` in bps and convert `* 100` to pips."""
    if len(raw) != 32:
        raise ShadowRunnerError(f"getFee(address,bool) must be 32 bytes, got {len(raw)}")
    fee_bps = int.from_bytes(raw, "big")
    if not 0 < fee_bps < _MAX_AERO_FEE_BPS:
        raise ShadowRunnerError(f"Aerodrome getFee bps {fee_bps} out of range (0, 1000)")
    return fee_bps * 100


def decode_l1_fee_wei(raw: bytes) -> int:
    """Decode GasPriceOracle `getL1Fee(bytes)` `uint256` output strictly."""
    if len(raw) != 32:
        raise ShadowRunnerError(f"getL1Fee(bytes) must be 32 bytes, got {len(raw)}")
    return _decode_uint_word(raw, _UINT128_LIMIT, "l1_data_fee_wei")


def _wei_to_bps_float(delta_wei: int, notional_wei: int) -> float:
    bps = (Decimal(delta_wei) * Decimal(_BPS_SCALE)) / Decimal(notional_wei)
    return round(float(bps), 6)


def _active_ticks(tick: int, liquidity: int) -> tuple[TickLiquidity, TickLiquidity]:
    lo = tick - ACTIVE_TICK_HALF_WIDTH
    hi = tick + ACTIVE_TICK_HALF_WIDTH
    if lo < MIN_TICK or hi > MAX_TICK or not lo < tick < hi:
        raise ShadowRunnerError(f"tick {tick} too close to TickMath bounds for narrow window")
    return (
        TickLiquidity(tick=lo, liquidity_net=liquidity),
        TickLiquidity(tick=hi, liquidity_net=-liquidity),
    )


def verify_attribution_chain(records: Sequence[Mapping[str, object]]) -> str:
    """Verify hash-chain continuity, sequence numbers, and `execution_eligible=False`."""
    if not records:
        raise ShadowRunnerError("attribution records must be nonempty")
    expected_prev = _GENESIS_PREV_SHA256
    for idx, rec in enumerate(records, start=1):
        if rec.get("seq") != idx:
            raise ShadowRunnerError(f"attribution seq mismatch at index {idx}")
        if rec.get("prev_sha256") != expected_prev:
            raise ShadowRunnerError(f"attribution prev_sha256 mismatch at seq {idx}")
        if rec.get("execution_eligible") is not False:
            raise ShadowRunnerError(f"attribution execution_eligible must be False at seq {idx}")
        raw_digest = rec.get("record_sha256")
        if not isinstance(raw_digest, str) or _SHA256_RE.fullmatch(raw_digest) is None:
            raise ShadowRunnerError(f"attribution record_sha256 invalid at seq {idx}")
        body = {k: v for k, v in rec.items() if k != "record_sha256"}
        computed = sha256_hex(canonical_json(body))
        if computed != raw_digest:
            raise ShadowRunnerError(f"attribution record_sha256 tampering detected at seq {idx}")
        expected_prev = raw_digest
    return expected_prev


def build_synthetic_base_rpc_transport(
    *,
    scenario: str = "positive_gross_negative_net",
    base_block_number: int = 57_800_000,
    base_timestamp_sec: int = 1_794_000_000,
    base_fee_per_gas_wei: int = 80_000_000,
    l1_data_fee_wei: int = 5_000_000_000_000,
    corrupt_basetime: str | None = None,
) -> Callable[[str, bytes, float], bytes]:
    """Build deterministic offline Base JSON-RPC transport across all 4 WETH/USDC pools."""
    if scenario not in {"positive_gross_negative_net", "dislocated", "aligned"}:
        raise ValueError(f"unsupported synthetic scenario: {scenario!r}")

    pools = canonical_base_weth_usdc_pools()
    v3_spec, aero_spec, ss1_spec, ss2_spec = pools
    get_fee_calldata = (
        GET_FEE_SELECTOR + encode(["address", "bool"], [aero_spec.address, False]).hex()
    )

    if scenario == "aligned":
        tick_v3 = -196_258
        tick_ss1 = -196_258
        tick_ss2 = -196_258
        aero_usdc = 3_000_000 * 10**6
        active_base_fee = base_fee_per_gas_wei
        active_l1_fee = l1_data_fee_wei
    elif scenario == "positive_gross_negative_net":
        tick_v3 = -196_258
        tick_ss1 = -196_258
        # +4 ticks (~+4 bps) vs 2 bps Slipstream round-trip fee => ~+2 bps gross
        tick_ss2 = -196_254
        aero_usdc = 3_000_000 * 10**6
        active_base_fee = base_fee_per_gas_wei  # 500k * 82M = 4.1e13 wei (~4.1 bps on 0.1 WETH)
        active_l1_fee = l1_data_fee_wei  # +0.5 bps => ~4.6 bps total gas > +2 bps gross
    else:
        tick_v3 = -196_258
        tick_ss1 = -196_258
        tick_ss2 = -196_138  # +120 ticks (~+120 bps) well inside +/-200 window & > gas
        aero_usdc = 3_000_000 * 10**6
        active_base_fee = 10_000_000
        active_l1_fee = 1_000_000_000_000

    cl_liquidity = 200_000_000_000_000_000  # 2e17 -> realistic depth without exhausting +/-200
    aero_weth = 1_000 * 10**18
    tick_counter = {"n": 0}

    def _u256_hex(val: int) -> str:
        return "0x" + val.to_bytes(32, "big").hex()

    def _slot0_v3_hex(tick_val: int) -> str:
        sqrt_p = sqrt_price_at_tick(tick_val)
        return (
            "0x"
            + encode(
                ["uint160", "int24", "uint16", "uint16", "uint16", "uint8", "bool"],
                [sqrt_p, tick_val, 0, 1, 1, 0, True],
            ).hex()
        )

    def _slot0_ss_hex(tick_val: int) -> str:
        sqrt_p = sqrt_price_at_tick(tick_val)
        return (
            "0x"
            + encode(
                ["uint160", "int24", "uint16", "uint16", "uint16", "bool"],
                [sqrt_p, tick_val, 0, 1, 1, True],
            ).hex()
        )

    def _transport(_url: str, payload: bytes, _timeout_s: float) -> bytes:
        decoded = json.loads(payload.decode("utf-8"))
        if isinstance(decoded, dict):
            method = decoded.get("method")
            if method != "eth_getBlockByNumber":
                raise ValueError(f"unexpected single RPC method: {method!r}")
            idx = tick_counter["n"]
            tick_counter["n"] = idx + 1
            block_num = base_block_number + idx * 6
            ts_ms = (base_timestamp_sec + idx) * 1000 + (idx * 200) % 1000
            ts_sec = ts_ms // 1000
            block_hash = "0x" + sha256_hex(f"base-synth-block-{block_num}".encode())
            result: dict[str, object] = {
                "number": hex(block_num),
                "hash": block_hash,
                "timestamp": hex(ts_sec),
                "baseFeePerGas": hex(active_base_fee),
            }
            if corrupt_basetime != "missing_header_ms":
                result["timestampMs"] = hex(ts_ms)
            return json.dumps({"jsonrpc": "2.0", "id": decoded["id"], "result": result}).encode(
                "utf-8"
            )

        if not isinstance(decoded, list):
            raise ValueError("unexpected synthetic RPC payload shape")

        responses: list[dict[str, object]] = []
        for req in decoded:
            req_id = req["id"]
            method = req["method"]
            if method == "eth_chainId":
                responses.append({"jsonrpc": "2.0", "id": req_id, "result": hex(BASE_CHAIN_ID)})
                continue
            if method != "eth_call":
                raise ValueError(f"unexpected batch RPC method: {method!r}")
            call_obj, block_hex = req["params"]
            block_num = int(block_hex, 16)
            idx = (block_num - base_block_number) // 6
            ts_ms = (base_timestamp_sec + idx) * 1000 + (idx * 200) % 1000
            millis_part = ts_ms % 1000
            to_addr = normalize_address(call_obj["to"])
            data = call_obj["data"].lower()

            if to_addr == v3_spec.address and data == SLOT0_SELECTOR:
                res_hex = _slot0_v3_hex(tick_v3)
            elif to_addr == v3_spec.address and data == LIQUIDITY_SELECTOR:
                res_hex = _u256_hex(cl_liquidity)
            elif to_addr == aero_spec.address and data == GET_RESERVES_SELECTOR:
                res_hex = (
                    "0x"
                    + encode(
                        ["uint256", "uint256", "uint256"],
                        [aero_weth, aero_usdc, ts_ms // 1000],
                    ).hex()
                )
            elif to_addr == aero_spec.factory and data == get_fee_calldata:
                res_hex = _u256_hex(30)  # 30 bps => 3000 pips
            elif to_addr == ss1_spec.address and data == SLOT0_SELECTOR:
                res_hex = _slot0_ss_hex(tick_ss1)
            elif to_addr == ss1_spec.address and data == LIQUIDITY_SELECTOR:
                res_hex = _u256_hex(cl_liquidity)
            elif to_addr == ss1_spec.address and data == FEE_SELECTOR:
                res_hex = _u256_hex(100)
            elif to_addr == ss2_spec.address and data == SLOT0_SELECTOR:
                res_hex = _slot0_ss_hex(tick_ss2)
            elif to_addr == ss2_spec.address and data == LIQUIDITY_SELECTOR:
                res_hex = _u256_hex(cl_liquidity)
            elif to_addr == ss2_spec.address and data == FEE_SELECTOR:
                res_hex = _u256_hex(100)
            elif to_addr == ORACLE and data == GET_L1_FEE_CALLDATA:
                res_hex = _u256_hex(active_l1_fee)
            elif to_addr == BASE_TIME and data == TIMESTAMP_MS_SELECTOR:
                ts_val = ts_ms + 1000 if corrupt_basetime == "second_mismatch" else ts_ms
                if corrupt_basetime == "header_mismatch":
                    ts_val = ts_ms + 200
                res_hex = _u256_hex(ts_val)
            elif to_addr == BASE_TIME and data == TIMESTAMP_MILLIS_PART_SELECTOR:
                part_val = (
                    (millis_part + 1) % 1000
                    if corrupt_basetime == "millis_mismatch"
                    else millis_part
                )
                res_hex = _u256_hex(part_val)
            else:
                raise ValueError(f"unexpected synthetic eth_call target={to_addr} data={data}")
            responses.append({"jsonrpc": "2.0", "id": req_id, "result": res_hex})
        return json.dumps(responses).encode("utf-8")

    return _transport


def build_synthetic_uniswapx_transport(
    orders: Sequence[Mapping[str, object]] = (),
) -> Callable[[str, float], bytes]:
    """Build deterministic offline UniswapX `/v2/orders` GET transport."""
    payload = canonical_json({"orders": list(orders)})

    def _transport(_url: str, _timeout_s: float) -> bytes:
        return payload

    return _transport


def _ensure_clean_output_dir(out_dir: Path) -> tuple[Path, Path]:
    if out_dir.is_symlink():
        raise ShadowRunnerError(f"refusing symlink output directory: {out_dir}")
    if out_dir.exists() and not out_dir.is_dir():
        raise ShadowRunnerError(f"output path is not a directory: {out_dir}")
    attribution_path = out_dir / "shadow_attribution.jsonl"
    summary_path = out_dir / "shadow_summary.json"
    for target in (attribution_path, summary_path):
        if target.is_symlink():
            raise ShadowRunnerError(f"refusing symlink output file: {target}")
        if target.exists():
            raise FileExistsError(f"refusing to overwrite existing output file: {target}")
    out_dir.mkdir(parents=True, exist_ok=True)
    return attribution_path, summary_path


def _write_new_bytes(path: Path, payload: bytes) -> None:
    if path.is_symlink():
        raise ShadowRunnerError(f"refusing symlink output path: {path}")
    with path.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _evaluate_tick_pools(
    specs: tuple[BasePoolSpec, ...],
    batch_results: Mapping[int, object],
    *,
    block_number: int,
    timestamp_ms: int,
) -> tuple[tuple[PoolNode, ...], list[dict[str, object]]]:
    v3_spec, aero_spec, ss1_spec, ss2_spec = specs

    v3_sqrt, v3_tick = decode_slot0_words(
        _decode_call_bytes(batch_results[3], "v3.slot0", expected_lengths=(192, 224))
    )
    v3_liq = decode_liquidity_word(
        _decode_call_bytes(batch_results[4], "v3.liquidity", expected_lengths=(32,))
    )
    if v3_spec.fixed_fee_pips is None:
        raise ShadowRunnerError("V3 spec missing fixed_fee_pips")
    v3_node = PoolNode(
        chain_id=BASE_CHAIN_ID,
        pool_id=v3_spec.address,
        kind=PoolKind.V3,
        token0=v3_spec.token0,
        token1=v3_spec.token1,
        decimals0=v3_spec.decimals0,
        decimals1=v3_spec.decimals1,
        fee_pips=v3_spec.fixed_fee_pips,
        sqrt_price_x96=v3_sqrt,
        tick=v3_tick,
        liquidity=v3_liq,
        ticks=_active_ticks(v3_tick, v3_liq),
        block_number=block_number,
        timestamp_ms=timestamp_ms,
    )

    aero_r0, aero_r1, aero_ts_last = decode_aero_get_reserves(
        _decode_call_bytes(batch_results[5], "aero.getReserves", expected_lengths=(96,))
    )
    aero_fee_pips = decode_aero_factory_fee_pips(
        _decode_call_bytes(batch_results[6], "aero_factory.getFee", expected_lengths=(32,))
    )
    aero_node = PoolNode(
        chain_id=BASE_CHAIN_ID,
        pool_id=aero_spec.address,
        kind=PoolKind.V2,
        token0=aero_spec.token0,
        token1=aero_spec.token1,
        decimals0=aero_spec.decimals0,
        decimals1=aero_spec.decimals1,
        fee_pips=aero_fee_pips,
        reserve0=aero_r0,
        reserve1=aero_r1,
        block_number=block_number,
        timestamp_ms=timestamp_ms,
    )

    ss1_sqrt, ss1_tick = decode_slot0_words(
        _decode_call_bytes(batch_results[7], "slipstream1.slot0", expected_lengths=(192, 224))
    )
    ss1_liq = decode_liquidity_word(
        _decode_call_bytes(batch_results[8], "slipstream1.liquidity", expected_lengths=(32,))
    )
    ss1_fee = decode_slipstream_fee_pips(
        _decode_call_bytes(batch_results[9], "slipstream1.fee", expected_lengths=(32,))
    )
    ss1_node = PoolNode(
        chain_id=BASE_CHAIN_ID,
        pool_id=ss1_spec.address,
        kind=PoolKind.SLIPSTREAM,
        token0=ss1_spec.token0,
        token1=ss1_spec.token1,
        decimals0=ss1_spec.decimals0,
        decimals1=ss1_spec.decimals1,
        fee_pips=ss1_fee,
        sqrt_price_x96=ss1_sqrt,
        tick=ss1_tick,
        liquidity=ss1_liq,
        ticks=_active_ticks(ss1_tick, ss1_liq),
        block_number=block_number,
        timestamp_ms=timestamp_ms,
    )

    ss2_sqrt, ss2_tick = decode_slot0_words(
        _decode_call_bytes(batch_results[10], "slipstream2.slot0", expected_lengths=(192, 224))
    )
    ss2_liq = decode_liquidity_word(
        _decode_call_bytes(batch_results[11], "slipstream2.liquidity", expected_lengths=(32,))
    )
    ss2_fee = decode_slipstream_fee_pips(
        _decode_call_bytes(batch_results[12], "slipstream2.fee", expected_lengths=(32,))
    )
    ss2_node = PoolNode(
        chain_id=BASE_CHAIN_ID,
        pool_id=ss2_spec.address,
        kind=PoolKind.SLIPSTREAM,
        token0=ss2_spec.token0,
        token1=ss2_spec.token1,
        decimals0=ss2_spec.decimals0,
        decimals1=ss2_spec.decimals1,
        fee_pips=ss2_fee,
        sqrt_price_x96=ss2_sqrt,
        tick=ss2_tick,
        liquidity=ss2_liq,
        ticks=_active_ticks(ss2_tick, ss2_liq),
        block_number=block_number,
        timestamp_ms=timestamp_ms,
    )

    pool_snapshots: list[dict[str, object]] = [
        {
            "name": v3_spec.name,
            "pool_id": v3_spec.address,
            "kind": PoolKind.V3.value,
            "fee_pips": v3_spec.fixed_fee_pips,
            "sqrt_price_x96": str(v3_sqrt),
            "tick": v3_tick,
            "liquidity": str(v3_liq),
        },
        {
            "name": aero_spec.name,
            "pool_id": aero_spec.address,
            "kind": PoolKind.V2.value,
            "fee_pips": aero_fee_pips,
            "reserve0_weth_wei": str(aero_r0),
            "reserve1_usdc_raw": str(aero_r1),
            "block_timestamp_last": aero_ts_last,
        },
        {
            "name": ss1_spec.name,
            "pool_id": ss1_spec.address,
            "kind": PoolKind.SLIPSTREAM.value,
            "fee_pips": ss1_fee,
            "sqrt_price_x96": str(ss1_sqrt),
            "tick": ss1_tick,
            "liquidity": str(ss1_liq),
        },
        {
            "name": ss2_spec.name,
            "pool_id": ss2_spec.address,
            "kind": PoolKind.SLIPSTREAM.value,
            "fee_pips": ss2_fee,
            "sqrt_price_x96": str(ss2_sqrt),
            "tick": ss2_tick,
            "liquidity": str(ss2_liq),
        },
    ]
    return (v3_node, aero_node, ss1_node, ss2_node), pool_snapshots


def _measure_pairwise_quotes(
    graph: FastpathPoolGraph,
    specs: tuple[BasePoolSpec, ...],
    nodes: tuple[PoolNode, ...],
    *,
    weth: str,
    usdc: str,
    l2_gas_cost_wei: int,
    l1_data_fee_wei: int,
) -> tuple[int, int, int, int, dict[str, object]]:
    total_gas_wei = l2_gas_cost_wei + l1_data_fee_wei
    name_by_id = {spec.address: spec.name for spec in specs}

    best_gross_delta: int | None = None
    best_net_delta: int | None = None
    best_pair: dict[str, object] | None = None
    evaluated = 0
    failures = 0

    for i, node_i in enumerate(nodes):
        for j, node_j in enumerate(nodes):
            if i == j:
                continue
            try:
                leg1 = graph.quote_pool(BASE_CHAIN_ID, node_i.pool_id, weth, PROBE_AMOUNT_IN_WEI)
                if leg1.amount_out <= 0:
                    failures += 1
                    continue
                leg2 = graph.quote_pool(BASE_CHAIN_ID, node_j.pool_id, usdc, leg1.amount_out)
            except FastpathCallError:
                failures += 1
                continue

            evaluated += 1
            gross_delta_wei = leg2.amount_out - PROBE_AMOUNT_IN_WEI
            net_delta_wei = gross_delta_wei - total_gas_wei
            if best_gross_delta is None or gross_delta_wei > best_gross_delta:
                best_gross_delta = gross_delta_wei
                best_net_delta = net_delta_wei
                best_pair = {
                    "buy_pool": name_by_id[node_i.pool_id],
                    "buy_pool_id": node_i.pool_id,
                    "buy_kind": node_i.kind.value,
                    "sell_pool": name_by_id[node_j.pool_id],
                    "sell_pool_id": node_j.pool_id,
                    "sell_kind": node_j.kind.value,
                    "amount_in_wei": PROBE_AMOUNT_IN_WEI,
                    "intermediate_usdc_raw": leg1.amount_out,
                    "amount_out_wei": leg2.amount_out,
                }

    if best_gross_delta is None or best_net_delta is None or best_pair is None:
        raise ShadowRunnerError("all pairwise pool quotes failed within active tick bounds")
    return evaluated, failures, best_gross_delta, best_net_delta, best_pair


def run_base_arb_shadow(
    *,
    rpc_url: str = DEFAULT_BASE_RPC_URL,
    out_dir: Path,
    mode: str = "public-shadow",
    ticks: int = 3,
    interval_ms: int = 1200,
    max_requests: int = 48,
    priority_fee_wei: int = DEFAULT_PRIORITY_FEE_WEI,
    gas_limit_units: int = DEFAULT_GAS_LIMIT_UNITS,
    skip_uniswapx: bool = False,
    block_tag: str = "latest",
    timeout_s: float = 8.0,
    bridge: FastpathBridge | None = None,
    transport: Callable[[str, bytes, float], bytes] | None = None,
    uniswapx_client: UniswapXOrderbookClient | None = None,
    sleeper: Callable[[float], None] | None = None,
) -> BaseShadowRunResult:
    """Execute bounded multi-tick Base multi-DEX arbitrage & L1/L2 cost shadow attribution."""
    if mode not in {"synthetic", "public-shadow"}:
        raise ShadowRunnerError(f"unsupported shadow mode: {mode!r}")
    if type(ticks) is not int or ticks <= 0:
        raise ShadowRunnerError("ticks must be a positive integer")
    if type(interval_ms) is not int or interval_ms < 0:
        raise ShadowRunnerError("interval_ms must be a nonnegative integer")
    if type(max_requests) is not int or max_requests <= 0:
        raise ShadowRunnerError("max_requests must be a positive integer")
    if type(priority_fee_wei) is not int or priority_fee_wei < 0:
        raise ShadowRunnerError("priority_fee_wei must be a nonnegative integer")
    if type(gas_limit_units) is not int or gas_limit_units <= 0:
        raise ShadowRunnerError("gas_limit_units must be a positive integer")
    if type(skip_uniswapx) is not bool:
        raise ShadowRunnerError("skip_uniswapx must be a boolean")
    if (
        isinstance(timeout_s, bool)
        or not isinstance(timeout_s, (int, float))
        or not math.isfinite(float(timeout_s))
        or float(timeout_s) <= 0
    ):
        raise ShadowRunnerError("timeout_s must be a positive finite number")

    attribution_path, summary_path = _ensure_clean_output_dir(out_dir)
    normalized_tag = _validate_block_tag(block_tag)
    origin, _, _ = _validate_rpc_url(rpc_url, enforce_public_host=transport is None)
    send = _default_https_transport if transport is None else transport
    sleep_fn = time.sleep if sleeper is None else sleeper

    specs = canonical_base_weth_usdc_pools()
    weth = specs[0].token0
    usdc = specs[0].token1
    aero_spec = specs[1]
    ss1_spec = specs[2]
    ss2_spec = specs[3]
    get_fee_calldata = (
        GET_FEE_SELECTOR + encode(["address", "bool"], [aero_spec.address, False]).hex()
    )

    active_bridge = resolve_fastpath_bridge(bridge)
    active_uniswapx: UniswapXOrderbookClient | None = None
    if not skip_uniswapx:
        active_uniswapx = (
            uniswapx_client
            if uniswapx_client is not None
            else UniswapXOrderbookClient(max_requests=ticks, timeout_s=float(timeout_s))
        )

    requests_used = 0

    def _reserve(cost: int) -> None:
        nonlocal requests_used
        if requests_used + cost > max_requests:
            raise ShadowBudgetError(
                f"Base shadow request budget exhausted ({requests_used} + {cost} > {max_requests})"
            )
        requests_used += cost

    records: list[dict[str, Any]] = []
    prev_sha256 = _GENESIS_PREV_SHA256
    prev_block_number: int | None = None
    prev_block_hash: str | None = None
    jsonl_handle = None

    try:
        for tick_idx in range(ticks):
            if tick_idx > 0 and interval_ms > 0:
                sleep_fn(interval_ms / 1000.0)

            if requests_used + RPC_CALLS_PER_TICK > max_requests:
                raise ShadowBudgetError(
                    "Base shadow request budget cannot cover next tick "
                    f"({requests_used} + {RPC_CALLS_PER_TICK} > {max_requests})"
                )

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
            if (
                not isinstance(header_resp_bytes, bytes)
                or len(header_resp_bytes) > MAX_RESPONSE_BYTES
            ):
                raise RpcError("Base shadow header response must be bounded bytes")
            header_resp_sha256 = sha256_hex(header_resp_bytes)

            header_envelope = strict_json_object(header_resp_bytes)
            if (
                header_envelope.get("jsonrpc") != "2.0"
                or type(header_envelope.get("id")) is not int
                or header_envelope["id"] != 1
            ):
                raise ShadowRunnerError("Base shadow header JSON-RPC version or ID mismatch")
            if ("result" in header_envelope) == ("error" in header_envelope):
                raise ShadowRunnerError(
                    "Base shadow header response must contain exactly one result or error"
                )
            if "error" in header_envelope:
                raise RpcError("Base shadow eth_getBlockByNumber returned JSON-RPC error")

            header = strict_object(header_envelope["result"], "block header")
            block_number = rpc_quantity(header.get("number"))
            block_hash = normalize_hash32(
                strict_string(header.get("hash"), "block hash"), "block hash"
            )
            header_timestamp = rpc_quantity(header.get("timestamp"))
            header_timestamp_ms = validate_header_timestamp_ms(header, chain_id=BASE_CHAIN_ID)
            base_fee_per_gas = rpc_quantity(header.get("baseFeePerGas"))

            if prev_block_number is not None:
                if block_number < prev_block_number:
                    raise ShadowRunnerError(
                        f"stale or regressed block number {block_number} < {prev_block_number}"
                    )
                if block_number == prev_block_number and block_hash != prev_block_hash:
                    raise ShadowRunnerError(
                        f"reorg detected at block {block_number}: block hash changed"
                    )

            _reserve(_BATCH_CALL_COUNT)
            pinned_block = hex(block_number)
            batch_payload = [
                {"jsonrpc": "2.0", "id": 2, "method": "eth_chainId", "params": []},
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "eth_call",
                    "params": [{"to": specs[0].address, "data": SLOT0_SELECTOR}, pinned_block],
                },
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "eth_call",
                    "params": [{"to": specs[0].address, "data": LIQUIDITY_SELECTOR}, pinned_block],
                },
                {
                    "jsonrpc": "2.0",
                    "id": 5,
                    "method": "eth_call",
                    "params": [
                        {"to": aero_spec.address, "data": GET_RESERVES_SELECTOR},
                        pinned_block,
                    ],
                },
                {
                    "jsonrpc": "2.0",
                    "id": 6,
                    "method": "eth_call",
                    "params": [
                        {"to": aero_spec.factory, "data": get_fee_calldata},
                        pinned_block,
                    ],
                },
                {
                    "jsonrpc": "2.0",
                    "id": 7,
                    "method": "eth_call",
                    "params": [{"to": ss1_spec.address, "data": SLOT0_SELECTOR}, pinned_block],
                },
                {
                    "jsonrpc": "2.0",
                    "id": 8,
                    "method": "eth_call",
                    "params": [{"to": ss1_spec.address, "data": LIQUIDITY_SELECTOR}, pinned_block],
                },
                {
                    "jsonrpc": "2.0",
                    "id": 9,
                    "method": "eth_call",
                    "params": [{"to": ss1_spec.address, "data": FEE_SELECTOR}, pinned_block],
                },
                {
                    "jsonrpc": "2.0",
                    "id": 10,
                    "method": "eth_call",
                    "params": [{"to": ss2_spec.address, "data": SLOT0_SELECTOR}, pinned_block],
                },
                {
                    "jsonrpc": "2.0",
                    "id": 11,
                    "method": "eth_call",
                    "params": [{"to": ss2_spec.address, "data": LIQUIDITY_SELECTOR}, pinned_block],
                },
                {
                    "jsonrpc": "2.0",
                    "id": 12,
                    "method": "eth_call",
                    "params": [{"to": ss2_spec.address, "data": FEE_SELECTOR}, pinned_block],
                },
                {
                    "jsonrpc": "2.0",
                    "id": 13,
                    "method": "eth_call",
                    "params": [{"to": ORACLE, "data": GET_L1_FEE_CALLDATA}, pinned_block],
                },
                {
                    "jsonrpc": "2.0",
                    "id": 14,
                    "method": "eth_call",
                    "params": [{"to": BASE_TIME, "data": TIMESTAMP_MS_SELECTOR}, pinned_block],
                },
                {
                    "jsonrpc": "2.0",
                    "id": 15,
                    "method": "eth_call",
                    "params": [
                        {"to": BASE_TIME, "data": TIMESTAMP_MILLIS_PART_SELECTOR},
                        pinned_block,
                    ],
                },
            ]
            batch_req_bytes = json.dumps(batch_payload, separators=(",", ":")).encode("utf-8")
            batch_req_sha256 = sha256_hex(batch_req_bytes)
            batch_resp_bytes = send(rpc_url, batch_req_bytes, float(timeout_s))
            if (
                not isinstance(batch_resp_bytes, bytes)
                or len(batch_resp_bytes) > MAX_RESPONSE_BYTES
            ):
                raise RpcError("Base shadow batch response must be bounded bytes")
            batch_resp_sha256 = sha256_hex(batch_resp_bytes)

            batch_results = _strict_batch_response(batch_resp_bytes, tuple(range(2, 16)))

            observed_chain_id = rpc_quantity(batch_results[2])
            if observed_chain_id != BASE_CHAIN_ID:
                raise ShadowRunnerError(
                    f"chain_id mismatch: expected {BASE_CHAIN_ID}, got {observed_chain_id}"
                )

            predeploy_ts_ms = int.from_bytes(
                _decode_call_bytes(batch_results[14], "timestampMs", expected_lengths=(32,)),
                "big",
            )
            predeploy_millis_part = int.from_bytes(
                _decode_call_bytes(
                    batch_results[15], "timestampMillisPart", expected_lengths=(32,)
                ),
                "big",
            )
            checked_ms = parse_base_time(
                timestamp_ms=predeploy_ts_ms,
                millis_part=predeploy_millis_part,
                header_timestamp=header_timestamp,
            )
            if header_timestamp_ms is not None and header_timestamp_ms != checked_ms:
                raise ShadowRunnerError("base_time_header_timestamp_ms_mismatch")

            l1_data_fee_wei = decode_l1_fee_wei(
                _decode_call_bytes(batch_results[13], "oracle.getL1Fee", expected_lengths=(32,))
            )
            l2_gas_cost_wei = gas_limit_units * (base_fee_per_gas + priority_fee_wei)
            total_gas_cost_wei = l2_gas_cost_wei + l1_data_fee_wei

            nodes, pool_snapshots = _evaluate_tick_pools(
                specs,
                batch_results,
                block_number=block_number,
                timestamp_ms=checked_ms,
            )

            graph = FastpathPoolGraph(bridge=active_bridge)
            for node in nodes:
                graph.upsert_pool(node)

            scan_report = graph.scan_cycles(
                [weth],
                max_hops=2,
                min_amount_in=SCAN_MIN_AMOUNT_IN_WEI,
                max_amount_in=SCAN_MAX_AMOUNT_IN_WEI,
                gas_cost_in_base=l2_gas_cost_wei,
                l1_fee_in_base=l1_data_fee_wei,
                min_net_profit=1,
                block_number=block_number,
                timestamp_ns=checked_ms * 1_000_000,
            )

            pair_eval, pair_fail, best_gross_wei, best_net_wei, best_pair = (
                _measure_pairwise_quotes(
                    graph,
                    specs,
                    nodes,
                    weth=weth,
                    usdc=usdc,
                    l2_gas_cost_wei=l2_gas_cost_wei,
                    l1_data_fee_wei=l1_data_fee_wei,
                )
            )
            best_gross_spread_bps = _wei_to_bps_float(best_gross_wei, PROBE_AMOUNT_IN_WEI)
            best_net_spread_bps = _wei_to_bps_float(best_net_wei, PROBE_AMOUNT_IN_WEI)

            uniswapx_status = "skipped"
            uniswapx_open_orders: int | None = None
            uniswapx_raw_sha256: str | None = None
            uniswapx_error: str | None = None
            if active_uniswapx is not None:
                if requests_used + 1 > max_requests:
                    uniswapx_status = "budget_exhausted"
                    uniswapx_error = "request_budget_exhausted"
                else:
                    requests_used += 1
                    try:
                        ux_result = active_uniswapx.poll_open_orders(
                            limit=25,
                            polled_at_ms=checked_ms,
                        )
                        uniswapx_status = "ok"
                        uniswapx_open_orders = len(ux_result.orders)
                        uniswapx_raw_sha256 = ux_result.raw_sha256
                    except (UniswapXClientError, OSError, ValueError) as exc:
                        uniswapx_status = "error"
                        uniswapx_error = str(exc)

            top_cand_profit = (
                scan_report.candidates[0].net_profit if scan_report.candidates else None
            )
            seq = tick_idx + 1
            record_body: dict[str, Any] = {
                "schema_version": "oasis-base-arb-shadow-record-v1",
                "seq": seq,
                "prev_sha256": prev_sha256,
                "execution_eligible": False,
                "mode": mode,
                "chain_id": BASE_CHAIN_ID,
                "rpc_origin": origin,
                "block_number": block_number,
                "block_hash": block_hash,
                "header_timestamp": header_timestamp,
                "header_timestamp_ms": header_timestamp_ms,
                "base_time_timestamp_ms": checked_ms,
                "base_time_millis_part": predeploy_millis_part,
                "base_fee_per_gas_wei": base_fee_per_gas,
                "priority_fee_wei": priority_fee_wei,
                "gas_limit_units": gas_limit_units,
                "l2_gas_cost_wei": l2_gas_cost_wei,
                "l1_data_fee_wei": l1_data_fee_wei,
                "total_gas_cost_wei": total_gas_cost_wei,
                "probe_amount_in_wei": PROBE_AMOUNT_IN_WEI,
                "pools": pool_snapshots,
                "rust_scan_elapsed_ns": scan_report.elapsed_ns,
                "cycles_evaluated": scan_report.cycles_evaluated,
                "candidates_found": len(scan_report.candidates),
                "top_candidate_net_profit_wei": top_cand_profit,
                "pairwise_quotes_evaluated": pair_eval,
                "pairwise_quote_failures": pair_fail,
                "best_pair": best_pair,
                "best_gross_delta_wei": best_gross_wei,
                "best_gross_spread_bps": best_gross_spread_bps,
                "best_net_delta_wei": best_net_wei,
                "best_net_spread_bps": best_net_spread_bps,
                "uniswapx_status": uniswapx_status,
                "uniswapx_open_orders": uniswapx_open_orders,
                "uniswapx_raw_sha256": uniswapx_raw_sha256,
                "uniswapx_error": uniswapx_error,
                "header_request_sha256": header_req_sha256,
                "header_response_sha256": header_resp_sha256,
                "batch_request_sha256": batch_req_sha256,
                "batch_response_sha256": batch_resp_sha256,
            }
            record_sha256 = sha256_hex(canonical_json(record_body))
            record = {**record_body, "record_sha256": record_sha256}
            records.append(record)

            if jsonl_handle is None:
                if attribution_path.is_symlink():
                    raise ShadowRunnerError(f"refusing symlink output path: {attribution_path}")
                jsonl_handle = attribution_path.open("xb")
            jsonl_handle.write(canonical_json(record) + b"\n")
            jsonl_handle.flush()
            os.fsync(jsonl_handle.fileno())

            prev_sha256 = record_sha256
            prev_block_number = block_number
            prev_block_hash = block_hash
    finally:
        if jsonl_handle is not None:
            jsonl_handle.close()

    chain_head_sha256 = verify_attribution_chain(records)
    gross_bps_list = [cast(float, rec["best_gross_spread_bps"]) for rec in records]
    net_bps_list = [cast(float, rec["best_net_spread_bps"]) for rec in records]
    scan_ns_list = [cast(int, rec["rust_scan_elapsed_ns"]) for rec in records]
    positive_gross_ticks = sum(1 for rec in records if cast(int, rec["best_gross_delta_wei"]) > 0)
    positive_net_ticks = sum(1 for rec in records if cast(int, rec["best_net_delta_wei"]) > 0)

    n_ticks = len(records)
    mean_net_raw = sum(net_bps_list) / n_ticks
    if n_ticks >= 2:
        variance = sum((x - mean_net_raw) ** 2 for x in net_bps_list) / (n_ticks - 1)
        std_error = math.sqrt(variance / n_ticks)
        lcb_net_edge_bps = round(mean_net_raw - _LCB_Z_SCORE * std_error, 6)
    else:
        lcb_net_edge_bps = round(mean_net_raw, 6)

    summary_body: dict[str, Any] = {
        "schema_version": "oasis-base-arb-shadow-summary-v1",
        "mode": mode,
        "chain_id": BASE_CHAIN_ID,
        "rpc_origin": origin,
        "ticks_completed": n_ticks,
        "requests_used": requests_used,
        "requests_budget": max_requests,
        "positive_gross_ticks": positive_gross_ticks,
        "positive_net_ticks": positive_net_ticks,
        "best_gross_spread_bps": max(gross_bps_list),
        "best_net_spread_bps": max(net_bps_list),
        "mean_net_spread_bps": round(mean_net_raw, 6),
        "lcb_net_edge_bps": lcb_net_edge_bps,
        "mean_rust_scan_ns": round(sum(scan_ns_list) / n_ticks, 2),
        "total_candidates_found": sum(cast(int, rec["candidates_found"]) for rec in records),
        "max_gross_delta_wei": max(cast(int, rec["best_gross_delta_wei"]) for rec in records),
        "max_net_delta_wei": max(cast(int, rec["best_net_delta_wei"]) for rec in records),
        "probe_amount_in_wei": PROBE_AMOUNT_IN_WEI,
        "gas_limit_units": gas_limit_units,
        "priority_fee_wei": priority_fee_wei,
        "first_block_number": records[0]["block_number"],
        "last_block_number": records[-1]["block_number"],
        "chain_head_sha256": chain_head_sha256,
        "execution_eligible": False,
    }
    summary = {**summary_body, "summary_sha256": sha256_hex(canonical_json(summary_body))}
    _write_new_bytes(summary_path, canonical_json(summary) + b"\n")

    return BaseShadowRunResult(
        summary=summary,
        records=tuple(records),
        summary_path=summary_path,
        attribution_path=attribution_path,
    )


__all__ = [
    "ACTIVE_TICK_HALF_WIDTH",
    "DEFAULT_BASE_RPC_URL",
    "DEFAULT_GAS_LIMIT_UNITS",
    "DEFAULT_PRIORITY_FEE_WEI",
    "FEE_SELECTOR",
    "GET_FEE_SELECTOR",
    "GET_L1_FEE_CALLDATA",
    "GET_L1_FEE_SELECTOR",
    "GET_RESERVES_SELECTOR",
    "LIQUIDITY_SELECTOR",
    "MAX_RESPONSE_BYTES",
    "PROBE_AMOUNT_IN_WEI",
    "RPC_CALLS_PER_TICK",
    "SCAN_MAX_AMOUNT_IN_WEI",
    "SCAN_MIN_AMOUNT_IN_WEI",
    "SLOT0_SELECTOR",
    "BasePoolSpec",
    "BaseShadowRunResult",
    "ShadowBudgetError",
    "ShadowRunnerError",
    "build_synthetic_base_rpc_transport",
    "build_synthetic_uniswapx_transport",
    "canonical_base_weth_usdc_pools",
    "decode_aero_factory_fee_pips",
    "decode_aero_get_reserves",
    "decode_l1_fee_wei",
    "decode_liquidity_word",
    "decode_slipstream_fee_pips",
    "decode_slot0_words",
    "resolve_fastpath_bridge",
    "run_base_arb_shadow",
    "verify_attribution_chain",
]
