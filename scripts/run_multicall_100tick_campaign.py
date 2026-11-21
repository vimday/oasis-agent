"""100-tick unified Base Multicall3 atomic DEX + Coinbase CEX-DEX shadow campaign CLI.

Runs bounded, read-only, keyless shadow attribution across Base mainnet DEX pools
(via `oasis_agent.fastpath.multicall_universe` when available, or canonical
`Multicall3` WETH/USDC fallback), scans WETH-anchored and USDC-anchored up-to-3-hop
cycles in the Rust `FastpathPoolGraph`, verifies positive Rust candidates against
on-chain Quoters to eliminate phantom profits, evaluates Coinbase <-> Base L2 VWAP
lead-lag on WETH/USDC (and cbBTC/USDC, AERO/USDC when present), and emits a
hash-chained `shadow_attribution.jsonl` plus `canary_readiness`-compatible
`shadow_summary.json` (`execution_eligible = False`).
"""

import argparse
import concurrent.futures
import http.client
import importlib
import inspect
import json
import math
import os
import re
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, Protocol, cast, runtime_checkable

import certifi
from eth_abi.abi import decode, encode
from eth_hash.auto import keccak

from oasis_agent.arbitrage.event_quote import GAS_LIMIT_MODEL, ORACLE, V3
from oasis_agent.cross_venue.bybit_spot_feed import (
    BYBIT_SYMBOL_BY_PRODUCT,
    BybitSpotFeedError,
    BybitSpotWebSocketFeed,
    UsdtUsdPeg,
    fetch_bybit_l2_book,
    fetch_bybit_recent_trades,
    fetch_usdt_usd_peg,
)
from oasis_agent.cross_venue.coinbase_trade_flow import (
    CoinbaseTradeFlowError,
    CoinbaseTradePrint,
    CoinbaseWebSocketFeed,
    _validate_coinbase_trades_url,
    fetch_coinbase_recent_trades,
)
from oasis_agent.cross_venue.live_cex_dex_shadow import (
    DEFAULT_CEX_TAKER_FEE_BPS,
    DEFAULT_LATENCY_HAIRCUT_BPS,
    DEFAULT_TRADE_NOTIONAL_USDC,
    CexDexTickEvaluation,
    CoinbaseL2Book,
    LiveCexDexError,
    _validate_coinbase_url,
    build_synthetic_coinbase_transport,
    compute_coinbase_book_capacity_usdc,
    evaluate_dynamic_cex_dex_opportunity,
    evaluate_live_cex_dex_shadow,
    fetch_coinbase_l2_book,
    gas_wei_to_usd,
    resolve_coinbase_product,
)
from oasis_agent.cross_venue.live_cex_dex_shadow import (
    MAX_RESPONSE_BYTES as CB_MAX_RESPONSE_BYTES,
)
from oasis_agent.cross_venue.live_cex_dex_shadow import (
    _validate_resolved_dns as _validate_cb_dns,
)
from oasis_agent.cross_venue.maker_hedge_shadow import (
    MakerFillEvent,
    MakerHedgeConfig,
    MakerHedgeLifecycleTracker,
    build_maker_quote_candidate,
)
from oasis_agent.cross_venue.oos_execution_tracker import (
    OosStrategyExecutionTracker,
    OosTrackerConfig,
)
from oasis_agent.errors import RpcError
from oasis_agent.evidence.base_denim import (
    BASE_CHAIN_ID,
    BASE_TIME,
    TIMESTAMP_MILLIS_PART_SELECTOR,
    TIMESTAMP_MS_SELECTOR,
    parse_base_time,
    validate_header_timestamp_ms,
)
from oasis_agent.evidence.rpc_validation import (
    rpc_quantity,
    strict_json_object,
    strict_object,
    strict_string,
)
from oasis_agent.fastpath.bridge import FastpathBridge, FastpathCallError, FastpathUnavailableError
from oasis_agent.fastpath.live_shadow_runner import (
    _USER_AGENT,
    ACTIVE_TICK_HALF_WIDTH,
    DEFAULT_BASE_RPC_URL,
    DEFAULT_PRIORITY_FEE_WEI,
    FEE_SELECTOR,
    GET_FEE_SELECTOR,
    GET_L1_FEE_CALLDATA,
    GET_RESERVES_SELECTOR,
    LIQUIDITY_SELECTOR,
    MAX_RESPONSE_BYTES,
    PROBE_AMOUNT_IN_WEI,
    SLOT0_SELECTOR,
    BasePoolSpec,
    ShadowBudgetError,
    ShadowRunnerError,
    _validate_block_tag,
    _validate_rpc_url,
    canonical_base_weth_usdc_pools,
    decode_aero_factory_fee_pips,
    decode_aero_get_reserves,
    decode_l1_fee_wei,
    decode_liquidity_word,
    decode_slipstream_fee_pips,
    decode_slot0_words,
    resolve_fastpath_bridge,
)
from oasis_agent.fastpath.live_shadow_runner import (
    _validate_resolved_dns as _validate_rpc_dns,
)
from oasis_agent.fastpath.multicall_universe import (
    CBETH_TOKEN,
    DEGEN_TOKEN,
    MORPHO_TOKEN,
    VIRTUAL_TOKEN,
    VVV_TOKEN,
    WELL_TOKEN,
    _IPv4PreferredHTTPSConnection,
)
from oasis_agent.fastpath.pool_graph import (
    CycleCandidate,
    FastpathPoolGraph,
    PoolKind,
    PoolNode,
    TickLiquidity,
)
from oasis_agent.models.common import (
    canonical_json,
    normalize_address,
    normalize_hash32,
    sha256_hex,
)
from oasis_agent.risk.capital_policy import (
    AccountCapitalSnapshot,
    CapitalRiskConfig,
    HeadroomAllocation,
    compute_deployable_headroom,
    load_capital_risk_config,
    solve_optimal_notional,
)
from oasis_agent.sniping.venues import (
    SLIPSTREAM,
    SLIPSTREAM_USDC_WETH,
    USDC,
    WETH,
)
from oasis_agent.uniswap_v4.liquidity_math import MAX_TICK, MIN_TICK, sqrt_price_at_tick


def _addr20(hi80: int, lo80: int) -> str:
    return f"0x{hi80:020x}{lo80:020x}"


MULTICALL3: Final[str] = _addr20(954245306222959992705383, 11960605330504318110225)
CBBTC: Final[str] = _addr20(962029985257869835451167, 429691464623657203872703)
AERO_TOKEN: Final[str] = _addr20(698938029289277408845381, 196638687667187760072241)
WSTETH: Final[str] = _addr20(915173236817837272783139, 736850604702865302676562)

WETH_ADDR: Final[str] = normalize_address(WETH)
USDC_ADDR: Final[str] = normalize_address(USDC)
CBBTC_ADDR: Final[str] = normalize_address(CBBTC)
AERO_TOKEN_ADDR: Final[str] = normalize_address(AERO_TOKEN)
WSTETH_ADDR: Final[str] = normalize_address(WSTETH)
VIRTUAL_ADDR: Final[str] = normalize_address(VIRTUAL_TOKEN)
VVV_ADDR: Final[str] = normalize_address(VVV_TOKEN)
MORPHO_ADDR: Final[str] = normalize_address(MORPHO_TOKEN)
DEGEN_ADDR: Final[str] = normalize_address(DEGEN_TOKEN)
WELL_ADDR: Final[str] = normalize_address(WELL_TOKEN)
CBETH_ADDR: Final[str] = normalize_address(CBETH_TOKEN)

SUMMARY_SCHEMA_VERSION: Final[str] = "oasis.base-multicall-shadow-summary.v1"
RECORD_SCHEMA_VERSION: Final[str] = "oasis.base-multicall-shadow-record.v1"
DEFAULT_TICKS: Final[int] = 100
DEFAULT_INTERVAL_MS: Final[int] = 800
DEFAULT_MAX_RPC_CALLS: Final[int] = 350
DEFAULT_TIMEOUT_S: Final[float] = 15.0
WETH_SCAN_MIN_WEI: Final[int] = 10**15
WETH_SCAN_MAX_WEI: Final[int] = 10**18
USDC_SCAN_MIN_RAW: Final[int] = 5_000_000
USDC_SCAN_MAX_RAW: Final[int] = 2_000_000_000
MAX_CANDIDATES_TO_QUOTE_PER_TICK: Final[int] = 3
_LCB_Z_SCORE: Final[float] = 1.96
_GENESIS_PREV_SHA256: Final[str] = "0" * 64
_SHA256_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")
_HEX_BYTES_RE: Final[re.Pattern[str]] = re.compile(r"^0x(?:[0-9a-fA-F]{2})*$")


def _selector(signature: str) -> str:
    return "0x" + keccak(signature.encode("ascii"))[:4].hex()


AGGREGATE3_SELECTOR: Final[str] = _selector("aggregate3((address,bool,bytes)[])")
MULTICALL3_GET_CHAIN_ID_SELECTOR: Final[str] = _selector("getChainId()")
AERO_POOL_GET_AMOUNT_OUT_SELECTOR: Final[str] = _selector("getAmountOut(uint256,address)")
V3_QUOTER_SINGLE_SELECTOR: Final[str] = _selector(
    "quoteExactInputSingle((address,address,uint256,uint24,uint160))"
)
SLIPSTREAM_QUOTER_SINGLE_SELECTOR: Final[str] = _selector(
    "quoteExactInputSingle((address,address,uint256,int24,uint160))"
)


@dataclass(frozen=True, slots=True)
class TokenMeta:
    """Base token metadata and optional Coinbase Exchange product mapping."""

    address: str
    symbol: str
    decimals: int
    coinbase_product: str | None


TOKEN_META_BY_ADDR: Final[dict[str, TokenMeta]] = {
    WETH_ADDR: TokenMeta(WETH_ADDR, "WETH", 18, "ETH-USD"),
    USDC_ADDR: TokenMeta(USDC_ADDR, "USDC", 6, None),
    CBBTC_ADDR: TokenMeta(CBBTC_ADDR, "cbBTC", 8, "BTC-USD"),
    AERO_TOKEN_ADDR: TokenMeta(AERO_TOKEN_ADDR, "AERO", 18, "AERO-USD"),
    WSTETH_ADDR: TokenMeta(WSTETH_ADDR, "wstETH", 18, None),
    VIRTUAL_ADDR: TokenMeta(VIRTUAL_ADDR, "VIRTUAL", 18, "VIRTUAL-USD"),
    VVV_ADDR: TokenMeta(VVV_ADDR, "VVV", 18, "VVV-USD"),
    MORPHO_ADDR: TokenMeta(MORPHO_ADDR, "MORPHO", 18, "MORPHO-USD"),
    DEGEN_ADDR: TokenMeta(DEGEN_ADDR, "DEGEN", 18, "DEGEN-USD"),
    WELL_ADDR: TokenMeta(WELL_ADDR, "WELL", 18, "WELL-USD"),
    CBETH_ADDR: TokenMeta(CBETH_ADDR, "cbETH", 18, "CBETH-USD"),
}

CEX_DEX_TOKEN_ORDER: Final[tuple[str, ...]] = (
    WETH_ADDR,
    CBBTC_ADDR,
    AERO_TOKEN_ADDR,
    VIRTUAL_ADDR,
    VVV_ADDR,
    MORPHO_ADDR,
    DEGEN_ADDR,
    WELL_ADDR,
    CBETH_ADDR,
)


@runtime_checkable
class MulticallUniverseProtocol(Protocol):
    """Typed dynamic interface for optional `oasis_agent.fastpath.multicall_universe`."""

    discover_base_universe_pools: Callable[..., Any]
    fetch_pinned_universe_snapshot: Callable[..., Any]
    verify_candidate_with_onchain_quoter: Callable[..., Any]


@dataclass(frozen=True, slots=True)
class PinnedTickSnapshot:
    """Normalized pinned-block snapshot produced by either Multicall3 path."""

    block_number: int
    block_hash: str
    header_timestamp: int
    base_time_ms: int
    base_fee_per_gas_wei: int
    l1_data_fee_wei: int
    nodes: tuple[PoolNode, ...]
    rpc_calls_used: int
    balances_by_pool: Mapping[str, tuple[int, int]] | None = None
    parent_hash: str | None = None


@dataclass(frozen=True, slots=True)
class QuoterCheckResult:
    """On-chain Quoter verification verdict for one Rust CycleCandidate."""

    verified: bool
    quoter_amount_out: int
    quoter_net_profit: int
    rpc_calls_used: int
    reason: str


@dataclass(frozen=True, slots=True)
class CampaignRunResult:
    """Completed shadow campaign summary, records, and output artifact paths."""

    summary: dict[str, Any]
    records: tuple[dict[str, Any], ...]
    summary_path: Path
    attribution_path: Path


def _load_multicall_universe_module() -> MulticallUniverseProtocol | None:
    """Dynamically load `oasis_agent.fastpath.multicall_universe` when merged."""
    try:
        mod = importlib.import_module("oasis_agent.fastpath.multicall_universe")
    except ModuleNotFoundError as exc:
        if exc.name in {
            "oasis_agent.fastpath.multicall_universe",
            "multicall_universe",
        }:
            return None
        raise
    if not isinstance(mod, MulticallUniverseProtocol):
        return None
    return mod


def _call_with_supported_kwargs(
    fn: Callable[..., Any],
    candidate_kwargs: Mapping[str, Any],
    *,
    first_positional_fallback: Any = None,
) -> Any:
    sig = inspect.signature(fn)
    params = sig.parameters
    accepts_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
    if accepts_var_kw:
        return fn(**candidate_kwargs)
    filtered = {k: v for k, v in candidate_kwargs.items() if k in params}
    positional: list[Any] = []
    if first_positional_fallback is not None:
        param_list = list(params.values())
        if param_list:
            first = param_list[0]
            if (
                first.kind
                in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
                and first.name not in filtered
                and first.default is inspect.Parameter.empty
            ):
                positional.append(first_positional_fallback)
    return fn(*positional, **filtered)


def _attr_or_key(obj: Any, *names: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        for name in names:
            if name in obj:
                return obj[name]
        return default
    for name in names:
        if hasattr(obj, name):
            return getattr(obj, name)
    return default


def _single_rpc_call(
    send: Callable[[str, bytes, float], bytes],
    rpc_url: str,
    *,
    method: str,
    params: list[object],
    timeout_s: float,
) -> object:
    req_bytes = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        separators=(",", ":"),
    ).encode("utf-8")
    resp_bytes = send(rpc_url, req_bytes, timeout_s)
    if not isinstance(resp_bytes, bytes) or len(resp_bytes) > MAX_RESPONSE_BYTES:
        raise RpcError(f"{method} response must be bounded bytes")
    envelope = strict_json_object(resp_bytes)
    if (
        envelope.get("jsonrpc") != "2.0"
        or type(envelope.get("id")) is not int
        or envelope["id"] != 1
    ):
        raise ShadowRunnerError(f"{method} JSON-RPC version or ID mismatch")
    if ("result" in envelope) == ("error" in envelope):
        raise ShadowRunnerError(f"{method} response must contain exactly one result or error")
    if "error" in envelope:
        raise RpcError(f"{method} returned JSON-RPC error")
    return envelope["result"]


def _decode_hex_bytes(value: object, name: str) -> bytes:
    raw_hex = strict_string(value, name)
    if _HEX_BYTES_RE.fullmatch(raw_hex) is None:
        raise ShadowRunnerError(f"{name} must be 0x-prefixed hex bytes")
    return bytes.fromhex(raw_hex[2:])


def _active_ticks(tick: int, liquidity: int) -> tuple[TickLiquidity, TickLiquidity]:
    lo = tick - ACTIVE_TICK_HALF_WIDTH
    hi = tick + ACTIVE_TICK_HALF_WIDTH
    if lo < MIN_TICK or hi > MAX_TICK or not lo < tick < hi:
        raise ShadowRunnerError(f"tick {tick} outside safe narrow window")
    return (
        TickLiquidity(tick=lo, liquidity_net=liquidity),
        TickLiquidity(tick=hi, liquidity_net=-liquidity),
    )


def _pool_metadata_lookup(
    pool_specs: Sequence[Any],
) -> tuple[dict[str, int], dict[str, int], dict[str, str | None]]:
    fixed_fees: dict[str, int] = {}
    tick_spacings: dict[str, int] = {}
    factories: dict[str, str | None] = {}
    ss_spacing_by_pool = {
        normalize_address(addr): int(spacing) for addr, spacing in SLIPSTREAM_USDC_WETH.values()
    }
    for spec in pool_specs:
        addr_raw = _attr_or_key(spec, "address", "pool_id")
        if not isinstance(addr_raw, str):
            continue
        addr = normalize_address(addr_raw)
        fee_val = _attr_or_key(spec, "fixed_fee_pips", "fee_pips", "fee")
        if type(fee_val) is int and fee_val > 0:
            fixed_fees[addr] = fee_val
        spacing_val = _attr_or_key(spec, "tick_spacing", "tickSpacing")
        if type(spacing_val) is int and spacing_val > 0:
            tick_spacings[addr] = spacing_val
        elif addr in ss_spacing_by_pool:
            tick_spacings[addr] = ss_spacing_by_pool[addr]
        fac_val = _attr_or_key(spec, "factory")
        factories[addr] = normalize_address(fac_val) if isinstance(fac_val, str) else None
    return fixed_fees, tick_spacings, factories


