"""Offline unit tests for Base Aave V3 and Morpho Blue lending health & liquidation reader."""

import json
import os
import socket
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from email.message import Message
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from eth_abi.abi import decode, encode

from oasis_agent.errors import RpcError
from oasis_agent.evidence.base_denim import (
    BASE_CHAIN_ID,
    BASE_TIME,
    TIMESTAMP_MILLIS_PART_SELECTOR,
    TIMESTAMP_MS_SELECTOR,
)
from oasis_agent.fastpath import (
    FastpathBridge,
    FastpathPoolGraph,
    FastpathUnavailableError,
    PoolKind,
)
from oasis_agent.fastpath.lending_reader import (
    AAVE_BORROW_TOPIC0,
    AAVE_GET_RESERVES_LIST_SELECTOR,
    AAVE_GET_USER_ACCOUNT_DATA_SELECTOR,
    AAVE_LIQUIDATION_CALL_TOPIC0,
    AAVE_REPAY_TOPIC0,
    AAVE_SUPPLY_TOPIC0,
    AAVE_V3_POOL,
    AGGREGATE3_SELECTOR,
    LENDING_HEALTH_SCHEMA_VERSION,
    MORPHO_AUDIT_SCHEMA_VERSION,
    MORPHO_BLUE,
    MORPHO_ID_TO_MARKET_PARAMS_SELECTOR,
    MORPHO_MARKET_SELECTOR,
    MORPHO_POSITION_SELECTOR,
    MULTICALL3,
    MULTICALL3_GET_CHAIN_ID_SELECTOR,
    AaveBorrowerAccountState,
    LendingHealthScanReport,
    LendingReaderError,
    MorphoBlueAuditReport,
    MorphoBorrowerPosition,
    MorphoMarketState,
    _create_ipv4_preferred_connection,
    _default_https_transport,
    _IPv4PreferredHTTPSHandler,
    _NoRedirectHandler,
    _validate_resolved_dns,
    _validate_rpc_url,
    main,
    read_morpho_blue_positions,
    scan_aave_v3_base_health,
)
from oasis_agent.fastpath.liquidation import FlashLiquidationConfig
from oasis_agent.fastpath.live_shadow_runner import MAX_RESPONSE_BYTES, resolve_fastpath_bridge
from oasis_agent.fastpath.pool_graph import PoolNode, TickLiquidity
from oasis_agent.models.common import ZERO_ADDRESS, normalize_address, sha256_hex
from oasis_agent.sniping.venues import USDC, WETH
from oasis_agent.uniswap_v4.liquidity_math import sqrt_price_at_tick

REPO_ROOT = Path(__file__).resolve().parents[2]
MOCK_RPC_URL = "https://rpc.example.invalid"


def _synth_addr(idx: int) -> str:
    return f"0x{idx:040x}"


def _synth_hash(label: bytes) -> str:
    return "0x" + sha256_hex(label)


def _topic_addr(addr: str) -> str:
    return "0x" + ("00" * 12) + normalize_address(addr)[2:]


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


