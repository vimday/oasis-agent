"""Unit tests for dual-provider RPC quorum, CEX-DEX shadow, and 100-tick campaign."""

import json
import math
import os
import socket
import subprocess
import sys
import types
import urllib.error
from decimal import Decimal
from email.message import Message
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest

from oasis_agent.arbitrage.event_quote import V3
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
