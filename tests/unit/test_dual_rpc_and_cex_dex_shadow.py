"""Unit tests for dual-provider RPC quorum, CEX-DEX shadow, and 100-tick campaign."""

import concurrent.futures
import http.client
import json
import math
import os
import socket
import ssl
import subprocess
import sys
import threading
import time
import types
import urllib.error
from decimal import Decimal
from email.message import Message
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest

from oasis_agent.arbitrage.event_quote import V3
from oasis_agent.cross_venue.coinbase_trade_flow import CoinbaseTradeFlowError, CoinbaseTradePrint
from oasis_agent.cross_venue.live_cex_dex_shadow import (
    CoinbaseL2Book,
    CoinbaseL2Level,
    LiveCexDexError,
    _validate_coinbase_url,
    build_synthetic_coinbase_l2_book,
    build_synthetic_coinbase_transport,
    evaluate_cex_dex_opportunity,
    evaluate_live_cex_dex_shadow,
    fetch_coinbase_l2_book,
    gas_wei_to_usd,
    parse_coinbase_l2_book,
    serialize_coinbase_book_payload,
)
from oasis_agent.cross_venue.live_cex_dex_shadow import (
    _validate_resolved_dns as _validate_cb_dns,
)
from oasis_agent.errors import RpcError
from oasis_agent.evidence.base_denim import (
    BASE_CHAIN_ID,
    BASE_TIME,
    TIMESTAMP_MILLIS_PART_SELECTOR,
    TIMESTAMP_MS_SELECTOR,
)
from oasis_agent.fastpath import FastpathBridge, FastpathUnavailableError, PoolKind
from oasis_agent.fastpath.live_shadow_runner import (
    BasePoolSpec,
    ShadowBudgetError,
    ShadowRunnerError,
    resolve_fastpath_bridge,
)
from oasis_agent.fastpath.multicall_universe import (
    UNIV3_FACTORY,
    VIRTUAL_TOKEN,
    DiscoveredPool,
    PinnedUniverseSnapshot,
    QuoterVerificationResult,
)
from oasis_agent.fastpath.pool_graph import PoolNode, TickLiquidity
from oasis_agent.live.canary_readiness import evaluate_profit_engine_readiness
from oasis_agent.live.dual_rpc_probe import (
    CODE_TARGETS,
    DualRpcQuorumError,
    DualRpcQuorumReport,
    probe_dual_base_rpc_quorum,
)
from oasis_agent.live.dual_rpc_probe import (
    SCHEMA_VERSION as DUAL_RPC_SCHEMA,
)
from oasis_agent.live.dual_rpc_probe import (
    _validate_resolved_dns as _validate_dual_dns,
)
from oasis_agent.live.dual_rpc_probe import (
    main as dual_rpc_main,
)
from oasis_agent.models.common import canonical_json, normalize_address, sha256_hex
from oasis_agent.sniping.venues import USDC, WETH
from oasis_agent.uniswap_v4.liquidity_math import sqrt_price_at_tick

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts import run_multicall_100tick_campaign as campaign_cli  # noqa: E402
from scripts.run_multicall_100tick_campaign import (  # noqa: E402
    AERO_TOKEN,
    CBBTC,
    SUMMARY_SCHEMA_VERSION,
    _KeepAliveHttpsPool,
    run_multicall_campaign,
    verify_campaign_attribution_chain,
)


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


def _noop_sleep(_seconds: float) -> None:
    return None


def _synth_hash(tag: str) -> str:
    return "0x" + sha256_hex(tag.encode("utf-8"))


def _synth_addr(idx: int) -> str:
    return f"0x{idx:040x}"


def _build_dual_rpc_transport(
    *,
    latest_block: int = 58_900_010,
    secondary_chain_id: int = BASE_CHAIN_ID,
    secondary_block_hash_override: str | None = None,
    secondary_parent_hash_override: str | None = None,
    secondary_state_root_override: str | None = None,
    secondary_timestamp_offset_s: int = 0,
    secondary_basetime_ms_offset: int = 0,
    secondary_sync_header_to_basetime: bool = False,
    secondary_code_override: dict[str, str] | None = None,
):
    base_ts_s = 1_795_000_000
    base_ts_ms = base_ts_s * 1000 + 400
    code_by_target = {
        normalize_address(WETH): "0x6080604052",
        normalize_address(V3.address): "0x6080604053",
        normalize_address(BASE_TIME): "0x6080604054",
    }

    def _u256_hex(val: int) -> str:
        return "0x" + val.to_bytes(32, "big").hex()

    def _transport(url: str, payload: bytes, _timeout_s: float) -> bytes:
        is_secondary = "secondary" in url
        decoded = json.loads(payload.decode("utf-8"))

        def _handle_one(req: dict[str, Any]) -> dict[str, Any]:
            req_id = req["id"]
            method = req["method"]
            params = req.get("params", [])
            if method == "eth_chainId":
                cid = secondary_chain_id if is_secondary else BASE_CHAIN_ID
                return {"jsonrpc": "2.0", "id": req_id, "result": hex(cid)}
            if method == "eth_blockNumber":
                return {"jsonrpc": "2.0", "id": req_id, "result": hex(latest_block)}
            if method == "eth_getBlockByNumber":
                block_num = int(params[0], 16)
                b_hash = (
                    secondary_block_hash_override
                    if (is_secondary and secondary_block_hash_override is not None)
                    else _synth_hash(f"block-{block_num}")
                )
                p_hash = (
                    secondary_parent_hash_override
                    if (is_secondary and secondary_parent_hash_override is not None)
                    else _synth_hash(f"parent-{block_num - 1}")
                )
                s_root = (
                    secondary_state_root_override
                    if (is_secondary and secondary_state_root_override is not None)
                    else _synth_hash(f"state-{block_num}")
                )
                ts_s = base_ts_s + (secondary_timestamp_offset_s if is_secondary else 0)
                if is_secondary and secondary_sync_header_to_basetime:
                    ts_ms = ts_s * 1000 + 400 + secondary_basetime_ms_offset
                else:
                    ts_ms = ts_s * 1000 + 400
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "number": hex(block_num),
                        "hash": b_hash,
                        "parentHash": p_hash,
                        "stateRoot": s_root,
                        "timestamp": hex(ts_s),
                        "timestampMs": hex(ts_ms),
                    },
                }
            if method == "eth_call":
                call_obj = params[0]
                data = call_obj["data"].lower()
                ts_ms = (
                    base_ts_ms
                    + (
                        secondary_timestamp_offset_s * 1000
                        if (is_secondary and secondary_sync_header_to_basetime)
                        else 0
                    )
                    + (secondary_basetime_ms_offset if is_secondary else 0)
                )
                if data == TIMESTAMP_MS_SELECTOR:
                    return {"jsonrpc": "2.0", "id": req_id, "result": _u256_hex(ts_ms)}
                if data == TIMESTAMP_MILLIS_PART_SELECTOR:
                    return {"jsonrpc": "2.0", "id": req_id, "result": _u256_hex(ts_ms % 1000)}
            if method == "eth_getCode":
                target = normalize_address(params[0])
                code = code_by_target[target]
                if is_secondary and secondary_code_override and target in secondary_code_override:
                    code = secondary_code_override[target]
                return {"jsonrpc": "2.0", "id": req_id, "result": code}
            raise ValueError(f"unexpected method: {method!r}")

        if isinstance(decoded, list):
            return json.dumps([_handle_one(item) for item in decoded]).encode("utf-8")
        return json.dumps(_handle_one(decoded)).encode("utf-8")

    return _transport


def test_dual_rpc_quorum_agreement_and_report_integrity() -> None:
    report = probe_dual_base_rpc_quorum(
        primary_url="https://primary-base-rpc.test",
        primary_provider="primary_publicnode",
        secondary_url="https://secondary-base-rpc.test",
        secondary_provider="secondary_base_official",
        block_lag=2,
        transport=_build_dual_rpc_transport(latest_block=58_900_010),
    )

    assert isinstance(report, DualRpcQuorumReport)
    assert report.schema_version == DUAL_RPC_SCHEMA
    assert report.chain_id == BASE_CHAIN_ID
    assert report.execution_eligible is False
    assert report.secondary_rpc_verified is True
    assert report.pinned_block_number == 58_900_008
    assert report.pinned_block_hash == _synth_hash("block-58900008")
    assert report.parent_hash == _synth_hash("parent-58900007")
    assert report.state_root == _synth_hash("state-58900008")
    assert report.base_time_ms == 1_795_000_000_400
    assert set(report.code_sha256_by_target) == set(CODE_TARGETS)

    body = report.to_dict()
    declared_sha = body.pop("report_sha256")
    assert declared_sha == sha256_hex(canonical_json(body))
    parsed = json.loads(report.to_json())
    assert parsed["execution_eligible"] is False
    assert parsed["secondary_rpc_verified"] is True


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"secondary_chain_id": 1}, "eth_chainId mismatch"),
        ({"secondary_block_hash_override": _synth_hash("mutated-block")}, "block hash"),
        ({"secondary_parent_hash_override": _synth_hash("mutated-parent")}, "parentHash"),
        ({"secondary_state_root_override": _synth_hash("mutated-state")}, "stateRoot"),
        ({"secondary_timestamp_offset_s": 1}, "base_time_second_mismatch"),
        (
            {"secondary_timestamp_offset_s": 1, "secondary_sync_header_to_basetime": True},
            "mismatch on timestamp",
        ),
        ({"secondary_basetime_ms_offset": 200}, "base_time_header_timestamp_ms_mismatch"),
        (
            {"secondary_basetime_ms_offset": 200, "secondary_sync_header_to_basetime": True},
            "mismatch on BaseTime",
        ),
        ({"secondary_code_override": {normalize_address(WETH): "0x60006000"}}, "eth_getCode"),
        ({"secondary_code_override": {normalize_address(V3.address): "0x"}}, "empty bytecode"),
    ],
)
def test_dual_rpc_quorum_fail_closed_mismatches(kwargs: dict[str, Any], match: str) -> None:
    transport = _build_dual_rpc_transport(**kwargs)
    with pytest.raises(DualRpcQuorumError, match=match):
        probe_dual_base_rpc_quorum(
            primary_url="https://primary-base-rpc.test",
            primary_provider="primary_publicnode",
            secondary_url="https://secondary-base-rpc.test",
            secondary_provider="secondary_base_official",
            transport=transport,
        )


def test_dual_rpc_security_validation_and_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = _build_dual_rpc_transport()
    cred_url = "https://" + "user" + ":" + "pass" + "@primary-base-rpc.test"
    bad_urls = (
        "http://primary-base-rpc.test",
        cred_url,
        "https://localhost:8545",
        "https://127.0.0.1",
        "https://10.0.0.1",
        "https://198.18.0.1",
        "https://[2001:2::1]",
        "https://unused.invalid",
        "https://rpc.example.invalid",
        "https://placeholder.base.org",
    )
    for bad in bad_urls:
        with pytest.raises(DualRpcQuorumError):
            probe_dual_base_rpc_quorum(
                primary_url=bad,
                primary_provider="primary_a",
                secondary_url="https://secondary-base-rpc.test",
                secondary_provider="secondary_b",
                transport=transport,
            )

    # Distinct providers and distinct netlocs enforced (including implicit vs :443 port)
    with pytest.raises(DualRpcQuorumError, match="distinct"):
        probe_dual_base_rpc_quorum(
            primary_url="https://primary-base-rpc.test",
            primary_provider="same_name",
            secondary_url="https://secondary-base-rpc.test",
            secondary_provider="SAME_NAME",
            transport=transport,
        )
    with pytest.raises(DualRpcQuorumError, match="distinct netlocs"):
        probe_dual_base_rpc_quorum(
            primary_url="https://primary-base-rpc.test",
            primary_provider="provider_a",
            secondary_url="https://primary-base-rpc.test:443",
            secondary_provider="provider_b",
            transport=transport,
        )

    # DNS validation: private IP blocked, TUN Fake-IP allowed for domain names
    monkeypatch.setattr(
        "oasis_agent.live.dual_rpc_probe.socket.getaddrinfo",
        lambda *_a, **_k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.2.3", 443))],
    )
    with pytest.raises(DualRpcQuorumError, match="private or loopback"):
        _validate_dual_dns("base-rpc.publicnode.com", 443)

    monkeypatch.setattr(
        "oasis_agent.live.dual_rpc_probe.socket.getaddrinfo",
        lambda *_a, **_k: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.0.22", 443)),
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001:2::22", 443, 0, 0)),
        ],
    )
    _validate_dual_dns("base-rpc.publicnode.com", 443)

    # CLI test using monkeypatched default transport
    monkeypatch.setattr(
        "oasis_agent.live.dual_rpc_probe._default_https_transport",
        transport,
    )
    out_file = tmp_path / "quorum.json"
    rc = dual_rpc_main(
        [
            "--primary-url",
            "https://primary-base-rpc.test",
            "--primary-provider",
            "primary_a",
            "--secondary-url",
            "https://secondary-base-rpc.test",
            "--secondary-provider",
            "secondary_b",
            "--block-lag",
            "2",
            "--out",
            str(out_file),
        ]
    )
    assert rc == 0
    assert out_file.is_file()
    doc = json.loads(out_file.read_text(encoding="utf-8"))
    assert doc["schema_version"] == DUAL_RPC_SCHEMA
    assert doc["execution_eligible"] is False
    # Refuses overwrite
    assert (
        dual_rpc_main(
            [
                "--primary-url",
                "https://primary-base-rpc.test",
                "--secondary-url",
                "https://secondary-base-rpc.test",
                "--out",
                str(out_file),
            ]
        )
        == 2
    )