def _build_aave_mock_transport(
    *,
    block_number: int = 58_300_120,
    ts_ms: int = 1_794_200_000_400,
    logs_by_range: dict[tuple[int, int], list[dict[str, Any]]] | None = None,
    accounts: dict[str, tuple[int, int, int, int, int, int]] | None = None,
    reserves: Sequence[str] = (),
    range_error_above_span: int | None = None,
    chain_id_override: int = BASE_CHAIN_ID,
    basetime_delta_ms: int = 0,
    corrupt_header_ms: bool = False,
) -> tuple[Callable[[str, bytes, float], bytes], list[dict[str, Any]]]:
    calls_seen: list[dict[str, Any]] = []
    block_hash = _synth_hash(f"base-lending-block-{block_number}".encode())
    parent_hash = _synth_hash(f"base-lending-parent-{block_number}".encode())
    ts_sec = ts_ms // 1000
    millis_part = ts_ms % 1000
    acct_map = {normalize_address(k): v for k, v in (accounts or {}).items()}
    reserves_list = [normalize_address(r) for r in reserves]

    def _transport(_url: str, payload: bytes, _timeout_s: float) -> bytes:
        req = json.loads(payload.decode("utf-8"))
        method = req["method"]
        req_id = req["id"]
        calls_seen.append(req)

        if method == "eth_getBlockByNumber":
            header: dict[str, object] = {
                "number": hex(block_number),
                "hash": block_hash,
                "parentHash": parent_hash,
                "timestamp": hex(ts_sec),
                "timestampMs": hex(ts_ms + (200 if corrupt_header_ms else 0)),
            }
            return json.dumps({"jsonrpc": "2.0", "id": req_id, "result": header}).encode("utf-8")

        if method == "eth_getLogs":
            flt = req["params"][0]
            assert normalize_address(flt["address"]) == AAVE_V3_POOL
            assert flt["topics"] == [AAVE_BORROW_TOPIC0]
            start = int(flt["fromBlock"], 16)
            end = int(flt["toBlock"], 16)
            span = end - start + 1
            if range_error_above_span is not None and span > range_error_above_span:
                return json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {
                            "code": -32005,
                            "message": f"eth_getLogs block range exceeds {range_error_above_span}",
                        },
                    }
                ).encode("utf-8")
            matched: list[dict[str, Any]] = []
            for (r_start, r_end), entries in sorted((logs_by_range or {}).items()):
                if r_end < start or r_start > end:
                    continue
                for entry in entries:
                    bn = int(entry["blockNumber"], 16)
                    if start <= bn <= end:
                        matched.append(entry)
            return json.dumps({"jsonrpc": "2.0", "id": req_id, "result": matched}).encode("utf-8")

        if method == "eth_call":
            call_obj, blk = req["params"]
            assert blk == hex(block_number)
            assert normalize_address(call_obj["to"]) == MULTICALL3
            raw_calldata = bytes.fromhex(call_obj["data"][2:])
            assert "0x" + raw_calldata[:4].hex() == AGGREGATE3_SELECTOR
            (subcalls,) = decode(["(address,bool,bytes)[]"], raw_calldata[4:], strict=True)

            mc_results: list[tuple[bool, bytes]] = []
            for target, _allow_fail, data in subcalls:
                norm_target = normalize_address(str(target))
                sel = "0x" + data[:4].hex()
                if norm_target == MULTICALL3 and sel == MULTICALL3_GET_CHAIN_ID_SELECTOR:
                    mc_results.append((True, chain_id_override.to_bytes(32, "big")))
                elif norm_target == BASE_TIME and sel == TIMESTAMP_MS_SELECTOR:
                    mc_results.append((True, (ts_ms + basetime_delta_ms).to_bytes(32, "big")))
                elif norm_target == BASE_TIME and sel == TIMESTAMP_MILLIS_PART_SELECTOR:
                    mc_results.append((True, millis_part.to_bytes(32, "big")))
                elif norm_target == AAVE_V3_POOL and sel == AAVE_GET_RESERVES_LIST_SELECTOR:
                    mc_results.append((True, encode(["address[]"], [reserves_list])))
                elif norm_target == AAVE_V3_POOL and sel == AAVE_GET_USER_ACCOUNT_DATA_SELECTOR:
                    (borrower,) = decode(["address"], data[4:], strict=True)
                    norm_b = normalize_address(str(borrower))
                    vals = acct_map.get(norm_b, (0, 0, 0, 0, 0, (1 << 256) - 1))
                    mc_results.append(
                        (
                            True,
                            encode(
                                ["uint256", "uint256", "uint256", "uint256", "uint256", "uint256"],
                                list(vals),
                            ),
                        )
                    )
                else:
                    raise AssertionError(f"unexpected subcall target={norm_target} sel={sel}")

            out_hex = "0x" + encode(["(bool,bytes)[]"], [mc_results]).hex()
            return json.dumps({"jsonrpc": "2.0", "id": req_id, "result": out_hex}).encode("utf-8")

        raise AssertionError(f"unexpected method {method}")

    return _transport, calls_seen


def _make_borrow_log(block_number: int, reserve: str, on_behalf_of: str) -> dict[str, Any]:
    return {
        "address": AAVE_V3_POOL,
        "blockNumber": hex(block_number),
        "removed": False,
        "topics": [
            AAVE_BORROW_TOPIC0,
            _topic_addr(reserve),
            _topic_addr(on_behalf_of),
            "0x" + (0).to_bytes(32, "big").hex(),
        ],
        "data": "0x"
        + encode(["address", "uint256", "uint8", "uint256"], [on_behalf_of, 1000, 2, 1]).hex(),
    }


def test_dynamic_topic0_and_addr20_constants() -> None:
    assert len(MULTICALL3) == 42
    assert len(AAVE_V3_POOL) == 42
    assert len(MORPHO_BLUE) == 42
    assert len({MULTICALL3, AAVE_V3_POOL, MORPHO_BLUE}) == 3
    assert len(AAVE_BORROW_TOPIC0) == 66
    assert len(AAVE_SUPPLY_TOPIC0) == 66
    assert len(AAVE_REPAY_TOPIC0) == 66
    assert len(AAVE_LIQUIDATION_CALL_TOPIC0) == 66
    assert (
        len(
            {
                AAVE_BORROW_TOPIC0,
                AAVE_SUPPLY_TOPIC0,
                AAVE_REPAY_TOPIC0,
                AAVE_LIQUIDATION_CALL_TOPIC0,
            }
        )
        == 4
    )
    assert AGGREGATE3_SELECTOR == "0x82ad56cb"
    assert MULTICALL3_GET_CHAIN_ID_SELECTOR == "0x3408e470"


