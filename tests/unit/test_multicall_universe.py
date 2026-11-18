"""Unit tests for Base Multicall3 universe discovery, snapshot & QuoterV2 verification."""

import json
import os
import socket
import subprocess
import urllib.error
from collections.abc import Callable
from email.message import Message
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from eth_abi.abi import decode, encode

from oasis_agent.arbitrage.event_quote import ORACLE
from oasis_agent.errors import RpcError
from oasis_agent.evidence.base_denim import (
    BASE_CHAIN_ID,
    BASE_TIME,
    TIMESTAMP_MILLIS_PART_SELECTOR,
    TIMESTAMP_MS_SELECTOR,
)
from oasis_agent.fastpath import (
    CycleHop,
    FastpathBridge,
    FastpathPoolGraph,
    FastpathUnavailableError,
    PoolKind,
)
from oasis_agent.fastpath.live_shadow_runner import (
    FEE_SELECTOR,
    GET_FEE_SELECTOR,
    GET_L1_FEE_CALLDATA,
    GET_RESERVES_SELECTOR,
    LIQUIDITY_SELECTOR,
    SLOT0_SELECTOR,
    resolve_fastpath_bridge,
)
from oasis_agent.fastpath.multicall_universe import (
    AERO_GET_POOL_SELECTOR,
    AERO_TOKEN,
    AGGREGATE3_SELECTOR,
    BALANCE_OF_SELECTOR,
    CANONICAL_BASE_TOKENS,
    CBBTC,
    MULTICALL3,
    SLIPSTREAM_FACTORY,
    SLIPSTREAM_GET_POOL_SELECTOR,
    UNIV3_FACTORY,
    UNIV3_GET_POOL_SELECTOR,
    UNIV3_QUOTE_SINGLE_SELECTOR,
    UNIV3_QUOTER_V2,
    WSTETH,
    DiscoveredPool,
    MulticallUniverseError,
    _validate_resolved_dns,
    _validate_rpc_url,
    compute_anti_hallucination_cl_bracket,
    discover_base_universe_pools,
    fetch_pinned_universe_snapshot,
    verify_candidate_with_onchain_quoter,
)
from oasis_agent.models.common import ZERO_ADDRESS, normalize_address, sha256_hex
from oasis_agent.sniping.venues import (
    AERO_FACTORY,
    AERO_USDC_WETH,
    SLIPSTREAM,
    SLIPSTREAM_USDC_WETH,
    USDC,
    WETH,
)
from oasis_agent.uniswap_v4.liquidity_math import (
    amount0_delta,
    amount1_delta,
    sqrt_price_at_tick,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
MOCK_RPC_URL = "https://rpc.example.invalid"


def _synth_addr(idx: int) -> str:
    return f"0x{idx:040x}"


def _synth_hash(label: bytes) -> str:
    return "0x" + sha256_hex(label)


def _ensure_release_cdylib() -> Path:
    crates_dir = REPO_ROOT / "crates"
    lib_name = "liboasis_core.dylib" if os.uname().sysname == "Darwin" else "liboasis_core.so"
    release_lib = crates_dir / "target" / "release" / lib_name
    rust_sources = [
        crates_dir / "Cargo.toml",
        crates_dir / "oasis-core" / "Cargo.toml",
        *(crates_dir / "oasis-core" / "src").rglob("*.rs"),
    ]
    latest_src_mtime = max(p.stat().st_mtime for p in rust_sources)
    if not release_lib.is_file() or release_lib.stat().st_mtime < latest_src_mtime:
        subprocess.run(
            [
                "cargo",
                "build",
                "--locked",
                "--manifest-path",
                str(crates_dir / "Cargo.toml"),
                "--release",
            ],
            cwd=REPO_ROOT,
            check=True,
        )
    return release_lib


@pytest.fixture(scope="module")
def bridge() -> FastpathBridge:
    try:
        return resolve_fastpath_bridge()
    except FastpathUnavailableError:
        return FastpathBridge(lib_path=_ensure_release_cdylib())


def _slot0_v3_bytes(tick_val: int) -> bytes:
    sqrt_p = sqrt_price_at_tick(tick_val)
    return encode(
        ["uint160", "int24", "uint16", "uint16", "uint16", "uint8", "bool"],
        [sqrt_p, tick_val, 0, 1, 1, 0, True],
    )


def _slot0_ss_bytes(tick_val: int) -> bytes:
    sqrt_p = sqrt_price_at_tick(tick_val)
    return encode(
        ["uint160", "int24", "uint16", "uint16", "uint16", "bool"],
        [sqrt_p, tick_val, 0, 1, 1, True],
    )


def _u256_bytes(val: int) -> bytes:
    return val.to_bytes(32, "big")


def test_canonical_tokens_and_selectors() -> None:
    assert UNIV3_QUOTE_SINGLE_SELECTOR == "0xc6a5026a"
    assert AGGREGATE3_SELECTOR == "0x82ad56cb"
    assert len(CANONICAL_BASE_TOKENS) == 5
    by_sym = {meta.symbol: meta for meta in CANONICAL_BASE_TOKENS}
    assert by_sym["WETH"].address == normalize_address(WETH)
    assert by_sym["WETH"].decimals == 18
    assert by_sym["WETH"].min_pool_reserve == 3 * 10**18
    assert by_sym["USDC"].address == normalize_address(USDC)
    assert by_sym["USDC"].decimals == 6
    assert by_sym["USDC"].min_pool_reserve == 5_000 * 10**6
    assert by_sym["cbBTC"].address == normalize_address(CBBTC)
    assert by_sym["cbBTC"].decimals == 8
    assert by_sym["cbBTC"].min_pool_reserve == 10_000_000
    assert by_sym["AERO"].address == normalize_address(AERO_TOKEN)
    assert by_sym["AERO"].decimals == 18
    assert by_sym["AERO"].min_pool_reserve == 10_000 * 10**18
    assert by_sym["wstETH"].address == normalize_address(WSTETH)
    assert by_sym["wstETH"].decimals == 18
    assert by_sym["wstETH"].min_pool_reserve == 2 * 10**18
    assert len(MULTICALL3) == 42
    assert len(UNIV3_FACTORY) == 42
    assert len(UNIV3_QUOTER_V2) == 42
    assert len(SLIPSTREAM_FACTORY) == 42


def _build_discovery_mock_transport(
    *,
    block_number: int = 58_100_000,
    ts_ms: int = 1_794_100_000_200,
    empty_factories: bool = False,
    all_factory_calls_fail: bool = False,
) -> tuple[Callable[[str, bytes, float], bytes], list[dict[str, Any]]]:
    calls_seen: list[dict[str, Any]] = []

    weth = normalize_address(WETH)
    usdc = normalize_address(USDC)
    cbbtc = normalize_address(CBBTC)
    aero = normalize_address(AERO_TOKEN)
    wsteth = normalize_address(WSTETH)

    v3_weth_usdc = _synth_addr(0x101)
    v3_weth_cbbtc = _synth_addr(0x102)
    v3_dust_aero_usdc = _synth_addr(0x103)
    ss_weth_usdc_1 = normalize_address(SLIPSTREAM_USDC_WETH[SLIPSTREAM_FACTORY][0])
    extra_ss_factory = next(f for f in SLIPSTREAM_USDC_WETH if f != SLIPSTREAM_FACTORY)
    ss_weth_usdc_2 = normalize_address(SLIPSTREAM_USDC_WETH[extra_ss_factory][0])
    ss_dead_liq0 = _synth_addr(0x104)
    aero_v2_weth_aero = _synth_addr(0x105)
    aero_v2_dust = normalize_address(AERO_USDC_WETH[0])
    v3_wsteth_weth = _synth_addr(0x106)

    def _pair(a: str, b: str) -> tuple[str, str]:
        return (a, b) if a < b else (b, a)

    factory_map: dict[tuple[str, bytes], str] = {}
    if not empty_factories:
        # Active UniV3 pools
        t0, t1 = _pair(weth, usdc)
        factory_map[
            (
                UNIV3_FACTORY,
                bytes.fromhex(
                    UNIV3_GET_POOL_SELECTOR[2:]
                    + encode(["address", "address", "uint24"], [t0, t1, 500]).hex()
                ),
            )
        ] = v3_weth_usdc

        t0, t1 = _pair(weth, cbbtc)
        factory_map[
            (
                UNIV3_FACTORY,
                bytes.fromhex(
                    UNIV3_GET_POOL_SELECTOR[2:]
                    + encode(["address", "address", "uint24"], [t0, t1, 3000]).hex()
                ),
            )
        ] = v3_weth_cbbtc

        t0, t1 = _pair(wsteth, weth)
        factory_map[
            (
                UNIV3_FACTORY,
                bytes.fromhex(
                    UNIV3_GET_POOL_SELECTOR[2:]
                    + encode(["address", "address", "uint24"], [t0, t1, 100]).hex()
                ),
            )
        ] = v3_wsteth_weth

        # Dust UniV3 pool (below min_pool_reserve)
        t0, t1 = _pair(aero, usdc)
        factory_map[
            (
                UNIV3_FACTORY,
                bytes.fromhex(
                    UNIV3_GET_POOL_SELECTOR[2:]
                    + encode(["address", "address", "uint24"], [t0, t1, 3000]).hex()
                ),
            )
        ] = v3_dust_aero_usdc

        # Slipstream pools (both SLIPSTREAM_USDC_WETH entries + 1 dead liq=0 pool)
        t0, t1 = _pair(weth, usdc)
        factory_map[
            (
                SLIPSTREAM_FACTORY,
                bytes.fromhex(
                    SLIPSTREAM_GET_POOL_SELECTOR[2:]
                    + encode(["address", "address", "int24"], [t0, t1, 1]).hex()
                ),
            )
        ] = ss_weth_usdc_1
        factory_map[
            (
                normalize_address(extra_ss_factory),
                bytes.fromhex(
                    SLIPSTREAM_GET_POOL_SELECTOR[2:]
                    + encode(["address", "address", "int24"], [t0, t1, 1]).hex()
                ),
            )
        ] = ss_weth_usdc_2

        t0, t1 = _pair(cbbtc, usdc)
        factory_map[
            (
                SLIPSTREAM_FACTORY,
                bytes.fromhex(
                    SLIPSTREAM_GET_POOL_SELECTOR[2:]
                    + encode(["address", "address", "int24"], [t0, t1, 100]).hex()
                ),
            )
        ] = ss_dead_liq0

        # Aero V2 pools (1 active + 1 unsynced dust)
        t0, t1 = _pair(weth, aero)
        factory_map[
            (
                normalize_address(AERO_FACTORY),
                bytes.fromhex(
                    AERO_GET_POOL_SELECTOR[2:]
                    + encode(["address", "address", "bool"], [t0, t1, False]).hex()
                ),
            )
        ] = aero_v2_weth_aero
        t0, t1 = _pair(weth, usdc)
        factory_map[
            (
                normalize_address(AERO_FACTORY),
                bytes.fromhex(
                    AERO_GET_POOL_SELECTOR[2:]
                    + encode(["address", "address", "bool"], [t0, t1, False]).hex()
                ),
            )
        ] = aero_v2_dust

    # Per-pool balances & liquidity/reserves
    balances: dict[tuple[str, str], int] = {
        (weth, v3_weth_usdc): 50 * 10**18,
        (usdc, v3_weth_usdc): 150_000 * 10**6,
        (weth, v3_weth_cbbtc): 20 * 10**18,
        (cbbtc, v3_weth_cbbtc): 100_000_000,  # 1.0 cbBTC >= 0.1 cbBTC
        (weth, v3_wsteth_weth): 10 * 10**18,
        (wsteth, v3_wsteth_weth): 9 * 10**18,
        # Dust pool: only 10 USDC (< 5_000 USDC min_pool_reserve)
        (usdc, v3_dust_aero_usdc): 10 * 10**6,
        (aero, v3_dust_aero_usdc): 20_000 * 10**18,
        # Slipstream WETH/USDC pools
        (weth, ss_weth_usdc_1): 30 * 10**18,
        (usdc, ss_weth_usdc_1): 90_000 * 10**6,
        (weth, ss_weth_usdc_2): 25 * 10**18,
        (usdc, ss_weth_usdc_2): 75_000 * 10**6,
        # Dead Slipstream pool with zero liquidity
        (usdc, ss_dead_liq0): 50_000 * 10**6,
        (cbbtc, ss_dead_liq0): 50_000_000,
        # Aero V2 WETH/AERO active
        (weth, aero_v2_weth_aero): 15 * 10**18,
        (aero, aero_v2_weth_aero): 45_000 * 10**18,
        # Aero V2 dust (balanceOf looks big, getReserves is dust!)
        (weth, aero_v2_dust): 50 * 10**18,
        (usdc, aero_v2_dust): 150_000 * 10**6,
    }
    liquidities: dict[str, int] = {
        v3_weth_usdc: 200_000_000_000_000_000,
        v3_weth_cbbtc: 50_000_000_000_000,
        v3_wsteth_weth: 400_000_000_000_000_000_000,
        v3_dust_aero_usdc: 100_000_000_000_000,
        ss_weth_usdc_1: 150_000_000_000_000_000,
        ss_weth_usdc_2: 120_000_000_000_000_000,
        ss_dead_liq0: 0,
    }
    reserves_v2: dict[str, tuple[int, int]] = {
        aero_v2_weth_aero: (15 * 10**18, 45_000 * 10**18),
        aero_v2_dust: (10**15, 3 * 10**6),  # below min_pool_reserve!
    }

    def _transport(_url: str, payload: bytes, _timeout_s: float) -> bytes:
        req = json.loads(payload.decode("utf-8"))
        calls_seen.append(req)
        method = req["method"]
        req_id = req["id"]
        if method == "eth_getBlockByNumber":
            return json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "number": hex(block_number),
                        "hash": _synth_hash(b"discover-block"),
                        "parentHash": _synth_hash(b"discover-parent"),
                        "timestamp": hex(ts_ms // 1000),
                        "timestampMs": hex(ts_ms),
                        "baseFeePerGas": hex(50_000_000),
                    },
                }
            ).encode("utf-8")

        call_obj, block_hex = req["params"]
        assert block_hex == hex(block_number)
        assert normalize_address(call_obj["to"]) == MULTICALL3
        cd = bytes.fromhex(call_obj["data"][2:])
        assert "0x" + cd[:4].hex() == AGGREGATE3_SELECTOR
        (subcalls,) = decode(["(address,bool,bytes)[]"], cd[4:], strict=True)

        mc_results: list[tuple[bool, bytes]] = []
        for target_raw, _allow_fail, data in subcalls:
            target = normalize_address(target_raw)
            sel = "0x" + data[:4].hex()
            if sel in (
                UNIV3_GET_POOL_SELECTOR,
                SLIPSTREAM_GET_POOL_SELECTOR,
                AERO_GET_POOL_SELECTOR,
            ):
                if all_factory_calls_fail:
                    mc_results.append((False, b""))
                else:
                    pool_addr = factory_map.get((target, data), ZERO_ADDRESS)
                    mc_results.append((True, encode(["address"], [pool_addr])))
            elif sel == BALANCE_OF_SELECTOR:
                (holder,) = decode(["address"], data[4:], strict=True)
                amt = balances[(target, normalize_address(holder))]
                mc_results.append((True, _u256_bytes(amt)))
            elif sel == LIQUIDITY_SELECTOR:
                mc_results.append((True, _u256_bytes(liquidities[target])))
            elif sel == GET_RESERVES_SELECTOR:
                r0, r1 = reserves_v2[target]
                mc_results.append((True, encode(["uint256", "uint256", "uint256"], [r0, r1, 1])))
            else:
                raise AssertionError(f"unexpected multicall subcall target={target} sel={sel}")

        encoded_res = "0x" + encode(["(bool,bytes)[]"], [mc_results]).hex()
        return json.dumps({"jsonrpc": "2.0", "id": req_id, "result": encoded_res}).encode("utf-8")

    return _transport, calls_seen