def test_coinbase_l2_vwap_bidirectional_math_and_degradation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    book = CoinbaseL2Book(
        product_id="ETH-USD",
        bids=(
            CoinbaseL2Level(price=Decimal("3020.00"), size=Decimal("0.10"), num_orders=1),
            CoinbaseL2Level(price=Decimal("3010.00"), size=Decimal("0.50"), num_orders=2),
        ),
        asks=(
            CoinbaseL2Level(price=Decimal("3021.00"), size=Decimal("0.10"), num_orders=1),
            CoinbaseL2Level(price=Decimal("3025.00"), size=Decimal("0.50"), num_orders=2),
        ),
        sequence=42,
        retrieved_at_utc="2026-11-18T08:00:00+00:00",
        raw_sha256="11" * 32,
    )
    # 500 USDC buys 1/6 ETH (~$3000 implied) -> walks 0.1 @ 3020 + 0.066666... @ 3010 => VWAP 3016
    trade_usdc = 500_000_000
    buy_token_out = (10**18) // 6
    trade_token_sell = (10**18) // 6
    # Selling 1/6 ETH on DEX yields 495 USDC (so buy_cex_sell_dex is negative)
    sell_usdc_out = 495_000_000

    res = evaluate_cex_dex_opportunity(
        product_or_symbol="WETH",
        book=book,
        trade_notional_usdc=trade_usdc,
        dex_buy_token_for_usdc_out=buy_token_out,
        trade_notional_token=trade_token_sell,
        dex_sell_token_for_usdc_out=sell_usdc_out,
        gas_cost_usd=Decimal("0.05"),
        cex_taker_fee_bps=Decimal("3.5"),
        latency_haircut_bps=Decimal("1.5"),
    )
    assert res.status == "ok"
    assert res.execution_eligible is False
    assert res.best_direction == "buy_dex_sell_cex"
    assert res.buy_dex_sell_cex is not None
    assert res.buy_dex_sell_cex.levels_consumed == 2
    assert res.buy_dex_sell_cex.cex_vwap_price_usd == pytest.approx(3016.0, rel=1e-4)
    assert res.best_raw_spread_bps is not None and res.best_raw_spread_bps > 50.0
    assert res.best_net_bps is not None and 45.0 < res.best_net_bps < 50.0
    assert res.maker_zero_fee_net_bps is not None
    assert res.maker_zero_fee_net_bps > res.best_net_bps
    assert res.maker_zero_fee_diagnostic_only is True

    # Insufficient CEX depth marks direction infeasible
    shallow_res = evaluate_cex_dex_opportunity(
        product_or_symbol="ETH-USD",
        book=book,
        trade_notional_usdc=5_000_000_000,
        dex_buy_token_for_usdc_out=2 * 10**18,
        trade_notional_token=2 * 10**18,
        dex_sell_token_for_usdc_out=5_000_000_000,
        gas_cost_usd=Decimal("0.05"),
    )
    assert shallow_res.status == "degraded"
    assert shallow_res.best_net_bps is None

    # Test BTC-USD (8 decimals) & AERO-USD (18 decimals) synthetic books + transport
    cb_transport = build_synthetic_coinbase_transport(fail_products=("AERO-USD",))
    btc_book = fetch_coinbase_l2_book("cbBTC", transport=cb_transport)
    assert btc_book.product_id == "BTC-USD"

    aero_deg = evaluate_live_cex_dex_shadow(
        product_or_symbol="AERO",
        trade_notional_usdc=500_000_000,
        dex_buy_token_for_usdc_out=400 * 10**18,
        trade_notional_token=400 * 10**18,
        dex_sell_token_for_usdc_out=499_000_000,
        gas_cost_usd=gas_wei_to_usd(10**13, Decimal(3000)),
        transport=cb_transport,
    )
    assert aero_deg.status == "degraded"
    assert aero_deg.execution_eligible is False
    assert aero_deg.error_reason is not None and "outage" in aero_deg.error_reason

    # Timeout fail-soft degradation
    def _timeout_transport(_url: str, _timeout_s: float) -> bytes:
        raise TimeoutError("coinbase timed out")

    timed_out = evaluate_live_cex_dex_shadow(
        product_or_symbol="ETH-USD",
        trade_notional_usdc=500_000_000,
        dex_buy_token_for_usdc_out=buy_token_out,
        trade_notional_token=trade_token_sell,
        dex_sell_token_for_usdc_out=sell_usdc_out,
        gas_cost_usd=Decimal("0.05"),
        transport=_timeout_transport,
    )
    assert timed_out.status == "degraded"
    assert timed_out.error_reason is not None and "timed out" in timed_out.error_reason

    # Reject NaN, duplicate keys, crossed books, auction_mode, and invalid URLs/DNS
    with pytest.raises(LiveCexDexError, match="non-finite"):
        parse_coinbase_l2_book(
            b'{"bids":[["3000","1"]],"asks":[[NaN,"1"]]}',
            product_id="ETH-USD",
        )
    with pytest.raises(LiveCexDexError, match="duplicate"):
        parse_coinbase_l2_book(
            b'{"bids":[["3000","1"]],"bids":[["2999","1"]],"asks":[["3001","1"]]}',
            product_id="ETH-USD",
        )
    with pytest.raises(LiveCexDexError, match="crossed"):
        parse_coinbase_l2_book(
            b'{"bids":[["3005","1"]],"asks":[["3001","1"]]}',
            product_id="ETH-USD",
        )
    with pytest.raises(LiveCexDexError, match="auction_mode"):
        parse_coinbase_l2_book(
            b'{"bids":[["3000","1"]],"asks":[["3001","1"]],"auction_mode":true}',
            product_id="ETH-USD",
        )
    with pytest.raises(LiveCexDexError, match="allowlisted"):
        _validate_coinbase_url("https://evil.test/products/ETH-USD/book?level=2")

    monkeypatch.setattr(
        "oasis_agent.cross_venue.live_cex_dex_shadow.socket.getaddrinfo",
        lambda *_a, **_k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))],
    )
    with pytest.raises(LiveCexDexError, match="non-public"):
        _validate_cb_dns("api.exchange.coinbase.com")

    monkeypatch.setattr(
        "oasis_agent.cross_venue.live_cex_dex_shadow.socket.getaddrinfo",
        lambda *_a, **_k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.0.55", 443))],
    )
    _validate_cb_dns("api.exchange.coinbase.com")


def test_campaign_quoter_separation_phantom_vs_verified_and_hash_chain(
    tmp_path: Path, bridge: FastpathBridge
) -> None:
    # 1. Dislocated & Quoter-verified => atomic_positive_net_ticks == ticks
    verified_dir = tmp_path / "verified"
    res_ok = run_multicall_campaign(
        out_dir=verified_dir,
        mode="fixture",
        ticks=3,
        interval_ms=0,
        max_rpc_calls=50,
        fixture_scenario="dislocated_quoter_verified",
        bridge=bridge,
        sleeper=_noop_sleep,
    )
    assert res_ok.summary["schema_version"] == SUMMARY_SCHEMA_VERSION
    assert res_ok.summary["execution_eligible"] is False
    assert res_ok.summary["ticks_completed"] == 3
    assert res_ok.summary["atomic_positive_net_ticks"] == 3
    assert res_ok.summary["positive_net_ticks"] == 3
    assert res_ok.summary["atomic_dex_summary"]["total_rust_raw_candidates"] >= 3
    assert res_ok.summary["atomic_dex_summary"]["total_quoter_verified_candidates"] >= 3
    assert res_ok.summary["atomic_dex_summary"]["total_quoter_rejected_candidates"] == 0
    assert type(res_ok.summary["lcb_net_edge_bps"]) is int
    assert res_ok.summary["lcb_net_edge_bps"] == math.floor(
        res_ok.summary["lcb_net_edge_bps_exact"]
    )

    disk_lines = res_ok.attribution_path.read_text(encoding="utf-8").splitlines()
    disk_records = [json.loads(line) for line in disk_lines]
    assert verify_campaign_attribution_chain(disk_records) == res_ok.summary["chain_head_sha256"]

    tampered = [dict(r) for r in disk_records]
    tampered[0]["atomic_best_net_bps"] = 9999.0
    with pytest.raises(ShadowRunnerError, match="tampering"):
        verify_campaign_attribution_chain(tampered)

    # 2. Phantom Rust candidate rejected by on-chain Quoter => ZERO phantom profits!
    phantom_dir = tmp_path / "phantom"
    res_phantom = run_multicall_campaign(
        out_dir=phantom_dir,
        mode="fixture",
        ticks=2,
        interval_ms=0,
        max_rpc_calls=50,
        fixture_scenario="phantom_rust_rejected",
        bridge=bridge,
        sleeper=_noop_sleep,
    )
    assert res_phantom.summary["atomic_dex_summary"]["total_rust_raw_candidates"] >= 2
    assert res_phantom.summary["atomic_dex_summary"]["total_quoter_verified_candidates"] == 0
    assert res_phantom.summary["atomic_dex_summary"]["total_quoter_rejected_candidates"] >= 2
    assert res_phantom.summary["atomic_positive_net_ticks"] == 0


def test_campaign_multi_asset_cex_dex_degradation_and_budget_guard(
    tmp_path: Path, bridge: FastpathBridge
) -> None:
    cbbtc_addr = normalize_address(CBBTC)
    aero_addr = normalize_address(AERO_TOKEN)
    usdc_addr = normalize_address(USDC)
    extra_pools = (
        BasePoolSpec(
            name="v3_cbbtc_usdc_500",
            address=_synth_addr(501),
            kind=PoolKind.V3,
            token0=min(cbbtc_addr, usdc_addr),
            token1=max(cbbtc_addr, usdc_addr),
            decimals0=6 if usdc_addr < cbbtc_addr else 8,
            decimals1=8 if usdc_addr < cbbtc_addr else 6,
            fixed_fee_pips=500,
        ),
        BasePoolSpec(
            name="v3_aero_usdc_3000",
            address=_synth_addr(502),
            kind=PoolKind.V3,
            token0=min(aero_addr, usdc_addr),
            token1=max(aero_addr, usdc_addr),
            decimals0=6 if usdc_addr < aero_addr else 18,
            decimals1=18 if usdc_addr < aero_addr else 6,
            fixed_fee_pips=3000,
        ),
    )
    # Coinbase ETH-USD book is dislocated above DEX ($3050), BTC-USD ok, AERO-USD degraded!
    eth_dislocated = build_synthetic_coinbase_l2_book("ETH-USD", mid_price=Decimal("3050.00"))
    cb_transport = build_synthetic_coinbase_transport(
        books_by_product={"ETH-USD": eth_dislocated},
        fail_products=("AERO-USD",),
    )

    out_multi = tmp_path / "multi"
    res = run_multicall_campaign(
        out_dir=out_multi,
        mode="fixture",
        ticks=3,
        interval_ms=0,
        max_rpc_calls=20,
        fixture_scenario="aligned",
        extra_fixture_pools=extra_pools,
        coinbase_transport=cb_transport,
        bridge=bridge,
        sleeper=_noop_sleep,
    )
    assert res.summary["rpc_calls_used"] == 6  # 2 RPC calls per tick via Multicall3
    assert res.summary["universe_pool_count"] == 6
    assert res.summary["cex_dex_positive_net_ticks"] == 3
    assert res.summary["positive_net_ticks"] == 3
    cex_sum = res.summary["cex_dex_lead_lag_summary"]
    assert cex_sum["products_evaluated"] == ["AERO-USD", "BTC-USD", "ETH-USD"]
    assert cex_sum["ok_evaluations"] == 6
    assert cex_sum["degraded_evaluations"] == 3

    # Budget exhaustion stops before exceeding max_rpc_calls
    with pytest.raises(ShadowBudgetError):
        run_multicall_campaign(
            out_dir=tmp_path / "budget-fail",
            mode="fixture",
            ticks=2,
            interval_ms=0,
            max_rpc_calls=3,
            bridge=bridge,
            sleeper=_noop_sleep,
        )