def test_scan_aave_v3_base_health_chunked_logs_and_classification() -> None:
    weth = normalize_address(WETH)
    usdc = normalize_address(USDC)
    b_liq = _synth_addr(0xB1)
    b_risk = _synth_addr(0xB2)
    b_healthy = _synth_addr(0xB3)
    b_zero_debt = _synth_addr(0xB4)

    head_block = 58_300_120
    logs = {
        (head_block - 119, head_block): [
            _make_borrow_log(head_block - 100, usdc, b_zero_debt),
            _make_borrow_log(head_block - 60, usdc, b_healthy),
            _make_borrow_log(head_block - 10, usdc, b_liq),
            _make_borrow_log(head_block - 5, weth, b_liq),  # duplicate borrower deduped
        ]
    }
    accounts = {
        b_liq: (10_500 * 10**8, 9_500 * 10**8, 0, 8250, 8000, 96 * 10**16),
        b_risk: (15_000 * 10**8, 12_000 * 10**8, 100 * 10**8, 8250, 8000, 104 * 10**16),
        b_healthy: (30_000 * 10**8, 11_000 * 10**8, 10_000 * 10**8, 8250, 8000, 210 * 10**16),
        b_zero_debt: (5_000 * 10**8, 0, 4_000 * 10**8, 8250, 8000, (1 << 256) - 1),
    }
    transport, calls = _build_aave_mock_transport(
        block_number=head_block,
        logs_by_range=logs,
        accounts=accounts,
        reserves=(weth, usdc),
    )

    report = scan_aave_v3_base_health(
        rpc_url=MOCK_RPC_URL,
        lookback_blocks=120,
        chunk_blocks=50,
        max_borrowers=60,
        seed_borrowers=(b_risk,),
        transport=transport,
    )

    assert isinstance(report, LendingHealthScanReport)
    assert report.schema_version == LENDING_HEALTH_SCHEMA_VERSION
    assert report.chain_id == BASE_CHAIN_ID
    assert report.execution_eligible is False
    assert report.protocol == "aave_v3"
    assert report.block_number == head_block
    assert report.reserves_count == 2
    assert report.reserves == (weth, usdc)
    assert report.lookback_blocks == 120
    assert report.borrowers_discovered == 4
    # 1 eth_getBlockByNumber + 3 eth_getLogs chunks (50, 50, 20) + 1 Multicall3 = 5 calls
    assert report.rpc_calls_used == 5
    get_logs_calls = [c for c in calls if c["method"] == "eth_getLogs"]
    assert len(get_logs_calls) == 3

    # Zero-debt account filtered out; remaining 3 sorted ascending by health_factor_wad
    assert len(report.active_borrowers) == 3
    assert [a.borrower for a in report.active_borrowers] == [b_liq, b_risk, b_healthy]
    assert report.min_health_factor_wad == 96 * 10**16

    assert len(report.liquidatable_accounts) == 1
    liq_acct = report.liquidatable_accounts[0]
    assert isinstance(liq_acct, AaveBorrowerAccountState)
    assert liq_acct.borrower == b_liq
    assert liq_acct.liquidatable is True
    assert liq_acct.distance_to_liquidation_bps == -400
    assert liq_acct.total_debt_usd == 9_500.0

    assert len(report.at_risk_watchlist) == 1
    risk_acct = report.at_risk_watchlist[0]
    assert risk_acct.borrower == b_risk
    assert risk_acct.liquidatable is False
    assert risk_acct.distance_to_liquidation_bps == 400

    healthy_acct = report.active_borrowers[2]
    assert healthy_acct.distance_to_liquidation_bps == 11_000
    assert healthy_acct.liquidatable is False

    # JSON & hash integrity
    as_dict = report.to_dict()
    assert as_dict["execution_eligible"] is False
    assert json.loads(report.to_json()) == as_dict
    body_without_hash = {k: v for k, v in as_dict.items() if k != "report_sha256"}
    reconstructed = LendingHealthScanReport(
        protocol=report.protocol,
        block_number=report.block_number,
        block_hash=report.block_hash,
        timestamp_ms=report.timestamp_ms,
        rpc_origin=report.rpc_origin,
        reserves_count=report.reserves_count,
        reserves=report.reserves,
        lookback_blocks=report.lookback_blocks,
        borrowers_discovered=report.borrowers_discovered,
        active_borrowers=report.active_borrowers,
        liquidatable_accounts=report.liquidatable_accounts,
        at_risk_watchlist=report.at_risk_watchlist,
        min_health_factor_wad=report.min_health_factor_wad,
        liquidation_candidates_count=report.liquidation_candidates_count,
        liquidation_candidates=report.liquidation_candidates,
        rpc_calls_used=report.rpc_calls_used,
    )
    assert reconstructed.report_sha256 == report.report_sha256
    assert len(body_without_hash["active_borrowers"]) == 3