def test_discover_base_universe_pools_filters_dust_and_dedups() -> None:
    transport, calls = _build_discovery_mock_transport()
    pools = discover_base_universe_pools(MOCK_RPC_URL, transport=transport)
    assert len(calls) == 3

    pool_ids = {p.pool_id for p in pools}
    assert len(pools) == len(pool_ids) == 6
    # Dust UniV3, dead liq=0 Slipstream, and unsynced-reserve Aero V2 were excluded:
    assert _synth_addr(0x103) not in pool_ids
    assert _synth_addr(0x104) not in pool_ids
    assert normalize_address(AERO_USDC_WETH[0]) not in pool_ids
    # Both SLIPSTREAM_USDC_WETH pools are present:
    for ss_addr, _ts in SLIPSTREAM_USDC_WETH.values():
        assert normalize_address(ss_addr) in pool_ids
    for pool in pools:
        assert pool.token0 < pool.token1
        assert pool.reserve0 > 0 and pool.reserve1 > 0

    # Empty discovery skips reserve probe call (only 2 RPC calls).
    empty_transport, empty_calls = _build_discovery_mock_transport(empty_factories=True)
    assert discover_base_universe_pools(MOCK_RPC_URL, transport=empty_transport) == ()
    assert len(empty_calls) == 2

    # All factory queries failing fails closed.
    failing_transport, _ = _build_discovery_mock_transport(all_factory_calls_fail=True)
    with pytest.raises(MulticallUniverseError, match="all factory getPool calls failed"):
        discover_base_universe_pools(MOCK_RPC_URL, transport=failing_transport)