def _build_multicall3_requests(
    pool_specs: Sequence[BasePoolSpec],
) -> tuple[list[tuple[str, bool, bytes]], list[tuple[str, str]]]:
    calls: list[tuple[str, bool, bytes]] = [
        (MULTICALL3, False, bytes.fromhex(MULTICALL3_GET_CHAIN_ID_SELECTOR[2:])),
        (ORACLE, False, bytes.fromhex(GET_L1_FEE_CALLDATA[2:])),
        (BASE_TIME, False, bytes.fromhex(TIMESTAMP_MS_SELECTOR[2:])),
        (BASE_TIME, False, bytes.fromhex(TIMESTAMP_MILLIS_PART_SELECTOR[2:])),
    ]
    tags: list[tuple[str, str]] = [
        ("meta", "chain_id"),
        ("meta", "l1_fee"),
        ("meta", "timestamp_ms"),
        ("meta", "millis_part"),
    ]
    for spec in pool_specs:
        if spec.kind in (PoolKind.V3, PoolKind.SLIPSTREAM):
            calls.append((spec.address, False, bytes.fromhex(SLOT0_SELECTOR[2:])))
            tags.append((spec.address, "slot0"))
            calls.append((spec.address, False, bytes.fromhex(LIQUIDITY_SELECTOR[2:])))
            tags.append((spec.address, "liquidity"))
            if spec.fixed_fee_pips is None:
                calls.append((spec.address, False, bytes.fromhex(FEE_SELECTOR[2:])))
                tags.append((spec.address, "fee"))
        elif spec.kind is PoolKind.V2:
            calls.append((spec.address, False, bytes.fromhex(GET_RESERVES_SELECTOR[2:])))
            tags.append((spec.address, "reserves"))
            if spec.factory is not None:
                fee_cd = GET_FEE_SELECTOR + encode(["address", "bool"], [spec.address, False]).hex()
                calls.append((spec.factory, False, bytes.fromhex(fee_cd[2:])))
                tags.append((spec.address, "aero_fee"))
        else:
            raise ShadowRunnerError(f"unsupported fallback pool kind: {spec.kind}")
    return calls, tags


def _fetch_pinned_multicall3_fallback(
    send: Callable[[str, bytes, float], bytes],
    rpc_url: str,
    pool_specs: Sequence[BasePoolSpec],
    *,
    timeout_s: float,
    block_tag: str = "latest",
) -> PinnedTickSnapshot:
    normalized_tag = _validate_block_tag(block_tag)
    header_raw = _single_rpc_call(
        send,
        rpc_url,
        method="eth_getBlockByNumber",
        params=[normalized_tag, False],
        timeout_s=timeout_s,
    )
    header = strict_object(header_raw, "block header")
    block_number = rpc_quantity(header.get("number"))
    block_hash = normalize_hash32(strict_string(header.get("hash"), "block hash"), "block hash")
    raw_parent = header.get("parentHash")
    parent_hash = (
        normalize_hash32(strict_string(raw_parent, "parentHash"), "parentHash")
        if raw_parent is not None
        else None
    )
    header_timestamp = rpc_quantity(header.get("timestamp"))
    header_ts_ms = validate_header_timestamp_ms(header, chain_id=BASE_CHAIN_ID)
    base_fee_per_gas = rpc_quantity(header.get("baseFeePerGas"))

    calls, tags = _build_multicall3_requests(pool_specs)
    encoded_calls = encode(["(address,bool,bytes)[]"], [calls])
    multicall_data = AGGREGATE3_SELECTOR + encoded_calls.hex()
    raw_mc_resp = _single_rpc_call(
        send,
        rpc_url,
        method="eth_call",
        params=[{"to": MULTICALL3, "data": multicall_data}, hex(block_number)],
        timeout_s=timeout_s,
    )
    mc_bytes = _decode_hex_bytes(raw_mc_resp, "multicall3.aggregate3")
    (decoded_results,) = decode(["(bool,bytes)[]"], mc_bytes)
    if len(decoded_results) != len(tags):
        raise ShadowRunnerError("Multicall3 return count mismatch")

    by_tag: dict[tuple[str, str], bytes] = {}
    for (tag_owner, tag_field), (success, ret_bytes) in zip(tags, decoded_results, strict=True):
        if success is not True:
            raise ShadowRunnerError(f"Multicall3 subcall failed for {tag_owner}.{tag_field}")
        by_tag[(tag_owner, tag_field)] = bytes(ret_bytes)

    chain_raw = by_tag[("meta", "chain_id")]
    if len(chain_raw) != 32 or int.from_bytes(chain_raw, "big") != BASE_CHAIN_ID:
        raise ShadowRunnerError("Multicall3 getChainId mismatch with Base (8453)")

    l1_data_fee_wei = decode_l1_fee_wei(by_tag[("meta", "l1_fee")])
    ts_ms_bytes = by_tag[("meta", "timestamp_ms")]
    millis_bytes = by_tag[("meta", "millis_part")]
    if len(ts_ms_bytes) != 32 or len(millis_bytes) != 32:
        raise ShadowRunnerError("BaseTime predeploy output length mismatch")
    checked_ms = parse_base_time(
        timestamp_ms=int.from_bytes(ts_ms_bytes, "big"),
        millis_part=int.from_bytes(millis_bytes, "big"),
        header_timestamp=header_timestamp,
    )
    if header_ts_ms is not None and header_ts_ms != checked_ms:
        raise ShadowRunnerError("base_time_header_timestamp_ms_mismatch")

    nodes: list[PoolNode] = []
    for spec in pool_specs:
        if spec.kind in (PoolKind.V3, PoolKind.SLIPSTREAM):
            sqrt_p, tick = decode_slot0_words(by_tag[(spec.address, "slot0")])
            liq = decode_liquidity_word(by_tag[(spec.address, "liquidity")])
            if spec.fixed_fee_pips is not None:
                fee_pips = spec.fixed_fee_pips
            else:
                fee_pips = decode_slipstream_fee_pips(by_tag[(spec.address, "fee")])
            nodes.append(
                PoolNode(
                    chain_id=BASE_CHAIN_ID,
                    pool_id=spec.address,
                    kind=spec.kind,
                    token0=spec.token0,
                    token1=spec.token1,
                    decimals0=spec.decimals0,
                    decimals1=spec.decimals1,
                    fee_pips=fee_pips,
                    sqrt_price_x96=sqrt_p,
                    tick=tick,
                    liquidity=liq,
                    ticks=_active_ticks(tick, liq),
                    block_number=block_number,
                    timestamp_ms=checked_ms,
                )
            )
        else:
            r0, r1, _ts_last = decode_aero_get_reserves(by_tag[(spec.address, "reserves")])
            if spec.fixed_fee_pips is not None:
                fee_pips = spec.fixed_fee_pips
            elif spec.factory is not None:
                fee_pips = decode_aero_factory_fee_pips(by_tag[(spec.address, "aero_fee")])
            else:
                fee_pips = 3000
            nodes.append(
                PoolNode(
                    chain_id=BASE_CHAIN_ID,
                    pool_id=spec.address,
                    kind=PoolKind.V2,
                    token0=spec.token0,
                    token1=spec.token1,
                    decimals0=spec.decimals0,
                    decimals1=spec.decimals1,
                    fee_pips=fee_pips,
                    reserve0=r0,
                    reserve1=r1,
                    block_number=block_number,
                    timestamp_ms=checked_ms,
                )
            )

    return PinnedTickSnapshot(
        block_number=block_number,
        block_hash=block_hash,
        header_timestamp=header_timestamp,
        base_time_ms=checked_ms,
        base_fee_per_gas_wei=base_fee_per_gas,
        l1_data_fee_wei=l1_data_fee_wei,
        nodes=tuple(nodes),
        rpc_calls_used=2,
        parent_hash=parent_hash,
    )


def _quote_hop_onchain_fallback(
    send: Callable[[str, bytes, float], bytes],
    rpc_url: str,
    *,
    pool_node: PoolNode,
    token_in: str,
    token_out: str,
    amount_in: int,
    pinned_block_hex: str,
    timeout_s: float,
    tick_spacings: Mapping[str, int],
    factories: Mapping[str, str | None],
) -> int:
    if pool_node.kind is PoolKind.V2:
        calldata = (
            AERO_POOL_GET_AMOUNT_OUT_SELECTOR
            + encode(["uint256", "address"], [amount_in, token_in]).hex()
        )
        raw = _single_rpc_call(
            send,
            rpc_url,
            method="eth_call",
            params=[{"to": pool_node.pool_id, "data": calldata}, pinned_block_hex],
            timeout_s=timeout_s,
        )
        raw_bytes = _decode_hex_bytes(raw, "aero.getAmountOut")
        if len(raw_bytes) != 32:
            raise ShadowRunnerError("aero.getAmountOut returned invalid byte length")
        (amount_out,) = decode(["uint256"], raw_bytes)
        return int(amount_out)

    if pool_node.kind is PoolKind.V3:
        calldata = (
            V3_QUOTER_SINGLE_SELECTOR
            + encode(
                ["(address,address,uint256,uint24,uint160)"],
                [(token_in, token_out, amount_in, pool_node.fee_pips, 0)],
            ).hex()
        )
        raw = _single_rpc_call(
            send,
            rpc_url,
            method="eth_call",
            params=[{"to": normalize_address(V3.quoter), "data": calldata}, pinned_block_hex],
            timeout_s=timeout_s,
        )
        raw_bytes = _decode_hex_bytes(raw, "v3.quoter")
        if len(raw_bytes) != 128:
            raise ShadowRunnerError("v3.quoter returned invalid byte length")
        amount_out, _, _, _ = decode(["uint256", "uint160", "uint32", "uint256"], raw_bytes)
        return int(amount_out)

    if pool_node.kind is PoolKind.SLIPSTREAM:
        factory = factories.get(pool_node.pool_id.lower())
        if factory is None or factory not in SLIPSTREAM:
            raise ShadowRunnerError(f"missing Slipstream factory for pool {pool_node.pool_id}")
        quoter_addr = normalize_address(SLIPSTREAM[factory][0])
        spacing = tick_spacings.get(pool_node.pool_id.lower(), 1)
        calldata = (
            SLIPSTREAM_QUOTER_SINGLE_SELECTOR
            + encode(
                ["(address,address,uint256,int24,uint160)"],
                [(token_in, token_out, amount_in, spacing, 0)],
            ).hex()
        )
        raw = _single_rpc_call(
            send,
            rpc_url,
            method="eth_call",
            params=[{"to": quoter_addr, "data": calldata}, pinned_block_hex],
            timeout_s=timeout_s,
        )
        raw_bytes = _decode_hex_bytes(raw, "slipstream.quoter")
        if len(raw_bytes) != 128:
            raise ShadowRunnerError("slipstream.quoter returned invalid byte length")
        amount_out, _, _, _ = decode(["uint256", "uint160", "uint32", "uint256"], raw_bytes)
        return int(amount_out)

    raise ShadowRunnerError(f"quoter fallback unsupported for kind {pool_node.kind}")


def _verify_candidate_fallback(
    send: Callable[[str, bytes, float], bytes],
    rpc_url: str,
    *,
    candidate: CycleCandidate,
    nodes_by_id: Mapping[str, PoolNode],
    pinned_block_number: int,
    timeout_s: float,
    tick_spacings: Mapping[str, int],
    factories: Mapping[str, str | None],
) -> QuoterCheckResult:
    cur_amount = candidate.optimal_amount_in
    pinned_hex = hex(pinned_block_number)
    calls_made = 0
    try:
        for hop in candidate.hops:
            pool_node = nodes_by_id.get(hop.pool_id.lower())
            if pool_node is None:
                return QuoterCheckResult(
                    verified=False,
                    quoter_amount_out=0,
                    quoter_net_profit=-(candidate.gas_cost_in_base + candidate.l1_fee_in_base),
                    rpc_calls_used=calls_made,
                    reason=f"missing_pool_node:{hop.pool_id}",
                )
            calls_made += 1
            cur_amount = _quote_hop_onchain_fallback(
                send,
                rpc_url,
                pool_node=pool_node,
                token_in=hop.token_in,
                token_out=hop.token_out,
                amount_in=cur_amount,
                pinned_block_hex=pinned_hex,
                timeout_s=timeout_s,
                tick_spacings=tick_spacings,
                factories=factories,
            )
            if cur_amount <= 0:
                return QuoterCheckResult(
                    verified=False,
                    quoter_amount_out=0,
                    quoter_net_profit=-(candidate.gas_cost_in_base + candidate.l1_fee_in_base),
                    rpc_calls_used=calls_made,
                    reason="quoter_zero_output",
                )
    except (RpcError, ShadowRunnerError, ValueError) as exc:
        return QuoterCheckResult(
            verified=False,
            quoter_amount_out=0,
            quoter_net_profit=-(candidate.gas_cost_in_base + candidate.l1_fee_in_base),
            rpc_calls_used=calls_made,
            reason=f"quoter_call_failed:{exc}",
        )

    total_gas = candidate.gas_cost_in_base + candidate.l1_fee_in_base
    net_profit = cur_amount - candidate.optimal_amount_in - total_gas
    verified = net_profit > 0
    return QuoterCheckResult(
        verified=verified,
        quoter_amount_out=cur_amount,
        quoter_net_profit=net_profit,
        rpc_calls_used=calls_made,
        reason="ok" if verified else "quoter_nonpositive_net_profit",
    )