def test_scan_aave_v3_range_narrowing_and_max_borrowers_cap() -> None:
    weth = normalize_address(WETH)
    usdc = normalize_address(USDC)
    head_block = 58_300_100
    b1, b2, b3 = _synth_addr(0xC1), _synth_addr(0xC2), _synth_addr(0xC3)
    logs = {
        (head_block - 99, head_block): [
            _make_borrow_log(head_block - 90, usdc, b3),
            _make_borrow_log(head_block - 30, usdc, b2),
            _make_borrow_log(head_block - 1, weth, b1),
        ]
    }
    accounts = {
        b1: (20_000 * 10**8, 10_000 * 10**8, 1_000 * 10**8, 8250, 8000, 150 * 10**16),
        b2: (20_000 * 10**8, 10_000 * 10**8, 1_000 * 10**8, 8250, 8000, 140 * 10**16),
        b3: (20_000 * 10**8, 10_000 * 10**8, 1_000 * 10**8, 8250, 8000, 130 * 10**16),
    }
    # Force chunk_blocks=50 to split down to <= 25 blocks on JSON-RPC -32005 range error.
    transport, calls = _build_aave_mock_transport(
        block_number=head_block,
        logs_by_range=logs,
        accounts=accounts,
        reserves=(weth, usdc),
        range_error_above_span=25,
    )
    report = scan_aave_v3_base_health(
        rpc_url=MOCK_RPC_URL,
        lookback_blocks=100,
        chunk_blocks=50,
        max_borrowers=2,
        transport=transport,
    )
    assert report.borrowers_discovered == 2
    assert {a.borrower for a in report.active_borrowers} == {b1, b2}
    get_logs_calls = [c for c in calls if c["method"] == "eth_getLogs"]
    assert len(get_logs_calls) >= 3
    assert report.rpc_calls_used == 2 + len(get_logs_calls)


def test_scan_aave_v3_integrates_flash_liquidation_scanner(bridge: FastpathBridge) -> None:
    weth = normalize_address(WETH)
    usdc = normalize_address(USDC)
    b_liq = _synth_addr(0xD1)
    head_block = 58_400_000

    # Deep V2 pool: 1000 WETH vs 3,000,000 USDC -> spot 3000 USDC/WETH, 0.3% fee (< 5% bonus)
    graph = FastpathPoolGraph(bridge=bridge)
    graph.upsert_pool(
        PoolNode(
            chain_id=BASE_CHAIN_ID,
            pool_id=_synth_addr(0x999),
            kind=PoolKind.V2,
            token0=weth,
            token1=usdc,
            decimals0=18,
            decimals1=6,
            fee_pips=3000,
            reserve0=1_000 * 10**18,
            reserve1=3_000_000 * 10**6,
            block_number=head_block,
            timestamp_ms=1_794_200_000_400,
        )
    )
    accounts = {
        b_liq: (10_500 * 10**8, 9_500 * 10**8, 0, 8250, 8000, 96 * 10**16),
    }
    transport, _ = _build_aave_mock_transport(
        block_number=head_block,
        accounts=accounts,
        reserves=(weth, usdc),
    )

    report = scan_aave_v3_base_health(
        rpc_url=MOCK_RPC_URL,
        lookback_blocks=0,
        seed_borrowers=(b_liq,),
        pool_graph=graph,
        transport=transport,
    )
    assert report.liquidation_candidates_count >= 1
    assert len(report.liquidation_candidates) == report.liquidation_candidates_count
    cand = report.liquidation_candidates[0]
    assert cand.protocol == "aave_v3"
    assert cand.borrower == b_liq
    assert cand.debt_token == usdc
    assert cand.collateral_token == weth
    assert cand.net_profit_debt_raw > 0
    assert cand.execution_eligible is False