def _sample_discovered_pools() -> tuple[DiscoveredPool, ...]:
    weth = normalize_address(WETH)
    usdc = normalize_address(USDC)
    aero = normalize_address(AERO_TOKEN)
    # Enforce token0 < token1 dynamically
    wu0, wu1 = (weth, usdc) if weth < usdc else (usdc, weth)
    wa0, wa1 = (weth, aero) if weth < aero else (aero, weth)
    return (
        DiscoveredPool(
            pool_id=_synth_addr(0x201),
            label="uniswap_v3_weth_usdc_500",
            venue="uniswap_v3",
            kind=PoolKind.V3,
            token0=wu0,
            token1=wu1,
            decimals0=18 if wu0 == weth else 6,
            decimals1=6 if wu1 == usdc else 18,
            fixed_fee_pips=500,
            tick_spacing=None,
            factory=UNIV3_FACTORY,
            reserve0=50 * 10**18,
            reserve1=150_000 * 10**6,
        ),
        DiscoveredPool(
            pool_id=normalize_address(SLIPSTREAM_USDC_WETH[SLIPSTREAM_FACTORY][0]),
            label="slipstream_weth_usdc_ts1",
            venue="aerodrome_slipstream",
            kind=PoolKind.SLIPSTREAM,
            token0=wu0,
            token1=wu1,
            decimals0=18 if wu0 == weth else 6,
            decimals1=6 if wu1 == usdc else 18,
            fixed_fee_pips=None,
            tick_spacing=1,
            factory=SLIPSTREAM_FACTORY,
            reserve0=40 * 10**18,
            reserve1=120_000 * 10**6,
        ),
        DiscoveredPool(
            pool_id=_synth_addr(0x203),
            label="aerodrome_v2_weth_aero",
            venue="aerodrome_v2",
            kind=PoolKind.V2,
            token0=wa0,
            token1=wa1,
            decimals0=18,
            decimals1=18,
            fixed_fee_pips=None,
            tick_spacing=None,
            factory=normalize_address(AERO_FACTORY),
            reserve0=20 * 10**18,
            reserve1=60_000 * 10**18,
        ),
    )