def _compute_eth_price_micro_usdc(nodes: Sequence[PoolNode]) -> int:
    """Compute median WETH/USDC mid-price in micro-USDC per 1 WETH across pinned pools."""
    prices: list[int] = []
    for node in nodes:
        t0 = node.token0.lower()
        t1 = node.token1.lower()
        if {t0, t1} != {WETH_ADDR, USDC_ADDR}:
            continue
        if node.kind in (PoolKind.V3, PoolKind.SLIPSTREAM, PoolKind.V4) and node.sqrt_price_x96:
            q192 = 1 << 192
            sq = node.sqrt_price_x96 * node.sqrt_price_x96
            price = (sq * 10**18) // q192 if t0 == WETH_ADDR else (10**18 * q192) // sq
            if price > 0:
                prices.append(price)
        elif node.reserve0 and node.reserve1:
            price = (
                (node.reserve1 * 10**18) // node.reserve0
                if t0 == WETH_ADDR
                else (node.reserve0 * 10**18) // node.reserve1
            )
            if price > 0:
                prices.append(price)
    if not prices:
        raise ShadowRunnerError("no valid WETH/USDC pool available to price ETH in micro-USDC")
    prices.sort()
    return prices[len(prices) // 2]


def _median_spot_scaled(
    nodes: Sequence[PoolNode],
    *,
    token_a: str,
    token_b: str,
    decimals_a: int,
) -> int | None:
    """Return median amount of `token_b` raw per 1 unit (`10**decimals_a`) of `token_a`."""
    scale_a = 10**decimals_a
    ratios: list[int] = []
    for node in nodes:
        t0 = node.token0.lower()
        t1 = node.token1.lower()
        if {t0, t1} != {token_a, token_b}:
            continue
        if node.kind in (PoolKind.V3, PoolKind.SLIPSTREAM, PoolKind.V4) and node.sqrt_price_x96:
            q192 = 1 << 192
            sq = node.sqrt_price_x96 * node.sqrt_price_x96
            val = (sq * scale_a) // q192 if t0 == token_a else (scale_a * q192) // sq
            if val > 0:
                ratios.append(val)
        elif node.reserve0 and node.reserve1:
            val = (
                (node.reserve1 * scale_a) // node.reserve0
                if t0 == token_a
                else (node.reserve0 * scale_a) // node.reserve1
            )
            if val > 0:
                ratios.append(val)
    if not ratios:
        return None
    ratios.sort()
    return ratios[len(ratios) // 2]


def _estimate_token_amount_for_usdc(
    nodes: Sequence[PoolNode],
    *,
    token_addr: str,
    token_decimals: int,
    usdc_amount_raw: int,
) -> int | None:
    """Estimate raw token amount corresponding to `usdc_amount_raw` via direct or 2-hop WETH."""
    scale_token = 10**token_decimals
    mid_micro = _median_spot_scaled(
        nodes, token_a=token_addr, token_b=USDC_ADDR, decimals_a=token_decimals
    )
    if mid_micro is not None and mid_micro > 0:
        est = (usdc_amount_raw * scale_token) // mid_micro
        return est if est > 0 else None
    if token_addr != WETH_ADDR:
        wei_per_tok = _median_spot_scaled(
            nodes, token_a=token_addr, token_b=WETH_ADDR, decimals_a=token_decimals
        )
        micro_per_weth = _median_spot_scaled(
            nodes, token_a=WETH_ADDR, token_b=USDC_ADDR, decimals_a=18
        )
        if wei_per_tok and micro_per_weth:
            mid_2hop = (wei_per_tok * micro_per_weth) // 10**18
            if mid_2hop > 0:
                est_2hop = (usdc_amount_raw * scale_token) // mid_2hop
                return est_2hop if est_2hop > 0 else None
    return None


def _measure_weth_usdc_pairwise_spread(
    graph: FastpathPoolGraph,
    nodes: Sequence[PoolNode],
    *,
    l2_gas_cost_wei: int,
    l1_data_fee_wei: int,
) -> tuple[float, float]:
    """Compute best gross and net bps across pairwise 0.1 WETH quotes on WETH/USDC pools."""
    weth_usdc_nodes = [
        n for n in nodes if {n.token0.lower(), n.token1.lower()} == {WETH_ADDR, USDC_ADDR}
    ]
    total_gas_wei = l2_gas_cost_wei + l1_data_fee_wei
    best_gross_delta: int | None = None
    best_net_delta: int | None = None

    for i, node_i in enumerate(weth_usdc_nodes):
        for j, node_j in enumerate(weth_usdc_nodes):
            if i == j:
                continue
            try:
                leg1 = graph.quote_pool(
                    BASE_CHAIN_ID, node_i.pool_id, WETH_ADDR, PROBE_AMOUNT_IN_WEI
                )
                if leg1.amount_out <= 0:
                    continue
                leg2 = graph.quote_pool(BASE_CHAIN_ID, node_j.pool_id, USDC_ADDR, leg1.amount_out)
            except FastpathCallError:
                continue
            gross_delta = leg2.amount_out - PROBE_AMOUNT_IN_WEI
            net_delta = gross_delta - total_gas_wei
            if best_gross_delta is None or gross_delta > best_gross_delta:
                best_gross_delta = gross_delta
                best_net_delta = net_delta

    if best_gross_delta is None or best_net_delta is None:
        return -10_000.0, -10_000.0
    gross_bps = round(
        float((Decimal(best_gross_delta) * Decimal(10_000)) / Decimal(PROBE_AMOUNT_IN_WEI)),
        6,
    )
    net_bps = round(
        float((Decimal(best_net_delta) * Decimal(10_000)) / Decimal(PROBE_AMOUNT_IN_WEI)),
        6,
    )
    return gross_bps, net_bps


def _best_pair_or_2hop_quotes(
    graph: FastpathPoolGraph,
    all_nodes: Sequence[PoolNode],
    *,
    token_addr: str,
    token_decimals: int,
    trade_notional_usdc: int,
) -> tuple[int | None, int, int | None]:
    """Return `(dex_buy_token_out, trade_notional_token, dex_sell_usdc_out)` via 1- or 2-hop."""
    direct_nodes = [
        n for n in all_nodes if {n.token0.lower(), n.token1.lower()} == {token_addr, USDC_ADDR}
    ]
    weth_usdc_nodes = (
        [n for n in all_nodes if {n.token0.lower(), n.token1.lower()} == {WETH_ADDR, USDC_ADDR}]
        if token_addr != WETH_ADDR
        else []
    )
    tok_weth_nodes = (
        [n for n in all_nodes if {n.token0.lower(), n.token1.lower()} == {token_addr, WETH_ADDR}]
        if token_addr != WETH_ADDR
        else []
    )
    if not direct_nodes and not (weth_usdc_nodes and tok_weth_nodes):
        return None, 0, None

    buy_outs: list[int] = []
    for node in direct_nodes:
        try:
            q = graph.quote_pool(BASE_CHAIN_ID, node.pool_id, USDC_ADDR, trade_notional_usdc)
        except FastpathCallError:
            continue
        if q.amount_out > 0:
            buy_outs.append(q.amount_out)

    if weth_usdc_nodes and tok_weth_nodes:
        weth_leg_outs: list[int] = []
        for w_node in weth_usdc_nodes:
            try:
                q1 = graph.quote_pool(BASE_CHAIN_ID, w_node.pool_id, USDC_ADDR, trade_notional_usdc)
            except FastpathCallError:
                continue
            if q1.amount_out > 0:
                weth_leg_outs.append(q1.amount_out)
        if weth_leg_outs:
            best_weth_mid = max(weth_leg_outs)
            for t_node in tok_weth_nodes:
                try:
                    q2 = graph.quote_pool(BASE_CHAIN_ID, t_node.pool_id, WETH_ADDR, best_weth_mid)
                except FastpathCallError:
                    continue
                if q2.amount_out > 0:
                    buy_outs.append(q2.amount_out)

    dex_buy_out = max(buy_outs) if buy_outs else None
    if dex_buy_out is not None:
        trade_notional_token = dex_buy_out
    else:
        est = _estimate_token_amount_for_usdc(
            all_nodes,
            token_addr=token_addr,
            token_decimals=token_decimals,
            usdc_amount_raw=trade_notional_usdc,
        )
        if est is None:
            return None, 0, None
        trade_notional_token = est

    sell_outs: list[int] = []
    for node in direct_nodes:
        try:
            q = graph.quote_pool(BASE_CHAIN_ID, node.pool_id, token_addr, trade_notional_token)
        except FastpathCallError:
            continue
        if q.amount_out > 0:
            sell_outs.append(q.amount_out)

    if weth_usdc_nodes and tok_weth_nodes:
        weth_back_outs: list[int] = []
        for t_node in tok_weth_nodes:
            try:
                q1 = graph.quote_pool(
                    BASE_CHAIN_ID, t_node.pool_id, token_addr, trade_notional_token
                )
            except FastpathCallError:
                continue
            if q1.amount_out > 0:
                weth_back_outs.append(q1.amount_out)
        if weth_back_outs:
            best_weth_back = max(weth_back_outs)
            for w_node in weth_usdc_nodes:
                try:
                    q2 = graph.quote_pool(BASE_CHAIN_ID, w_node.pool_id, WETH_ADDR, best_weth_back)
                except FastpathCallError:
                    continue
                if q2.amount_out > 0:
                    sell_outs.append(q2.amount_out)

    dex_sell_out = max(sell_outs) if sell_outs else None
    return dex_buy_out, trade_notional_token, dex_sell_out


def _resolve_campaign_risk_config(
    *,
    risk_config: CapitalRiskConfig | None,
    risk_config_path: Path | None,
    total_capital_usd: int | None,
    max_daily_loss_usd: int | None,
) -> CapitalRiskConfig:
    if risk_config is not None and risk_config_path is not None:
        raise ShadowRunnerError("cannot supply both risk_config and risk_config_path")
    if risk_config_path is not None:
        base_cfg = load_capital_risk_config(risk_config_path)
    elif risk_config is not None:
        if not isinstance(risk_config, CapitalRiskConfig):
            raise ShadowRunnerError("risk_config must be a CapitalRiskConfig")
        base_cfg = risk_config
    else:
        base_cfg = CapitalRiskConfig()

    if total_capital_usd is not None and (
        type(total_capital_usd) is not int or total_capital_usd <= 0
    ):
        raise ShadowRunnerError("total_capital_usd must be a positive integer")
    if max_daily_loss_usd is not None and (
        type(max_daily_loss_usd) is not int or max_daily_loss_usd <= 0
    ):
        raise ShadowRunnerError("max_daily_loss_usd must be a positive integer")

    if total_capital_usd is None and max_daily_loss_usd is None:
        return base_cfg

    new_total = (
        total_capital_usd * 1_000_000
        if total_capital_usd is not None
        else base_cfg.total_capital_usd_micros
    )
    new_daily = (
        max_daily_loss_usd * 1_000_000
        if max_daily_loss_usd is not None
        else min(base_cfg.max_daily_loss_usd_micros, new_total)
    )
    new_cum = min(
        new_total,
        max(
            new_daily,
            new_total if total_capital_usd is not None else base_cfg.max_cumulative_loss_usd_micros,
        ),
    )
    new_min_clip = min(base_cfg.min_clip_usd_micros, new_total)
    return CapitalRiskConfig(
        total_capital_usd_micros=new_total,
        max_daily_loss_usd_micros=new_daily,
        max_cumulative_loss_usd_micros=new_cum,
        reserve_buffer_bps=base_cfg.reserve_buffer_bps,
        max_single_clip_share_bps=base_cfg.max_single_clip_share_bps,
        min_clip_usd_micros=new_min_clip,
        assumed_tail_stress_bps=base_cfg.assumed_tail_stress_bps,
        max_feed_age_seconds=base_cfg.max_feed_age_seconds,
        usdc_price_usd_micros=base_cfg.usdc_price_usd_micros,
    )


def _pool_pair_endpoints(pool_item: Any) -> tuple[str, str] | None:
    t0 = _attr_or_key(pool_item, "token0", default=None)
    t1 = _attr_or_key(pool_item, "token1", default=None)
    if isinstance(t0, str) and isinstance(t1, str) and t0 and t1:
        return normalize_address(t0), normalize_address(t1)
    return None


def _discover_eligible_coinbase_products(pool_specs: Sequence[Any]) -> tuple[str, ...]:
    pairs: set[frozenset[str]] = set()
    for item in pool_specs:
        endpoints = _pool_pair_endpoints(item)
        if endpoints is not None:
            pairs.add(frozenset(endpoints))
    eligible: list[str] = []
    has_weth_usdc = frozenset((WETH_ADDR, USDC_ADDR)) in pairs
    for tok_addr in CEX_DEX_TOKEN_ORDER:
        meta = TOKEN_META_BY_ADDR[tok_addr]
        if meta.coinbase_product is None:
            continue
        has_direct = frozenset((tok_addr, USDC_ADDR)) in pairs
        has_2hop = (
            tok_addr != WETH_ADDR and has_weth_usdc and frozenset((tok_addr, WETH_ADDR)) in pairs
        )
        if has_direct or has_2hop:
            eligible.append(meta.coinbase_product)
    return tuple(eligible)


def _normalize_allowed_products(
    allowed_products: Sequence[str] | None,
) -> tuple[str, ...] | None:
    if allowed_products is None:
        return None
    if isinstance(allowed_products, (str, bytes)) or not isinstance(allowed_products, Sequence):
        raise ShadowRunnerError(
            "allowed_products must be a non-empty sequence of product IDs or token symbols"
        )
    if not allowed_products:
        raise ShadowRunnerError("allowed_products must not be empty when specified")
    deduped: dict[str, None] = {}
    for raw in allowed_products:
        if not isinstance(raw, str) or not raw.strip():
            raise ShadowRunnerError("allowed_products entries must be non-empty strings")
        try:
            spec = resolve_coinbase_product(raw)
        except ValueError as exc:
            raise ShadowRunnerError(str(exc)) from exc
        deduped.setdefault(spec.product_id, None)
    return tuple(deduped)


def _parse_allowed_products_cli(raw: str | None) -> tuple[str, ...] | None:
    if raw is None:
        return None
    if not raw.strip():
        raise ShadowRunnerError(
            "allowed-products must be a comma-separated list of non-empty product IDs or symbols"
        )
    parts = [part.strip() for part in raw.split(",")]
    if any(not part for part in parts):
        raise ShadowRunnerError("allowed-products must not contain empty entries")
    return _normalize_allowed_products(parts)


def _resolve_maker_min_edge_bps(
    maker_min_edge_bps: Decimal | float | None,
    *,
    dynamic_sizing: bool,
) -> Decimal:
    if maker_min_edge_bps is None:
        return Decimal("6.5") if dynamic_sizing else Decimal("3.0")
    if isinstance(maker_min_edge_bps, bool) or not isinstance(
        maker_min_edge_bps, (Decimal, int, float)
    ):
        raise ShadowRunnerError("maker_min_edge_bps must be a positive finite number")
    if isinstance(maker_min_edge_bps, Decimal):
        dec = maker_min_edge_bps
    elif isinstance(maker_min_edge_bps, float):
        if not math.isfinite(maker_min_edge_bps):
            raise ShadowRunnerError("maker_min_edge_bps must be finite")
        dec = Decimal(str(maker_min_edge_bps))
    else:
        dec = Decimal(maker_min_edge_bps)
    if not dec.is_finite() or dec <= 0:
        raise ShadowRunnerError("maker_min_edge_bps must be finite and positive")
    return dec


def _make_dex_quote_cache_fn(
    graph: FastpathPoolGraph,
    nodes: tuple[PoolNode, ...],
    *,
    tok_addr: str,
    token_decimals: int,
) -> Callable[[int], tuple[int | None, int, int | None]]:
    cache: dict[int, tuple[int | None, int, int | None]] = {}

    def _quote_dex_cached(notional_usdc: int) -> tuple[int | None, int, int | None]:
        cached = cache.get(notional_usdc)
        if cached is not None:
            return cached
        quoted = _best_pair_or_2hop_quotes(
            graph,
            nodes,
            token_addr=tok_addr,
            token_decimals=token_decimals,
            trade_notional_usdc=notional_usdc,
        )
        cache[notional_usdc] = quoted
        return quoted

    return _quote_dex_cached


def _select_dynamic_maker_quotes(
    *,
    product_id: str,
    token_decimals: int,
    fetched_book: CoinbaseL2Book,
    quote_fn: Callable[[int], tuple[int | None, int, int | None]],
    gas_cost_usd: Decimal,
    maker_config: MakerHedgeConfig,
    min_clip_usdc: int,
    max_clip_usdc: int,
    fallback_notional_usdc: int,
    effective_adverse_bps: Decimal | None = None,
) -> tuple[int, tuple[int | None, int, int | None]]:
    book_cap = compute_coinbase_book_capacity_usdc(fetched_book).max_directional_capacity_usdc
    effective_max = min(max_clip_usdc, book_cap)
    if effective_max < min_clip_usdc:
        return fallback_notional_usdc, quote_fn(fallback_notional_usdc)

    def _eval_maker(notional_usdc: int) -> tuple[int, float] | None:
        dex_buy_out, notional_tok, dex_sell_out = quote_fn(notional_usdc)
        if notional_tok <= 0 or (dex_buy_out is None and dex_sell_out is None):
            return None
        cand = build_maker_quote_candidate(
            product_id,
            current_book=fetched_book,
            trade_notional_usdc=notional_usdc,
            dex_buy_token_out=dex_buy_out,
            trade_notional_token=notional_tok,
            dex_sell_usdc_out=dex_sell_out,
            token_decimals=token_decimals,
            gas_cost_usd=gas_cost_usd,
            config=maker_config,
            effective_adverse_bps=effective_adverse_bps,
        )
        active_edges: list[float] = []
        if cand.ask_active and cand.ask_edge_bps is not None:
            active_edges.append(float(cand.ask_edge_bps))
        if cand.bid_active and cand.bid_edge_bps is not None:
            active_edges.append(float(cand.bid_edge_bps))
        if active_edges:
            best_edge = max(active_edges)
        else:
            all_edges = [
                float(edge) for edge in (cand.ask_edge_bps, cand.bid_edge_bps) if edge is not None
            ]
            if not all_edges:
                return None
            best_edge = max(all_edges)
        profit_micros = round((notional_usdc * best_edge) / 10_000.0)
        return profit_micros, best_edge

    opt = solve_optimal_notional(
        min_notional_raw=min_clip_usdc,
        max_notional_raw=effective_max,
        evaluate_fn=_eval_maker,
    )
    chosen = min_clip_usdc if opt.optimal_notional_raw is None else opt.optimal_notional_raw
    return chosen, quote_fn(chosen)


def _fetch_cex_book_for_tick(
    product_id: str,
    *,
    cex_venue: str,
    usdt_peg: UsdtUsdPeg | None,
    timeout_s: float,
    transport: Callable[[str, float], bytes] | None,
) -> tuple[CoinbaseL2Book | None, str | None]:
    """Fetch CEX L2 book for either Coinbase or Bybit Spot (USD-normalized)."""
    if cex_venue == "bybit":
        if usdt_peg is None:
            return None, "usdt_usd_peg_unavailable"
        try:
            return (
                fetch_bybit_l2_book(
                    product_id,
                    usdt_peg=usdt_peg,
                    limit=25,
                    timeout_s=timeout_s,
                    transport=transport,
                ),
                None,
            )
        except (
            BybitSpotFeedError,
            LiveCexDexError,
            OSError,
            TimeoutError,
            urllib.error.URLError,
            ValueError,
        ) as exc:
            return None, str(exc) or exc.__class__.__name__
    return _fetch_coinbase_book_for_tick(
        product_id,
        timeout_s=timeout_s,
        transport=transport,
    )


def _fetch_coinbase_book_for_tick(
    product_id: str,
    *,
    timeout_s: float,
    transport: Callable[[str, float], bytes] | None,
) -> tuple[CoinbaseL2Book | None, str | None]:
    """Fetch Coinbase L2 book once per product, falling back cleanly to level=1 on byte limit."""
    try:
        return (
            fetch_coinbase_l2_book(product_id, level=2, timeout_s=timeout_s, transport=transport),
            None,
        )
    except LiveCexDexError as exc:
        msg = str(exc)
        if "byte limit" in msg or "MAX_RESPONSE_BYTES" in msg:
            try:
                return (
                    fetch_coinbase_l2_book(
                        product_id, level=1, timeout_s=timeout_s, transport=transport
                    ),
                    None,
                )
            except (LiveCexDexError, OSError, TimeoutError, urllib.error.URLError, ValueError) as e:
                return None, str(e) or e.__class__.__name__
        return None, msg or exc.__class__.__name__
    except (OSError, TimeoutError, urllib.error.URLError, ValueError) as exc:
        return None, str(exc) or exc.__class__.__name__


def verify_campaign_attribution_chain(records: Sequence[Mapping[str, object]]) -> str:
    """Verify hash-chain continuity, `execution_eligible=False`, and digests."""
    if not records:
        raise ShadowRunnerError("campaign attribution records must be non-empty")
    expected_prev = _GENESIS_PREV_SHA256
    for idx, rec in enumerate(records, start=1):
        if rec.get("seq") != idx:
            raise ShadowRunnerError(f"attribution seq mismatch at index {idx}")
        if rec.get("prev_record_sha256") != expected_prev:
            raise ShadowRunnerError(f"attribution prev_record_sha256 mismatch at seq {idx}")
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


def build_synthetic_multicall_rpc_transport(
    *,
    scenario: str = "aligned",
    base_block_number: int = 58_900_000,
    base_timestamp_sec: int = 1_795_000_000,
    base_fee_per_gas_wei: int = 10_000_000,
    l1_data_fee_wei: int = 1_000_000_000_000,
    extra_pools: Sequence[BasePoolSpec] = (),
) -> Callable[[str, bytes, float], bytes]:
    """Build deterministic offline Base Multicall3 + Quoter JSON-RPC transport."""
    valid_scenarios = {"aligned", "dislocated_quoter_verified", "phantom_rust_rejected"}
    if scenario not in valid_scenarios:
        raise ValueError(f"unsupported synthetic scenario: {scenario!r}")

    pools = list(canonical_base_weth_usdc_pools()) + list(extra_pools)
    specs_by_addr = {spec.address: spec for spec in pools}
    aero_spec = pools[1]
    ss1_spec = pools[2]
    ss2_spec = pools[3]
    aero_fee_cd = (
        GET_FEE_SELECTOR + encode(["address", "bool"], [aero_spec.address, False]).hex()
    ).lower()

    tick_v3 = -196_258
    tick_ss1 = -196_258
    tick_ss2 = -196_258 if scenario == "aligned" else -196_138
    cl_liquidity = 200_000_000_000_000_000
    aero_weth = 1_000 * 10**18
    aero_usdc = 3_000_000 * 10**6
    extra_cl_liquidity = 500_000_000_000_000_000
    tick_counter = {"n": 0}

    def _u256_bytes(val: int) -> bytes:
        return val.to_bytes(32, "big")

    def _slot0_v3_bytes(tick_val: int) -> bytes:
        return encode(
            ["uint160", "int24", "uint16", "uint16", "uint16", "uint8", "bool"],
            [sqrt_price_at_tick(tick_val), tick_val, 0, 1, 1, 0, True],
        )

    def _slot0_ss_bytes(tick_val: int) -> bytes:
        return encode(
            ["uint160", "int24", "uint16", "uint16", "uint16", "bool"],
            [sqrt_price_at_tick(tick_val), tick_val, 0, 1, 1, True],
        )

    def _default_tick_for_pair(spec: BasePoolSpec) -> int:
        tokens = {spec.token0.lower(), spec.token1.lower()}
        if tokens == {CBBTC_ADDR, USDC_ADDR}:
            # 1 cbBTC (10^8) = 90,000 USDC (9e10 raw) => raw ratio 1/900 when token0=USDC
            return -68_028 if spec.token0.lower() == USDC_ADDR else 68_028
        if tokens == {AERO_TOKEN_ADDR, USDC_ADDR}:
            # 1 AERO (10^18) = 1.20 USDC (1.2e6 raw) => raw ratio ~8.33e11 when token0=USDC
            return 274_500 if spec.token0.lower() == USDC_ADDR else -274_500
        return tick_v3

    def _handle_subcall(target: str, data_bytes: bytes, ts_ms: int) -> bytes:
        norm_target = normalize_address(target)
        data_hex = "0x" + data_bytes.hex()
        if norm_target == MULTICALL3 and data_hex == MULTICALL3_GET_CHAIN_ID_SELECTOR:
            return _u256_bytes(BASE_CHAIN_ID)
        if norm_target == ORACLE and data_hex == GET_L1_FEE_CALLDATA:
            return _u256_bytes(l1_data_fee_wei)
        if norm_target == BASE_TIME and data_hex == TIMESTAMP_MS_SELECTOR:
            return _u256_bytes(ts_ms)
        if norm_target == BASE_TIME and data_hex == TIMESTAMP_MILLIS_PART_SELECTOR:
            return _u256_bytes(ts_ms % 1000)
        if norm_target == aero_spec.factory and data_hex == aero_fee_cd:
            return _u256_bytes(30)

        spec = specs_by_addr.get(norm_target)
        if spec is None:
            raise ValueError(f"unexpected synthetic Multicall3 subcall target={norm_target}")

        if spec.kind is PoolKind.V2 and data_hex == GET_RESERVES_SELECTOR:
            return encode(
                ["uint256", "uint256", "uint256"],
                [aero_weth, aero_usdc, ts_ms // 1000],
            )
        if spec.kind is PoolKind.V3:
            t_val = tick_v3 if spec.address == pools[0].address else _default_tick_for_pair(spec)
            if data_hex == SLOT0_SELECTOR:
                return _slot0_v3_bytes(t_val)
            if data_hex == LIQUIDITY_SELECTOR:
                liq = cl_liquidity if spec.address == pools[0].address else extra_cl_liquidity
                return _u256_bytes(liq)
        if spec.kind is PoolKind.SLIPSTREAM:
            if spec.address == ss1_spec.address:
                t_val = tick_ss1
            elif spec.address == ss2_spec.address:
                t_val = tick_ss2
            else:
                t_val = _default_tick_for_pair(spec)
            if data_hex == SLOT0_SELECTOR:
                return _slot0_ss_bytes(t_val)
            if data_hex == LIQUIDITY_SELECTOR:
                liq = (
                    cl_liquidity
                    if spec.address in (ss1_spec.address, ss2_spec.address)
                    else (extra_cl_liquidity)
                )
                return _u256_bytes(liq)
            if data_hex == FEE_SELECTOR:
                return _u256_bytes(spec.fixed_fee_pips or 100)
        raise ValueError(f"unexpected subcall target={norm_target} data={data_hex}")

    def _quote_exact_input_synthetic(
        kind: PoolKind, token_in: str, token_out: str, amount_in: int, spec_tick: int, fee_pips: int
    ) -> int:
        matched_t0 = WETH_ADDR
        pair_set = {normalize_address(token_in), normalize_address(token_out)}
        for candidate_spec in pools:
            if {candidate_spec.token0.lower(), candidate_spec.token1.lower()} == pair_set:
                matched_t0 = candidate_spec.token0.lower()
                break
        if kind in (PoolKind.V3, PoolKind.SLIPSTREAM):
            sqrt_p = sqrt_price_at_tick(spec_tick)
            q192 = 1 << 192
            raw_num = amount_in * (1_000_000 - fee_pips) // 1_000_000
            if normalize_address(token_in) == matched_t0:
                out = (raw_num * sqrt_p * sqrt_p) // q192
            else:
                out = (raw_num * q192) // (sqrt_p * sqrt_p)
            if scenario == "phantom_rust_rejected" and spec_tick == tick_ss2:
                return (out * 98) // 100
            return out
        return 0

    def _transport(_url: str, payload: bytes, _timeout_s: float) -> bytes:
        req = json.loads(payload.decode("utf-8"))
        method = req.get("method")
        req_id = req.get("id")
        params = req.get("params", [])

        if method == "eth_chainId":
            return json.dumps(
                {"jsonrpc": "2.0", "id": req_id, "result": hex(BASE_CHAIN_ID)}
            ).encode("utf-8")
        if method == "eth_getBlockByNumber":
            idx = tick_counter["n"]
            tick_counter["n"] = idx + 1
            block_num = base_block_number + idx * 4
            ts_ms = (base_timestamp_sec + idx) * 1000 + (idx * 200) % 1000
            block_hash = "0x" + sha256_hex(f"base-mc-synth-block-{block_num}".encode())
            parent_hash = "0x" + sha256_hex(f"base-mc-synth-parent-{block_num - 1}".encode())
            state_root = "0x" + sha256_hex(f"base-mc-synth-state-{block_num}".encode())
            header = {
                "number": hex(block_num),
                "hash": block_hash,
                "parentHash": parent_hash,
                "stateRoot": state_root,
                "timestamp": hex(ts_ms // 1000),
                "timestampMs": hex(ts_ms),
                "baseFeePerGas": hex(base_fee_per_gas_wei),
            }
            return json.dumps({"jsonrpc": "2.0", "id": req_id, "result": header}).encode("utf-8")

        if method == "eth_call":
            call_obj, block_hex = params
            block_num = int(block_hex, 16)
            idx = max(0, (block_num - base_block_number) // 4)
            ts_ms = (base_timestamp_sec + idx) * 1000 + (idx * 200) % 1000
            to_addr = normalize_address(call_obj["to"])
            data_hex = call_obj["data"].lower()

            if to_addr == MULTICALL3 and data_hex.startswith(AGGREGATE3_SELECTOR):
                raw_args = bytes.fromhex(data_hex[10:])
                (calls_list,) = decode(["(address,bool,bytes)[]"], raw_args)
                ret_pairs = [
                    (True, _handle_subcall(target, bytes(cd), ts_ms))
                    for target, _allow_fail, cd in calls_list
                ]
                encoded_ret = "0x" + encode(["(bool,bytes)[]"], [ret_pairs]).hex()
                return json.dumps({"jsonrpc": "2.0", "id": req_id, "result": encoded_ret}).encode(
                    "utf-8"
                )

            if to_addr == normalize_address(V3.quoter) and data_hex.startswith(
                V3_QUOTER_SINGLE_SELECTOR
            ):
                ((t_in, t_out, amt_in, fee_pips, _lim),) = decode(
                    ["(address,address,uint256,uint24,uint160)"],
                    bytes.fromhex(data_hex[10:]),
                )
                out_amt = _quote_exact_input_synthetic(
                    PoolKind.V3, t_in, t_out, int(amt_in), tick_v3, int(fee_pips)
                )
                encoded_quote = encode(
                    ["uint256", "uint160", "uint32", "uint256"], [out_amt, 0, 0, 120_000]
                ).hex()
                return json.dumps(
                    {"jsonrpc": "2.0", "id": req_id, "result": "0x" + encoded_quote}
                ).encode("utf-8")

            ss_quoter_ticks: dict[str, int] = {}
            if ss1_spec.factory is not None and ss1_spec.factory in SLIPSTREAM:
                ss_quoter_ticks[normalize_address(SLIPSTREAM[ss1_spec.factory][0])] = tick_ss1
            if ss2_spec.factory is not None and ss2_spec.factory in SLIPSTREAM:
                ss_quoter_ticks[normalize_address(SLIPSTREAM[ss2_spec.factory][0])] = tick_ss2
            if to_addr in ss_quoter_ticks and data_hex.startswith(
                SLIPSTREAM_QUOTER_SINGLE_SELECTOR
            ):
                ((t_in, t_out, amt_in, _spacing, _lim),) = decode(
                    ["(address,address,uint256,int24,uint160)"],
                    bytes.fromhex(data_hex[10:]),
                )
                used_tick = ss_quoter_ticks[to_addr]
                out_amt = _quote_exact_input_synthetic(
                    PoolKind.SLIPSTREAM, t_in, t_out, int(amt_in), used_tick, 100
                )
                encoded_quote = encode(
                    ["uint256", "uint160", "uint32", "uint256"], [out_amt, 0, 0, 120_000]
                ).hex()
                return json.dumps(
                    {"jsonrpc": "2.0", "id": req_id, "result": "0x" + encoded_quote}
                ).encode("utf-8")

            if to_addr == aero_spec.address and data_hex.startswith(
                AERO_POOL_GET_AMOUNT_OUT_SELECTOR
            ):
                amt_in, t_in = decode(["uint256", "address"], bytes.fromhex(data_hex[10:]))
                r_in, r_out = (
                    (aero_weth, aero_usdc)
                    if normalize_address(t_in) == WETH_ADDR
                    else (aero_usdc, aero_weth)
                )
                amt_with_fee = int(amt_in) * 9970
                out_amt = (amt_with_fee * r_out) // (r_in * 10_000 + amt_with_fee)
                return json.dumps(
                    {"jsonrpc": "2.0", "id": req_id, "result": "0x" + _u256_bytes(out_amt).hex()}
                ).encode("utf-8")

        raise ValueError(f"unexpected synthetic Multicall RPC request: method={method!r}")

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


def _extract_snapshot_from_multicall_universe(raw_snap: Any) -> PinnedTickSnapshot:
    block_number = int(_attr_or_key(raw_snap, "block_number", "blockNumber"))
    block_hash = normalize_hash32(
        str(_attr_or_key(raw_snap, "block_hash", "blockHash")), "block_hash"
    )
    raw_parent = _attr_or_key(raw_snap, "parent_hash", "parentHash", default=None)
    parent_hash = (
        normalize_hash32(str(raw_parent), "parent_hash") if isinstance(raw_parent, str) else None
    )
    raw_header_ts = _attr_or_key(raw_snap, "header_timestamp", "timestamp_s", "timestamp")
    if raw_header_ts is None:
        raw_header_ts_ms = _attr_or_key(raw_snap, "header_timestamp_ms", "base_time_ms")
        if raw_header_ts_ms is None:
            raise ShadowRunnerError("multicall_universe snapshot missing header timestamp")
        header_ts = int(raw_header_ts_ms) // 1000
    else:
        header_ts = int(raw_header_ts)
    base_time_ms = int(_attr_or_key(raw_snap, "base_time_ms", "timestamp_ms", "checked_ms"))
    base_fee_wei = int(
        _attr_or_key(raw_snap, "base_fee_per_gas_wei", "base_fee_per_gas", "baseFeePerGas")
    )
    l1_fee_wei = int(_attr_or_key(raw_snap, "l1_fee_wei", "l1_data_fee_wei", "l1DataFeeWei"))
    raw_nodes = _attr_or_key(raw_snap, "nodes", "pool_nodes")
    if not isinstance(raw_nodes, Sequence) or not raw_nodes:
        raise ShadowRunnerError("multicall_universe snapshot returned empty nodes")
    nodes = tuple(raw_nodes)
    raw_balances = _attr_or_key(raw_snap, "balances_by_pool", default=None)
    balances_by_pool = dict(raw_balances) if isinstance(raw_balances, Mapping) else None
    rpc_used = _attr_or_key(raw_snap, "rpc_calls_used", default=2)
    calls_used = int(rpc_used) if type(rpc_used) is int and rpc_used > 0 else 2
    return PinnedTickSnapshot(
        block_number=block_number,
        block_hash=block_hash,
        header_timestamp=header_ts,
        base_time_ms=base_time_ms,
        base_fee_per_gas_wei=base_fee_wei,
        l1_data_fee_wei=l1_fee_wei,
        nodes=nodes,
        rpc_calls_used=calls_used,
        balances_by_pool=balances_by_pool,
        parent_hash=parent_hash,
    )


class _KeepAliveHttpsPool:
    """Persistent HTTP/1.1 Keep-Alive HTTPS pool for keyless RPC POST and Coinbase GET."""

    def __init__(
        self,
        *,
        connection_factory: (
            Callable[[str, int, float, ssl.SSLContext], http.client.HTTPSConnection] | None
        ) = None,
        max_requests_per_conn: int = 200,
    ) -> None:
        if type(max_requests_per_conn) is not int or max_requests_per_conn <= 0:
            raise ValueError("max_requests_per_conn must be a positive integer")
        self._ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        self._local = threading.local()
        self._conn_lock = threading.Lock()
        self._all_conns: list[http.client.HTTPSConnection] = []
        self._connection_factory = connection_factory
        self._max_requests_per_conn = max_requests_per_conn

    def _thread_conns(self) -> dict[tuple[str, int], http.client.HTTPSConnection]:
        conns = getattr(self._local, "conns", None)
        if conns is None:
            conns = {}
            self._local.conns = conns
        return cast(dict[tuple[str, int], http.client.HTTPSConnection], conns)

    def _thread_uses(self) -> dict[tuple[str, int], int]:
        uses = getattr(self._local, "uses", None)
        if uses is None:
            uses = {}
            self._local.uses = uses
        return cast(dict[tuple[str, int], int], uses)

    @property
    def _conns(self) -> dict[tuple[str, int], http.client.HTTPSConnection]:
        return self._thread_conns()

    def _new_conn(self, hostname: str, port: int, timeout_s: float) -> http.client.HTTPSConnection:
        if self._connection_factory is not None:
            return self._connection_factory(hostname, port, timeout_s, self._ssl_ctx)
        return _IPv4PreferredHTTPSConnection(
            hostname, port, timeout=timeout_s, context=self._ssl_ctx
        )

    def _drop_conn(self, key: tuple[str, int]) -> None:
        self._thread_uses().pop(key, None)
        conn = self._thread_conns().pop(key, None)
        if conn is not None:
            with self._conn_lock:
                for idx, tracked in enumerate(self._all_conns):
                    if tracked is conn:
                        del self._all_conns[idx]
                        break
            with suppress(Exception):
                conn.close()

    def _request(
        self,
        *,
        method: str,
        url: str,
        body: bytes | None,
        headers: dict[str, str],
        timeout_s: float,
        max_bytes: int,
        is_coinbase: bool,
    ) -> bytes:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or not parsed.netloc or not parsed.hostname:
            if is_coinbase:
                raise LiveCexDexError("Coinbase URL must be https://")
            raise ShadowRunnerError("Base shadow RPC requires an https:// endpoint")
        if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
            if is_coinbase:
                raise LiveCexDexError("Coinbase URL forbids userinfo credentials")
            raise ShadowRunnerError("Base shadow RPC forbids URL userinfo credentials")
        hostname = parsed.hostname
        try:
            port = parsed.port if parsed.port is not None else 443
        except ValueError as exc:
            if is_coinbase:
                raise LiveCexDexError("Coinbase URL contains invalid port") from exc
            raise ShadowRunnerError("Base shadow RPC URL contains an invalid port") from exc

        if is_coinbase:
            if parsed.path.endswith("/trades"):
                _validate_coinbase_trades_url(url)
            else:
                _validate_coinbase_url(url)
            _validate_cb_dns(hostname)
        else:
            _validate_rpc_dns(hostname, port)

        target_path = parsed.path or "/"
        if parsed.query:
            target_path = f"{target_path}?{parsed.query}"

        key = (hostname, port)
        thread_conns = self._thread_conns()
        thread_uses = self._thread_uses()
        for attempt in range(2):
            conn = thread_conns.get(key)
            reused = conn is not None
            if conn is None:
                conn = self._new_conn(hostname, port, timeout_s)
                thread_conns[key] = conn
                thread_uses[key] = 0
                with self._conn_lock:
                    self._all_conns.append(conn)
            else:
                if conn.sock is not None:
                    with suppress(OSError):
                        conn.sock.settimeout(timeout_s)
                conn.timeout = timeout_s

            try:
                conn.request(method, target_path, body=body, headers=headers)
                resp = conn.getresponse()
                raw = resp.read(max_bytes + 1)
            except (
                http.client.RemoteDisconnected,
                http.client.BadStatusLine,
                http.client.IncompleteRead,
                BrokenPipeError,
                ConnectionResetError,
                ssl.SSLError,
                EOFError,
            ):
                self._drop_conn(key)
                if reused and attempt == 0:
                    continue
                if is_coinbase:
                    raise LiveCexDexError("Coinbase book transport failure") from None
                raise RpcError("Base shadow RPC transport failure") from None
            except http.client.HTTPException, OSError, TimeoutError:
                self._drop_conn(key)
                if is_coinbase:
                    raise LiveCexDexError("Coinbase book transport failure") from None
                raise RpcError("Base shadow RPC transport failure") from None

            if 300 <= resp.status < 400:
                self._drop_conn(key)
                if is_coinbase:
                    raise LiveCexDexError("Coinbase book redirects are forbidden")
                raise RpcError("Base shadow RPC redirects are forbidden")
            if resp.status != 200:
                self._drop_conn(key)
                if is_coinbase:
                    raise LiveCexDexError(f"Coinbase book HTTP failure: status {resp.status}")
                raise RpcError(f"Base shadow RPC HTTP failure: status {resp.status}")
            if len(raw) > max_bytes:
                self._drop_conn(key)
                if is_coinbase:
                    raise LiveCexDexError("Coinbase book response exceeds byte limit")
                raise RpcError("Base shadow RPC response exceeds byte limit")
            uses = thread_uses.get(key, 0) + 1
            if getattr(resp, "will_close", False) or uses >= self._max_requests_per_conn:
                self._drop_conn(key)
            else:
                thread_uses[key] = uses
            return raw

        if is_coinbase:
            raise LiveCexDexError("Coinbase book transport failure")
        raise RpcError("Base shadow RPC transport failure")

    def post(self, rpc_url: str, payload: bytes, timeout_s: float) -> bytes:
        """Execute a keep-alive HTTPS JSON-RPC POST."""
        return self._request(
            method="POST",
            url=rpc_url,
            body=payload,
            headers={
                "Content-Type": "application/json",
                "Connection": "keep-alive",
                "User-Agent": _USER_AGENT,
            },
            timeout_s=timeout_s,
            max_bytes=MAX_RESPONSE_BYTES,
            is_coinbase=False,
        )

    def get(self, url: str, timeout_s: float) -> bytes:
        """Execute a keep-alive HTTPS GET for allowlisted Coinbase exchange L2 books."""
        return self._request(
            method="GET",
            url=url,
            body=None,
            headers={
                "Accept": "application/json",
                "Connection": "keep-alive",
                "User-Agent": _USER_AGENT,
            },
            timeout_s=timeout_s,
            max_bytes=CB_MAX_RESPONSE_BYTES,
            is_coinbase=True,
        )

    def close(self) -> None:
        """Close all persistent HTTPS sockets."""
        with self._conn_lock:
            conns = list(self._all_conns)
            self._all_conns.clear()
            self._local = threading.local()
        for conn in conns:
            with suppress(Exception):
                conn.close()


def _interpret_quoter_verification_result(
    raw_res: Any, candidate: CycleCandidate
) -> QuoterCheckResult:
    if isinstance(raw_res, bool):
        return QuoterCheckResult(
            verified=raw_res and candidate.net_profit > 0,
            quoter_amount_out=candidate.amount_out if raw_res else 0,
            quoter_net_profit=(
                candidate.net_profit
                if raw_res
                else -(candidate.gas_cost_in_base + candidate.l1_fee_in_base)
            ),
            rpc_calls_used=len(candidate.hops),
            reason="ok" if raw_res else "quoter_rejected",
        )
    verified = bool(_attr_or_key(raw_res, "verified", "passed", "ok", default=False))
    amount_out = int(
        _attr_or_key(
            raw_res,
            "quoter_amount_out",
            "simulated_amount_out",
            "amount_out",
            default=candidate.amount_out,
        )
    )
    net_profit = int(
        _attr_or_key(
            raw_res,
            "quoter_net_profit",
            "simulated_net_profit",
            "net_profit",
            default=candidate.net_profit,
        )
    )
    calls_used = int(_attr_or_key(raw_res, "rpc_calls_used", default=len(candidate.hops)))
    reason = str(
        _attr_or_key(raw_res, "reason", "detail", default="ok" if verified else "rejected")
    )
    effective_verified = verified and net_profit > 0
    return QuoterCheckResult(
        verified=effective_verified,
        quoter_amount_out=amount_out,
        quoter_net_profit=net_profit,
        rpc_calls_used=calls_used,
        reason=reason,
    )


def run_multicall_campaign(
    *,
    out_dir: Path,
    mode: str = "public-shadow",
    ticks: int = DEFAULT_TICKS,
    interval_ms: int = DEFAULT_INTERVAL_MS,
    max_rpc_calls: int = DEFAULT_MAX_RPC_CALLS,
    rpc_url: str = DEFAULT_BASE_RPC_URL,
    cex_taker_fee_bps: float = float(DEFAULT_CEX_TAKER_FEE_BPS),
    latency_haircut_bps: float = float(DEFAULT_LATENCY_HAIRCUT_BPS),
    trade_notional_usdc: int = DEFAULT_TRADE_NOTIONAL_USDC,
    priority_fee_wei: int = DEFAULT_PRIORITY_FEE_WEI,
    gas_limit_units: int = GAS_LIMIT_MODEL,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    bridge: FastpathBridge | None = None,
    rpc_transport: Callable[[str, bytes, float], bytes] | None = None,
    coinbase_transport: Callable[[str, float], bytes] | None = None,
    sleeper: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
    fixture_scenario: str = "aligned",
    extra_fixture_pools: Sequence[BasePoolSpec] = (),
    include_ecosystem_tokens: bool = False,
    include_aero_cl2: bool = False,
    cex_venue: str = "coinbase",
    risk_config: CapitalRiskConfig | None = None,
    risk_config_path: Path | None = None,
    total_capital_usd: int | None = None,
    max_daily_loss_usd: int | None = None,
    dynamic_sizing: bool = False,
    enable_coinbase_ws: bool = False,
    ws_feed_factory: (
        Callable[[Sequence[str]], CoinbaseWebSocketFeed | BybitSpotWebSocketFeed] | None
    ) = None,
    trades_transport: Callable[[str, float], bytes] | None = None,
    allowed_products: Sequence[str] | None = None,
    min_taker_edge_bps: int | None = None,
    maker_min_edge_bps: Decimal | float | None = None,
    max_book_workers: int = 9,
    stop_gracefully_on_rpc_error: bool = False,
) -> CampaignRunResult:
    """Execute a bounded multi-tick Base Multicall3 + CEX-DEX shadow campaign."""
    if mode not in {"public-shadow", "fixture"}:
        raise ShadowRunnerError(f"unsupported campaign mode: {mode!r}")
    if type(include_ecosystem_tokens) is not bool:
        raise ShadowRunnerError("include_ecosystem_tokens must be a boolean")
    if type(include_aero_cl2) is not bool:
        raise ShadowRunnerError("include_aero_cl2 must be a boolean")
    if cex_venue not in {"coinbase", "bybit"}:
        raise ShadowRunnerError(f"unsupported cex_venue: {cex_venue!r}")
    if type(dynamic_sizing) is not bool:
        raise ShadowRunnerError("dynamic_sizing must be a boolean")
    if type(enable_coinbase_ws) is not bool:
        raise ShadowRunnerError("enable_coinbase_ws must be a boolean")
    if type(max_book_workers) is not int or max_book_workers <= 0:
        raise ShadowRunnerError("max_book_workers must be a positive integer")
    if type(stop_gracefully_on_rpc_error) is not bool:
        raise ShadowRunnerError("stop_gracefully_on_rpc_error must be a boolean")
    normalized_allowed_products = _normalize_allowed_products(allowed_products)
    allowed_products_set = (
        frozenset(normalized_allowed_products) if normalized_allowed_products is not None else None
    )
    if min_taker_edge_bps is not None and (
        type(min_taker_edge_bps) is not int or min_taker_edge_bps <= 0
    ):
        raise ShadowRunnerError("min_taker_edge_bps must be a positive integer")
    effective_min_taker_edge_bps = (
        min_taker_edge_bps if min_taker_edge_bps is not None else (6 if dynamic_sizing else 1)
    )
    effective_maker_min_edge = _resolve_maker_min_edge_bps(
        maker_min_edge_bps, dynamic_sizing=dynamic_sizing
    )
    active_risk_cfg = _resolve_campaign_risk_config(
        risk_config=risk_config,
        risk_config_path=risk_config_path,
        total_capital_usd=total_capital_usd,
        max_daily_loss_usd=max_daily_loss_usd,
    )
    if type(ticks) is not int or ticks <= 0:
        raise ShadowRunnerError("ticks must be a positive integer")
    if type(interval_ms) is not int or interval_ms < 0:
        raise ShadowRunnerError("interval_ms must be a non-negative integer")
    if type(max_rpc_calls) is not int or max_rpc_calls <= 0:
        raise ShadowRunnerError("max_rpc_calls must be a positive integer")
    if type(trade_notional_usdc) is not int or trade_notional_usdc <= 0:
        raise ShadowRunnerError("trade_notional_usdc must be a positive integer")
    if type(priority_fee_wei) is not int or priority_fee_wei < 0:
        raise ShadowRunnerError("priority_fee_wei must be non-negative")
    if type(gas_limit_units) is not int or gas_limit_units <= 0:
        raise ShadowRunnerError("gas_limit_units must be positive")
    for label, val in (
        ("cex_taker_fee_bps", cex_taker_fee_bps),
        ("latency_haircut_bps", latency_haircut_bps),
    ):
        if isinstance(val, bool) or not math.isfinite(float(val)) or float(val) < 0:
            raise ShadowRunnerError(f"{label} must be finite and non-negative")
    if (
        isinstance(timeout_s, bool)
        or not isinstance(timeout_s, (int, float))
        or not math.isfinite(float(timeout_s))
        or float(timeout_s) <= 0
    ):
        raise ShadowRunnerError("timeout_s must be a positive finite number")

    attribution_path, summary_path = _ensure_clean_output_dir(out_dir)
    origin, _, _ = _validate_rpc_url(
        rpc_url, enforce_public_host=rpc_transport is None and mode != "fixture"
    )

    http_pool: _KeepAliveHttpsPool | None = None
    if mode == "fixture":
        active_rpc_transport = (
            build_synthetic_multicall_rpc_transport(
                scenario=fixture_scenario,
                extra_pools=extra_fixture_pools,
            )
            if rpc_transport is None
            else rpc_transport
        )
        active_cb_transport = (
            build_synthetic_coinbase_transport()
            if coinbase_transport is None
            else coinbase_transport
        )
        multicall_mod: MulticallUniverseProtocol | None = None
    else:
        if rpc_transport is None:
            http_pool = _KeepAliveHttpsPool()
        active_rpc_transport = (
            http_pool.post
            if rpc_transport is None and http_pool is not None
            else cast(Callable[[str, bytes, float], bytes], rpc_transport)
        )
        active_cb_transport = (
            http_pool.get
            if cex_venue == "coinbase"
            and coinbase_transport is None
            and rpc_transport is None
            and http_pool is not None
            else coinbase_transport
        )
        multicall_mod = _load_multicall_universe_module()

    sleep_fn = time.sleep if sleeper is None else sleeper
    clock_fn = time.monotonic if clock is None else clock
    active_bridge = resolve_fastpath_bridge(bridge)

    rpc_calls_used = 0

    def _reserve_rpc(cost: int) -> None:
        nonlocal rpc_calls_used
        if rpc_calls_used + cost > max_rpc_calls:
            raise ShadowBudgetError(
                f"campaign RPC budget exceeded ({rpc_calls_used} + {cost} > {max_rpc_calls})"
            )
        rpc_calls_used += cost

    jsonl_handle = None
    records: list[dict[str, Any]] = []
    prev_record_sha256 = _GENESIS_PREV_SHA256
    prev_block_number: int | None = None
    prev_block_hash: str | None = None
    seen_hashes_by_block: dict[int, str] = {}
    total_rust_raw_candidates = 0
    total_quoter_verified_candidates = 0
    total_quoter_rejected_candidates = 0
    atomic_net_bps_series: list[float] = []
    cex_ok_evals = 0
    cex_degraded_evals = 0
    products_evaluated: set[str] = set()
    cex_best_net_values: list[float] = []
    cex_best_raw_values: list[float] = []
    cex_best_maker_values: list[float] = []
    cex_taker_fee_dec = (
        cex_taker_fee_bps
        if isinstance(cex_taker_fee_bps, Decimal)
        else Decimal(str(cex_taker_fee_bps))
    )
    if dynamic_sizing:
        maker_config = MakerHedgeConfig(
            trade_notional_usdc=trade_notional_usdc,
            min_quote_edge_bps=effective_maker_min_edge,
            adaptive_volatility_enabled=True,
            volatility_multiplier=Decimal("1.5"),
            min_hedge_notional_usdc=max(25_000_000, active_risk_cfg.min_clip_usd_micros),
            max_unhedged_steps=60,
            preserve_queue_priority=True,
            deduplicate_fifo_and_strict=enable_coinbase_ws,
            cex_dust_unwind_enabled=True,
            cex_dust_unwind_taker_fee_bps=cex_taker_fee_dec,
            post_fill_cooldown_steps=5,
            max_same_dir_momentum_bps=Decimal("9.0"),
        )
    else:
        maker_config = MakerHedgeConfig(
            trade_notional_usdc=trade_notional_usdc,
            min_quote_edge_bps=effective_maker_min_edge,
        )
    maker_tracker = MakerHedgeLifecycleTracker(maker_config)
    oos_tracker = OosStrategyExecutionTracker(
        OosTrackerConfig(
            max_signal_age_ms=max(30_000, interval_ms * 4),
            coinbase_taker_fee_bps=cex_taker_fee_dec,
            min_signal_edge_bps=effective_min_taker_edge_bps,
            cex_ioc_limit_enabled=dynamic_sizing,
        )
    )
    ws_feed: CoinbaseWebSocketFeed | BybitSpotWebSocketFeed | None = None
    active_usdt_peg: UsdtUsdPeg | None = None
    book_executor: concurrent.futures.ThreadPoolExecutor | None = None
    ws_candidate_products: tuple[str, ...] = ()
    ws_watermarks: dict[str, int] = {}
    ws_total_prints = 0
    ws_prints_by_product_counts: dict[str, int] = {}
    ws_gap_events = 0
    ws_reconnects = 0
    ws_rest_fallback_calls = 0
    ws_connected_ticks = 0
    shadow_daily_loss_usd_micros = 0
    shadow_cumulative_loss_usd_micros = 0
    headroom_max_clips_micros: list[int] = []
    headroom_blocked_ticks = 0
    selected_taker_notionals: list[int] = []
    selected_maker_notionals: list[int] = []
    final_headroom: HeadroomAllocation | None = None

    def _refresh_usdt_peg_if_needed(force: bool = False) -> None:
        nonlocal active_usdt_peg
        if cex_venue != "bybit":
            return
        if active_usdt_peg is not None and not force:
            return
        try:
            active_usdt_peg = fetch_usdt_usd_peg(
                timeout_s=float(timeout_s),
                transport=coinbase_transport,
            )
            if isinstance(ws_feed, BybitSpotWebSocketFeed):
                ws_feed.update_usdt_peg(active_usdt_peg)
        except BybitSpotFeedError, OSError, ssl.SSLError, TimeoutError, urllib.error.URLError:
            if active_usdt_peg is None:
                raise

    def _open_ws(products: Sequence[str]) -> CoinbaseWebSocketFeed | BybitSpotWebSocketFeed | None:
        if not products:
            return None
        if ws_feed_factory is not None:
            return ws_feed_factory(products)
        if cex_venue == "bybit":
            _refresh_usdt_peg_if_needed(force=active_usdt_peg is None)
            if active_usdt_peg is None:
                return None
            feed = BybitSpotWebSocketFeed(
                products,
                usdt_peg=active_usdt_peg,
                timeout_s=float(timeout_s),
            )
            feed.connect()
            return feed
        return CoinbaseWebSocketFeed(products, timeout_s=float(timeout_s))

    try:
        pool_specs_raw: Sequence[Any]
        discovered_pools_by_id: dict[str, Any] = {}
        if multicall_mod is not None:
            is_native_universe = (
                getattr(multicall_mod.discover_base_universe_pools, "__module__", "")
                == "oasis_agent.fastpath.multicall_universe"
            )
            if is_native_universe:
                _reserve_rpc(3)
            discovered = _call_with_supported_kwargs(
                multicall_mod.discover_base_universe_pools,
                {
                    "rpc_url": rpc_url,
                    "transport": active_rpc_transport,
                    "timeout_s": float(timeout_s),
                    "max_rpc_calls": max_rpc_calls - rpc_calls_used,
                    "include_ecosystem_tokens": include_ecosystem_tokens,
                    "include_aero_cl2": include_aero_cl2,
                },
            )
            extra_disc_calls = _attr_or_key(discovered, "rpc_calls_used", default=0)
            if not is_native_universe and type(extra_disc_calls) is int and extra_disc_calls > 0:
                _reserve_rpc(extra_disc_calls)
            extracted_pools = _attr_or_key(discovered, "pools", "pool_specs", default=discovered)
            pool_specs_raw = tuple(extracted_pools)
            for item in pool_specs_raw:
                pid = _attr_or_key(item, "pool_id", "address", default=None)
                if isinstance(pid, str) and pid:
                    discovered_pools_by_id[normalize_address(pid)] = item
        else:
            pool_specs_raw = tuple(canonical_base_weth_usdc_pools()) + tuple(extra_fixture_pools)

        _fixed_fees, tick_spacings, factories = _pool_metadata_lookup(pool_specs_raw)
        universe_pool_count = len(pool_specs_raw)
        if universe_pool_count == 0:
            raise ShadowRunnerError("discovered Base universe contains zero pools")

        discovered_cb_products = _discover_eligible_coinbase_products(pool_specs_raw)
        if cex_venue == "bybit":
            discovered_cb_products = tuple(
                p for p in discovered_cb_products if p in BYBIT_SYMBOL_BY_PRODUCT
            )
            _refresh_usdt_peg_if_needed(force=True)
        ws_candidate_products = (
            tuple(p for p in discovered_cb_products if p in allowed_products_set)
            if allowed_products_set is not None
            else discovered_cb_products
        )
        if (
            enable_coinbase_ws
            and ws_candidate_products
            and (mode == "public-shadow" or ws_feed_factory is not None)
        ):
            try:
                ws_feed = _open_ws(ws_candidate_products)
            except (
                CoinbaseTradeFlowError,
                BybitSpotFeedError,
                OSError,
                ssl.SSLError,
                TimeoutError,
            ):
                ws_feed = None
                ws_gap_events += 1

        if coinbase_transport is None and mode == "public-shadow" and max_book_workers > 1:
            worker_count = min(max_book_workers, max(1, len(ws_candidate_products)))
            book_executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=worker_count,
                thread_name_prefix="cb-book",
            )

        def _read_snapshot_at_tag(tag: str) -> PinnedTickSnapshot:
            _reserve_rpc(2)
            if multicall_mod is not None:
                snap_kwargs: dict[str, Any] = {
                    "pools": pool_specs_raw,
                    "pool_specs": pool_specs_raw,
                    "specs": pool_specs_raw,
                    "universe": pool_specs_raw,
                    "rpc_url": rpc_url,
                    "transport": active_rpc_transport,
                    "timeout_s": float(timeout_s),
                    "block_tag": tag,
                }
                raw_snap = _call_with_supported_kwargs(
                    multicall_mod.fetch_pinned_universe_snapshot,
                    snap_kwargs,
                    first_positional_fallback=pool_specs_raw,
                )
                snap = _extract_snapshot_from_multicall_universe(raw_snap)
                if snap.rpc_calls_used > 2:
                    _reserve_rpc(snap.rpc_calls_used - 2)
                return snap
            return _fetch_pinned_multicall3_fallback(
                active_rpc_transport,
                rpc_url,
                cast(Sequence[BasePoolSpec], pool_specs_raw),
                timeout_s=float(timeout_s),
                block_tag=tag,
            )

        stopped_early = False
        stop_reason: str | None = None
        last_tick_started: float | None = None
        for tick_idx in range(ticks):
            if tick_idx > 0 and interval_ms > 0 and last_tick_started is not None:
                elapsed = max(0.0, clock_fn() - last_tick_started)
                remaining = (interval_ms / 1000.0) - elapsed
                if remaining > 0:
                    sleep_fn(remaining)
            last_tick_started = clock_fn()

            calls_before_tick = rpc_calls_used
            try:
                snapshot = _read_snapshot_at_tag("latest")

                seen_hash = seen_hashes_by_block.get(snapshot.block_number)
                if seen_hash is not None and seen_hash != snapshot.block_hash:
                    raise ShadowRunnerError(
                        f"reorg detected at block {snapshot.block_number}: block hash changed"
                    )
                if (
                    prev_block_number is not None
                    and snapshot.block_number == prev_block_number + 1
                    and snapshot.parent_hash is not None
                    and snapshot.parent_hash != prev_block_hash
                ):
                    raise ShadowRunnerError(
                        f"reorg detected at block {snapshot.block_number}: parent_hash mismatch"
                    )
                if prev_block_number is not None and snapshot.block_number < prev_block_number:
                    seen_hashes_by_block.setdefault(snapshot.block_number, snapshot.block_hash)
                    refetched = _read_snapshot_at_tag(hex(prev_block_number))
                    if (
                        refetched.block_number != prev_block_number
                        or refetched.block_hash != prev_block_hash
                    ):
                        raise ShadowRunnerError(
                            f"stale/regressed block number {snapshot.block_number} < "
                            f"{prev_block_number}"
                        )
                    snapshot = refetched
            except RpcError as exc:
                if stop_gracefully_on_rpc_error and records:
                    stopped_early = True
                    stop_reason = str(exc)
                    break
                raise
            seen_hashes_by_block[snapshot.block_number] = snapshot.block_hash

            graph = FastpathPoolGraph(bridge=active_bridge)
            nodes_by_id: dict[str, PoolNode] = {}
            for node in snapshot.nodes:
                graph.upsert_pool(node)
                nodes_by_id[node.pool_id.lower()] = node

            l2_gas_cost_wei = gas_limit_units * (snapshot.base_fee_per_gas_wei + priority_fee_wei)
            total_gas_cost_wei = l2_gas_cost_wei + snapshot.l1_data_fee_wei
            eth_price_micro_usdc = _compute_eth_price_micro_usdc(snapshot.nodes)
            eth_price_usd = Decimal(eth_price_micro_usdc) / Decimal(1_000_000)

            l2_gas_usdc = (l2_gas_cost_wei * eth_price_micro_usdc + 10**18 - 1) // 10**18
            l1_fee_usdc = (snapshot.l1_data_fee_wei * eth_price_micro_usdc + 10**18 - 1) // 10**18

            acct_snap = AccountCapitalSnapshot(
                cash_available_usd_micros=active_risk_cfg.total_capital_usd_micros,
                locked_inventory_usd_micros=(
                    maker_tracker.unhedged_exposure_usd_micros() if dynamic_sizing else 0
                ),
                pending_commitments_usd_micros=0,
                realized_daily_loss_usd_micros=shadow_daily_loss_usd_micros,
                unrealized_drawdown_usd_micros=(
                    maker_tracker.unrealized_drawdown_usd_micros() if dynamic_sizing else 0
                ),
                cumulative_loss_usd_micros=shadow_cumulative_loss_usd_micros,
                unresolved_residuals=0,
                halted=False,
            )
            headroom = compute_deployable_headroom(active_risk_cfg, acct_snap)
            final_headroom = headroom
            headroom_max_clips_micros.append(headroom.max_clip_usd_micros)
            if not headroom.can_deploy:
                headroom_blocked_ticks += 1

            min_clip_usdc = active_risk_cfg.usd_micros_to_usdc_raw(headroom.min_clip_usd_micros)
            max_clip_usdc = active_risk_cfg.usd_micros_to_usdc_raw(headroom.max_clip_usd_micros)

            if dynamic_sizing and headroom.can_deploy and max_clip_usdc > 0:
                scan_weth_max = max(
                    WETH_SCAN_MIN_WEI,
                    (headroom.max_clip_usd_micros * 10**18) // max(1, eth_price_micro_usdc),
                )
                scan_weth_min = min(
                    scan_weth_max,
                    max(
                        WETH_SCAN_MIN_WEI,
                        (headroom.min_clip_usd_micros * 10**18) // max(1, eth_price_micro_usdc),
                    ),
                )
                scan_usdc_max = max(USDC_SCAN_MIN_RAW, max_clip_usdc)
                scan_usdc_min = min(scan_usdc_max, max(USDC_SCAN_MIN_RAW, min_clip_usdc))
            else:
                scan_weth_min = WETH_SCAN_MIN_WEI
                scan_weth_max = WETH_SCAN_MAX_WEI
                scan_usdc_min = USDC_SCAN_MIN_RAW
                scan_usdc_max = USDC_SCAN_MAX_RAW

            if dynamic_sizing and not headroom.can_deploy:
                weth_scan = graph.scan_cycles(
                    [WETH_ADDR],
                    max_hops=3,
                    min_amount_in=WETH_SCAN_MIN_WEI,
                    max_amount_in=WETH_SCAN_MIN_WEI,
                    gas_cost_in_base=l2_gas_cost_wei,
                    l1_fee_in_base=snapshot.l1_data_fee_wei,
                    min_net_profit=1,
                    block_number=snapshot.block_number,
                    timestamp_ns=snapshot.base_time_ms * 1_000_000,
                )
                usdc_scan = graph.scan_cycles(
                    [USDC_ADDR],
                    max_hops=3,
                    min_amount_in=USDC_SCAN_MIN_RAW,
                    max_amount_in=USDC_SCAN_MIN_RAW,
                    gas_cost_in_base=l2_gas_usdc,
                    l1_fee_in_base=l1_fee_usdc,
                    min_net_profit=1,
                    block_number=snapshot.block_number,
                    timestamp_ns=snapshot.base_time_ms * 1_000_000,
                )
                rust_raw_candidates = []
            else:
                weth_scan = graph.scan_cycles(
                    [WETH_ADDR],
                    max_hops=3,
                    min_amount_in=scan_weth_min,
                    max_amount_in=scan_weth_max,
                    gas_cost_in_base=l2_gas_cost_wei,
                    l1_fee_in_base=snapshot.l1_data_fee_wei,
                    min_net_profit=1,
                    block_number=snapshot.block_number,
                    timestamp_ns=snapshot.base_time_ms * 1_000_000,
                )
                usdc_scan = graph.scan_cycles(
                    [USDC_ADDR],
                    max_hops=3,
                    min_amount_in=scan_usdc_min,
                    max_amount_in=scan_usdc_max,
                    gas_cost_in_base=l2_gas_usdc,
                    l1_fee_in_base=l1_fee_usdc,
                    min_net_profit=1,
                    block_number=snapshot.block_number,
                    timestamp_ns=snapshot.base_time_ms * 1_000_000,
                )
                rust_raw_candidates = [
                    c for c in (*weth_scan.candidates, *usdc_scan.candidates) if c.net_profit > 0
                ]
            rust_raw_candidates.sort(
                key=lambda c: (c.net_profit * 10_000) / max(1, c.optimal_amount_in),
                reverse=True,
            )
            total_rust_raw_candidates += len(rust_raw_candidates)

            quoter_verified_list: list[dict[str, Any]] = []
            quoter_rejected_count = 0
            for cand in rust_raw_candidates[:MAX_CANDIDATES_TO_QUOTE_PER_TICK]:
                hop_cost = len(cand.hops)
                if rpc_calls_used + hop_cost > max_rpc_calls:
                    quoter_rejected_count += 1
                    continue
                _reserve_rpc(hop_cost)
                if multicall_mod is not None:
                    try:
                        raw_q = _call_with_supported_kwargs(
                            multicall_mod.verify_candidate_with_onchain_quoter,
                            {
                                "candidate": cand,
                                "hops": cand.hops,
                                "amount_in": cand.optimal_amount_in,
                                "gas_cost_in_base": cand.gas_cost_in_base,
                                "l1_fee_in_base": cand.l1_fee_in_base,
                                "pools": pool_specs_raw,
                                "pool_specs": pool_specs_raw,
                                "specs": pool_specs_raw,
                                "nodes": snapshot.nodes,
                                "nodes_by_id": nodes_by_id,
                                "pools_by_id": nodes_by_id,
                                "rpc_url": rpc_url,
                                "block_number": snapshot.block_number,
                                "pinned_block": snapshot.block_number,
                                "block_tag": hex(snapshot.block_number),
                                "block_number_hex": hex(snapshot.block_number),
                                "balances_by_pool": snapshot.balances_by_pool,
                                "discovered_pools_by_id": discovered_pools_by_id or None,
                                "bridge": active_bridge,
                                "transport": active_rpc_transport,
                                "timeout_s": float(timeout_s),
                            },
                            first_positional_fallback=cand,
                        )
                        q_res = _interpret_quoter_verification_result(raw_q, cand)
                    except (RpcError, ShadowRunnerError, ValueError) as exc:
                        q_res = QuoterCheckResult(
                            verified=False,
                            quoter_amount_out=0,
                            quoter_net_profit=-(cand.gas_cost_in_base + cand.l1_fee_in_base),
                            rpc_calls_used=hop_cost,
                            reason=f"quoter_bridge_failed:{exc}",
                        )
                    if q_res.rpc_calls_used > hop_cost:
                        _reserve_rpc(q_res.rpc_calls_used - hop_cost)
                else:
                    q_res = _verify_candidate_fallback(
                        active_rpc_transport,
                        rpc_url,
                        candidate=cand,
                        nodes_by_id=nodes_by_id,
                        pinned_block_number=snapshot.block_number,
                        timeout_s=float(timeout_s),
                        tick_spacings=tick_spacings,
                        factories=factories,
                    )

                if q_res.verified and q_res.quoter_net_profit > 0:
                    verified_net_bps = round(
                        float(
                            (Decimal(q_res.quoter_net_profit) * Decimal(10_000))
                            / Decimal(cand.optimal_amount_in)
                        ),
                        6,
                    )
                    gross_gain = q_res.quoter_amount_out - cand.optimal_amount_in
                    verified_gross_bps = round(
                        float(
                            (Decimal(gross_gain) * Decimal(10_000))
                            / Decimal(cand.optimal_amount_in)
                        ),
                        6,
                    )
                    quoter_verified_list.append(
                        {
                            "base_token": cand.base_token,
                            "hops": len(cand.hops),
                            "optimal_amount_in": str(cand.optimal_amount_in),
                            "quoter_amount_out": str(q_res.quoter_amount_out),
                            "quoter_net_profit": str(q_res.quoter_net_profit),
                            "net_bps": verified_net_bps,
                            "gross_bps": verified_gross_bps,
                        }
                    )
                else:
                    quoter_rejected_count += 1

            total_quoter_verified_candidates += len(quoter_verified_list)
            total_quoter_rejected_candidates += quoter_rejected_count

            pairwise_gross_bps, pairwise_net_bps = _measure_weth_usdc_pairwise_spread(
                graph,
                snapshot.nodes,
                l2_gas_cost_wei=l2_gas_cost_wei,
                l1_data_fee_wei=snapshot.l1_data_fee_wei,
            )
            if quoter_verified_list:
                atomic_best_net_bps = max(float(v["net_bps"]) for v in quoter_verified_list)
                atomic_best_gross_bps = max(float(v["gross_bps"]) for v in quoter_verified_list)
                atomic_positive = True
            else:
                atomic_best_net_bps = pairwise_net_bps
                atomic_best_gross_bps = pairwise_gross_bps
                atomic_positive = False
            atomic_net_bps_series.append(atomic_best_net_bps)

            # Refresh USDT-USD peg periodically when evaluating Bybit Spot
            if cex_venue == "bybit" and tick_idx > 0 and tick_idx % 30 == 0:
                _refresh_usdt_peg_if_needed(force=True)

            # Drain CEX WebSocket trade prints once per tick (REST trades fallback on gap)
            ws_gap_tick = False
            tick_prints_by_product: dict[str, list[CoinbaseTradePrint]] = {}
            if enable_coinbase_ws:
                if (
                    ws_feed is None
                    and ws_candidate_products
                    and (mode == "public-shadow" or ws_feed_factory is not None)
                ):
                    ws_gap_tick = True
                    try:
                        ws_feed = _open_ws(ws_candidate_products)
                        ws_reconnects += 1
                    except (
                        CoinbaseTradeFlowError,
                        BybitSpotFeedError,
                        OSError,
                        ssl.SSLError,
                        TimeoutError,
                    ):
                        ws_feed = None
                if ws_feed is not None:
                    try:
                        drained_prints, ws_gap = ws_feed.drain_events()
                        ws_connected_ticks += 1
                        if ws_gap:
                            ws_gap_tick = True
                            ws_gap_events += 1
                            with suppress(Exception):
                                ws_feed.close()
                            ws_feed = None
                        for tp in drained_prints:
                            prev_tid = ws_watermarks.get(tp.product_id, 0)
                            if tp.trade_id > prev_tid:
                                ws_watermarks[tp.product_id] = tp.trade_id
                                tick_prints_by_product.setdefault(tp.product_id, []).append(tp)
                                ws_total_prints += 1
                                ws_prints_by_product_counts[tp.product_id] = (
                                    ws_prints_by_product_counts.get(tp.product_id, 0) + 1
                                )
                    except (
                        CoinbaseTradeFlowError,
                        BybitSpotFeedError,
                        OSError,
                        ssl.SSLError,
                        TimeoutError,
                    ):
                        ws_gap_tick = True
                        ws_gap_events += 1
                        with suppress(Exception):
                            ws_feed.close()
                        ws_feed = None
                is_live_net = (
                    mode == "public-shadow" and rpc_transport is None and coinbase_transport is None
                )
                if (
                    (ws_gap_tick or ws_feed is None)
                    and ws_candidate_products
                    and (trades_transport is not None or is_live_net)
                ):
                    active_trades_tr = (
                        trades_transport if trades_transport is not None else active_cb_transport
                    )
                    for fb_prod in ws_candidate_products:
                        ws_rest_fallback_calls += 1
                        try:
                            if cex_venue == "bybit":
                                if active_usdt_peg is None:
                                    continue
                                rest_prints = fetch_bybit_recent_trades(
                                    fb_prod,
                                    usdt_peg=active_usdt_peg,
                                    limit=60,
                                    since_trade_id=ws_watermarks.get(fb_prod),
                                    timeout_s=float(timeout_s),
                                    transport=active_trades_tr,
                                )
                            else:
                                rest_prints = fetch_coinbase_recent_trades(
                                    fb_prod,
                                    limit=25,
                                    since_trade_id=ws_watermarks.get(fb_prod),
                                    timeout_s=float(timeout_s),
                                    transport=active_trades_tr,
                                )
                        except (
                            CoinbaseTradeFlowError,
                            BybitSpotFeedError,
                            LiveCexDexError,
                            OSError,
                            urllib.error.URLError,
                        ):
                            continue
                        for tp in rest_prints:
                            prev_tid = ws_watermarks.get(tp.product_id, 0)
                            if tp.trade_id > prev_tid:
                                ws_watermarks[tp.product_id] = tp.trade_id
                                tick_prints_by_product.setdefault(tp.product_id, []).append(tp)
                                ws_total_prints += 1
                                ws_prints_by_product_counts[tp.product_id] = (
                                    ws_prints_by_product_counts.get(tp.product_id, 0) + 1
                                )

            # Evaluate Coinbase <-> Base CEX-DEX & Maker-Hedge lifecycle across 1/2-hop pairs
            gas_cost_usd = gas_wei_to_usd(total_gas_cost_wei, eth_price_usd)
            cex_evals_dict: dict[str, dict[str, Any]] = {}
            maker_tick_dict: dict[str, dict[str, Any]] = {}
            tick_cex_ok: list[CexDexTickEvaluation] = []
            pending_notional_by_product = {
                intent.product_id: intent.notional_usdc_micros
                for intent in oos_tracker.pending_intents
            }
            tick_dex_quotes_by_product: dict[
                str, tuple[int, int | None, int, int | None, int, Decimal]
            ] = {}
            tick_bbo_by_product: dict[str, tuple[Decimal, Decimal]] = {}
            tick_gas_by_product: dict[str, Decimal] = {}
            tick_maker_realized_fills: list[MakerFillEvent] = []
            probe_notional = (
                min_clip_usdc if dynamic_sizing and min_clip_usdc > 0 else trade_notional_usdc
            )
            active_token_quotes: list[
                tuple[
                    TokenMeta,
                    Callable[[int], tuple[int | None, int, int | None]],
                    int | None,
                    int,
                    int | None,
                ]
            ] = []
            for tok_addr in CEX_DEX_TOKEN_ORDER:
                meta = TOKEN_META_BY_ADDR[tok_addr]
                if meta.coinbase_product is None:
                    continue
                if cex_venue == "bybit" and meta.coinbase_product not in BYBIT_SYMBOL_BY_PRODUCT:
                    continue
                if (
                    allowed_products_set is not None
                    and meta.coinbase_product not in allowed_products_set
                ):
                    continue
                quote_dex_cached = _make_dex_quote_cache_fn(
                    graph,
                    snapshot.nodes,
                    tok_addr=tok_addr,
                    token_decimals=meta.decimals,
                )
                dex_buy_out, notional_tok, dex_sell_out = quote_dex_cached(probe_notional)
                if notional_tok <= 0:
                    continue
                active_token_quotes.append(
                    (meta, quote_dex_cached, dex_buy_out, notional_tok, dex_sell_out)
                )

            prefetched_books: dict[str, tuple[CoinbaseL2Book | None, str | None]] = {}
            if book_executor is not None and active_token_quotes:
                future_by_product = {
                    cast(str, meta.coinbase_product): book_executor.submit(
                        _fetch_cex_book_for_tick,
                        cast(str, meta.coinbase_product),
                        cex_venue=cex_venue,
                        usdt_peg=active_usdt_peg,
                        timeout_s=float(timeout_s),
                        transport=active_cb_transport,
                    )
                    for meta, _quote_fn, _buy_out, _tok_amt, _sell_out in active_token_quotes
                }
                for product_id, fut in future_by_product.items():
                    prefetched_books[product_id] = fut.result()

            for (
                meta,
                quote_dex_cached,
                dex_buy_out,
                notional_tok,
                dex_sell_out,
            ) in active_token_quotes:
                product_id = cast(str, meta.coinbase_product)
                products_evaluated.add(product_id)
                if product_id in prefetched_books:
                    fetched_book, cb_err = prefetched_books[product_id]
                else:
                    fetched_book, cb_err = _fetch_cex_book_for_tick(
                        product_id,
                        cex_venue=cex_venue,
                        usdt_peg=active_usdt_peg,
                        timeout_s=float(timeout_s),
                        transport=active_cb_transport,
                    )
                if fetched_book is None:
                    cb_spec = resolve_coinbase_product(meta.coinbase_product)
                    ev = CexDexTickEvaluation(
                        product_id=cb_spec.product_id,
                        token_symbol=cb_spec.token_symbol,
                        status="degraded",
                        error_reason=cb_err or "coinbase_book_unavailable",
                        cex_taker_fee_bps=float(cex_taker_fee_bps),
                        latency_haircut_bps=float(latency_haircut_bps),
                        gas_cost_usd=round(float(gas_cost_usd), 6),
                        best_bid_usd=None,
                        best_ask_usd=None,
                        cex_mid_usd=None,
                        book_sequence=None,
                        book_sha256=None,
                        buy_dex_sell_cex=None,
                        buy_cex_sell_dex=None,
                        best_direction=None,
                        best_net_bps=None,
                        best_raw_spread_bps=None,
                        maker_zero_fee_net_bps=None,
                        book_level=2,
                    )
                    cex_evals_dict[meta.coinbase_product] = ev.to_dict()
                    cex_degraded_evals += 1
                    continue

                if dynamic_sizing:
                    if not headroom.can_deploy or max_clip_usdc < min_clip_usdc:
                        cb_spec = resolve_coinbase_product(meta.coinbase_product)
                        ev = CexDexTickEvaluation(
                            product_id=cb_spec.product_id,
                            token_symbol=cb_spec.token_symbol,
                            status="degraded",
                            error_reason=f"risk_headroom_blocked:{headroom.block_reason}",
                            cex_taker_fee_bps=float(cex_taker_fee_bps),
                            latency_haircut_bps=float(latency_haircut_bps),
                            gas_cost_usd=round(float(gas_cost_usd), 6),
                            best_bid_usd=round(float(fetched_book.best_bid), 6),
                            best_ask_usd=round(float(fetched_book.best_ask), 6),
                            cex_mid_usd=round(float(fetched_book.mid_price), 6),
                            book_sequence=fetched_book.sequence,
                            book_sha256=fetched_book.raw_sha256,
                            buy_dex_sell_cex=None,
                            buy_cex_sell_dex=None,
                            best_direction=None,
                            best_net_bps=None,
                            best_raw_spread_bps=None,
                            maker_zero_fee_net_bps=None,
                            book_level=fetched_book.book_level,
                            selected_notional_usdc=None,
                            book_capacity_usdc=None,
                        )
                    else:
                        ev = evaluate_dynamic_cex_dex_opportunity(
                            product_or_symbol=meta.coinbase_product,
                            book=fetched_book,
                            min_notional_usdc=min_clip_usdc,
                            max_notional_usdc=max_clip_usdc,
                            quote_dex_for_usdc_fn=quote_dex_cached,
                            gas_cost_usd=gas_cost_usd,
                            cex_taker_fee_bps=cex_taker_fee_bps,
                            latency_haircut_bps=latency_haircut_bps,
                        )
                        if ev.selected_notional_usdc is not None:
                            selected_taker_notionals.append(ev.selected_notional_usdc)
                else:
                    ev = evaluate_live_cex_dex_shadow(
                        product_or_symbol=meta.coinbase_product,
                        trade_notional_usdc=trade_notional_usdc,
                        dex_buy_token_for_usdc_out=dex_buy_out,
                        trade_notional_token=notional_tok,
                        dex_sell_token_for_usdc_out=dex_sell_out,
                        gas_cost_usd=gas_cost_usd,
                        cex_taker_fee_bps=cex_taker_fee_bps,
                        latency_haircut_bps=latency_haircut_bps,
                        timeout_s=float(timeout_s),
                        book=fetched_book,
                        transport=active_cb_transport,
                    )

                cex_evals_dict[meta.coinbase_product] = ev.to_dict()
                if ev.status == "ok" and ev.best_net_bps is not None:
                    cex_ok_evals += 1
                    tick_cex_ok.append(ev)
                else:
                    cex_degraded_evals += 1

                if dynamic_sizing and headroom.can_deploy and max_clip_usdc >= min_clip_usdc:
                    peek_adv_bps = maker_tracker.peek_effective_adverse_bps(
                        meta.coinbase_product, fetched_book.mid_price
                    )
                    maker_notional_usdc, (m_buy_out, m_tok, m_sell_out) = (
                        _select_dynamic_maker_quotes(
                            product_id=meta.coinbase_product,
                            token_decimals=meta.decimals,
                            fetched_book=fetched_book,
                            quote_fn=quote_dex_cached,
                            gas_cost_usd=gas_cost_usd,
                            maker_config=maker_config,
                            min_clip_usdc=min_clip_usdc,
                            max_clip_usdc=max_clip_usdc,
                            fallback_notional_usdc=min_clip_usdc,
                            effective_adverse_bps=peek_adv_bps,
                        )
                    )
                    selected_maker_notionals.append(maker_notional_usdc)
                elif dynamic_sizing:
                    maker_notional_usdc = probe_notional
                    m_buy_out, m_tok, m_sell_out = dex_buy_out, notional_tok, dex_sell_out
                else:
                    maker_notional_usdc = trade_notional_usdc
                    m_buy_out, m_tok, m_sell_out = dex_buy_out, notional_tok, dex_sell_out

                prod_prints = tuple(tick_prints_by_product.get(meta.coinbase_product, ()))
                if not dynamic_sizing or (headroom.can_deploy and max_clip_usdc >= min_clip_usdc):
                    maker_step = maker_tracker.step(
                        meta.coinbase_product,
                        current_book=fetched_book,
                        trade_notional_usdc=maker_notional_usdc,
                        dex_buy_token_out=m_buy_out,
                        trade_notional_token=m_tok,
                        dex_sell_usdc_out=m_sell_out,
                        token_decimals=meta.decimals,
                        gas_cost_usd=gas_cost_usd,
                        recent_trades=prod_prints,
                        feed_gap_detected=ws_gap_tick,
                    )
                    for realized_fill in (*maker_step.fifo_fills, *maker_step.strict_fills):
                        tick_maker_realized_fills.append(realized_fill)
                        if realized_fill.realized_net_profit_usd < 0:
                            loss_micros = round(
                                float(-realized_fill.realized_net_profit_usd) * 1_000_000
                            )
                            shadow_daily_loss_usd_micros += loss_micros
                            shadow_cumulative_loss_usd_micros += loss_micros
                    maker_tick_dict[meta.coinbase_product] = {
                        "selected_notional_usdc": maker_notional_usdc,
                        "prints_observed": len(prod_prints),
                        "quote": maker_step.quote.to_dict(),
                        "strict_fills": [f.to_dict() for f in maker_step.strict_fills],
                        "fifo_fills": [f.to_dict() for f in maker_step.fifo_fills],
                        "touch_lift_fills": [f.to_dict() for f in maker_step.touch_lift_fills],
                        "unhedged_fill_reasons": list(maker_step.unhedged_fill_reasons),
                        "execution_eligible": False,
                    }
                else:
                    maker_tick_dict[meta.coinbase_product] = {
                        "selected_notional_usdc": 0,
                        "prints_observed": len(prod_prints),
                        "blocked_reason": headroom.block_reason,
                        "quote": None,
                        "strict_fills": [],
                        "fifo_fills": [],
                        "touch_lift_fills": [],
                        "unhedged_fill_reasons": [],
                        "execution_eligible": False,
                    }

                tick_bbo_by_product[meta.coinbase_product] = (
                    fetched_book.best_bid,
                    fetched_book.best_ask,
                )
                tick_gas_by_product[meta.coinbase_product] = gas_cost_usd
                ref_usdc = pending_notional_by_product.get(meta.coinbase_product)
                if ref_usdc is None:
                    ref_usdc = (
                        ev.selected_notional_usdc
                        if (ev.status == "ok" and ev.selected_notional_usdc is not None)
                        else maker_notional_usdc
                    )
                if ref_usdc <= 0:
                    ref_usdc = probe_notional
                if ref_usdc == probe_notional:
                    ref_buy_out, ref_tok, ref_sell_out = dex_buy_out, notional_tok, dex_sell_out
                else:
                    ref_buy_out, ref_tok, ref_sell_out = quote_dex_cached(ref_usdc)
                if ref_tok > 0:
                    tick_dex_quotes_by_product[meta.coinbase_product] = (
                        ref_usdc,
                        ref_buy_out,
                        ref_tok,
                        ref_sell_out,
                        meta.decimals,
                        gas_cost_usd,
                    )

            observed_at_utc = (
                datetime.fromtimestamp(snapshot.base_time_ms / 1000.0, tz=UTC)
                .isoformat()
                .replace("+00:00", "Z")
            )
            if tick_idx == ticks - 1 and maker_tracker.open_lots():
                flushed_fills = maker_tracker.flush_all_unhedged(
                    tick_dex_quotes_by_product,
                    recorded_at_utc=observed_at_utc,
                )
                for flushed_fill in flushed_fills:
                    if flushed_fill.realized_net_profit_usd < 0:
                        loss_micros = round(
                            float(-flushed_fill.realized_net_profit_usd) * 1_000_000
                        )
                        shadow_daily_loss_usd_micros += loss_micros
                        shadow_cumulative_loss_usd_micros += loss_micros
                    prod_entry = maker_tick_dict.get(flushed_fill.product_id)
                    if prod_entry is not None:
                        prod_entry.setdefault("flushed_fills", []).append(flushed_fill.to_dict())
                tick_maker_realized_fills.extend(flushed_fills)

            cex_dex_best_net_bps: float | None = None
            cex_dex_best_raw_bps: float | None = None
            cex_dex_best_maker_bps: float | None = None
            cex_dex_positive = False
            if tick_cex_ok:
                cex_dex_best_net_bps = max(cast(float, e.best_net_bps) for e in tick_cex_ok)
                raw_vals = [
                    e.best_raw_spread_bps for e in tick_cex_ok if e.best_raw_spread_bps is not None
                ]
                if raw_vals:
                    cex_dex_best_raw_bps = max(raw_vals)
                    cex_best_raw_values.append(cex_dex_best_raw_bps)
                maker_vals = [
                    e.maker_zero_fee_net_bps
                    for e in tick_cex_ok
                    if e.maker_zero_fee_net_bps is not None
                ]
                if maker_vals:
                    cex_dex_best_maker_bps = max(maker_vals)
                    cex_best_maker_values.append(cex_dex_best_maker_bps)
                cex_best_net_values.append(cex_dex_best_net_bps)
                cex_dex_positive = cex_dex_best_net_bps > 0.0

            tick_net_candidates = [atomic_best_net_bps]
            if cex_dex_best_net_bps is not None:
                tick_net_candidates.append(cex_dex_best_net_bps)
            tick_best_net_bps = max(tick_net_candidates)

            tick_raw_candidates = [atomic_best_gross_bps]
            if cex_dex_best_raw_bps is not None:
                tick_raw_candidates.append(cex_dex_best_raw_bps)
            tick_best_raw_spread_bps = max(tick_raw_candidates)
            tick_positive = atomic_positive or cex_dex_positive

            seq = tick_idx + 1
            oos_step = oos_tracker.step_tick(
                step_seq=seq,
                observed_at_utc=observed_at_utc,
                observed_at_ms=snapshot.base_time_ms,
                cex_dex_opportunities=tick_cex_ok,
                latest_dex_quotes_by_product=tick_dex_quotes_by_product,
                latest_bbo_by_product=tick_bbo_by_product,
                maker_fills=tick_maker_realized_fills,
                gas_cost_usd_by_product=tick_gas_by_product,
                coinbase_taker_fee_bps=cex_taker_fee_dec,
            )
            for settled_taker in oos_step.settled_taker_trades:
                if settled_taker.realized_net_profit_usd < 0:
                    loss_micros = round(float(-settled_taker.realized_net_profit_usd) * 1_000_000)
                    shadow_daily_loss_usd_micros += loss_micros
                    shadow_cumulative_loss_usd_micros += loss_micros

            record_body: dict[str, Any] = {
                "schema_version": RECORD_SCHEMA_VERSION,
                "seq": seq,
                "prev_record_sha256": prev_record_sha256,
                "execution_eligible": False,
                "mode": mode,
                "chain_id": BASE_CHAIN_ID,
                "rpc_origin": origin,
                "block_number": snapshot.block_number,
                "block_hash": snapshot.block_hash,
                "header_timestamp": snapshot.header_timestamp,
                "base_time_ms": snapshot.base_time_ms,
                "l2_gas_cost_wei": l2_gas_cost_wei,
                "l1_data_fee_wei": snapshot.l1_data_fee_wei,
                "total_gas_cost_wei": total_gas_cost_wei,
                "eth_price_usd": round(float(eth_price_usd), 4),
                "universe_pool_count": len(snapshot.nodes),
                "rpc_calls_this_tick": rpc_calls_used - calls_before_tick,
                "rpc_calls_used_total": rpc_calls_used,
                "rust_cycles_evaluated": weth_scan.cycles_evaluated + usdc_scan.cycles_evaluated,
                "rust_raw_candidates_count": len(rust_raw_candidates),
                "quoter_verified_candidates_count": len(quoter_verified_list),
                "quoter_rejected_candidates_count": quoter_rejected_count,
                "quoter_verified_candidates": quoter_verified_list,
                "atomic_best_gross_bps": atomic_best_gross_bps,
                "atomic_best_net_bps": atomic_best_net_bps,
                "atomic_positive": atomic_positive,
                "headroom": headroom.to_dict(),
                "cex_dex_evaluations": cex_evals_dict,
                "maker_hedge": maker_tick_dict,
                "oos_step": oos_step.to_dict(),
                "cex_dex_best_net_bps": cex_dex_best_net_bps,
                "cex_dex_best_raw_spread_bps": cex_dex_best_raw_bps,
                "cex_dex_best_maker_diagnostic_bps": cex_dex_best_maker_bps,
                "cex_dex_positive": cex_dex_positive,
                "tick_best_net_bps": tick_best_net_bps,
                "tick_best_raw_spread_bps": tick_best_raw_spread_bps,
                "tick_positive": tick_positive,
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

            prev_record_sha256 = record_sha256
            prev_block_number = snapshot.block_number
            prev_block_hash = snapshot.block_hash
    finally:
        if jsonl_handle is not None:
            jsonl_handle.close()
        if book_executor is not None:
            book_executor.shutdown(wait=True)
        if ws_feed is not None:
            with suppress(Exception):
                ws_feed.close()
        if http_pool is not None:
            http_pool.close()

    chain_head_sha256 = verify_campaign_attribution_chain(records)
    ticks_completed = len(records)
    net_series = [float(rec["tick_best_net_bps"]) for rec in records]
    raw_series = [float(rec["tick_best_raw_spread_bps"]) for rec in records]

    mean_net_raw = sum(net_series) / ticks_completed
    if ticks_completed >= 2:
        variance = sum((x - mean_net_raw) ** 2 for x in net_series) / (ticks_completed - 1)
        std_net_raw = math.sqrt(variance)
        std_error = std_net_raw / math.sqrt(ticks_completed)
        lcb_exact = mean_net_raw - _LCB_Z_SCORE * std_error
    else:
        std_net_raw = 0.0
        lcb_exact = mean_net_raw

    lcb_int = math.floor(lcb_exact)
    atomic_positive_ticks = sum(1 for rec in records if bool(rec["atomic_positive"]))
    cex_dex_positive_ticks = sum(1 for rec in records if bool(rec["cex_dex_positive"]))
    positive_net_ticks = sum(1 for rec in records if bool(rec["tick_positive"]))
    unique_blocks_observed = len({int(rec["block_number"]) for rec in records})

    atomic_dex_summary: dict[str, Any] = {
        "total_rust_raw_candidates": total_rust_raw_candidates,
        "total_quoter_verified_candidates": total_quoter_verified_candidates,
        "total_quoter_rejected_candidates": total_quoter_rejected_candidates,
        "atomic_positive_net_ticks": atomic_positive_ticks,
        "best_atomic_net_bps": round(max(atomic_net_bps_series), 6),
        "mean_atomic_net_bps": round(sum(atomic_net_bps_series) / ticks_completed, 6),
        "best_atomic_raw_spread_bps": round(
            max(float(rec["atomic_best_gross_bps"]) for rec in records), 6
        ),
    }

    cex_dex_lead_lag_summary: dict[str, Any] = {
        "products_evaluated": sorted(products_evaluated),
        "ok_evaluations": cex_ok_evals,
        "degraded_evaluations": cex_degraded_evals,
        "cex_dex_positive_net_ticks": cex_dex_positive_ticks,
        "best_cex_dex_net_bps": round(max(cex_best_net_values), 6) if cex_best_net_values else None,
        "best_cex_dex_raw_spread_bps": (
            round(max(cex_best_raw_values), 6) if cex_best_raw_values else None
        ),
        "best_maker_zero_fee_diagnostic_bps": (
            round(max(cex_best_maker_values), 6) if cex_best_maker_values else None
        ),
        "maker_zero_fee_diagnostic_only": True,
        "cex_taker_fee_bps": float(cex_taker_fee_bps),
        "latency_haircut_bps": float(latency_haircut_bps),
    }

    headroom_summary: dict[str, Any] = {
        "risk_config_sha256": active_risk_cfg.config_sha256,
        "total_capital_usd_micros": active_risk_cfg.total_capital_usd_micros,
        "max_daily_loss_usd_micros": active_risk_cfg.max_daily_loss_usd_micros,
        "max_cumulative_loss_usd_micros": active_risk_cfg.max_cumulative_loss_usd_micros,
        "min_clip_usd_micros": active_risk_cfg.min_clip_usd_micros,
        "min_max_clip_usd_micros": (
            min(headroom_max_clips_micros) if headroom_max_clips_micros else 0
        ),
        "max_max_clip_usd_micros": (
            max(headroom_max_clips_micros) if headroom_max_clips_micros else 0
        ),
        "blocked_ticks": headroom_blocked_ticks,
        "final_remaining_daily_loss_usd_micros": (
            final_headroom.remaining_daily_loss_usd_micros
            if final_headroom is not None
            else active_risk_cfg.max_daily_loss_usd_micros
        ),
        "final_remaining_cumulative_loss_usd_micros": (
            final_headroom.remaining_cumulative_loss_usd_micros
            if final_headroom is not None
            else active_risk_cfg.max_cumulative_loss_usd_micros
        ),
        "dynamic_taker_clip_count": len(selected_taker_notionals),
        "dynamic_taker_min_usdc": (
            min(selected_taker_notionals) if selected_taker_notionals else None
        ),
        "dynamic_taker_max_usdc": (
            max(selected_taker_notionals) if selected_taker_notionals else None
        ),
        "dynamic_maker_clip_count": len(selected_maker_notionals),
        "dynamic_maker_min_usdc": (
            min(selected_maker_notionals) if selected_maker_notionals else None
        ),
        "dynamic_maker_max_usdc": (
            max(selected_maker_notionals) if selected_maker_notionals else None
        ),
    }

    coinbase_trade_flow_summary: dict[str, Any] = {
        "ws_enabled": enable_coinbase_ws,
        "ws_products_subscribed": list(ws_candidate_products) if enable_coinbase_ws else [],
        "ws_connected_ticks": ws_connected_ticks,
        "ws_total_prints_received": ws_total_prints,
        "ws_prints_by_product": dict(sorted(ws_prints_by_product_counts.items())),
        "ws_gap_events": ws_gap_events,
        "ws_reconnects": ws_reconnects,
        "ws_rest_fallback_calls": ws_rest_fallback_calls,
    }

    maker_summary = maker_tracker.summary()
    oos_execution_summary = oos_tracker.summary()
    summary_body: dict[str, Any] = {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "mode": mode,
        "chain_id": BASE_CHAIN_ID,
        "execution_eligible": False,
        "cex_venue": cex_venue,
        "include_ecosystem_tokens": include_ecosystem_tokens,
        "include_aero_cl2": include_aero_cl2,
        "usdt_usd_peg_mid": (
            round(float(active_usdt_peg.mid), 6) if active_usdt_peg is not None else None
        ),
        "allowed_products": (
            sorted(allowed_products_set) if allowed_products_set is not None else None
        ),
        "min_taker_edge_bps": effective_min_taker_edge_bps,
        "maker_min_edge_bps": round(float(effective_maker_min_edge), 6),
        "dynamic_sizing_enabled": dynamic_sizing,
        "coinbase_ws_enabled": enable_coinbase_ws,
        "trade_notional_usdc": trade_notional_usdc,
        "risk_policy": active_risk_cfg.to_dict(),
        "headroom_summary": headroom_summary,
        "coinbase_trade_flow_summary": coinbase_trade_flow_summary,
        "rpc_origin": origin,
        "ticks_requested": ticks,
        "ticks_completed": ticks_completed,
        "stopped_early": stopped_early,
        "stop_reason": stop_reason,
        "positive_net_ticks": positive_net_ticks,
        "atomic_positive_net_ticks": atomic_positive_ticks,
        "cex_dex_positive_net_ticks": cex_dex_positive_ticks,
        "lcb_net_edge_bps": lcb_int,
        "lcb_net_edge_bps_exact": round(lcb_exact, 6),
        "mean_net_edge_bps": round(mean_net_raw, 6),
        "std_net_edge_bps": round(std_net_raw, 6),
        "best_raw_spread_bps": round(max(raw_series), 6),
        "revert_budget_status": "active",
        "unresolved_residuals": maker_summary["open_unhedged_lots"],
        "rpc_calls_used": rpc_calls_used,
        "max_rpc_calls": max_rpc_calls,
        "universe_pool_count": universe_pool_count,
        "unique_blocks_observed": unique_blocks_observed,
        "first_block_number": records[0]["block_number"],
        "last_block_number": records[-1]["block_number"],
        "chain_head_sha256": chain_head_sha256,
        "atomic_dex_summary": atomic_dex_summary,
        "cex_dex_lead_lag_summary": cex_dex_lead_lag_summary,
        "maker_hedge_summary": maker_summary,
        "oos_execution_summary": oos_execution_summary,
    }
    summary_sha256 = sha256_hex(canonical_json(summary_body))
    summary = {**summary_body, "summary_sha256": summary_sha256}
    _write_new_bytes(
        summary_path, json.dumps(summary, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    )

    return CampaignRunResult(
        summary=summary,
        records=tuple(records),
        summary_path=summary_path,
        attribution_path=attribution_path,
    )


def build_parser() -> argparse.ArgumentParser:
    """Construct CLI parser for the 100-tick Base Multicall3 shadow campaign."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("public-shadow", "fixture"),
        default="public-shadow",
        help="Live read-only Base public-shadow or deterministic offline fixture mode.",
    )
    parser.add_argument(
        "--ticks",
        type=int,
        default=DEFAULT_TICKS,
        help=f"Number of block ticks to evaluate (default: {DEFAULT_TICKS}).",
    )
    parser.add_argument(
        "--interval-ms",
        type=int,
        default=DEFAULT_INTERVAL_MS,
        help=f"Target interval between ticks in milliseconds (default: {DEFAULT_INTERVAL_MS}).",
    )
    parser.add_argument(
        "--max-rpc-calls",
        type=int,
        default=DEFAULT_MAX_RPC_CALLS,
        help=f"Hard cumulative Base RPC call budget (default: {DEFAULT_MAX_RPC_CALLS}).",
    )
    parser.add_argument(
        "--rpc-url",
        default=DEFAULT_BASE_RPC_URL,
        help=f"Base HTTPS JSON-RPC URL (default: {DEFAULT_BASE_RPC_URL}).",
    )
    parser.add_argument(
        "--timeout-s",
        type=float,
        default=DEFAULT_TIMEOUT_S,
        help=(
            "Per-request timeout in seconds for Base JSON-RPC and Coinbase HTTPS/WS I/O "
            f"(default: {DEFAULT_TIMEOUT_S})."
        ),
    )
    parser.add_argument(
        "--cex-taker-fee-bps",
        type=float,
        default=float(DEFAULT_CEX_TAKER_FEE_BPS),
        help=f"Coinbase taker fee assumption in bps (default: {DEFAULT_CEX_TAKER_FEE_BPS}).",
    )
    parser.add_argument(
        "--latency-haircut-bps",
        type=float,
        default=float(DEFAULT_LATENCY_HAIRCUT_BPS),
        help=(
            "Non-atomic CEX-DEX latency/drift haircut in bps "
            f"(default: {DEFAULT_LATENCY_HAIRCUT_BPS})."
        ),
    )
    parser.add_argument(
        "--trade-notional-usdc",
        type=int,
        default=DEFAULT_TRADE_NOTIONAL_USDC,
        help=(
            "Trade clip notional in raw 6-decimal USDC base units "
            f"(default: {DEFAULT_TRADE_NOTIONAL_USDC})."
        ),
    )
    parser.add_argument(
        "--include-ecosystem-tokens",
        action="store_true",
        default=False,
        help=(
            "Include expanded Base ecosystem tokens (VIRTUAL, MORPHO, WELL, DEGEN, VVV, cbETH) "
            "and WETH 2-hop CEX-DEX/Maker-Hedge evaluation."
        ),
    )
    parser.add_argument(
        "--include-aero-cl2",
        action="store_true",
        default=False,
        help="Include Aero Slipstream 2 CLFactory pools in Base Multicall3 universe discovery.",
    )
    parser.add_argument(
        "--cex-venue",
        choices=("coinbase", "bybit"),
        default="coinbase",
        help="Public CEX L2 + WebSocket venue for CEX-DEX shadow evaluation (default: coinbase).",
    )
    parser.add_argument(
        "--risk-config",
        type=Path,
        default=None,
        help="Optional CapitalRiskConfig JSON path overriding default capital/loss bounds.",
    )
    parser.add_argument(
        "--total-capital-usd",
        type=int,
        default=None,
        help="Optional total capital ceiling override in integer USD.",
    )
    parser.add_argument(
        "--max-daily-loss-usd",
        type=int,
        default=None,
        help="Optional daily loss limit override in integer USD.",
    )
    parser.add_argument(
        "--dynamic-sizing",
        action="store_true",
        default=False,
        help="Dynamically size clips from book/DEX capacity and remaining risk headroom.",
    )
    parser.add_argument(
        "--enable-coinbase-ws",
        action="store_true",
        default=False,
        help="Stream live Coinbase Exchange `matches` prints for FIFO Maker-Hedge simulation.",
    )
    parser.add_argument(
        "--allowed-products",
        type=str,
        default=None,
        help=(
            "Optional comma-separated Coinbase products or token symbols to evaluate "
            "(e.g., ETH-USD,AERO-USD)."
        ),
    )
    parser.add_argument(
        "--min-taker-edge-bps",
        type=int,
        default=None,
        help=(
            "Minimum CEX-DEX taker edge in integer bps required to queue an OOS intent "
            "(default: 6 with --dynamic-sizing, else 1)."
        ),
    )
    parser.add_argument(
        "--maker-min-edge-bps",
        type=float,
        default=None,
        help=(
            "Minimum maker quote edge floor in bps (default: 6.5 with --dynamic-sizing, else 3.0)."
        ),
    )
    parser.add_argument(
        "--max-book-workers",
        type=int,
        default=9,
        help=(
            "Maximum concurrent threads for Coinbase L2 book prefetching in live "
            "public-shadow mode (default: 9)."
        ),
    )
    parser.add_argument(
        "--stop-gracefully-on-rpc-error",
        action="store_true",
        default=False,
        help=(
            "On a post-tick-1 RpcError, stop immediately without retrying and finalize "
            "the completed SHA-256 chain and shadow_summary.json."
        ),
    )
    parser.add_argument(
        "--out",
        type=Path,
        required=True,
        help="Output directory for shadow_attribution.jsonl and shadow_summary.json.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint for the unified Base shadow campaign."""
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        parsed_allowed_products = _parse_allowed_products_cli(args.allowed_products)
        _ensure_clean_output_dir(args.out)
        result = run_multicall_campaign(
            out_dir=args.out,
            mode=args.mode,
            ticks=args.ticks,
            interval_ms=args.interval_ms,
            max_rpc_calls=args.max_rpc_calls,
            rpc_url=args.rpc_url,
            timeout_s=args.timeout_s,
            cex_taker_fee_bps=args.cex_taker_fee_bps,
            latency_haircut_bps=args.latency_haircut_bps,
            trade_notional_usdc=args.trade_notional_usdc,
            include_ecosystem_tokens=args.include_ecosystem_tokens,
            include_aero_cl2=args.include_aero_cl2,
            cex_venue=args.cex_venue,
            risk_config_path=args.risk_config,
            total_capital_usd=args.total_capital_usd,
            max_daily_loss_usd=args.max_daily_loss_usd,
            dynamic_sizing=args.dynamic_sizing,
            enable_coinbase_ws=args.enable_coinbase_ws,
            allowed_products=parsed_allowed_products,
            min_taker_edge_bps=args.min_taker_edge_bps,
            maker_min_edge_bps=args.maker_min_edge_bps,
            max_book_workers=args.max_book_workers,
            stop_gracefully_on_rpc_error=args.stop_gracefully_on_rpc_error,
        )
    except (
        OSError,
        ValueError,
        TypeError,
        RpcError,
        FastpathUnavailableError,
    ) as exc:
        print(f"multicall-campaign failed: {exc}", file=sys.stderr)
        return 2

    print(
        json.dumps(
            {
                "schema_version": result.summary["schema_version"],
                "mode": result.summary["mode"],
                "chain_id": result.summary["chain_id"],
                "execution_eligible": result.summary["execution_eligible"],
                "cex_venue": result.summary["cex_venue"],
                "include_ecosystem_tokens": result.summary["include_ecosystem_tokens"],
                "include_aero_cl2": result.summary["include_aero_cl2"],
                "usdt_usd_peg_mid": result.summary["usdt_usd_peg_mid"],
                "allowed_products": result.summary["allowed_products"],
                "min_taker_edge_bps": result.summary["min_taker_edge_bps"],
                "maker_min_edge_bps": result.summary["maker_min_edge_bps"],
                "dynamic_sizing_enabled": result.summary["dynamic_sizing_enabled"],
                "coinbase_ws_enabled": result.summary["coinbase_ws_enabled"],
                "ticks_requested": result.summary["ticks_requested"],
                "ticks_completed": result.summary["ticks_completed"],
                "stopped_early": result.summary["stopped_early"],
                "stop_reason": result.summary["stop_reason"],
                "positive_net_ticks": result.summary["positive_net_ticks"],
                "atomic_positive_net_ticks": result.summary["atomic_positive_net_ticks"],
                "cex_dex_positive_net_ticks": result.summary["cex_dex_positive_net_ticks"],
                "lcb_net_edge_bps": result.summary["lcb_net_edge_bps"],
                "mean_net_edge_bps": result.summary["mean_net_edge_bps"],
                "best_raw_spread_bps": result.summary["best_raw_spread_bps"],
                "rpc_calls_used": result.summary["rpc_calls_used"],
                "universe_pool_count": result.summary["universe_pool_count"],
                "unique_blocks_observed": result.summary["unique_blocks_observed"],
                "headroom_summary": result.summary["headroom_summary"],
                "coinbase_trade_flow_summary": result.summary["coinbase_trade_flow_summary"],
                "maker_hedge_summary": result.summary["maker_hedge_summary"],
                "oos_execution_summary": result.summary["oos_execution_summary"],
                "summary_sha256": result.summary["summary_sha256"],
            },
            sort_keys=True,
        )
    )
    return 0


__all__ = [
    "AERO_TOKEN",
    "CBBTC",
    "DEFAULT_INTERVAL_MS",
    "DEFAULT_MAX_RPC_CALLS",
    "DEFAULT_TICKS",
    "MULTICALL3",
    "RECORD_SCHEMA_VERSION",
    "SUMMARY_SCHEMA_VERSION",
    "WSTETH",
    "CampaignRunResult",
    "MulticallUniverseProtocol",
    "PinnedTickSnapshot",
    "QuoterCheckResult",
    "build_parser",
    "build_synthetic_multicall_rpc_transport",
    "main",
    "run_multicall_campaign",
    "verify_campaign_attribution_chain",
]

if __name__ == "__main__":
    raise SystemExit(main())