def test_read_morpho_blue_positions_multicall_decoding() -> None:
    weth = normalize_address(WETH)
    usdc = normalize_address(USDC)
    oracle = _synth_addr(0x501)
    irm = _synth_addr(0x502)
    market_id_1 = _synth_hash(b"morpho-weth-usdc")
    market_id_2 = _synth_hash(b"morpho-cbbtc-usdc")
    borrower_1 = _synth_addr(0x701)
    borrower_2 = _synth_addr(0x702)
    block_number = 58_500_000
    ts_ms = 1_794_300_000_600

    params_by_market = {
        market_id_1: (usdc, weth, oracle, irm, 860_000_000_000_000_000),
        market_id_2: (usdc, _synth_addr(0x503), oracle, irm, 770_000_000_000_000_000),
    }
    market_state_by_id = {
        market_id_1: (
            5_000_000 * 10**6,
            5_000_000 * 10**12,
            3_500_000 * 10**6,
            3_500_000 * 10**12,
            ts_ms // 1000,
            0,
        ),
        market_id_2: (
            2_000_000 * 10**6,
            2_000_000 * 10**12,
            1_100_000 * 10**6,
            1_100_000 * 10**12,
            ts_ms // 1000,
            0,
        ),
    }
    positions_map = {
        (market_id_1, borrower_1): (0, 25_000 * 10**12, 10 * 10**18),
        (market_id_1, borrower_2): (10_000 * 10**12, 0, 0),
        (market_id_2, borrower_1): (0, 0, 0),
        (market_id_2, borrower_2): (0, 5_000 * 10**12, 100_000_000),
    }

    def _morpho_transport(_url: str, payload: bytes, _timeout_s: float) -> bytes:
        req = json.loads(payload.decode("utf-8"))
        method = req["method"]
        req_id = req["id"]
        if method == "eth_getBlockByNumber":
            return json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "number": hex(block_number),
                        "hash": _synth_hash(b"morpho-blk"),
                        "parentHash": _synth_hash(b"morpho-parent"),
                        "timestamp": hex(ts_ms // 1000),
                        "timestampMs": hex(ts_ms),
                    },
                }
            ).encode("utf-8")

        assert method == "eth_call"
        call_obj, blk = req["params"]
        assert blk == hex(block_number)
        assert normalize_address(call_obj["to"]) == MULTICALL3
        raw_cd = bytes.fromhex(call_obj["data"][2:])
        (subcalls,) = decode(["(address,bool,bytes)[]"], raw_cd[4:], strict=True)
        out: list[tuple[bool, bytes]] = []
        for target, _allow_fail, data in subcalls:
            norm_target = normalize_address(str(target))
            sel = "0x" + data[:4].hex()
            if norm_target == MULTICALL3 and sel == MULTICALL3_GET_CHAIN_ID_SELECTOR:
                out.append((True, BASE_CHAIN_ID.to_bytes(32, "big")))
            elif norm_target == BASE_TIME and sel == TIMESTAMP_MS_SELECTOR:
                out.append((True, ts_ms.to_bytes(32, "big")))
            elif norm_target == BASE_TIME and sel == TIMESTAMP_MILLIS_PART_SELECTOR:
                out.append((True, (ts_ms % 1000).to_bytes(32, "big")))
            elif norm_target == MORPHO_BLUE and sel == MORPHO_ID_TO_MARKET_PARAMS_SELECTOR:
                (mid_b,) = decode(["bytes32"], data[4:], strict=True)
                mid = "0x" + mid_b.hex()
                out.append(
                    (
                        True,
                        encode(
                            ["address", "address", "address", "address", "uint256"],
                            list(params_by_market[mid]),
                        ),
                    )
                )
            elif norm_target == MORPHO_BLUE and sel == MORPHO_MARKET_SELECTOR:
                (mid_b,) = decode(["bytes32"], data[4:], strict=True)
                mid = "0x" + mid_b.hex()
                out.append(
                    (
                        True,
                        encode(
                            ["uint128", "uint128", "uint128", "uint128", "uint128", "uint128"],
                            list(market_state_by_id[mid]),
                        ),
                    )
                )
            elif norm_target == MORPHO_BLUE and sel == MORPHO_POSITION_SELECTOR:
                mid_b, bor = decode(["bytes32", "address"], data[4:], strict=True)
                mid = "0x" + mid_b.hex()
                norm_bor = normalize_address(str(bor))
                out.append(
                    (
                        True,
                        encode(
                            ["uint256", "uint128", "uint128"],
                            list(positions_map[(mid, norm_bor)]),
                        ),
                    )
                )
            else:
                raise AssertionError(f"unexpected morpho subcall {norm_target} {sel}")
        return json.dumps(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": "0x" + encode(["(bool,bytes)[]"], [out]).hex(),
            }
        ).encode("utf-8")

    report = read_morpho_blue_positions(
        MOCK_RPC_URL,
        market_ids=[market_id_1, market_id_2, market_id_1],
        borrowers=[borrower_1, borrower_2],
        transport=_morpho_transport,
    )

    assert report.schema_version == MORPHO_AUDIT_SCHEMA_VERSION
    assert report.chain_id == BASE_CHAIN_ID
    assert report.execution_eligible is False
    assert report.rpc_calls_used == 2
    assert len(report.markets) == 2
    assert len(report.positions) == 4

    m1 = report.markets[0]
    assert isinstance(m1, MorphoMarketState)
    assert m1.market_id == market_id_1
    assert m1.loan_token == usdc
    assert m1.collateral_token == weth
    assert m1.lltv_wad == 860_000_000_000_000_000
    assert m1.total_supply_assets == 5_000_000 * 10**6
    assert m1.total_borrow_assets == 3_500_000 * 10**6

    p1 = report.positions[0]
    assert isinstance(p1, MorphoBorrowerPosition)
    assert p1.market_id == market_id_1
    assert p1.borrower == borrower_1
    assert p1.borrow_shares == 25_000 * 10**12
    assert p1.collateral == 10 * 10**18
    assert json.loads(report.to_json()) == report.to_dict()