def _build_snapshot_mock_transport(
    pools: tuple[DiscoveredPool, ...],
    *,
    block_number: int = 58_200_000,
    ts_ms: int = 1_794_200_000_400,
    base_fee_wei: int = 60_000_000,
    l1_fee_wei: int = 4_000_000_000_000,
    block_hash: str | None = None,
    parent_hash: str | None = None,
    corrupt_mode: str | None = None,
    v3_liq_override: int | None = None,
    v3_bal0_override: int | None = None,
    v3_bal1_override: int | None = None,
) -> tuple[Callable[[str, bytes, float], bytes], list[dict[str, Any]]]:
    calls_seen: list[dict[str, Any]] = []
    v3_pool, ss_pool, aero_pool = pools
    b_hash = block_hash if block_hash is not None else _synth_hash(f"blk-{block_number}".encode())
    p_hash = parent_hash if parent_hash is not None else _synth_hash(f"par-{block_number}".encode())

    def _transport(_url: str, payload: bytes, _timeout_s: float) -> bytes:
        req = json.loads(payload.decode("utf-8"))
        calls_seen.append(req)
        method = req["method"]
        req_id = req["id"]
        if method == "eth_getBlockByNumber":
            header_obj: dict[str, object] = {
                "number": hex(block_number),
                "hash": p_hash if corrupt_mode == "self_parent" else b_hash,
                "parentHash": p_hash,
                "timestamp": hex(ts_ms // 1000),
                "baseFeePerGas": hex(base_fee_wei),
            }
            if corrupt_mode != "missing_header_ms":
                header_obj["timestampMs"] = hex(ts_ms)
            return json.dumps({"jsonrpc": "2.0", "id": req_id, "result": header_obj}).encode(
                "utf-8"
            )

        call_obj, block_hex = req["params"]
        assert block_hex == hex(block_number)
        assert normalize_address(call_obj["to"]) == MULTICALL3
        cd = bytes.fromhex(call_obj["data"][2:])
        (subcalls,) = decode(["(address,bool,bytes)[]"], cd[4:], strict=True)

        mc_res: list[tuple[bool, bytes]] = []
        for idx, (target_raw, _allow_fail, data) in enumerate(subcalls):
            target = normalize_address(target_raw)
            data_hex = "0x" + data.hex()
            sel = "0x" + data[:4].hex()
            if target == BASE_TIME and data_hex == TIMESTAMP_MS_SELECTOR:
                val = ts_ms + 200 if corrupt_mode == "basetime_header_mismatch" else ts_ms
                mc_res.append((True, _u256_bytes(val)))
            elif target == BASE_TIME and data_hex == TIMESTAMP_MILLIS_PART_SELECTOR:
                part = (
                    (ts_ms % 1000 + 1) % 1000 if corrupt_mode == "basetime_millis" else ts_ms % 1000
                )
                mc_res.append((True, _u256_bytes(part)))
            elif target == normalize_address(ORACLE) and data_hex == GET_L1_FEE_CALLDATA:
                mc_res.append((True, _u256_bytes(l1_fee_wei)))
            elif target == v3_pool.pool_id and data_hex == SLOT0_SELECTOR:
                mc_res.append((True, _slot0_v3_bytes(-196_258)))
            elif target == v3_pool.pool_id and data_hex == LIQUIDITY_SELECTOR:
                liq = v3_liq_override if v3_liq_override is not None else 200_000_000_000_000_000
                mc_res.append((True, _u256_bytes(liq)))
            elif target == v3_pool.pool_id and data_hex == FEE_SELECTOR:
                fee = 3000 if corrupt_mode == "v3_fee_mismatch" else 500
                mc_res.append((True, _u256_bytes(fee)))
            elif target == ss_pool.pool_id and data_hex == SLOT0_SELECTOR:
                mc_res.append((True, _slot0_ss_bytes(-196_150)))
            elif target == ss_pool.pool_id and data_hex == LIQUIDITY_SELECTOR:
                mc_res.append((True, _u256_bytes(150_000_000_000_000_000)))
            elif target == ss_pool.pool_id and data_hex == FEE_SELECTOR:
                mc_res.append((True, _u256_bytes(100)))
            elif target == aero_pool.pool_id and data_hex == GET_RESERVES_SELECTOR:
                if corrupt_mode == "subcall_fail" and idx > 2:
                    mc_res.append((False, b""))
                else:
                    mc_res.append(
                        (
                            True,
                            encode(
                                ["uint256", "uint256", "uint256"],
                                [20 * 10**18, 60_000 * 10**18, ts_ms // 1000],
                            ),
                        )
                    )
            elif target == aero_pool.factory and data_hex.startswith(GET_FEE_SELECTOR):
                mc_res.append((True, _u256_bytes(30)))
            elif sel == BALANCE_OF_SELECTOR:
                (holder_raw,) = decode(["address"], data[4:], strict=True)
                holder = normalize_address(holder_raw)
                if holder == v3_pool.pool_id:
                    b0 = v3_bal0_override if v3_bal0_override is not None else 50 * 10**18
                    b1 = v3_bal1_override if v3_bal1_override is not None else 150_000 * 10**6
                    bal = b0 if target == v3_pool.token0 else b1
                elif holder == ss_pool.pool_id:
                    bal = 40 * 10**18 if target == ss_pool.token0 else 120_000 * 10**6
                elif holder == aero_pool.pool_id:
                    bal = 20 * 10**18 if target == aero_pool.token0 else 60_000 * 10**18
                else:
                    raise AssertionError(f"unexpected balanceOf holder={holder}")
                mc_res.append((True, _u256_bytes(bal)))
            else:
                raise AssertionError(f"unexpected snapshot subcall target={target} data={data_hex}")

        encoded_res = "0x" + encode(["(bool,bytes)[]"], [mc_res]).hex()
        return json.dumps({"jsonrpc": "2.0", "id": req_id, "result": encoded_res}).encode("utf-8")

    return _transport, calls_seen


def test_fetch_pinned_universe_snapshot_uses_two_rpc_calls(bridge: FastpathBridge) -> None:
    pools = _sample_discovered_pools()
    transport, calls = _build_snapshot_mock_transport(pools)
    snap = fetch_pinned_universe_snapshot(MOCK_RPC_URL, pools, transport=transport)

    assert snap.rpc_calls_used == 2
    assert len(calls) == 2
    assert calls[0]["method"] == "eth_getBlockByNumber"
    assert calls[1]["method"] == "eth_call"
    assert snap.chain_id == BASE_CHAIN_ID
    assert snap.execution_eligible is False
    assert snap.header_timestamp_ms == snap.base_time_ms
    assert len(snap.pool_nodes) == 3
    assert set(snap.balances_by_pool) == {p.pool_id for p in pools}

    # Nodes evaluate cleanly inside Rust FastpathPoolGraph
    graph = FastpathPoolGraph(bridge=bridge)
    for node in snap.pool_nodes:
        graph.upsert_pool(node)
    quote = graph.quote_pool(BASE_CHAIN_ID, pools[0].pool_id, pools[0].token0, 10**17)
    assert quote.amount_out > 0


def test_anti_hallucination_cl_bracket_caps_virtual_deliverables_at_80_pct(
    bridge: FastpathBridge,
) -> None:
    tick = -196_258
    sqrt_p = sqrt_price_at_tick(tick)
    # High concentrated liquidity vs balances -> h=200 would far exceed pool balances,
    # while h ~ 8 keeps deliverables <= 80% of actual reserves without scaling liquidity.
    liquidity = 100_000_000_000_000_000  # 1e17
    balance0 = 10**18  # 1 WETH
    balance1 = 3_000 * 10**6  # 3000 USDC

    lo_tick, hi_tick, eff_liq, ticks = compute_anti_hallucination_cl_bracket(
        sqrt_price_x96=sqrt_p,
        tick=tick,
        liquidity=liquidity,
        balance0=balance0,
        balance1=balance1,
    )
    assert eff_liq == liquidity
    half = hi_tick - tick
    assert tick - lo_tick == half
    assert 1 <= half < 200

    d0 = amount0_delta(sqrt_p, sqrt_price_at_tick(hi_tick), eff_liq, round_up=True)
    d1 = amount1_delta(sqrt_price_at_tick(lo_tick), sqrt_p, eff_liq, round_up=True)
    assert d0 <= (4 * balance0) // 5
    assert d1 <= (4 * balance1) // 5

    # Verify h+1 would have exceeded the 80% cap (tightest symmetric bracket).
    d0_next = amount0_delta(sqrt_p, sqrt_price_at_tick(hi_tick + 1), eff_liq, round_up=True)
    d1_next = amount1_delta(sqrt_price_at_tick(lo_tick - 1), sqrt_p, eff_liq, round_up=True)
    assert d0_next > (4 * balance0) // 5 or d1_next > (4 * balance1) // 5

    # Also test snapshot integration & Rust FastpathPoolGraph bound
    pools = _sample_discovered_pools()
    transport, _ = _build_snapshot_mock_transport(
        pools,
        v3_liq_override=liquidity,
        v3_bal0_override=balance0,
        v3_bal1_override=balance1,
    )
    snap = fetch_pinned_universe_snapshot(MOCK_RPC_URL, pools, transport=transport)
    v3_node = snap.pool_nodes[0]
    assert v3_node.ticks == ticks

    graph = FastpathPoolGraph(bridge=bridge)
    graph.upsert_pool(v3_node)
    # Swapping 10 WETH in (> 3000 USDC balance) must fail closed, never hallucinate > 80%.
    with pytest.raises(Exception, match="liquidity"):
        graph.quote_pool(BASE_CHAIN_ID, v3_node.pool_id, v3_node.token0, 10 * 10**18)

    # Tight 1-tick pool where even h=1 exceeds 80% at full L scales down effective_liquidity safely
    lo_1, hi_1, scaled_liq, _ = compute_anti_hallucination_cl_bracket(
        sqrt_price_x96=sqrt_p,
        tick=tick,
        liquidity=10**22,
        balance0=10**18,
        balance1=3_000 * 10**6,
    )
    assert hi_1 - tick == 1 and tick - lo_1 == 1
    assert scaled_liq < 10**22
    assert (
        amount0_delta(sqrt_p, sqrt_price_at_tick(hi_1), scaled_liq, round_up=True)
        <= (4 * 10**18) // 5
    )
    assert (
        amount1_delta(sqrt_price_at_tick(lo_1), sqrt_p, scaled_liq, round_up=True)
        <= (4 * 3_000 * 10**6) // 5
    )


@pytest.mark.parametrize(
    "corrupt_mode",
    [
        "missing_header_ms",
        "basetime_header_mismatch",
        "basetime_millis",
        "self_parent",
        "v3_fee_mismatch",
        "subcall_fail",
    ],
)
def test_snapshot_header_basetime_and_abi_failures_reject(corrupt_mode: str) -> None:
    pools = _sample_discovered_pools()
    transport, _ = _build_snapshot_mock_transport(pools, corrupt_mode=corrupt_mode)
    with pytest.raises(ValueError):
        fetch_pinned_universe_snapshot(MOCK_RPC_URL, pools, transport=transport)


def test_snapshot_reorg_and_ssrf_guards(monkeypatch: pytest.MonkeyPatch) -> None:
    pools = _sample_discovered_pools()
    t1, _ = _build_snapshot_mock_transport(pools, block_number=58_200_000)
    snap1 = fetch_pinned_universe_snapshot(MOCK_RPC_URL, pools, transport=t1)

    # Same height, mutated block hash -> reorg
    t_same_mutated, _ = _build_snapshot_mock_transport(
        pools, block_number=58_200_000, block_hash=_synth_hash(b"mutated-fork-hash")
    )
    with pytest.raises(MulticallUniverseError, match="reorg detected"):
        fetch_pinned_universe_snapshot(
            MOCK_RPC_URL, pools, transport=t_same_mutated, previous_snapshot=snap1
        )

    # Height + 1, wrong parentHash -> reorg
    t_bad_parent, _ = _build_snapshot_mock_transport(
        pools, block_number=58_200_001, parent_hash=_synth_hash(b"wrong-parent")
    )
    with pytest.raises(MulticallUniverseError, match="parent_hash mismatch"):
        fetch_pinned_universe_snapshot(
            MOCK_RPC_URL, pools, transport=t_bad_parent, previous_snapshot=snap1
        )

    # Regressed block height -> stale/reorg
    t_stale, _ = _build_snapshot_mock_transport(pools, block_number=58_199_999)
    with pytest.raises(MulticallUniverseError, match="stale or regressed"):
        fetch_pinned_universe_snapshot(
            MOCK_RPC_URL, pools, transport=t_stale, previous_snapshot=snap1
        )

    # SSRF & TUN Fake-IP DNS validation
    for bad_url in ("http://rpc.example.invalid", "https://localhost:8545", "https://127.0.0.1"):
        with pytest.raises(MulticallUniverseError):
            _validate_rpc_url(bad_url, enforce_public_host=True)

    monkeypatch.setattr(
        "oasis_agent.fastpath.multicall_universe.socket.getaddrinfo",
        lambda *_a, **_k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.10", 443))],
    )
    with pytest.raises(MulticallUniverseError, match="private/loopback"):
        _validate_resolved_dns("rpc.example.invalid", 443)

    monkeypatch.setattr(
        "oasis_agent.fastpath.multicall_universe.socket.getaddrinfo",
        lambda *_a, **_k: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.0.15", 443)),
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001:2::15", 443, 0, 0)),
        ],
    )
    _validate_resolved_dns("rpc.example.invalid", 443)

    # Default HTTPS transport fails closed with RpcError on HTTP 429 (zero retries).
    http_calls = 0

    def _http_429_opener(*_args: object, **_kwargs: object) -> object:
        class _Opener:
            def open(self, *_a: object, **_k: object) -> object:
                nonlocal http_calls
                http_calls += 1
                raise urllib.error.HTTPError(
                    MOCK_RPC_URL,
                    429,
                    "Too Many Requests",
                    Message(),
                    BytesIO(b"rate limited"),
                )

        return _Opener()

    monkeypatch.setattr(
        "oasis_agent.fastpath.multicall_universe.socket.getaddrinfo",
        lambda *_a, **_k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
    )
    monkeypatch.setattr(
        "oasis_agent.fastpath.multicall_universe.urllib.request.build_opener",
        _http_429_opener,
    )
    with pytest.raises(RpcError, match="status 429"):
        discover_base_universe_pools(MOCK_RPC_URL)
    assert http_calls == 1


def test_verify_candidate_with_onchain_quoter_and_revert_fail_closed(
    bridge: FastpathBridge,
) -> None:
    pools = _sample_discovered_pools()
    t_snap, _ = _build_snapshot_mock_transport(pools)
    snap = fetch_pinned_universe_snapshot(MOCK_RPC_URL, pools, transport=t_snap)

    v3_pool, ss_pool, _ = pools
    pools_by_id = {node.pool_id: node for node in snap.pool_nodes}
    weth = normalize_address(WETH)
    usdc = normalize_address(USDC)
    amount_in = 10**17  # 0.1 WETH

    # Profitable 2-hop route: WETH -> USDC on V3 (QuoterV2), USDC -> WETH on Slipstream.
    # At tick -196150 (1 WETH ~ 3032 USDC), 320 USDC yields ~0.1055 WETH > 0.1 WETH.
    local_graph = FastpathPoolGraph(bridge=bridge)
    ss_node = pools_by_id[ss_pool.pool_id]
    local_graph.upsert_pool(ss_node)
    usdc_mid = 320 * 10**6
    ss_out = local_graph.quote_pool(BASE_CHAIN_ID, ss_pool.pool_id, usdc, usdc_mid).amount_out
    assert ss_out > amount_in + 10**14

    hops = (
        CycleHop(
            pool_id=v3_pool.pool_id,
            kind=PoolKind.V3,
            token_in=weth,
            token_out=usdc,
            amount_in=amount_in,
            amount_out=usdc_mid,
        ),
        CycleHop(
            pool_id=ss_pool.pool_id,
            kind=PoolKind.SLIPSTREAM,
            token_in=usdc,
            token_out=weth,
            amount_in=usdc_mid,
            amount_out=ss_out,
        ),
    )

    pinned_hex = hex(snap.block_number)
    sqrt_after = sqrt_price_at_tick(-196_260)

    def _make_quoter_transport(
        *, mode: str = "ok", ss_quoter_override: int | None = None
    ) -> Callable[[str, bytes, float], bytes]:
        def _transport(_url: str, payload: bytes, _timeout_s: float) -> bytes:
            req = json.loads(payload.decode("utf-8"))
            assert req["method"] == "eth_call"
            call_obj, blk = req["params"]
            assert blk == pinned_hex
            to_addr = normalize_address(call_obj["to"])
            data_hex = call_obj["data"].lower()
            if to_addr == UNIV3_QUOTER_V2:
                assert data_hex.startswith(UNIV3_QUOTE_SINGLE_SELECTOR)
                if mode == "revert":
                    return json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": req["id"],
                            "error": {"code": -32000, "message": "execution reverted"},
                        }
                    ).encode("utf-8")
                out_amt = 0 if mode == "zero" else usdc_mid
                if mode == "exceed_balance":
                    out_amt = snap.balances_by_pool[v3_pool.pool_id][1] + 1
                res_hex = (
                    "0x"
                    + encode(
                        ["uint256", "uint160", "uint32", "uint256"],
                        [out_amt, sqrt_after, 1, 110_000],
                    ).hex()
                )
                return json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": res_hex}).encode(
                    "utf-8"
                )
            if to_addr == normalize_address(SLIPSTREAM[SLIPSTREAM_FACTORY][0]):
                ss_amt = ss_quoter_override if ss_quoter_override is not None else ss_out
                res_hex = (
                    "0x"
                    + encode(
                        ["uint256", "uint160", "uint32", "uint256"],
                        [ss_amt, sqrt_after, 1, 105_000],
                    ).hex()
                )
                return json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": res_hex}).encode(
                    "utf-8"
                )
            raise AssertionError(f"unexpected quoter target {to_addr}")

        return _transport

    # 1. Success via V3 QuoterV2 + local Slipstream bounded quote (1 RPC call)
    ok_res = verify_candidate_with_onchain_quoter(
        MOCK_RPC_URL,
        hops,
        amount_in,
        gas_cost_in_base=50_000_000_000_000,
        l1_fee_in_base=4_000_000_000_000,
        pools_by_id=pools_by_id,
        block_number_hex=pinned_hex,
        transport=_make_quoter_transport(mode="ok"),
        balances_by_pool=snap.balances_by_pool,
        bridge=bridge,
    )
    assert ok_res.verified is True
    assert ok_res.execution_eligible is False
    assert ok_res.rpc_calls_used == 1
    assert ok_res.hop_outputs == (usdc_mid, ss_out)
    assert ok_res.simulated_amount_out == ss_out
    assert ok_res.simulated_net_profit == ss_out - amount_in - 54_000_000_000_000
    assert ok_res.reason == "ok"

    # 2. Success via V3 QuoterV2 + Slipstream on-chain Quoter (when discovered_pools_by_id passed)
    disc_by_id = {p.pool_id: p for p in pools}
    ok_ss_quoter = verify_candidate_with_onchain_quoter(
        MOCK_RPC_URL,
        hops,
        amount_in,
        gas_cost_in_base=50_000_000_000_000,
        l1_fee_in_base=4_000_000_000_000,
        pools_by_id=pools_by_id,
        block_number_hex=pinned_hex,
        transport=_make_quoter_transport(mode="ok"),
        balances_by_pool=snap.balances_by_pool,
        discovered_pools_by_id=disc_by_id,
        bridge=bridge,
    )
    assert ok_ss_quoter.verified is True
    assert ok_ss_quoter.rpc_calls_used == 2

    # 3. QuoterV2 EVM revert -> verified=False
    rev_res = verify_candidate_with_onchain_quoter(
        MOCK_RPC_URL,
        hops,
        amount_in,
        gas_cost_in_base=50_000_000_000_000,
        l1_fee_in_base=4_000_000_000_000,
        pools_by_id=pools_by_id,
        block_number_hex=pinned_hex,
        transport=_make_quoter_transport(mode="revert"),
        balances_by_pool=snap.balances_by_pool,
        bridge=bridge,
    )
    assert rev_res.verified is False
    assert rev_res.execution_eligible is False
    assert "quoter_v2_failed" in rev_res.reason

    # 4. QuoterV2 zero output -> verified=False
    zero_res = verify_candidate_with_onchain_quoter(
        MOCK_RPC_URL,
        hops,
        amount_in,
        gas_cost_in_base=0,
        l1_fee_in_base=0,
        pools_by_id=pools_by_id,
        block_number_hex=pinned_hex,
        transport=_make_quoter_transport(mode="zero"),
        balances_by_pool=snap.balances_by_pool,
        bridge=bridge,
    )
    assert zero_res.verified is False
    assert "zero_output" in zero_res.reason

    # 5. QuoterV2 output exceeding pool balanceOf -> verified=False
    bal_res = verify_candidate_with_onchain_quoter(
        MOCK_RPC_URL,
        hops,
        amount_in,
        gas_cost_in_base=0,
        l1_fee_in_base=0,
        pools_by_id=pools_by_id,
        block_number_hex=pinned_hex,
        transport=_make_quoter_transport(mode="exceed_balance"),
        balances_by_pool=snap.balances_by_pool,
        bridge=bridge,
    )
    assert bal_res.verified is False
    assert "exceeds_pool_balance" in bal_res.reason

    # 6. Non-positive net profit after gas/L1 fee -> verified=False
    unprof_res = verify_candidate_with_onchain_quoter(
        MOCK_RPC_URL,
        hops,
        amount_in,
        gas_cost_in_base=ss_out,
        l1_fee_in_base=0,
        pools_by_id=pools_by_id,
        block_number_hex=pinned_hex,
        transport=_make_quoter_transport(mode="ok"),
        balances_by_pool=snap.balances_by_pool,
        bridge=bridge,
    )
    assert unprof_res.verified is False
    assert unprof_res.simulated_net_profit < 0
    assert unprof_res.reason == "non_positive_net_profit"

    # 7. V2 local quote failure (exceeding reserves) and broken continuity fail closed
    aero_pool = pools[2]
    aero_node = pools_by_id[aero_pool.pool_id]
    v2_oversized = (
        CycleHop(
            pool_id=aero_pool.pool_id,
            kind=PoolKind.V2,
            token_in=aero_node.token0,
            token_out=aero_node.token1,
            amount_in=10**25,
            amount_out=1,
        ),
    )
    bad_cont = verify_candidate_with_onchain_quoter(
        MOCK_RPC_URL,
        v2_oversized,
        amount_in=10**18,
        gas_cost_in_base=0,
        l1_fee_in_base=0,
        pools_by_id=pools_by_id,
        block_number_hex=pinned_hex,
        transport=_make_quoter_transport(mode="ok"),
        balances_by_pool=snap.balances_by_pool,
        bridge=bridge,
    )
    assert bad_cont.verified is False
    assert bad_cont.reason == "route_does_not_close_cycle"

    zero_in = verify_candidate_with_onchain_quoter(
        MOCK_RPC_URL,
        hops,
        amount_in=0,
        gas_cost_in_base=0,
        l1_fee_in_base=0,
        pools_by_id=pools_by_id,
        block_number_hex=pinned_hex,
        transport=_make_quoter_transport(mode="ok"),
        bridge=bridge,
    )
    assert zero_in.verified is False
    assert zero_in.reason == "amount_in_must_be_positive"