def test_campaign_multicall_universe_bridge_and_100tick_readiness_compatibility(
    tmp_path: Path, bridge: FastpathBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Verify dynamic bridge to oasis_agent.fastpath.multicall_universe when present
    weth_addr = normalize_address(WETH)
    usdc_addr = normalize_address(USDC)
    calls_seen = {"discover": 0, "snap": 0, "quoter": 0}
    tick_counter = {"n": 0}

    fake_mod = types.ModuleType("oasis_agent.fastpath.multicall_universe")

    def discover_base_universe_pools(**_kwargs: Any) -> tuple[BasePoolSpec, ...]:
        calls_seen["discover"] += 1
        return (
            BasePoolSpec(
                name="bridge_pool_a",
                address=_synth_addr(701),
                kind=PoolKind.V3,
                token0=weth_addr,
                token1=usdc_addr,
                decimals0=18,
                decimals1=6,
                fixed_fee_pips=500,
            ),
            BasePoolSpec(
                name="bridge_pool_b",
                address=_synth_addr(702),
                kind=PoolKind.V3,
                token0=weth_addr,
                token1=usdc_addr,
                decimals0=18,
                decimals1=6,
                fixed_fee_pips=100,
            ),
        )

    def fetch_pinned_universe_snapshot(pools: Any, **_kwargs: Any) -> dict[str, Any]:
        calls_seen["snap"] += 1
        tick_counter["n"] += 1
        b_num = 59_000_000 + tick_counter["n"]
        ts_ms = 1_795_100_000_000 + tick_counter["n"] * 1000
        liq = 200_000_000_000_000_000
        t_a = -196_258
        t_b = -196_120  # dislocated +138 ticks
        nodes = (
            PoolNode(
                chain_id=BASE_CHAIN_ID,
                pool_id=pools[0].address,
                kind=PoolKind.V3,
                token0=weth_addr,
                token1=usdc_addr,
                decimals0=18,
                decimals1=6,
                fee_pips=500,
                sqrt_price_x96=sqrt_price_at_tick(t_a),
                tick=t_a,
                liquidity=liq,
                ticks=(
                    TickLiquidity(t_a - 200, liq),
                    TickLiquidity(t_a + 200, -liq),
                ),
                block_number=b_num,
                timestamp_ms=ts_ms,
            ),
            PoolNode(
                chain_id=BASE_CHAIN_ID,
                pool_id=pools[1].address,
                kind=PoolKind.V3,
                token0=weth_addr,
                token1=usdc_addr,
                decimals0=18,
                decimals1=6,
                fee_pips=100,
                sqrt_price_x96=sqrt_price_at_tick(t_b),
                tick=t_b,
                liquidity=liq,
                ticks=(
                    TickLiquidity(t_b - 200, liq),
                    TickLiquidity(t_b + 200, -liq),
                ),
                block_number=b_num,
                timestamp_ms=ts_ms,
            ),
        )
        return {
            "block_number": b_num,
            "block_hash": _synth_hash(f"bridge-block-{b_num}"),
            "header_timestamp": ts_ms // 1000,
            "base_time_ms": ts_ms,
            "base_fee_per_gas_wei": 10_000_000,
            "l1_data_fee_wei": 1_000_000_000_000,
            "nodes": nodes,
            "rpc_calls_used": 2,
        }

    def verify_candidate_with_onchain_quoter(candidate: Any, **_kwargs: Any) -> dict[str, Any]:
        calls_seen["quoter"] += 1
        return {
            "verified": True,
            "quoter_amount_out": candidate.amount_out,
            "quoter_net_profit": candidate.net_profit,
            "rpc_calls_used": len(candidate.hops),
            "reason": "ok",
        }

    fake_mod.discover_base_universe_pools = discover_base_universe_pools  # type: ignore[attr-defined]
    fake_mod.fetch_pinned_universe_snapshot = fetch_pinned_universe_snapshot  # type: ignore[attr-defined]
    fake_mod.verify_candidate_with_onchain_quoter = verify_candidate_with_onchain_quoter  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "oasis_agent.fastpath.multicall_universe", fake_mod)

    # Mock Coinbase GET transport for public-shadow 100-tick readiness test
    eth_book = build_synthetic_coinbase_l2_book("ETH-USD", mid_price=Decimal("3000.00"))
    cb_bytes = serialize_coinbase_book_payload(eth_book)
    monkeypatch.setattr(
        "oasis_agent.cross_venue.live_cex_dex_shadow._default_https_get",
        lambda _url, _timeout: cb_bytes,
    )

    out_100 = tmp_path / "campaign-100"
    res_100 = run_multicall_campaign(
        out_dir=out_100,
        mode="public-shadow",
        ticks=100,
        interval_ms=0,
        max_rpc_calls=650,
        rpc_url="https://bridge-base-rpc.test",
        bridge=bridge,
        rpc_transport=lambda _u, _p, _t: b"{}",
        sleeper=_noop_sleep,
    )
    assert calls_seen["discover"] == 1
    assert calls_seen["snap"] == 100
    assert calls_seen["quoter"] >= 100
    assert res_100.summary["ticks_completed"] == 100
    assert res_100.summary["unique_blocks_observed"] == 100
    assert type(res_100.summary["lcb_net_edge_bps"]) is int
    assert res_100.summary["lcb_net_edge_bps"] >= 5

    # Feed directly into canary_readiness evaluator (Gates 4, 5, 6 must pass!)
    readiness = evaluate_profit_engine_readiness(shadow_summary_path=res_100.summary_path)
    gate_map = {g.name: g for g in readiness.gates}
    assert gate_map["untouched_shadow_sample_size"].passed is True
    assert gate_map["positive_cost_adjusted_lcb_edge"].passed is True
    assert readiness.execution_eligible is False

    # CLI subprocess smoke check in fixture mode
    monkeypatch.delitem(sys.modules, "oasis_agent.fastpath.multicall_universe")
    out_cli = tmp_path / "cli-fixture"
    env = dict(os.environ)
    env["OASIS_FASTPATH_LIB"] = str(bridge.lib_path)
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.run_multicall_100tick_campaign",
            "--mode",
            "fixture",
            "--ticks",
            "2",
            "--interval-ms",
            "0",
            "--max-rpc-calls",
            "20",
            "--cex-taker-fee-bps",
            "3.5",
            "--latency-haircut-bps",
            "1.5",
            "--out",
            str(out_cli),
        ],
        cwd=REPO_ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    cli_json = json.loads(proc.stdout.strip())
    assert cli_json["mode"] == "fixture"
    assert cli_json["ticks_completed"] == 2
    assert cli_json["execution_eligible"] is False
    assert (out_cli / "shadow_summary.json").is_file()
    assert (out_cli / "shadow_attribution.jsonl").is_file()
    # Direct CLI main check for duplicate --out refusal and symlink refusal
    assert (
        campaign_cli.main(
            ["--mode", "fixture", "--ticks", "1", "--interval-ms", "0", "--out", str(out_cli)]
        )
        == 2
    )
    sym_out = tmp_path / "sym_campaign"
    sym_out.symlink_to(out_cli)
    assert (
        campaign_cli.main(
            ["--mode", "fixture", "--ticks", "1", "--interval-ms", "0", "--out", str(sym_out)]
        )
        == 2
    )


def test_http_429_single_shot_no_retry_guards(monkeypatch: pytest.MonkeyPatch) -> None:
    dual_calls = 0

    def _429_opener(*_args: object, **_kwargs: object) -> object:
        class _Opener:
            def open(self, *_a: object, **_k: object) -> object:
                nonlocal dual_calls
                dual_calls += 1
                raise urllib.error.HTTPError(
                    "https://primary-base-rpc.test",
                    429,
                    "Too Many Requests",
                    Message(),
                    BytesIO(b"rate limited"),
                )

        return _Opener()

    monkeypatch.setattr(
        "oasis_agent.live.dual_rpc_probe.socket.getaddrinfo",
        lambda *_a, **_k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
    )
    monkeypatch.setattr(
        "oasis_agent.live.dual_rpc_probe.urllib.request.build_opener",
        _429_opener,
    )
    with pytest.raises(RpcError, match="status 429"):
        probe_dual_base_rpc_quorum(
            primary_url="https://primary-base-rpc.test",
            primary_provider="primary_a",
            secondary_url="https://secondary-base-rpc.test",
            secondary_provider="secondary_b",
        )
    assert dual_calls == 1

    cb_calls = 0

    def _429_cb_opener(*_args: object, **_kwargs: object) -> object:
        class _Opener:
            def open(self, *_a: object, **_k: object) -> object:
                nonlocal cb_calls
                cb_calls += 1
                raise urllib.error.HTTPError(
                    "https://api.exchange.coinbase.com/products/ETH-USD/book?level=2",
                    429,
                    "Too Many Requests",
                    Message(),
                    BytesIO(b"rate limited"),
                )

        return _Opener()

    monkeypatch.setattr(
        "oasis_agent.cross_venue.live_cex_dex_shadow.socket.getaddrinfo",
        lambda *_a, **_k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
    )
    monkeypatch.setattr(
        "oasis_agent.cross_venue.live_cex_dex_shadow.urllib.request.build_opener",
        _429_cb_opener,
    )
    deg = evaluate_live_cex_dex_shadow(
        product_or_symbol="ETH-USD",
        trade_notional_usdc=500_000_000,
        dex_buy_token_for_usdc_out=(10**18) // 6,
        trade_notional_token=(10**18) // 6,
        dex_sell_token_for_usdc_out=500_000_000,
        gas_cost_usd=Decimal("0.05"),
    )
    assert deg.status == "degraded"
    assert deg.error_reason is not None and "429" in deg.error_reason
    assert cb_calls == 1