def test_ipv4_first_tls_ssrf_http429_and_malformed_rejections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 1. URL SSRF guards
    for bad_url in (
        "http://rpc.example.invalid",
        "https://localhost:8545",
        "https://sub.localhost",
        "https://127.0.0.1",
        "https://10.0.0.1",
        "https://198.18.0.1",
        f"https://{'u'}:{'p'}@rpc.example.invalid",
        " https://rpc.example.invalid",
    ):
        with pytest.raises(LendingReaderError):
            _validate_rpc_url(bad_url, enforce_public_host=True)

    # 2. DNS private IP rejection vs TUN Fake-IP domain exemption
    monkeypatch.setattr(
        "oasis_agent.fastpath.lending_reader.socket.getaddrinfo",
        lambda *_a, **_k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.10", 443))],
    )
    with pytest.raises(LendingReaderError, match="private or loopback"):
        _validate_resolved_dns("rpc.example.invalid", 443)

    monkeypatch.setattr(
        "oasis_agent.fastpath.lending_reader.socket.getaddrinfo",
        lambda *_a, **_k: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.0.15", 443)),
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001:2::15", 443, 0, 0)),
        ],
    )
    infos = _validate_resolved_dns("rpc.example.invalid", 443)
    assert len(infos) == 2

    # 3. IPv4-preferred socket connection order when DNS returns IPv6 before IPv4
    attempted_families: list[socket.AddressFamily] = []

    class _FakeSocket:
        def __init__(self, af: socket.AddressFamily, *_args: object) -> None:
            self.af = af

        def settimeout(self, _timeout: float) -> None:
            return None

        def connect(self, _sa: object) -> None:
            attempted_families.append(self.af)

        def close(self) -> None:
            return None

    monkeypatch.setattr(
        "oasis_agent.fastpath.lending_reader.socket.getaddrinfo",
        lambda *_a, **_k: [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:4700:4700::1111", 443, 0, 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
        ],
    )
    monkeypatch.setattr("oasis_agent.fastpath.lending_reader.socket.socket", _FakeSocket)
    sock = _create_ipv4_preferred_connection(("rpc.example.invalid", 443), timeout=5.0)
    assert isinstance(sock, _FakeSocket)
    assert attempted_families == [socket.AF_INET]

    # Falls back to IPv6 when IPv4 fails
    attempted_families.clear()

    class _FailV4Socket(_FakeSocket):
        def connect(self, _sa: object) -> None:
            attempted_families.append(self.af)
            if self.af == socket.AF_INET:
                raise OSError("ipv4 unreachable")

    monkeypatch.setattr("oasis_agent.fastpath.lending_reader.socket.socket", _FailV4Socket)
    sock2 = _create_ipv4_preferred_connection(("rpc.example.invalid", 443), timeout=5.0)
    assert isinstance(sock2, _FailV4Socket)
    assert attempted_families == [socket.AF_INET, socket.AF_INET6]

    # 4. build_opener uses _IPv4PreferredHTTPSHandler + _NoRedirectHandler + certifi;
    #    HTTP 429 fails closed with zero retries.
    http_calls = 0
    seen_handlers: tuple[object, ...] = ()

    def _429_opener(*handlers: object) -> object:
        nonlocal seen_handlers
        seen_handlers = handlers

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
        "oasis_agent.fastpath.lending_reader.socket.getaddrinfo",
        lambda *_a, **_k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
    )
    monkeypatch.setattr(
        "oasis_agent.fastpath.lending_reader.urllib.request.build_opener",
        _429_opener,
    )
    with pytest.raises(RpcError, match="status 429"):
        _default_https_transport(MOCK_RPC_URL, b"{}", 5.0)
    assert http_calls == 1
    assert any(isinstance(h, _IPv4PreferredHTTPSHandler) for h in seen_handlers)
    assert any(isinstance(h, _NoRedirectHandler) for h in seen_handlers)
    ipv4_handler = next(h for h in seen_handlers if isinstance(h, _IPv4PreferredHTTPSHandler))
    assert isinstance(ipv4_handler._ssl_context, ssl.SSLContext)

    # 5. Redirect handler forbids HTTP redirects
    redir = _NoRedirectHandler()
    with pytest.raises(RpcError, match="redirects are forbidden"):
        redir.redirect_request(
            urllib.request.Request(MOCK_RPC_URL),
            BytesIO(b""),
            302,
            "Found",
            Message(),
            "https://other.invalid",
        )

    # 6. Oversized response rejected
    class _BigOpener:
        def open(self, *_a: object, **_k: object) -> object:
            return BytesIO(b"x" * (MAX_RESPONSE_BYTES + 1))

    monkeypatch.setattr(
        "oasis_agent.fastpath.lending_reader.urllib.request.build_opener",
        lambda *_handlers: _BigOpener(),
    )
    with pytest.raises(RpcError, match="exceeds byte limit"):
        _default_https_transport(MOCK_RPC_URL, b"{}", 5.0)


def test_malformed_logs_chain_id_and_basetime_fail_closed() -> None:
    head = 58_300_050
    weth = normalize_address(WETH)

    # Chain ID mismatch
    bad_chain_transport, _ = _build_aave_mock_transport(block_number=head, chain_id_override=1)
    with pytest.raises(LendingReaderError, match="chain_id mismatch"):
        scan_aave_v3_base_health(
            rpc_url=MOCK_RPC_URL, lookback_blocks=0, transport=bad_chain_transport
        )

    # BaseTime header mismatch
    bad_bt_transport, _ = _build_aave_mock_transport(block_number=head, corrupt_header_ms=True)
    with pytest.raises(LendingReaderError, match="base_time_header_timestamp_ms_mismatch"):
        scan_aave_v3_base_health(
            rpc_url=MOCK_RPC_URL, lookback_blocks=0, transport=bad_bt_transport
        )

    # Malformed / removed Borrow log
    removed_log = _make_borrow_log(head, weth, _synth_addr(0xA1))
    removed_log["removed"] = True
    removed_transport, _ = _build_aave_mock_transport(
        block_number=head, logs_by_range={(head - 49, head): [removed_log]}
    )
    with pytest.raises(LendingReaderError, match="removed"):
        scan_aave_v3_base_health(
            rpc_url=MOCK_RPC_URL, lookback_blocks=10, transport=removed_transport
        )

    # Non-canonical address padding in topics[2]
    bad_pad_log = _make_borrow_log(head, weth, _synth_addr(0xA2))
    bad_pad_log["topics"][2] = "0x" + ("ff" * 12) + _synth_addr(0xA2)[2:]
    bad_pad_transport, _ = _build_aave_mock_transport(
        block_number=head, logs_by_range={(head - 49, head): [bad_pad_log]}
    )
    with pytest.raises(LendingReaderError, match="non-canonical address padding"):
        scan_aave_v3_base_health(
            rpc_url=MOCK_RPC_URL, lookback_blocks=10, transport=bad_pad_transport
        )

    # Zero address onBehalfOf rejected
    zero_log = _make_borrow_log(head, weth, ZERO_ADDRESS)
    zero_transport, _ = _build_aave_mock_transport(
        block_number=head, logs_by_range={(head - 49, head): [zero_log]}
    )
    with pytest.raises(LendingReaderError, match="zero address"):
        scan_aave_v3_base_health(rpc_url=MOCK_RPC_URL, lookback_blocks=10, transport=zero_transport)

    # Non-range eth_getLogs error fails closed without splitting/retrying
    err_calls = 0

    def _non_range_err_transport(_url: str, payload: bytes, _timeout_s: float) -> bytes:
        nonlocal err_calls
        req = json.loads(payload.decode("utf-8"))
        if req["method"] == "eth_getBlockByNumber":
            return _build_aave_mock_transport(block_number=head)[0](_url, payload, _timeout_s)
        err_calls += 1
        return json.dumps(
            {
                "jsonrpc": "2.0",
                "id": req["id"],
                "error": {"code": -32601, "message": "method not supported"},
            }
        ).encode("utf-8")

    with pytest.raises(RpcError, match="eth_getLogs RPC error"):
        scan_aave_v3_base_health(
            rpc_url=MOCK_RPC_URL, lookback_blocks=10, transport=_non_range_err_transport
        )
    assert err_calls == 1

    # Duplicate JSON keys in RPC envelope rejected
    def _dup_json_transport(_url: str, _payload: bytes, _timeout_s: float) -> bytes:
        return b'{"jsonrpc":"2.0","id":1,"id":1,"result":{}}'

    with pytest.raises(LendingReaderError, match="malformed JSON envelope"):
        scan_aave_v3_base_health(
            rpc_url=MOCK_RPC_URL, lookback_blocks=0, transport=_dup_json_transport
        )