def test_campaign_real_multicall_universe_dataclass_interoperability(
    tmp_path: Path, bridge: FastpathBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise native PinnedUniverseSnapshot, DiscoveredPool, and QuoterVerificationResult."""
    weth_addr = normalize_address(WETH)
    usdc_addr = normalize_address(USDC)
    seen: dict[str, int] = {"disc": 0, "snap": 0, "quoter": 0}
    tick_no = {"n": 0}

    disc_pools = (
        DiscoveredPool(
            pool_id=_synth_addr(801),
            label="v3_weth_usdc_500",
            venue="uniswap_v3",
            kind=PoolKind.V3,
            token0=weth_addr,
            token1=usdc_addr,
            decimals0=18,
            decimals1=6,
            fixed_fee_pips=500,
            tick_spacing=None,
            factory=UNIV3_FACTORY,
            reserve0=50 * 10**18,
            reserve1=150_000 * 10**6,
        ),
        DiscoveredPool(
            pool_id=_synth_addr(802),
            label="v3_weth_usdc_100",
            venue="uniswap_v3",
            kind=PoolKind.V3,
            token0=weth_addr,
            token1=usdc_addr,
            decimals0=18,
            decimals1=6,
            fixed_fee_pips=100,
            tick_spacing=None,
            factory=UNIV3_FACTORY,
            reserve0=50 * 10**18,
            reserve1=150_000 * 10**6,
        ),
    )

    def _mock_disc(*_args: Any, **_kwargs: Any) -> tuple[DiscoveredPool, ...]:
        seen["disc"] += 1
        return disc_pools

    def _mock_snap(*_args: Any, **_kwargs: Any) -> PinnedUniverseSnapshot:
        seen["snap"] += 1
        tick_no["n"] += 1
        bnum = 59_100_000 + tick_no["n"]
        ts_ms = 1_795_200_000_000 + tick_no["n"] * 1000
        liq = 200_000_000_000_000_000
        t_a = -196_258
        t_b = -196_120
        nodes = (
            PoolNode(
                chain_id=BASE_CHAIN_ID,
                pool_id=disc_pools[0].pool_id,
                kind=PoolKind.V3,
                token0=weth_addr,
                token1=usdc_addr,
                decimals0=18,
                decimals1=6,
                fee_pips=500,
                sqrt_price_x96=sqrt_price_at_tick(t_a),
                tick=t_a,
                liquidity=liq,
                ticks=(TickLiquidity(t_a - 200, liq), TickLiquidity(t_a + 200, -liq)),
                block_number=bnum,
                timestamp_ms=ts_ms,
            ),
            PoolNode(
                chain_id=BASE_CHAIN_ID,
                pool_id=disc_pools[1].pool_id,
                kind=PoolKind.V3,
                token0=weth_addr,
                token1=usdc_addr,
                decimals0=18,
                decimals1=6,
                fee_pips=100,
                sqrt_price_x96=sqrt_price_at_tick(t_b),
                tick=t_b,
                liquidity=liq,
                ticks=(TickLiquidity(t_b - 200, liq), TickLiquidity(t_b + 200, -liq)),
                block_number=bnum,
                timestamp_ms=ts_ms,
            ),
        )
        return PinnedUniverseSnapshot(
            chain_id=BASE_CHAIN_ID,
            block_number=bnum,
            block_hash=_synth_hash(f"real-snap-{bnum}"),
            parent_hash=_synth_hash(f"real-snap-{bnum - 1}"),
            header_timestamp_ms=ts_ms,
            base_time_ms=ts_ms,
            base_fee_per_gas_wei=10_000_000,
            l1_fee_wei=1_000_000_000_000,
            pools=disc_pools,
            pool_nodes=nodes,
            balances_by_pool={
                disc_pools[0].pool_id: (50 * 10**18, 150_000 * 10**6),
                disc_pools[1].pool_id: (50 * 10**18, 150_000 * 10**6),
            },
            rpc_calls_used=2,
            execution_eligible=False,
        )

    def _mock_quoter(
        rpc_url: str,
        hops: Any,
        amount_in: int,
        gas_cost_in_base: int,
        l1_fee_in_base: int,
        pools_by_id: Any,
        block_number_hex: str,
        **_kwargs: Any,
    ) -> QuoterVerificationResult:
        del rpc_url, hops, pools_by_id, block_number_hex
        seen["quoter"] += 1
        sim_out = amount_in + gas_cost_in_base + l1_fee_in_base + (amount_in // 500)
        return QuoterVerificationResult(
            verified=True,
            simulated_amount_out=sim_out,
            simulated_net_profit=sim_out - amount_in - gas_cost_in_base - l1_fee_in_base,
            hop_outputs=(amount_in, sim_out),
            reason="ok",
            rpc_calls_used=2,
            execution_eligible=False,
        )

    monkeypatch.setattr(
        "oasis_agent.fastpath.multicall_universe.discover_base_universe_pools", _mock_disc
    )
    monkeypatch.setattr(
        "oasis_agent.fastpath.multicall_universe.fetch_pinned_universe_snapshot", _mock_snap
    )
    monkeypatch.setattr(
        "oasis_agent.fastpath.multicall_universe.verify_candidate_with_onchain_quoter", _mock_quoter
    )

    eth_book = build_synthetic_coinbase_l2_book("ETH-USD", mid_price=Decimal("3000.00"))
    cb_bytes = serialize_coinbase_book_payload(eth_book)
    monkeypatch.setattr(
        "oasis_agent.cross_venue.live_cex_dex_shadow._default_https_get",
        lambda _url, _timeout: cb_bytes,
    )

    out_dir = tmp_path / "native-bridge"
    res = run_multicall_campaign(
        out_dir=out_dir,
        mode="public-shadow",
        ticks=2,
        interval_ms=0,
        max_rpc_calls=30,
        rpc_url="https://native-bridge.test",
        bridge=bridge,
        rpc_transport=lambda _u, _p, _t: b"{}",
        sleeper=_noop_sleep,
    )
    assert seen["disc"] == 1
    assert seen["snap"] == 2
    assert seen["quoter"] == 4
    assert res.summary["atomic_positive_net_ticks"] == 2
    assert res.summary["rpc_calls_used"] == 12  # 2 * (2 snapshot + 2 * 2 quoter calls)
    assert res.records[0]["quoter_verified_candidates"][0]["net_bps"] > 0


def test_keep_alive_https_pool_guards(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "oasis_agent.fastpath.live_shadow_runner.socket.getaddrinfo",
        lambda *_a, **_k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
    )
    monkeypatch.setattr(
        "oasis_agent.cross_venue.live_cex_dex_shadow.socket.getaddrinfo",
        lambda *_a, **_k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
    )

    events: list[str] = []
    outcomes: list[tuple[int, bytes] | tuple[int, bytes, bool] | Exception] = []

    class _FakeResp:
        def __init__(self, status: int, body: bytes, *, will_close: bool = False) -> None:
            self.status = status
            self._body = body
            self.will_close = will_close

        def read(self, _limit: int) -> bytes:
            return self._body

    class _FakeConn:
        def __init__(self, host: str, port: int, timeout: float, context: object) -> None:
            del host, port, context
            self.timeout = timeout
            self.sock = None
            events.append("new_conn")

        def request(self, method: str, path: str, body: Any = None, headers: Any = None) -> None:
            del body, headers
            events.append(f"req:{method}:{path}")
            nxt = outcomes.pop(0)
            if isinstance(nxt, Exception):
                raise nxt
            will_close = nxt[2] if len(nxt) == 3 else False
            self._next_resp = _FakeResp(nxt[0], nxt[1], will_close=bool(will_close))

        def getresponse(self) -> _FakeResp:
            return self._next_resp

        def close(self) -> None:
            events.append("close")

    pool = _KeepAliveHttpsPool(connection_factory=_FakeConn)  # type: ignore[arg-type]

    # 1. Persistent reuse + single reconnect ONLY on idle RemoteDisconnected
    outcomes.extend(
        [
            (200, b'{"jsonrpc":"2.0","id":1,"result":"0x1"}'),
            http.client.RemoteDisconnected("idle close"),
            (200, b'{"jsonrpc":"2.0","id":1,"result":"0x2"}'),
        ]
    )
    r1 = pool.post("https://rpc.example.com", b"{}", 5.0)
    r2 = pool.post("https://rpc.example.com", b"{}", 5.0)
    assert r1.endswith(b'"0x1"}')
    assert r2.endswith(b'"0x2"}')
    assert events.count("new_conn") == 2

    # 2. HTTP 429 stops cleanly after ONE call (no retry) and drops socket
    outcomes.append((429, b"rate limit"))
    with pytest.raises(RpcError, match="status 429"):
        pool.post("https://rpc.example.com", b"{}", 5.0)
    assert events[-1] == "close"

    # 3. Redirect 302 forbidden for Coinbase GET
    outcomes.append((302, b"redirect"))
    with pytest.raises(LiveCexDexError, match="redirects"):
        pool.get("https://api.exchange.coinbase.com/products/ETH-USD/book?level=2", 5.0)

    # 4. Fresh-socket transport failure must NOT retry
    events.clear()
    outcomes.append(TimeoutError("connect stall"))
    with pytest.raises(RpcError, match="transport failure"):
        pool.post("https://rpc.example.com", b"{}", 5.0)
    assert events.count("new_conn") == 1

    # 5. Reused-socket ssl.SSLEOFError and BadStatusLine reconnect once; fresh-socket fails closed
    events.clear()
    outcomes.extend(
        [
            (200, b'{"jsonrpc":"2.0","id":1,"result":"0xa"}'),
            ssl.SSLEOFError("tls idle eof"),
            (200, b'{"jsonrpc":"2.0","id":1,"result":"0xb"}'),
            http.client.BadStatusLine(""),
            (200, b'{"jsonrpc":"2.0","id":1,"result":"0xc"}'),
        ]
    )
    assert pool.post("https://rpc.example.com", b"{}", 5.0).endswith(b'"0xa"}')
    assert pool.post("https://rpc.example.com", b"{}", 5.0).endswith(b'"0xb"}')
    assert pool.post("https://rpc.example.com", b"{}", 5.0).endswith(b'"0xc"}')
    assert events.count("new_conn") == 3
    pool.close()

    events.clear()
    outcomes.append(ssl.SSLEOFError("fresh tls failure"))
    with pytest.raises(RpcError, match="transport failure"):
        pool.post("https://rpc.example.com", b"{}", 5.0)
    assert events.count("new_conn") == 1

    # 6. Proactive socket rotation after max_requests_per_conn=2 and on response will_close=True
    rot_pool = _KeepAliveHttpsPool(
        connection_factory=_FakeConn,  # type: ignore[arg-type]
        max_requests_per_conn=2,
    )
    with pytest.raises(ValueError, match="max_requests_per_conn"):
        _KeepAliveHttpsPool(max_requests_per_conn=0)
    events.clear()
    outcomes.extend(
        [
            (200, b'{"jsonrpc":"2.0","id":1,"result":"0x1"}'),
            (200, b'{"jsonrpc":"2.0","id":1,"result":"0x2"}'),
            (200, b'{"jsonrpc":"2.0","id":1,"result":"0x3"}', True),
            (200, b'{"jsonrpc":"2.0","id":1,"result":"0x4"}'),
        ]
    )
    assert rot_pool.post("https://rpc.example.com", b"{}", 5.0).endswith(b'"0x1"}')
    assert rot_pool.post("https://rpc.example.com", b"{}", 5.0).endswith(b'"0x2"}')
    assert events.count("new_conn") == 1
    assert events[-1] == "close"  # dropped cleanly after 2nd request
    assert rot_pool.post("https://rpc.example.com", b"{}", 5.0).endswith(b'"0x3"}')
    assert events.count("new_conn") == 2
    assert events[-1] == "close"  # dropped immediately due to will_close=True
    assert rot_pool.post("https://rpc.example.com", b"{}", 5.0).endswith(b'"0x4"}')
    assert events.count("new_conn") == 3
    rot_pool.close()

    # 7. Coinbase healthy GET then clean close()
    outcomes.append((200, b'{"bids":[["3000","1"]],"asks":[["3001","1"]]}'))
    cb_raw = pool.get("https://api.exchange.coinbase.com/products/ETH-USD/book?level=2", 5.0)
    assert b"3000" in cb_raw
    pool.close()
    assert events[-1] == "close"


def test_campaign_replica_lag_recovery_and_true_reorg_rejection(
    tmp_path: Path, bridge: FastpathBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    weth_addr = normalize_address(WETH)
    usdc_addr = normalize_address(USDC)
    disc_pools = (
        DiscoveredPool(
            pool_id=_synth_addr(901),
            label="v3_weth_usdc_500",
            venue="uniswap_v3",
            kind=PoolKind.V3,
            token0=weth_addr,
            token1=usdc_addr,
            decimals0=18,
            decimals1=6,
            fixed_fee_pips=500,
            tick_spacing=None,
            factory=UNIV3_FACTORY,
            reserve0=50 * 10**18,
            reserve1=150_000 * 10**6,
        ),
    )

    def _make_snap(bnum: int, bhash: str, phash: str) -> PinnedUniverseSnapshot:
        ts_ms = 1_795_300_000_000 + bnum * 1000
        liq = 200_000_000_000_000_000
        t = -196_258
        node = PoolNode(
            chain_id=BASE_CHAIN_ID,
            pool_id=disc_pools[0].pool_id,
            kind=PoolKind.V3,
            token0=weth_addr,
            token1=usdc_addr,
            decimals0=18,
            decimals1=6,
            fee_pips=500,
            sqrt_price_x96=sqrt_price_at_tick(t),
            tick=t,
            liquidity=liq,
            ticks=(TickLiquidity(t - 200, liq), TickLiquidity(t + 200, -liq)),
            block_number=bnum,
            timestamp_ms=ts_ms,
        )
        return PinnedUniverseSnapshot(
            chain_id=BASE_CHAIN_ID,
            block_number=bnum,
            block_hash=bhash,
            parent_hash=phash,
            header_timestamp_ms=ts_ms,
            base_time_ms=ts_ms,
            base_fee_per_gas_wei=10_000_000,
            l1_fee_wei=1_000_000_000_000,
            pools=disc_pools,
            pool_nodes=(node,),
            balances_by_pool={disc_pools[0].pool_id: (50 * 10**18, 150_000 * 10**6)},
            rpc_calls_used=2,
            execution_eligible=False,
        )

    h99 = _synth_hash("blk-99")
    h100 = _synth_hash("blk-100")
    h100_fork = _synth_hash("blk-100-reorg")
    h101_bad_parent = _synth_hash("blk-101-bad-parent")

    eth_book = build_synthetic_coinbase_l2_book("ETH-USD", mid_price=Decimal("3000.00"))
    cb_bytes = serialize_coinbase_book_payload(eth_book)
    monkeypatch.setattr(
        "oasis_agent.cross_venue.live_cex_dex_shadow._default_https_get",
        lambda _url, _timeout: cb_bytes,
    )
    monkeypatch.setattr(
        "oasis_agent.fastpath.multicall_universe.discover_base_universe_pools",
        lambda *_a, **_k: disc_pools,
    )

    # Scenario A: 1-block replica lag recovers via hex(prev_block_number) re-fetch
    tags_seen: list[str] = []

    def _lag_snap(*_args: Any, **kwargs: Any) -> PinnedUniverseSnapshot:
        tag = str(kwargs.get("block_tag", "latest"))
        tags_seen.append(tag)
        if len(tags_seen) == 1:
            return _make_snap(59_000_100, h100, h99)
        if len(tags_seen) == 2:
            return _make_snap(59_000_099, h99, _synth_hash("blk-98"))
        return _make_snap(59_000_100, h100, h99)

    monkeypatch.setattr(
        "oasis_agent.fastpath.multicall_universe.fetch_pinned_universe_snapshot",
        _lag_snap,
    )
    res_lag = run_multicall_campaign(
        out_dir=tmp_path / "replica-lag-ok",
        mode="public-shadow",
        ticks=2,
        interval_ms=0,
        max_rpc_calls=30,
        rpc_url="https://lag-test.example.com",
        bridge=bridge,
        rpc_transport=lambda _u, _p, _t: b"{}",
        sleeper=_noop_sleep,
    )
    assert res_lag.summary["ticks_completed"] == 2
    assert tags_seen == ["latest", "latest", hex(59_000_100)]
    assert res_lag.summary["rpc_calls_used"] == 6  # tick1=2, tick2=2(latest)+2(refetch)

    # Scenario B: True reorg (parent_hash mismatch at block+1) fails closed
    call_idx_b = {"n": 0}

    def _reorg_parent_snap(*_args: Any, **_kwargs: Any) -> PinnedUniverseSnapshot:
        call_idx_b["n"] += 1
        if call_idx_b["n"] == 1:
            return _make_snap(59_000_100, h100, h99)
        return _make_snap(59_000_101, h101_bad_parent, h100_fork)

    monkeypatch.setattr(
        "oasis_agent.fastpath.multicall_universe.fetch_pinned_universe_snapshot",
        _reorg_parent_snap,
    )
    with pytest.raises(ShadowRunnerError, match="parent_hash mismatch"):
        run_multicall_campaign(
            out_dir=tmp_path / "reorg-parent-fail",
            mode="public-shadow",
            ticks=2,
            interval_ms=0,
            max_rpc_calls=30,
            rpc_url="https://reorg-test.example.com",
            bridge=bridge,
            rpc_transport=lambda _u, _p, _t: b"{}",
            sleeper=_noop_sleep,
        )

    # Scenario C: Conflicting block hash at same height fails closed
    call_idx_c = {"n": 0}

    def _reorg_same_snap(*_args: Any, **_kwargs: Any) -> PinnedUniverseSnapshot:
        call_idx_c["n"] += 1
        if call_idx_c["n"] == 1:
            return _make_snap(59_000_100, h100, h99)
        return _make_snap(59_000_100, h100_fork, h99)

    monkeypatch.setattr(
        "oasis_agent.fastpath.multicall_universe.fetch_pinned_universe_snapshot",
        _reorg_same_snap,
    )
    with pytest.raises(ShadowRunnerError, match="block hash changed"):
        run_multicall_campaign(
            out_dir=tmp_path / "reorg-hash-fail",
            mode="public-shadow",
            ticks=2,
            interval_ms=0,
            max_rpc_calls=30,
            rpc_url="https://reorg-hash-test.example.com",
            bridge=bridge,
            rpc_transport=lambda _u, _p, _t: b"{}",
            sleeper=_noop_sleep,
        )


def test_campaign_ecosystem_2hop_coinbase_fallback_and_maker_hedge(
    tmp_path: Path, bridge: FastpathBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    weth_addr = normalize_address(WETH)
    usdc_addr = normalize_address(USDC)
    cbbtc_addr = normalize_address(CBBTC)
    virt_addr = normalize_address(VIRTUAL_TOKEN)

    def _ordered_pair(a: str, dec_a: int, res_a: int, b: str, dec_b: int, res_b: int):
        if a < b:
            return a, b, dec_a, dec_b, res_a, res_b
        return b, a, dec_b, dec_a, res_b, res_a

    # WETH/USDC V3 pool (~3000 USDC/WETH), VIRTUAL/WETH V2 pool (~2000 VIRTUAL/WETH -> ~$1.50),
    # and cbBTC/USDC V2 pool (~90,000 USDC/cbBTC); intentionally omit direct VIRTUAL/USDC pool.
    vw_t0, vw_t1, vw_d0, vw_d1, vw_r0, vw_r1 = _ordered_pair(
        virt_addr, 18, 2_000_000 * 10**18, weth_addr, 18, 1_000 * 10**18
    )
    bc_t0, bc_t1, bc_d0, bc_d1, bc_r0, bc_r1 = _ordered_pair(
        cbbtc_addr, 8, 100 * 10**8, usdc_addr, 6, 9_000_000 * 10**6
    )

    disc_pools = (
        DiscoveredPool(
            pool_id=_synth_addr(911),
            label="v3_weth_usdc_500",
            venue="uniswap_v3",
            kind=PoolKind.V3,
            token0=weth_addr,
            token1=usdc_addr,
            decimals0=18,
            decimals1=6,
            fixed_fee_pips=500,
            tick_spacing=None,
            factory=UNIV3_FACTORY,
            reserve0=50 * 10**18,
            reserve1=150_000 * 10**6,
        ),
        DiscoveredPool(
            pool_id=_synth_addr(912),
            label="v2_virtual_weth",
            venue="aerodrome_v2",
            kind=PoolKind.V2,
            token0=vw_t0,
            token1=vw_t1,
            decimals0=vw_d0,
            decimals1=vw_d1,
            fixed_fee_pips=3000,
            tick_spacing=None,
            factory=_synth_addr(9912),
            reserve0=vw_r0,
            reserve1=vw_r1,
        ),
        DiscoveredPool(
            pool_id=_synth_addr(913),
            label="v2_cbbtc_usdc",
            venue="aerodrome_v2",
            kind=PoolKind.V2,
            token0=bc_t0,
            token1=bc_t1,
            decimals0=bc_d0,
            decimals1=bc_d1,
            fixed_fee_pips=3000,
            tick_spacing=None,
            factory=_synth_addr(9913),
            reserve0=bc_r0,
            reserve1=bc_r1,
        ),
    )

    include_flags_seen: list[bool] = []

    def _mock_discover(*_args: Any, **kwargs: Any) -> tuple[DiscoveredPool, ...]:
        include_flags_seen.append(bool(kwargs.get("include_ecosystem_tokens", False)))
        return disc_pools

    snap_idx = {"n": 0}

    def _mock_snap(*_args: Any, **_kwargs: Any) -> PinnedUniverseSnapshot:
        snap_idx["n"] += 1
        bnum = 59_100_000 + snap_idx["n"]
        ts_ms = 1_795_400_000_000 + snap_idx["n"] * 1000
        liq = 200_000_000_000_000_000
        t = -196_258
        nodes = (
            PoolNode(
                chain_id=BASE_CHAIN_ID,
                pool_id=disc_pools[0].pool_id,
                kind=PoolKind.V3,
                token0=weth_addr,
                token1=usdc_addr,
                decimals0=18,
                decimals1=6,
                fee_pips=500,
                sqrt_price_x96=sqrt_price_at_tick(t),
                tick=t,
                liquidity=liq,
                ticks=(TickLiquidity(t - 200, liq), TickLiquidity(t + 200, -liq)),
                block_number=bnum,
                timestamp_ms=ts_ms,
            ),
            PoolNode(
                chain_id=BASE_CHAIN_ID,
                pool_id=disc_pools[1].pool_id,
                kind=PoolKind.V2,
                token0=vw_t0,
                token1=vw_t1,
                decimals0=vw_d0,
                decimals1=vw_d1,
                fee_pips=3000,
                reserve0=vw_r0,
                reserve1=vw_r1,
                block_number=bnum,
                timestamp_ms=ts_ms,
            ),
            PoolNode(
                chain_id=BASE_CHAIN_ID,
                pool_id=disc_pools[2].pool_id,
                kind=PoolKind.V2,
                token0=bc_t0,
                token1=bc_t1,
                decimals0=bc_d0,
                decimals1=bc_d1,
                fee_pips=3000,
                reserve0=bc_r0,
                reserve1=bc_r1,
                block_number=bnum,
                timestamp_ms=ts_ms,
            ),
        )
        bhash = _synth_hash(f"eco-blk-{bnum}")
        phash = _synth_hash(f"eco-blk-{bnum - 1}")
        return PinnedUniverseSnapshot(
            chain_id=BASE_CHAIN_ID,
            block_number=bnum,
            block_hash=bhash,
            parent_hash=phash,
            header_timestamp_ms=ts_ms,
            base_time_ms=ts_ms,
            base_fee_per_gas_wei=10_000_000,
            l1_fee_wei=1_000_000_000_000,
            pools=disc_pools,
            pool_nodes=nodes,
            balances_by_pool={
                disc_pools[0].pool_id: (50 * 10**18, 150_000 * 10**6),
                disc_pools[1].pool_id: (vw_r0, vw_r1),
                disc_pools[2].pool_id: (bc_r0, bc_r1),
            },
            rpc_calls_used=2,
            execution_eligible=False,
        )

    monkeypatch.setattr(
        "oasis_agent.fastpath.multicall_universe.discover_base_universe_pools",
        _mock_discover,
    )
    monkeypatch.setattr(
        "oasis_agent.fastpath.multicall_universe.fetch_pinned_universe_snapshot",
        _mock_snap,
    )

    cb_urls_seen: list[str] = []
    virt_calls = {"n": 0}

    def _mock_cb_transport(url: str, _timeout: float) -> bytes:
        cb_urls_seen.append(url)
        if "BTC-USD" in url:
            if "level=2" in url:
                raise LiveCexDexError("Coinbase response exceeded MAX_RESPONSE_BYTES limit")
            btc_book = build_synthetic_coinbase_l2_book(
                "BTC-USD", mid_price=Decimal("90000.00"), level_size=Decimal("1.0"), levels=1
            )
            return serialize_coinbase_book_payload(btc_book)
        if "VIRTUAL-USD" in url:
            virt_calls["n"] += 1
            # Tick 1 mid=1.5600: DEX buys VIRTUAL ~$1.540 so Maker Ask ~$1.5597 has >100 bps edge;
            # Tick 2 jumps best_bid above $1.59 so Tick 1 resting Ask hits a strict_cross fill.
            mid = Decimal("1.5600") if virt_calls["n"] == 1 else Decimal("1.6000")
            virt_book = build_synthetic_coinbase_l2_book(
                "VIRTUAL-USD", mid_price=mid, level_size=Decimal("5000.0")
            )
            return serialize_coinbase_book_payload(virt_book)
        eth_book = build_synthetic_coinbase_l2_book("ETH-USD", mid_price=Decimal("3000.00"))
        return serialize_coinbase_book_payload(eth_book)

    out_dir = tmp_path / "eco-2hop"
    res = run_multicall_campaign(
        out_dir=out_dir,
        mode="public-shadow",
        ticks=2,
        interval_ms=0,
        max_rpc_calls=30,
        rpc_url="https://eco-test.example.com",
        bridge=bridge,
        rpc_transport=lambda _u, _p, _t: b"{}",
        coinbase_transport=_mock_cb_transport,
        sleeper=_noop_sleep,
        include_ecosystem_tokens=True,
    )
    assert include_flags_seen == [True]
    assert res.summary["include_ecosystem_tokens"] is True
    assert set(res.summary["cex_dex_lead_lag_summary"]["products_evaluated"]) == {
        "BTC-USD",
        "ETH-USD",
        "VIRTUAL-USD",
    }
    assert res.records[0]["cex_dex_evaluations"]["BTC-USD"]["book_level"] == 1
    assert res.records[0]["cex_dex_evaluations"]["VIRTUAL-USD"]["status"] == "ok"
    assert res.summary["maker_hedge_summary"]["strict_cross_fills"] >= 1
    assert len(res.records[1]["maker_hedge"]["VIRTUAL-USD"]["strict_fills"]) >= 1
    assert (
        cb_urls_seen.count("https://api.exchange.coinbase.com/products/ETH-USD/book?level=2") == 2
    )
    assert any("BTC-USD/book?level=1" in u for u in cb_urls_seen)

    # Verify CLI --include-ecosystem-tokens flag forwards properly
    cli_out = tmp_path / "cli-eco"
    monkeypatch.setattr(
        "oasis_agent.cross_venue.live_cex_dex_shadow._default_https_get",
        _mock_cb_transport,
    )
    # Force coinbase_transport in CLI test by hooking run_multicall_campaign via monkeypatch
    captured_cli_kw: dict[str, Any] = {}
    orig_run = campaign_cli.run_multicall_campaign

    def _spy_run(**kwargs: Any) -> Any:
        captured_cli_kw.update(kwargs)
        return orig_run(
            **{
                **kwargs,
                "bridge": bridge,
                "rpc_transport": lambda _u, _p, _t: b"{}",
                "coinbase_transport": _mock_cb_transport,
                "sleeper": _noop_sleep,
            }
        )

    monkeypatch.setattr(campaign_cli, "run_multicall_campaign", _spy_run)
    rc = campaign_cli.main(
        [
            "--mode",
            "public-shadow",
            "--ticks",
            "2",
            "--interval-ms",
            "0",
            "--max-rpc-calls",
            "30",
            "--rpc-url",
            "https://eco-cli.example.com",
            "--include-ecosystem-tokens",
            "--out",
            str(cli_out),
        ]
    )
    assert rc == 0
    assert captured_cli_kw["include_ecosystem_tokens"] is True


def test_campaign_dynamic_sizing_risk_headroom_and_ws_fifo_fills(
    tmp_path: Path, bridge: FastpathBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    weth_addr = normalize_address(WETH)
    usdc_addr = normalize_address(USDC)
    cbbtc_addr = normalize_address(CBBTC)
    bc_t0, bc_t1, bc_d0, bc_d1, bc_r0, bc_r1 = (
        (cbbtc_addr, usdc_addr, 8, 6, 100 * 10**8, 9_000_000 * 10**6)
        if cbbtc_addr < usdc_addr
        else (usdc_addr, cbbtc_addr, 6, 8, 9_000_000 * 10**6, 100 * 10**8)
    )
    disc_pools = (
        DiscoveredPool(
            pool_id=_synth_addr(941),
            label="v3_weth_usdc_500",
            venue="uniswap_v3",
            kind=PoolKind.V3,
            token0=weth_addr,
            token1=usdc_addr,
            decimals0=18,
            decimals1=6,
            fixed_fee_pips=500,
            tick_spacing=None,
            factory=UNIV3_FACTORY,
            reserve0=500 * 10**18,
            reserve1=1_500_000 * 10**6,
        ),
        DiscoveredPool(
            pool_id=_synth_addr(942),
            label="v2_cbbtc_usdc",
            venue="aerodrome_v2",
            kind=PoolKind.V2,
            token0=bc_t0,
            token1=bc_t1,
            decimals0=bc_d0,
            decimals1=bc_d1,
            fixed_fee_pips=3000,
            tick_spacing=None,
            factory=_synth_addr(9942),
            reserve0=bc_r0,
            reserve1=bc_r1,
        ),
    )

    snap_idx = {"n": 0}

    def _mock_snap(*_args: Any, **_kwargs: Any) -> PinnedUniverseSnapshot:
        snap_idx["n"] += 1
        bnum = 59_200_000 + snap_idx["n"]
        ts_ms = 1_795_500_000_000 + snap_idx["n"] * 1000
        liq = 2_000_000_000_000_000_000
        t = -196_258  # ~3068 USDC/WETH in V3
        nodes = (
            PoolNode(
                chain_id=BASE_CHAIN_ID,
                pool_id=disc_pools[0].pool_id,
                kind=PoolKind.V3,
                token0=weth_addr,
                token1=usdc_addr,
                decimals0=18,
                decimals1=6,
                fee_pips=500,
                sqrt_price_x96=sqrt_price_at_tick(t),
                tick=t,
                liquidity=liq,
                ticks=(TickLiquidity(t - 500, liq), TickLiquidity(t + 500, -liq)),
                block_number=bnum,
                timestamp_ms=ts_ms,
            ),
            PoolNode(
                chain_id=BASE_CHAIN_ID,
                pool_id=disc_pools[1].pool_id,
                kind=PoolKind.V2,
                token0=bc_t0,
                token1=bc_t1,
                decimals0=bc_d0,
                decimals1=bc_d1,
                fee_pips=3000,
                reserve0=bc_r0,
                reserve1=bc_r1,
                block_number=bnum,
                timestamp_ms=ts_ms,
            ),
        )
        return PinnedUniverseSnapshot(
            chain_id=BASE_CHAIN_ID,
            block_number=bnum,
            block_hash=_synth_hash(f"dyn-blk-{bnum}"),
            parent_hash=_synth_hash(f"dyn-blk-{bnum - 1}"),
            header_timestamp_ms=ts_ms,
            base_time_ms=ts_ms,
            base_fee_per_gas_wei=10_000_000,
            l1_fee_wei=1_000_000_000_000,
            pools=disc_pools,
            pool_nodes=nodes,
            balances_by_pool={
                disc_pools[0].pool_id: (500 * 10**18, 1_500_000 * 10**6),
                disc_pools[1].pool_id: (bc_r0, bc_r1),
            },
            rpc_calls_used=2,
            execution_eligible=False,
        )

    monkeypatch.setattr(
        "oasis_agent.fastpath.multicall_universe.discover_base_universe_pools",
        lambda *_a, **_k: disc_pools,
    )
    monkeypatch.setattr(
        "oasis_agent.fastpath.multicall_universe.fetch_pinned_universe_snapshot",
        _mock_snap,
    )

    def _mock_cb_transport(url: str, _timeout: float) -> bytes:
        if "BTC-USD" in url:
            if "level=2" in url:
                raise LiveCexDexError("Coinbase response exceeded MAX_RESPONSE_BYTES limit")
            # L1 top-of-book depth is ONLY 0.002 BTC * 90000 = $180 (< old hardcoded $1000!)
            btc_book = build_synthetic_coinbase_l2_book(
                "BTC-USD", mid_price=Decimal("90000.00"), level_size=Decimal("0.002"), levels=1
            )
            return serialize_coinbase_book_payload(btc_book)
        # ETH-USD quotes mid $3088 (~+60 bps above DEX ~$3070), deep $30k/lvl -> Maker Ask active!
        eth_book = build_synthetic_coinbase_l2_book(
            "ETH-USD", mid_price=Decimal("3088.00"), level_size=Decimal("10.0"), levels=5
        )
        return serialize_coinbase_book_payload(eth_book)

    class _FakeWsFeed:
        def __init__(self, product_ids: Any) -> None:
            self.product_ids = tuple(product_ids)
            self._tick = 0
            self.closed = False

        def drain_events(self) -> tuple[tuple[CoinbaseTradePrint, ...], bool]:
            self._tick += 1
            if self._tick == 1:
                # Seed print on tick 0 before tick 0 quote rests
                return (
                    (
                        CoinbaseTradePrint(
                            product_id="ETH-USD",
                            trade_id=100,
                            price=Decimal("3088.00"),
                            size=Decimal("0.1"),
                            maker_side="sell",
                            traded_at_utc="2026-11-18T11:30:00Z",
                        ),
                    ),
                    False,
                )
            # Tick 2 print clears the resting queue and fills our active ETH-USD Ask!
            return (
                (
                    CoinbaseTradePrint(
                        product_id="ETH-USD",
                        trade_id=101,
                        price=Decimal("3092.00"),
                        size=Decimal("2.0"),
                        maker_side="sell",
                        traded_at_utc="2026-11-18T11:30:01Z",
                    ),
                ),
                False,
            )

        def close(self) -> None:
            self.closed = True

    ws_created: list[_FakeWsFeed] = []

    def _ws_factory(products: Any) -> Any:
        feed = _FakeWsFeed(products)
        ws_created.append(feed)
        return feed

    # 1. Default $500/$200 config -> headroom max_clip = $225 (`225_000_000`)
    out_500 = tmp_path / "dyn-500"
    res_500 = run_multicall_campaign(
        out_dir=out_500,
        mode="public-shadow",
        ticks=2,
        interval_ms=0,
        max_rpc_calls=30,
        rpc_url="https://dyn-test.example.com",
        bridge=bridge,
        rpc_transport=lambda _u, _p, _t: b"{}",
        coinbase_transport=_mock_cb_transport,
        sleeper=_noop_sleep,
        risk_config_path=REPO_ROOT / "configs" / "profit-engine-risk.example.json",
        dynamic_sizing=True,
        enable_coinbase_ws=True,
        ws_feed_factory=_ws_factory,
    )
    assert res_500.summary["execution_eligible"] is False
    assert res_500.summary["dynamic_sizing_enabled"] is True
    assert res_500.summary["coinbase_ws_enabled"] is True
    assert res_500.summary["headroom_summary"]["max_max_clip_usd_micros"] == 225_000_000
    # BTC-USD L1 has only $180 capacity -> dynamic sizer auto-scales down to <= $180 and succeeds!
    btc_eval = res_500.records[0]["cex_dex_evaluations"]["BTC-USD"]
    assert btc_eval["status"] == "ok"
    assert btc_eval["book_level"] == 1
    assert btc_eval["selected_notional_usdc"] <= 180_000_000
    # ETH-USD WS FIFO print triggers trade_flow_fifo fill on Tick 2!
    assert res_500.summary["coinbase_trade_flow_summary"]["ws_total_prints_received"] == 2
    assert res_500.summary["maker_hedge_summary"]["trade_flow_fifo_fills"] >= 1
    assert ws_created[0].closed is True

    # 2. Scaled $5,000/$750 override -> Maker expands above $500 on deep ETH while BTC <= $180
    out_5000 = tmp_path / "dyn-5000"
    res_5000 = run_multicall_campaign(
        out_dir=out_5000,
        mode="public-shadow",
        ticks=2,
        interval_ms=0,
        max_rpc_calls=30,
        rpc_url="https://dyn-test-5000.example.com",
        bridge=bridge,
        rpc_transport=lambda _u, _p, _t: b"{}",
        coinbase_transport=_mock_cb_transport,
        sleeper=_noop_sleep,
        total_capital_usd=5000,
        max_daily_loss_usd=750,
        dynamic_sizing=True,
    )
    assert res_5000.summary["headroom_summary"]["total_capital_usd_micros"] == 5_000_000_000
    assert res_5000.summary["headroom_summary"]["max_max_clip_usd_micros"] == 2_250_000_000
    assert (
        res_5000.records[0]["cex_dex_evaluations"]["BTC-USD"]["selected_notional_usdc"]
        <= 180_000_000
    )
    assert res_5000.records[0]["maker_hedge"]["ETH-USD"]["selected_notional_usdc"] > 500_000_000

    # 3. Verify new CLI flags forward cleanly
    captured_cli_kw: dict[str, Any] = {}
    orig_run = campaign_cli.run_multicall_campaign

    def _spy_run(**kwargs: Any) -> Any:
        captured_cli_kw.update(kwargs)
        return orig_run(
            **{
                **kwargs,
                "bridge": bridge,
                "rpc_transport": lambda _u, _p, _t: b"{}",
                "coinbase_transport": _mock_cb_transport,
                "ws_feed_factory": _ws_factory,
                "sleeper": _noop_sleep,
            }
        )

    monkeypatch.setattr(campaign_cli, "run_multicall_campaign", _spy_run)
    rc = campaign_cli.main(
        [
            "--mode",
            "public-shadow",
            "--ticks",
            "2",
            "--interval-ms",
            "0",
            "--max-rpc-calls",
            "30",
            "--rpc-url",
            "https://dyn-cli.example.com",
            "--risk-config",
            str(REPO_ROOT / "configs" / "profit-engine-risk.example.json"),
            "--total-capital-usd",
            "5000",
            "--max-daily-loss-usd",
            "750",
            "--dynamic-sizing",
            "--enable-coinbase-ws",
            "--out",
            str(tmp_path / "cli-dyn"),
        ]
    )
    assert rc == 0
    assert captured_cli_kw["total_capital_usd"] == 5000
    assert captured_cli_kw["max_daily_loss_usd"] == 750
    assert captured_cli_kw["dynamic_sizing"] is True
    assert captured_cli_kw["enable_coinbase_ws"] is True


def test_campaign_wave14_adaptive_maker_batch_oos_persistence_and_gate5(
    tmp_path: Path, bridge: FastpathBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify Wave 14 walk-forward OOS tracker, batch flush, adaptive vol, and Gate 5."""
    weth_addr = normalize_address(WETH)
    usdc_addr = normalize_address(USDC)
    cbbtc_addr = normalize_address(CBBTC)
    bc_t0, bc_t1, bc_d0, bc_d1, bc_r0, bc_r1 = (
        (cbbtc_addr, usdc_addr, 8, 6, 100 * 10**8, 9_000_000 * 10**6)
        if cbbtc_addr < usdc_addr
        else (usdc_addr, cbbtc_addr, 6, 8, 9_000_000 * 10**6, 100 * 10**8)
    )
    disc_pools = (
        DiscoveredPool(
            pool_id=_synth_addr(951),
            label="v3_weth_usdc_500",
            venue="uniswap_v3",
            kind=PoolKind.V3,
            token0=weth_addr,
            token1=usdc_addr,
            decimals0=18,
            decimals1=6,
            fixed_fee_pips=500,
            tick_spacing=None,
            factory=UNIV3_FACTORY,
            reserve0=500 * 10**18,
            reserve1=1_500_000 * 10**6,
        ),
        DiscoveredPool(
            pool_id=_synth_addr(952),
            label="v2_cbbtc_usdc",
            venue="aerodrome_v2",
            kind=PoolKind.V2,
            token0=bc_t0,
            token1=bc_t1,
            decimals0=bc_d0,
            decimals1=bc_d1,
            fixed_fee_pips=3000,
            tick_spacing=None,
            factory=_synth_addr(9952),
            reserve0=bc_r0,
            reserve1=bc_r1,
        ),
    )

    snap_idx = {"n": 0}

    def _mock_snap(*_args: Any, **_kwargs: Any) -> PinnedUniverseSnapshot:
        snap_idx["n"] += 1
        bnum = 59_300_000 + snap_idx["n"]
        ts_ms = 1_795_600_000_000 + snap_idx["n"] * 1000
        liq = 2_000_000_000_000_000_000
        t = -196_258
        nodes = (
            PoolNode(
                chain_id=BASE_CHAIN_ID,
                pool_id=disc_pools[0].pool_id,
                kind=PoolKind.V3,
                token0=weth_addr,
                token1=usdc_addr,
                decimals0=18,
                decimals1=6,
                fee_pips=500,
                sqrt_price_x96=sqrt_price_at_tick(t),
                tick=t,
                liquidity=liq,
                ticks=(TickLiquidity(t - 500, liq), TickLiquidity(t + 500, -liq)),
                block_number=bnum,
                timestamp_ms=ts_ms,
            ),
            PoolNode(
                chain_id=BASE_CHAIN_ID,
                pool_id=disc_pools[1].pool_id,
                kind=PoolKind.V2,
                token0=bc_t0,
                token1=bc_t1,
                decimals0=bc_d0,
                decimals1=bc_d1,
                fee_pips=3000,
                reserve0=bc_r0,
                reserve1=bc_r1,
                block_number=bnum,
                timestamp_ms=ts_ms,
            ),
        )
        return PinnedUniverseSnapshot(
            chain_id=BASE_CHAIN_ID,
            block_number=bnum,
            block_hash=_synth_hash(f"w14-blk-{bnum}"),
            parent_hash=_synth_hash(f"w14-blk-{bnum - 1}"),
            header_timestamp_ms=ts_ms,
            base_time_ms=ts_ms,
            base_fee_per_gas_wei=10_000_000,
            l1_fee_wei=1_000_000_000_000,
            pools=disc_pools,
            pool_nodes=nodes,
            balances_by_pool={
                disc_pools[0].pool_id: (500 * 10**18, 1_500_000 * 10**6),
                disc_pools[1].pool_id: (bc_r0, bc_r1),
            },
            rpc_calls_used=2,
            execution_eligible=False,
        )

    monkeypatch.setattr(
        "oasis_agent.fastpath.multicall_universe.discover_base_universe_pools",
        lambda *_a, **_k: disc_pools,
    )
    monkeypatch.setattr(
        "oasis_agent.fastpath.multicall_universe.fetch_pinned_universe_snapshot",
        _mock_snap,
    )

    def _mock_cb_transport(url: str, _timeout: float) -> bytes:
        if "BTC-USD" in url:
            # Tick 1 spikes BTC +170 bps (1-tick flicker rejected!); Ticks 2..8 collapse to $89900
            btc_mid = Decimal("91500.00") if snap_idx["n"] == 1 else Decimal("89900.00")
            return serialize_coinbase_book_payload(
                build_synthetic_coinbase_l2_book(
                    "BTC-USD", mid_price=btc_mid, level_size=Decimal("0.01"), levels=3
                )
            )
        # Sustained +60 bps ETH mid above DEX so Ticks 2..7 queue & settle persistent OOS trades;
        # level_size=0.5 gives >$1500 top-3 depth while 0.502 ETH prints leave 0.002 partial.
        return serialize_coinbase_book_payload(
            build_synthetic_coinbase_l2_book(
                "ETH-USD", mid_price=Decimal("3090.00"), level_size=Decimal("0.5"), levels=5
            )
        )

    class _PartialFillWsFeed:
        def __init__(self, product_ids: Any) -> None:
            self.product_ids = tuple(product_ids)
            self._tick = 0
            self.closed = False

        def drain_events(self) -> tuple[tuple[CoinbaseTradePrint, ...], bool]:
            self._tick += 1
            if self._tick == 1:
                return (
                    (
                        CoinbaseTradePrint(
                            product_id="ETH-USD",
                            trade_id=200,
                            price=Decimal("3089.50"),
                            size=Decimal("0.05"),
                            maker_side="sell",
                            traded_at_utc="2026-11-18T12:00:00Z",
                        ),
                    ),
                    False,
                )
            if self._tick == 2:
                # 0.5 queue_ahead + 0.002 ETH (~$6.18 < $10 min_hedge_notional_usdc) batches!
                return (
                    (
                        CoinbaseTradePrint(
                            product_id="ETH-USD",
                            trade_id=201,
                            price=Decimal("3090.50"),
                            size=Decimal("0.502"),
                            maker_side="sell",
                            traded_at_utc="2026-11-18T12:00:01Z",
                        ),
                    ),
                    False,
                )
            if self._tick == 3:
                # Another 0.002 ETH (~$6.18); combined ~$12.36 >= $10 triggers batched FIFO flush!
                return (
                    (
                        CoinbaseTradePrint(
                            product_id="ETH-USD",
                            trade_id=202,
                            price=Decimal("3090.50"),
                            size=Decimal("0.502"),
                            maker_side="sell",
                            traded_at_utc="2026-11-18T12:00:02Z",
                        ),
                    ),
                    False,
                )
            return ((), False)

        def close(self) -> None:
            self.closed = True

    out_dir = tmp_path / "wave14-walkforward"
    res = run_multicall_campaign(
        out_dir=out_dir,
        mode="public-shadow",
        ticks=8,
        interval_ms=0,
        max_rpc_calls=60,
        rpc_url="https://w14-oos.example.com",
        bridge=bridge,
        rpc_transport=lambda _u, _p, _t: b"{}",
        coinbase_transport=_mock_cb_transport,
        sleeper=_noop_sleep,
        risk_config_path=REPO_ROOT / "configs" / "profit-engine-risk.example.json",
        dynamic_sizing=True,
        enable_coinbase_ws=True,
        ws_feed_factory=_PartialFillWsFeed,
    )

    maker_summary = res.summary["maker_hedge_summary"]
    oos_summary = res.summary["oos_execution_summary"]
    assert res.summary["execution_eligible"] is False
    assert maker_summary["adaptive_volatility_enabled"] is True
    assert maker_summary["open_unhedged_lots"] == 0
    assert maker_summary["unhedged_exposure_usd_micros"] == 0
    assert res.summary["unresolved_residuals"] == 0

    assert oos_summary["execution_eligible"] is False
    assert oos_summary["cex_ioc_limit_enabled"] is True
    assert oos_summary["rejected_one_tick_flickers"] >= 1
    assert oos_summary["oos_taker_trades_count"] >= 5
    assert oos_summary["oos_trades_count"] >= 5
    assert type(oos_summary["oos_lcb_net_edge_bps"]) is int
    assert oos_summary["oos_lcb_net_edge_bps"] >= 5
    assert Decimal(oos_summary["oos_total_net_profit_usd"]) > 0
    assert all("oos_step" in rec for rec in res.records)

    readiness = evaluate_profit_engine_readiness(
        shadow_summary_path=out_dir / "shadow_summary.json",
        min_shadow_ticks=8,
        risk_config_path=REPO_ROOT / "configs" / "profit-engine-risk.example.json",
    )
    gate5 = next(g for g in readiness.gates if g.name == "positive_cost_adjusted_lcb_edge")
    assert gate5.passed is True
    assert (
        readiness.evidence["oos_execution"]["oos_trades_count"] == oos_summary["oos_trades_count"]
    )


def test_keepalive_pool_validates_coinbase_trades_fallback_and_rejects_bad_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify `_KeepAliveHttpsPool.get` allows `/trades` fallback while blocking other paths."""
    monkeypatch.setattr(campaign_cli, "_validate_cb_dns", lambda _host: None)
    requested_paths: list[str] = []

    class _FakeResp:
        status = 200

        def read(self, _limit: int) -> bytes:
            return b"[]"

    class _FakeConn:
        def __init__(self, *_a: Any, **_k: Any) -> None:
            self.sock = None
            self.timeout = 5.0

        def request(self, _method: str, path: str, **_k: Any) -> None:
            requested_paths.append(path)

        def getresponse(self) -> _FakeResp:
            return _FakeResp()

        def close(self) -> None:
            pass

    pool = _KeepAliveHttpsPool(connection_factory=_FakeConn)  # type: ignore[arg-type]
    raw = pool.get("https://api.exchange.coinbase.com/products/ETH-USD/trades?limit=25", 5.0)
    assert raw == b"[]"
    assert requested_paths == ["/products/ETH-USD/trades?limit=25"]

    with pytest.raises(LiveCexDexError):
        pool.get("https://api.exchange.coinbase.com/products/ETH-USD/candles", 5.0)
    with pytest.raises(CoinbaseTradeFlowError):
        pool.get("https://api.exchange.coinbase.com/products/INVALID-USD/trades?limit=25", 5.0)


def test_keep_alive_https_pool_concurrent_thread_isolation_and_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify `_KeepAliveHttpsPool` isolates sockets per thread and closes all on `close()`."""
    monkeypatch.setattr(campaign_cli, "_validate_cb_dns", lambda _host: None)
    conn_list_lock = threading.Lock()
    created_conns: list[Any] = []

    class _FakeResp:
        def __init__(self, status: int, body: bytes) -> None:
            self.status = status
            self._body = body

        def read(self, _limit: int) -> bytes:
            return self._body

    class _ThreadTrackingConn:
        def __init__(self, host: str, port: int, timeout: float, context: object) -> None:
            del host, port, context
            self.timeout = timeout
            self.sock = None
            self.creator_tid = threading.get_ident()
            self.used_tids: set[int] = {self.creator_tid}
            self.in_flight = 0
            self.max_in_flight = 0
            self.req_count = 0
            self.close_calls = 0
            self._next_body = b"{}"
            with conn_list_lock:
                created_conns.append(self)

        def request(self, _method: str, _path: str, **_k: Any) -> None:
            tid = threading.get_ident()
            self.used_tids.add(tid)
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            self.req_count += 1
            time.sleep(0.01)
            self._next_body = json.dumps({"tid": tid, "req": self.req_count}).encode("utf-8")

        def getresponse(self) -> _FakeResp:
            self.in_flight -= 1
            return _FakeResp(200, self._next_body)

        def close(self) -> None:
            self.close_calls += 1

    pool = _KeepAliveHttpsPool(connection_factory=_ThreadTrackingConn)  # type: ignore[arg-type]
    barrier = threading.Barrier(6, timeout=3.0)

    def _worker(idx: int) -> tuple[int, dict[str, Any]]:
        if idx < 6:
            barrier.wait()
        tid = threading.get_ident()
        raw = pool.get("https://api.exchange.coinbase.com/products/ETH-USD/book?level=2", 5.0)
        return tid, json.loads(raw.decode("utf-8"))

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
        results = list(executor.map(_worker, range(24)))

    assert len(results) == 24
    for worker_tid, payload in results:
        assert payload["tid"] == worker_tid

    assert len(created_conns) == 6
    assert len({c.creator_tid for c in created_conns}) == 6
    assert all(c.used_tids == {c.creator_tid} for c in created_conns)
    assert all(c.max_in_flight == 1 for c in created_conns)
    assert sum(c.req_count for c in created_conns) == 24

    pool.close()
    assert all(c.close_calls == 1 for c in created_conns)
    assert pool._all_conns == []
    assert pool._conns == {}

    # Idempotent second close() does not re-close already closed sockets
    pool.close()
    assert all(c.close_calls == 1 for c in created_conns)

    # Single-thread _drop_conn removes stale connection from both _conns and _all_conns
    pool.get("https://api.exchange.coinbase.com/products/BTC-USD/book?level=2", 5.0)
    assert len(pool._all_conns) == 1
    assert ("api.exchange.coinbase.com", 443) in pool._conns
    pool._drop_conn(("api.exchange.coinbase.com", 443))
    assert pool._all_conns == []
    assert pool._conns == {}


def test_campaign_allowed_products_filtering_and_validation(
    tmp_path: Path, bridge: FastpathBridge, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify `--allowed-products` filtering, alias normalization, WS scoping, and validation."""
    cbbtc_addr = normalize_address(CBBTC)
    aero_addr = normalize_address(AERO_TOKEN)
    usdc_addr = normalize_address(USDC)
    extra_pools = (
        BasePoolSpec(
            name="v3_cbbtc_usdc_500",
            address=_synth_addr(961),
            kind=PoolKind.V3,
            token0=min(cbbtc_addr, usdc_addr),
            token1=max(cbbtc_addr, usdc_addr),
            decimals0=6 if usdc_addr < cbbtc_addr else 8,
            decimals1=8 if usdc_addr < cbbtc_addr else 6,
            fixed_fee_pips=500,
        ),
        BasePoolSpec(
            name="v3_aero_usdc_3000",
            address=_synth_addr(962),
            kind=PoolKind.V3,
            token0=min(aero_addr, usdc_addr),
            token1=max(aero_addr, usdc_addr),
            decimals0=6 if usdc_addr < aero_addr else 18,
            decimals1=18 if usdc_addr < aero_addr else 6,
            fixed_fee_pips=3000,
        ),
    )
    seen_urls: list[str] = []

    def _cb_transport(url: str, _timeout: float) -> bytes:
        seen_urls.append(url)
        if "BTC-USD" in url:
            book = build_synthetic_coinbase_l2_book("BTC-USD", mid_price=Decimal("90000.00"))
        elif "AERO-USD" in url:
            book = build_synthetic_coinbase_l2_book("AERO-USD", mid_price=Decimal("1.20"))
        else:
            book = build_synthetic_coinbase_l2_book("ETH-USD", mid_price=Decimal("3000.00"))
        return serialize_coinbase_book_payload(book)

    res_all = run_multicall_campaign(
        out_dir=tmp_path / "unfiltered",
        mode="fixture",
        ticks=1,
        interval_ms=0,
        max_rpc_calls=20,
        extra_fixture_pools=extra_pools,
        coinbase_transport=_cb_transport,
        bridge=bridge,
        sleeper=_noop_sleep,
    )
    assert res_all.summary["allowed_products"] is None
    assert res_all.summary["cex_dex_lead_lag_summary"]["products_evaluated"] == [
        "AERO-USD",
        "BTC-USD",
        "ETH-USD",
    ]

    ws_products_seen: list[tuple[str, ...]] = []

    class _RecordingWsFeed:
        def __init__(self, products: Any) -> None:
            ws_products_seen.append(tuple(products))

        def drain_events(self) -> tuple[tuple[CoinbaseTradePrint, ...], bool]:
            return ((), False)

        def close(self) -> None:
            return None

    seen_urls.clear()
    res_filtered = run_multicall_campaign(
        out_dir=tmp_path / "filtered",
        mode="fixture",
        ticks=1,
        interval_ms=0,
        max_rpc_calls=20,
        extra_fixture_pools=extra_pools,
        coinbase_transport=_cb_transport,
        bridge=bridge,
        sleeper=_noop_sleep,
        enable_coinbase_ws=True,
        ws_feed_factory=_RecordingWsFeed,  # type: ignore[arg-type]
        allowed_products=("WETH", "aero-usd", "ETH-USD"),
    )
    assert res_filtered.summary["allowed_products"] == ["AERO-USD", "ETH-USD"]
    assert res_filtered.summary["cex_dex_lead_lag_summary"]["products_evaluated"] == [
        "AERO-USD",
        "ETH-USD",
    ]
    assert set(res_filtered.records[0]["cex_dex_evaluations"]) == {"ETH-USD", "AERO-USD"}
    assert not any("BTC-USD" in url for url in seen_urls)
    assert ws_products_seen == [("ETH-USD", "AERO-USD")]
    assert res_filtered.summary["coinbase_trade_flow_summary"]["ws_products_subscribed"] == [
        "ETH-USD",
        "AERO-USD",
    ]

    for bad_allowed in ((), "ETH-USD", ["ETH-USD", " "], ["UNKNOWN-USD"]):
        with pytest.raises(ShadowRunnerError):
            run_multicall_campaign(
                out_dir=tmp_path / f"bad-{abs(hash(str(bad_allowed)))}",
                mode="fixture",
                ticks=1,
                interval_ms=0,
                max_rpc_calls=10,
                bridge=bridge,
                sleeper=_noop_sleep,
                allowed_products=bad_allowed,  # type: ignore[arg-type]
            )

    cli_out = tmp_path / "cli-allowed"
    rc = campaign_cli.main(
        [
            "--mode",
            "fixture",
            "--ticks",
            "1",
            "--interval-ms",
            "0",
            "--max-rpc-calls",
            "20",
            "--allowed-products",
            "WETH,ETH-USD",
            "--out",
            str(cli_out),
        ]
    )
    assert rc == 0
    cli_stdout = json.loads(capsys.readouterr().out.strip())
    assert cli_stdout["allowed_products"] == ["ETH-USD"]

    for bad_cli in ("", "ETH-USD,", "ETH-USD,NOPE-USD"):
        rc_bad = campaign_cli.main(
            [
                "--mode",
                "fixture",
                "--ticks",
                "1",
                "--interval-ms",
                "0",
                "--allowed-products",
                bad_cli,
                "--out",
                str(tmp_path / f"cli-bad-{abs(hash(bad_cli))}"),
            ]
        )
        assert rc_bad == 2
        capsys.readouterr()


def test_campaign_selectivity_thresholds_and_concurrent_book_prefetch(
    tmp_path: Path,
    bridge: FastpathBridge,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify taker/maker edge floors, concurrent L2 book prefetching, and CLI forwarding."""
    weth_addr = normalize_address(WETH)
    usdc_addr = normalize_address(USDC)
    cbbtc_addr = normalize_address(CBBTC)
    bc_t0, bc_t1, bc_d0, bc_d1, bc_r0, bc_r1 = (
        (cbbtc_addr, usdc_addr, 8, 6, 100 * 10**8, 9_000_000 * 10**6)
        if cbbtc_addr < usdc_addr
        else (usdc_addr, cbbtc_addr, 6, 8, 9_000_000 * 10**6, 100 * 10**8)
    )
    disc_pools = (
        DiscoveredPool(
            pool_id=_synth_addr(971),
            label="v3_weth_usdc_500",
            venue="uniswap_v3",
            kind=PoolKind.V3,
            token0=weth_addr,
            token1=usdc_addr,
            decimals0=18,
            decimals1=6,
            fixed_fee_pips=500,
            tick_spacing=None,
            factory=UNIV3_FACTORY,
            reserve0=500 * 10**18,
            reserve1=1_500_000 * 10**6,
        ),
        DiscoveredPool(
            pool_id=_synth_addr(972),
            label="v2_cbbtc_usdc",
            venue="aerodrome_v2",
            kind=PoolKind.V2,
            token0=bc_t0,
            token1=bc_t1,
            decimals0=bc_d0,
            decimals1=bc_d1,
            fixed_fee_pips=3000,
            tick_spacing=None,
            factory=_synth_addr(9972),
            reserve0=bc_r0,
            reserve1=bc_r1,
        ),
    )
    snap_idx = {"n": 0}

    def _mock_snap(*_args: Any, **_kwargs: Any) -> PinnedUniverseSnapshot:
        snap_idx["n"] += 1
        bnum = 59_400_000 + snap_idx["n"]
        ts_ms = 1_795_700_000_000 + snap_idx["n"] * 1000
        liq = 2_000_000_000_000_000_000
        t = -196_258
        nodes = (
            PoolNode(
                chain_id=BASE_CHAIN_ID,
                pool_id=disc_pools[0].pool_id,
                kind=PoolKind.V3,
                token0=weth_addr,
                token1=usdc_addr,
                decimals0=18,
                decimals1=6,
                fee_pips=500,
                sqrt_price_x96=sqrt_price_at_tick(t),
                tick=t,
                liquidity=liq,
                ticks=(TickLiquidity(t - 500, liq), TickLiquidity(t + 500, -liq)),
                block_number=bnum,
                timestamp_ms=ts_ms,
            ),
            PoolNode(
                chain_id=BASE_CHAIN_ID,
                pool_id=disc_pools[1].pool_id,
                kind=PoolKind.V2,
                token0=bc_t0,
                token1=bc_t1,
                decimals0=bc_d0,
                decimals1=bc_d1,
                fee_pips=3000,
                reserve0=bc_r0,
                reserve1=bc_r1,
                block_number=bnum,
                timestamp_ms=ts_ms,
            ),
        )
        return PinnedUniverseSnapshot(
            chain_id=BASE_CHAIN_ID,
            block_number=bnum,
            block_hash=_synth_hash(f"sel-blk-{bnum}"),
            parent_hash=_synth_hash(f"sel-blk-{bnum - 1}"),
            header_timestamp_ms=ts_ms,
            base_time_ms=ts_ms,
            base_fee_per_gas_wei=10_000_000,
            l1_fee_wei=1_000_000_000_000,
            pools=disc_pools,
            pool_nodes=nodes,
            balances_by_pool={
                disc_pools[0].pool_id: (500 * 10**18, 1_500_000 * 10**6),
                disc_pools[1].pool_id: (bc_r0, bc_r1),
            },
            rpc_calls_used=2,
            execution_eligible=False,
        )

    monkeypatch.setattr(
        "oasis_agent.fastpath.multicall_universe.discover_base_universe_pools",
        lambda *_a, **_k: disc_pools,
    )
    monkeypatch.setattr(
        "oasis_agent.fastpath.multicall_universe.fetch_pinned_universe_snapshot",
        _mock_snap,
    )

    # ETH-USD mid=$3006 (~+11 bps taker net, ~+14 bps maker edge above DEX ~$3001)
    def _cb_selectivity_transport(url: str, _timeout: float) -> bytes:
        if "BTC-USD" in url:
            book = build_synthetic_coinbase_l2_book(
                "BTC-USD", mid_price=Decimal("90000.00"), level_size=Decimal("0.1"), levels=3
            )
        else:
            book = build_synthetic_coinbase_l2_book(
                "ETH-USD", mid_price=Decimal("3006.00"), level_size=Decimal("5.0"), levels=5
            )
        return serialize_coinbase_book_payload(book)

    # 1. Dynamic defaults: min_taker_edge_bps=6, maker_min_edge_bps=6.5
    res_dyn_default = run_multicall_campaign(
        out_dir=tmp_path / "dyn-default-floors",
        mode="public-shadow",
        ticks=3,
        interval_ms=0,
        max_rpc_calls=30,
        rpc_url="https://sel-dyn.example.com",
        bridge=bridge,
        rpc_transport=lambda _u, _p, _t: b"{}",
        coinbase_transport=_cb_selectivity_transport,
        sleeper=_noop_sleep,
        risk_config_path=REPO_ROOT / "configs" / "profit-engine-risk.example.json",
        dynamic_sizing=True,
    )
    assert res_dyn_default.summary["min_taker_edge_bps"] == 6
    assert res_dyn_default.summary["maker_min_edge_bps"] == 6.5
    assert res_dyn_default.summary["oos_execution_summary"]["oos_taker_trades_count"] >= 1
    assert res_dyn_default.records[0]["maker_hedge"]["ETH-USD"]["quote"]["ask_active"] is True

    # 2. Tightened floors (30 bps) suppress both ~15 bps taker OOS and ~18 bps maker quotes
    res_tight = run_multicall_campaign(
        out_dir=tmp_path / "dyn-tight-floors",
        mode="public-shadow",
        ticks=3,
        interval_ms=0,
        max_rpc_calls=30,
        rpc_url="https://sel-tight.example.com",
        bridge=bridge,
        rpc_transport=lambda _u, _p, _t: b"{}",
        coinbase_transport=_cb_selectivity_transport,
        sleeper=_noop_sleep,
        risk_config_path=REPO_ROOT / "configs" / "profit-engine-risk.example.json",
        dynamic_sizing=True,
        min_taker_edge_bps=30,
        maker_min_edge_bps=Decimal("30.0"),
    )
    assert res_tight.summary["min_taker_edge_bps"] == 30
    assert res_tight.summary["maker_min_edge_bps"] == 30.0
    assert res_tight.summary["oos_execution_summary"]["oos_taker_trades_count"] == 0
    eth_maker_quote = res_tight.records[0]["maker_hedge"]["ETH-USD"]["quote"]
    assert eth_maker_quote["ask_active"] is False
    assert eth_maker_quote["bid_active"] is False

    # 3. Non-dynamic defaults: min_taker_edge_bps=1, maker_min_edge_bps=3.0
    res_static = run_multicall_campaign(
        out_dir=tmp_path / "static-floors",
        mode="fixture",
        ticks=1,
        interval_ms=0,
        max_rpc_calls=10,
        bridge=bridge,
        sleeper=_noop_sleep,
        dynamic_sizing=False,
    )
    assert res_static.summary["min_taker_edge_bps"] == 1
    assert res_static.summary["maker_min_edge_bps"] == 3.0

    # 4. Invalid selectivity, timeout, and worker parameters fail closed
    for bad_kw in (
        {"min_taker_edge_bps": 0},
        {"min_taker_edge_bps": True},
        {"maker_min_edge_bps": 0},
        {"maker_min_edge_bps": float("nan")},
        {"maker_min_edge_bps": True},
        {"max_book_workers": 0},
        {"max_book_workers": True},
        {"timeout_s": 0.0},
        {"timeout_s": float("nan")},
        {"timeout_s": True},
        {"stop_gracefully_on_rpc_error": 1},
    ):
        with pytest.raises(ShadowRunnerError):
            run_multicall_campaign(
                out_dir=tmp_path / f"bad-kw-{abs(hash(str(bad_kw)))}",
                mode="fixture",
                ticks=1,
                interval_ms=0,
                max_rpc_calls=10,
                bridge=bridge,
                sleeper=_noop_sleep,
                **bad_kw,  # type: ignore[arg-type]
            )

    # 5. Concurrent book prefetching in public-shadow when coinbase_transport is None
    par_tids: list[int] = []
    par_tid_lock = threading.Lock()
    par_barrier = threading.Barrier(2, timeout=2.0)

    def _parallel_https_get(url: str, _timeout: float) -> bytes:
        with par_tid_lock:
            par_tids.append(threading.get_ident())
        par_barrier.wait()
        return _cb_selectivity_transport(url, _timeout)

    monkeypatch.setattr(
        "oasis_agent.cross_venue.live_cex_dex_shadow._default_https_get",
        _parallel_https_get,
    )
    res_par = run_multicall_campaign(
        out_dir=tmp_path / "parallel-books",
        mode="public-shadow",
        ticks=1,
        interval_ms=0,
        max_rpc_calls=20,
        rpc_url="https://sel-par.example.com",
        bridge=bridge,
        rpc_transport=lambda _u, _p, _t: b"{}",
        sleeper=_noop_sleep,
        max_book_workers=4,
    )
    assert len(par_tids) == 2
    assert len(set(par_tids)) == 2
    assert res_par.summary["cex_dex_lead_lag_summary"]["products_evaluated"] == [
        "BTC-USD",
        "ETH-USD",
    ]
    assert res_par.summary["cex_dex_lead_lag_summary"]["ok_evaluations"] == 2

    # max_book_workers=1 disables executor and fetches sequentially on main thread
    seq_tids: list[int] = []
    main_tid = threading.get_ident()

    def _seq_https_get(url: str, _timeout: float) -> bytes:
        seq_tids.append(threading.get_ident())
        return _cb_selectivity_transport(url, _timeout)

    monkeypatch.setattr(
        "oasis_agent.cross_venue.live_cex_dex_shadow._default_https_get",
        _seq_https_get,
    )
    run_multicall_campaign(
        out_dir=tmp_path / "sequential-books",
        mode="public-shadow",
        ticks=1,
        interval_ms=0,
        max_rpc_calls=20,
        rpc_url="https://sel-seq.example.com",
        bridge=bridge,
        rpc_transport=lambda _u, _p, _t: b"{}",
        sleeper=_noop_sleep,
        max_book_workers=1,
    )
    assert seq_tids == [main_tid, main_tid]

    # 6. CLI forwarding of --allowed-products, --min-taker-edge-bps, --maker-min-edge-bps,
    # and --max-book-workers
    captured_cli_kw: dict[str, Any] = {}
    orig_run = campaign_cli.run_multicall_campaign

    def _spy_run(**kwargs: Any) -> Any:
        captured_cli_kw.update(kwargs)
        return orig_run(
            **{
                **kwargs,
                "bridge": bridge,
                "rpc_transport": lambda _u, _p, _t: b"{}",
                "coinbase_transport": _cb_selectivity_transport,
                "sleeper": _noop_sleep,
            }
        )

    monkeypatch.setattr(campaign_cli, "run_multicall_campaign", _spy_run)
    capsys.readouterr()
    rc = campaign_cli.main(
        [
            "--mode",
            "public-shadow",
            "--ticks",
            "2",
            "--interval-ms",
            "0",
            "--max-rpc-calls",
            "20",
            "--rpc-url",
            "https://sel-cli.example.com",
            "--timeout-s",
            "22.5",
            "--dynamic-sizing",
            "--allowed-products",
            "ETH-USD,BTC-USD",
            "--min-taker-edge-bps",
            "5",
            "--maker-min-edge-bps",
            "12.5",
            "--max-book-workers",
            "4",
            "--stop-gracefully-on-rpc-error",
            "--out",
            str(tmp_path / "cli-selectivity"),
        ]
    )
    assert rc == 0
    assert captured_cli_kw["timeout_s"] == 22.5
    assert captured_cli_kw["allowed_products"] == ("ETH-USD", "BTC-USD")
    assert captured_cli_kw["min_taker_edge_bps"] == 5
    assert captured_cli_kw["maker_min_edge_bps"] == 12.5
    assert captured_cli_kw["max_book_workers"] == 4
    assert captured_cli_kw["stop_gracefully_on_rpc_error"] is True
    cli_json = json.loads(capsys.readouterr().out.strip())
    assert cli_json["allowed_products"] == ["BTC-USD", "ETH-USD"]
    assert cli_json["min_taker_edge_bps"] == 5
    assert cli_json["maker_min_edge_bps"] == 12.5
    assert cli_json["ticks_requested"] == 2
    assert cli_json["stopped_early"] is False
    assert cli_json["stop_reason"] is None

    # 7. stop_gracefully_on_rpc_error finalizes completed chain on tick >= 2 without retry
    snap_calls = {"n": 0}

    def _fail_tick2_snap(*args: Any, **kwargs: Any) -> PinnedUniverseSnapshot:
        snap_calls["n"] += 1
        if snap_calls["n"] >= 2:
            raise RpcError("Base shadow RPC HTTP failure: status 429")
        return _mock_snap(*args, **kwargs)

    monkeypatch.setattr(
        "oasis_agent.fastpath.multicall_universe.fetch_pinned_universe_snapshot",
        _fail_tick2_snap,
    )
    res_graceful = orig_run(
        out_dir=tmp_path / "graceful-rpc-stop",
        mode="public-shadow",
        ticks=4,
        interval_ms=0,
        max_rpc_calls=30,
        rpc_url="https://graceful-stop.example.com",
        bridge=bridge,
        rpc_transport=lambda _u, _p, _t: b"{}",
        coinbase_transport=_cb_selectivity_transport,
        sleeper=_noop_sleep,
        stop_gracefully_on_rpc_error=True,
    )
    assert snap_calls["n"] == 2  # single failure stops immediately with 0 retries
    assert res_graceful.summary["ticks_requested"] == 4
    assert res_graceful.summary["ticks_completed"] == 1
    assert res_graceful.summary["stopped_early"] is True
    assert "429" in str(res_graceful.summary["stop_reason"])
    assert len(res_graceful.records) == 1
    disk_graceful = json.loads(res_graceful.summary_path.read_text(encoding="utf-8"))
    assert disk_graceful["ticks_completed"] == 1
    disk_records = [
        json.loads(line)
        for line in res_graceful.attribution_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert verify_campaign_attribution_chain(disk_records) == disk_graceful["chain_head_sha256"]

    # Tick-1 RPC failure still fails closed even when stop_gracefully_on_rpc_error=True
    def _fail_tick1_snap(*_args: Any, **_kwargs: Any) -> PinnedUniverseSnapshot:
        raise RpcError("Base shadow RPC transport failure")

    monkeypatch.setattr(
        "oasis_agent.fastpath.multicall_universe.fetch_pinned_universe_snapshot",
        _fail_tick1_snap,
    )
    with pytest.raises(RpcError, match="transport failure"):
        orig_run(
            out_dir=tmp_path / "graceful-tick1-fail",
            mode="public-shadow",
            ticks=4,
            interval_ms=0,
            max_rpc_calls=30,
            rpc_url="https://graceful-tick1.example.com",
            bridge=bridge,
            rpc_transport=lambda _u, _p, _t: b"{}",
            coinbase_transport=_cb_selectivity_transport,
            sleeper=_noop_sleep,
            stop_gracefully_on_rpc_error=True,
        )