def test_v3_pool_liquidation_scan_and_morpho_validation_guards(bridge: FastpathBridge) -> None:
    weth = normalize_address(WETH)
    usdc = normalize_address(USDC)
    b_liq = _synth_addr(0xF1)
    head_block = 58_450_000
    v3_tick = -196_258
    v3_liq = 200_000_000_000_000_000
    v3_graph = FastpathPoolGraph(bridge=bridge)
    v3_graph.upsert_pool(
        PoolNode(
            chain_id=BASE_CHAIN_ID,
            pool_id=_synth_addr(0x888),
            kind=PoolKind.V3,
            token0=weth,
            token1=usdc,
            decimals0=18,
            decimals1=6,
            fee_pips=500,
            sqrt_price_x96=sqrt_price_at_tick(v3_tick),
            tick=v3_tick,
            liquidity=v3_liq,
            ticks=(
                TickLiquidity(tick=v3_tick - 200, liquidity_net=v3_liq),
                TickLiquidity(tick=v3_tick + 200, liquidity_net=-v3_liq),
            ),
            block_number=head_block,
            timestamp_ms=1_794_200_000_400,
        )
    )
    accounts = {
        b_liq: (5_200 * 10**8, 4_800 * 10**8, 0, 8250, 8000, 96 * 10**16),
    }
    transport, _ = _build_aave_mock_transport(
        block_number=head_block,
        accounts=accounts,
        reserves=(weth, usdc),
    )

    v3_report = scan_aave_v3_base_health(
        rpc_url=MOCK_RPC_URL,
        lookback_blocks=0,
        seed_borrowers=(b_liq,),
        pool_graph=v3_graph,
        transport=transport,
    )
    assert v3_report.liquidation_candidates_count == 1
    assert v3_report.liquidation_candidates[0].execution_eligible is False

    # Prohibitive gas cost suppresses candidate
    unprofitable = scan_aave_v3_base_health(
        rpc_url=MOCK_RPC_URL,
        lookback_blocks=0,
        seed_borrowers=(b_liq,),
        pool_graph=v3_graph,
        transport=transport,
        liquidation_config=FlashLiquidationConfig(gas_cost_in_debt_raw=10_000 * 10**6),
    )
    assert unprofitable.liquidation_candidates_count == 0
    assert unprofitable.liquidation_candidates == ()

    # Morpho validation guards
    with pytest.raises(LendingReaderError, match="market_ids must be non-empty"):
        read_morpho_blue_positions(MOCK_RPC_URL, market_ids=[], borrowers=[], transport=transport)

    with pytest.raises(LendingReaderError, match="loan_token cannot be zero address"):
        MorphoMarketState(
            market_id=_synth_hash(b"bad-morpho"),
            loan_token=ZERO_ADDRESS,
            collateral_token=weth,
            oracle=_synth_addr(0x1),
            irm=_synth_addr(0x2),
            lltv_wad=860_000_000_000_000_000,
            total_supply_assets=1,
            total_borrow_assets=0,
        )

    with pytest.raises(LendingReaderError, match="execution_eligible=False"):
        MorphoBlueAuditReport(
            protocol="morpho_blue",
            block_number=head_block,
            block_hash=_synth_hash(b"h"),
            timestamp_ms=1_794_200_000_400,
            rpc_origin=MOCK_RPC_URL,
            markets=(),
            positions=(),
            rpc_calls_used=2,
            execution_eligible=True,  # type: ignore[arg-type]
        )


def test_cli_out_file_overwrite_and_symlink_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    weth = normalize_address(WETH)
    usdc = normalize_address(USDC)
    borrower = _synth_addr(0xE1)
    transport, _ = _build_aave_mock_transport(
        block_number=58_600_000,
        accounts={borrower: (10_500 * 10**8, 9_500 * 10**8, 0, 8250, 8000, 96 * 10**16)},
        reserves=(weth, usdc),
    )
    monkeypatch.setattr("oasis_agent.fastpath.lending_reader._default_https_transport", transport)
    monkeypatch.setattr(
        "oasis_agent.fastpath.lending_reader.socket.getaddrinfo",
        lambda *_a, **_k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
    )

    out_file = tmp_path / "reports" / "aave_health.json"
    rc = main(
        [
            "--rpc-url",
            MOCK_RPC_URL,
            "--lookback-blocks",
            "0",
            "--seed-borrower",
            borrower,
            "--out",
            str(out_file),
        ]
    )
    assert rc == 0
    assert out_file.is_file()
    written = json.loads(out_file.read_text(encoding="utf-8"))
    assert written["schema_version"] == LENDING_HEALTH_SCHEMA_VERSION
    assert written["execution_eligible"] is False
    assert len(written["liquidatable_accounts"]) == 1

    # Clean overwrite of regular file succeeds
    rc_overwrite = main(
        [
            "--rpc-url",
            MOCK_RPC_URL,
            "--lookback-blocks",
            "0",
            "--seed-borrower",
            borrower,
            "--out",
            str(out_file),
        ]
    )
    assert rc_overwrite == 0

    # Stdout mode works without --out
    capsys.readouterr()
    rc_stdout = main(["--rpc-url", MOCK_RPC_URL, "--lookback-blocks", "0"])
    assert rc_stdout == 0
    stdout_payload = json.loads(capsys.readouterr().out)
    assert stdout_payload["execution_eligible"] is False

    # Symlink file refusal
    target_sentinel = tmp_path / "sentinel.json"
    target_sentinel.write_text("do-not-touch", encoding="utf-8")
    symlink_out = tmp_path / "symlink_out.json"
    symlink_out.symlink_to(target_sentinel)
    assert (
        main(["--rpc-url", MOCK_RPC_URL, "--lookback-blocks", "0", "--out", str(symlink_out)]) == 2
    )
    assert target_sentinel.read_text(encoding="utf-8") == "do-not-touch"

    # Symlink parent directory refusal
    real_dir = tmp_path / "real_dir"
    real_dir.mkdir()
    symlink_dir = tmp_path / "symlink_dir"
    symlink_dir.symlink_to(real_dir)
    assert (
        main(
            [
                "--rpc-url",
                MOCK_RPC_URL,
                "--lookback-blocks",
                "0",
                "--out",
                str(symlink_dir / "out.json"),
            ]
        )
        == 2
    )

    proc = subprocess.run(
        [sys.executable, "-m", "oasis_agent.fastpath.lending_reader", "--help"],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")},
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    assert proc.returncode == 0
    assert "--lookback-blocks" in proc.stdout
