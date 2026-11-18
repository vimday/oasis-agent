"""Unit tests for the Base live multi-DEX pool reader & shadow attribution runner."""

import json
import os
import socket
import subprocess
import sys
import urllib.error
from email.message import Message
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from eth_abi.abi import encode

from oasis_agent.arbitrage.event_quote import V3
from oasis_agent.errors import RpcError
from oasis_agent.evidence.base_denim import BASE_CHAIN_ID
from oasis_agent.fastpath import FastpathBridge, FastpathUnavailableError, PoolKind
from oasis_agent.fastpath.live_shadow_runner import (
    DEFAULT_PRIORITY_FEE_WEI,
    FEE_SELECTOR,
    GET_FEE_SELECTOR,
    GET_L1_FEE_CALLDATA,
    GET_L1_FEE_SELECTOR,
    GET_RESERVES_SELECTOR,
    LIQUIDITY_SELECTOR,
    PROBE_AMOUNT_IN_WEI,
    RPC_CALLS_PER_TICK,
    SLOT0_SELECTOR,
    ShadowBudgetError,
    ShadowRunnerError,
    _validate_resolved_dns,
    _validate_rpc_url,
    build_synthetic_base_rpc_transport,
    build_synthetic_uniswapx_transport,
    canonical_base_weth_usdc_pools,
    decode_aero_factory_fee_pips,
    decode_aero_get_reserves,
    decode_l1_fee_wei,
    decode_liquidity_word,
    decode_slipstream_fee_pips,
    decode_slot0_words,
    resolve_fastpath_bridge,
    run_base_arb_shadow,
    verify_attribution_chain,
)
from oasis_agent.intents.uniswapx_client import UniswapXClientError, UniswapXOrderbookClient
from oasis_agent.models.common import canonical_json, normalize_address, sha256_hex
from oasis_agent.sniping.venues import (
    AERO_FACTORY,
    AERO_USDC_WETH,
    SLIPSTREAM_USDC_WETH,
    USDC,
    WETH,
)
from oasis_agent.uniswap_v4.liquidity_math import sqrt_price_at_tick

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "run_base_arb_shadow.py"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts import run_base_arb_shadow as cli_module  # noqa: E402


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


def _hash32(label: bytes) -> str:
    return "0x" + sha256_hex(label)


def test_canonical_selectors_and_base_pool_specs() -> None:
    assert SLOT0_SELECTOR == "0x3850c7bd"
    assert len(LIQUIDITY_SELECTOR) == 10
    assert len(FEE_SELECTOR) == 10
    assert len(GET_RESERVES_SELECTOR) == 10
    assert len(GET_FEE_SELECTOR) == 10
    assert len(GET_L1_FEE_SELECTOR) == 10
    assert GET_L1_FEE_CALLDATA.startswith(GET_L1_FEE_SELECTOR)
    assert len(bytes.fromhex(GET_L1_FEE_CALLDATA[2:])) == 4 + 32 + 32 + 704

    specs = canonical_base_weth_usdc_pools()
    assert len(specs) == 4
    weth = normalize_address(WETH)
    usdc = normalize_address(USDC)
    assert weth < usdc
    assert specs[0].address == normalize_address(V3.address)
    assert specs[0].kind is PoolKind.V3
    assert specs[1].address == normalize_address(AERO_USDC_WETH[0])
    assert specs[1].kind is PoolKind.V2
    assert specs[1].factory == normalize_address(AERO_FACTORY)
    ss_addrs = {normalize_address(val[0]) for val in SLIPSTREAM_USDC_WETH.values()}
    assert {specs[2].address, specs[3].address} == ss_addrs
    for spec in specs:
        assert spec.token0 == weth
        assert spec.token1 == usdc
        assert spec.decimals0 == 18
        assert spec.decimals1 == 6


def test_abi_decoders_strict_bounds_and_6_vs_7_word_slot0() -> None:
    tick = -196_258
    sqrt_p = sqrt_price_at_tick(tick)

    raw_7 = encode(
        ["uint160", "int24", "uint16", "uint16", "uint16", "uint8", "bool"],
        [sqrt_p, tick, 1, 2, 2, 0, True],
    )
    raw_6 = encode(
        ["uint160", "int24", "uint16", "uint16", "uint16", "bool"],
        [sqrt_p, tick, 1, 2, 2, True],
    )
    assert decode_slot0_words(raw_7) == (sqrt_p, tick)
    assert decode_slot0_words(raw_6) == (sqrt_p, tick)

    locked_6 = encode(
        ["uint160", "int24", "uint16", "uint16", "uint16", "bool"],
        [sqrt_p, tick, 1, 2, 2, False],
    )
    with pytest.raises(ShadowRunnerError, match="unlocked"):
        decode_slot0_words(locked_6)

    mismatched_tick_6 = encode(
        ["uint160", "int24", "uint16", "uint16", "uint16", "bool"],
        [sqrt_p, tick + 1, 1, 2, 2, True],
    )
    with pytest.raises(ShadowRunnerError, match="sqrt_price_at_tick"):
        decode_slot0_words(mismatched_tick_6)

    # Canonical zeroForOne resting boundary: tick paired with exact sqrt_price_at_tick(tick + 1)
    upper_boundary_sqrt = sqrt_price_at_tick(tick + 1)
    boundary_tick_6 = encode(
        ["uint160", "int24", "uint16", "uint16", "uint16", "bool"],
        [upper_boundary_sqrt, tick, 1, 2, 2, True],
    )
    assert decode_slot0_words(boundary_tick_6) == (upper_boundary_sqrt, tick)

    above_boundary_6 = encode(
        ["uint160", "int24", "uint16", "uint16", "uint16", "bool"],
        [upper_boundary_sqrt + 1, tick, 1, 2, 2, True],
    )
    with pytest.raises(ShadowRunnerError, match="sqrt_price_at_tick"):
        decode_slot0_words(above_boundary_6)

    with pytest.raises(ShadowRunnerError, match="192 or 224"):
        decode_slot0_words(b"\x00" * 160)

    assert decode_liquidity_word(encode(["uint128"], [123_456])) == 123_456
    with pytest.raises(ShadowRunnerError, match="positive"):
        decode_liquidity_word(encode(["uint128"], [0]))

    assert decode_slipstream_fee_pips(encode(["uint24"], [100])) == 100
    for bad_fee in (0, 100_000, 250_000):
        with pytest.raises(ShadowRunnerError, match="fee_pips"):
            decode_slipstream_fee_pips(encode(["uint24"], [bad_fee]))

    assert decode_aero_get_reserves(encode(["uint256", "uint256", "uint256"], [10, 20, 30])) == (
        10,
        20,
        30,
    )
    with pytest.raises(ShadowRunnerError, match="positive"):
        decode_aero_get_reserves(encode(["uint256", "uint256", "uint256"], [0, 20, 30]))

    assert decode_aero_factory_fee_pips(encode(["uint256"], [30])) == 3000
    for bad_bps in (0, 1_000, 5_000):
        with pytest.raises(ShadowRunnerError, match="getFee"):
            decode_aero_factory_fee_pips(encode(["uint256"], [bad_bps]))

    assert decode_l1_fee_wei(encode(["uint256"], [4_200_000_000_000])) == 4_200_000_000_000


@pytest.mark.parametrize("ticks", [2, 3])
def test_synthetic_multitick_positive_gross_negative_net_and_hash_chain(
    tmp_path: Path, bridge: FastpathBridge, ticks: int
) -> None:
    out_dir = tmp_path / f"run-{ticks}"
    transport = build_synthetic_base_rpc_transport(scenario="positive_gross_negative_net")
    ux_client = UniswapXOrderbookClient(
        max_requests=ticks,
        transport=build_synthetic_uniswapx_transport(),
        sleeper=_noop_sleep,
    )

    result = run_base_arb_shadow(
        out_dir=out_dir,
        mode="synthetic",
        ticks=ticks,
        interval_ms=0,
        max_requests=48,
        skip_uniswapx=False,
        bridge=bridge,
        transport=transport,
        uniswapx_client=ux_client,
        sleeper=_noop_sleep,
    )

    assert result.attribution_path.is_file()
    assert result.summary_path.is_file()

    disk_lines = result.attribution_path.read_text(encoding="utf-8").splitlines()
    assert len(disk_lines) == ticks
    disk_records = [json.loads(line) for line in disk_lines]
    chain_head = verify_attribution_chain(disk_records)

    disk_summary = json.loads(result.summary_path.read_text(encoding="utf-8"))
    assert disk_summary["execution_eligible"] is False
    assert disk_summary["ticks_completed"] == ticks
    assert disk_summary["requests_used"] == ticks * (RPC_CALLS_PER_TICK + 1)
    assert disk_summary["requests_budget"] == 48
    assert disk_summary["chain_head_sha256"] == chain_head
    assert disk_summary["positive_gross_ticks"] == ticks
    assert disk_summary["positive_net_ticks"] == 0
    assert disk_summary["best_gross_spread_bps"] > 0
    assert disk_summary["best_net_spread_bps"] < 0
    assert disk_summary["mean_net_spread_bps"] < 0
    assert disk_summary["lcb_net_edge_bps"] <= disk_summary["mean_net_spread_bps"] < 0
    assert disk_summary["mean_rust_scan_ns"] > 0
    assert disk_summary["total_candidates_found"] == 0

    for idx, rec in enumerate(disk_records, start=1):
        assert rec["seq"] == idx
        assert rec["execution_eligible"] is False
        assert rec["chain_id"] == BASE_CHAIN_ID
        assert rec["probe_amount_in_wei"] == PROBE_AMOUNT_IN_WEI
        assert rec["cycles_evaluated"] == 12
        assert rec["pairwise_quotes_evaluated"] == 12
        assert rec["pairwise_quote_failures"] == 0
        assert rec["candidates_found"] == 0
        assert rec["top_candidate_net_profit_wei"] is None
        assert rec["best_gross_delta_wei"] > 0
        assert rec["best_net_delta_wei"] < 0
        assert rec["l2_gas_cost_wei"] > 0
        assert rec["l1_data_fee_wei"] > 0
        assert rec["uniswapx_status"] == "ok"
        assert rec["uniswapx_open_orders"] == 0
        assert len(rec["pools"]) == 4

    # Verify tampering detection on the retained attribution chain
    tampered = [dict(r) for r in disk_records]
    tampered[0]["best_net_delta_wei"] = 999
    with pytest.raises(ShadowRunnerError, match="tampering"):
        verify_attribution_chain(tampered)


def test_synthetic_dislocated_detects_positive_net_and_rust_candidate(
    tmp_path: Path, bridge: FastpathBridge
) -> None:
    out_dir = tmp_path / "dislocated"
    transport = build_synthetic_base_rpc_transport(scenario="dislocated")

    result = run_base_arb_shadow(
        out_dir=out_dir,
        mode="synthetic",
        ticks=3,
        interval_ms=0,
        max_requests=48,
        skip_uniswapx=True,
        bridge=bridge,
        transport=transport,
        sleeper=_noop_sleep,
    )

    summary = result.summary
    assert summary["execution_eligible"] is False
    assert summary["ticks_completed"] == 3
    assert summary["requests_used"] == 3 * RPC_CALLS_PER_TICK
    assert summary["positive_gross_ticks"] == 3
    assert summary["positive_net_ticks"] == 3
    assert summary["total_candidates_found"] >= 3
    assert summary["best_gross_spread_bps"] > 50
    assert summary["best_net_spread_bps"] > 50
    assert summary["mean_net_spread_bps"] > 50
    assert summary["lcb_net_edge_bps"] > 50

    for rec in result.records:
        assert rec["execution_eligible"] is False
        assert rec["candidates_found"] >= 1
        assert rec["top_candidate_net_profit_wei"] is not None
        assert rec["top_candidate_net_profit_wei"] > 0
        assert rec["best_net_delta_wei"] > 0
        assert rec["uniswapx_status"] == "skipped"


def test_pairwise_quote_catches_single_range_tick_bound_exhaustion(
    tmp_path: Path, bridge: FastpathBridge
) -> None:
    base_transport = build_synthetic_base_rpc_transport(scenario="positive_gross_negative_net")
    specs = canonical_base_weth_usdc_pools()
    v3_addr = specs[0].address

    def _low_liq_v3_transport(url: str, payload: bytes, timeout_s: float) -> bytes:
        raw_resp = base_transport(url, payload, timeout_s)
        decoded_req = json.loads(payload.decode("utf-8"))
        if not isinstance(decoded_req, list):
            return raw_resp
        decoded_resp = json.loads(raw_resp.decode("utf-8"))
        for req, resp in zip(decoded_req, decoded_resp, strict=True):
            if req.get("method") == "eth_call":
                call_obj = req["params"][0]
                if (
                    normalize_address(call_obj["to"]) == v3_addr
                    and call_obj["data"].lower() == LIQUIDITY_SELECTOR
                ):
                    resp["result"] = "0x" + (10**12).to_bytes(32, "big").hex()
        return json.dumps(decoded_resp).encode("utf-8")

    result = run_base_arb_shadow(
        out_dir=tmp_path / "tick-bound",
        mode="synthetic",
        ticks=1,
        interval_ms=0,
        max_requests=20,
        skip_uniswapx=True,
        bridge=bridge,
        transport=_low_liq_v3_transport,
        sleeper=_noop_sleep,
    )
    rec = result.records[0]
    assert rec["pairwise_quote_failures"] > 0
    assert rec["pairwise_quotes_evaluated"] > 0
    assert rec["pairwise_quotes_evaluated"] + rec["pairwise_quote_failures"] == 12


def test_request_budget_exhaustion_halts_before_extra_rpc_calls(
    tmp_path: Path, bridge: FastpathBridge
) -> None:
    inner = build_synthetic_base_rpc_transport()
    seen_calls: list[bytes] = []

    def _counting_transport(url: str, payload: bytes, timeout_s: float) -> bytes:
        seen_calls.append(payload)
        return inner(url, payload, timeout_s)

    # Insufficient even for tick 1 -> zero network calls issued
    with pytest.raises(ShadowBudgetError):
        run_base_arb_shadow(
            out_dir=tmp_path / "budget-0",
            mode="synthetic",
            ticks=1,
            interval_ms=0,
            max_requests=RPC_CALLS_PER_TICK - 1,
            skip_uniswapx=True,
            bridge=bridge,
            transport=_counting_transport,
            sleeper=_noop_sleep,
        )
    assert len(seen_calls) == 0

    # Budget enough for tick 1 (15 RPC calls) but not tick 2 (needs 30) -> stops after tick 1
    out_partial = tmp_path / "budget-partial"
    with pytest.raises(ShadowBudgetError):
        run_base_arb_shadow(
            out_dir=out_partial,
            mode="synthetic",
            ticks=2,
            interval_ms=0,
            max_requests=20,
            skip_uniswapx=True,
            bridge=bridge,
            transport=_counting_transport,
            sleeper=_noop_sleep,
        )
    # Exactly 2 HTTP calls for tick 1 (header + batch); zero HTTP calls issued for tick 2
    assert len(seen_calls) == 2
    assert (out_partial / "shadow_attribution.jsonl").is_file()
    assert not (out_partial / "shadow_summary.json").exists()


@pytest.mark.parametrize(
    "corrupt_mode",
    ["millis_mismatch", "second_mismatch", "header_mismatch", "missing_header_ms"],
)
def test_basetime_mismatch_fails_closed(
    tmp_path: Path, bridge: FastpathBridge, corrupt_mode: str
) -> None:
    transport = build_synthetic_base_rpc_transport(corrupt_basetime=corrupt_mode)
    out_dir = tmp_path / f"basetime-{corrupt_mode}"
    with pytest.raises(ValueError, match="base_"):
        run_base_arb_shadow(
            out_dir=out_dir,
            mode="synthetic",
            ticks=1,
            interval_ms=0,
            max_requests=20,
            skip_uniswapx=True,
            bridge=bridge,
            transport=transport,
            sleeper=_noop_sleep,
        )
    assert not (out_dir / "shadow_summary.json").exists()
    assert not (out_dir / "shadow_attribution.jsonl").exists()


def test_http_429_zero_retries_and_chain_id_and_reorg_guards(
    tmp_path: Path, bridge: FastpathBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    def _http_429_opener(*_args: object, **_kwargs: object) -> object:
        class _Opener:
            def open(self, *_a: object, **_k: object) -> object:
                nonlocal calls
                calls += 1
                raise urllib.error.HTTPError(
                    "https://rpc.example.invalid",
                    429,
                    "Too Many Requests",
                    Message(),
                    BytesIO(b"rate limited"),
                )

        return _Opener()

    monkeypatch.setattr(
        "oasis_agent.fastpath.live_shadow_runner.socket.getaddrinfo",
        lambda *_a, **_k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
    )
    monkeypatch.setattr(
        "oasis_agent.fastpath.live_shadow_runner.urllib.request.build_opener",
        _http_429_opener,
    )
    with pytest.raises(RpcError, match="status 429"):
        run_base_arb_shadow(
            rpc_url="https://rpc.example.invalid",
            out_dir=tmp_path / "http429",
            mode="public-shadow",
            ticks=1,
            interval_ms=0,
            max_requests=20,
            skip_uniswapx=True,
            bridge=bridge,
            sleeper=_noop_sleep,
        )
    assert calls == 1

    # Chain ID mismatch fails closed
    base_transport = build_synthetic_base_rpc_transport()

    def _wrong_chain_transport(url: str, payload: bytes, timeout_s: float) -> bytes:
        raw_resp = base_transport(url, payload, timeout_s)
        decoded_req = json.loads(payload.decode("utf-8"))
        if not isinstance(decoded_req, list):
            return raw_resp
        decoded_resp = json.loads(raw_resp.decode("utf-8"))
        for req, resp in zip(decoded_req, decoded_resp, strict=True):
            if req.get("method") == "eth_chainId":
                resp["result"] = hex(1)
        return json.dumps(decoded_resp).encode("utf-8")

    with pytest.raises(ShadowRunnerError, match="chain_id mismatch"):
        run_base_arb_shadow(
            out_dir=tmp_path / "wrong-chain",
            mode="synthetic",
            ticks=1,
            interval_ms=0,
            max_requests=20,
            skip_uniswapx=True,
            bridge=bridge,
            transport=_wrong_chain_transport,
            sleeper=_noop_sleep,
        )

    # Reorg across ticks (same block height, changed block hash) fails closed
    reorg_base = build_synthetic_base_rpc_transport()
    tick_seen = 0

    def _reorg_transport(url: str, payload: bytes, timeout_s: float) -> bytes:
        nonlocal tick_seen
        decoded_req = json.loads(payload.decode("utf-8"))
        if isinstance(decoded_req, dict):
            tick_seen += 1
            if tick_seen == 2:
                # Force same block number as tick 1 but mutated hash
                first_block_num = 57_800_000
                ts_ms = 1_794_000_000_000
                return json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": decoded_req["id"],
                        "result": {
                            "number": hex(first_block_num),
                            "hash": _hash32(b"reorged-block-hash"),
                            "timestamp": hex(ts_ms // 1000),
                            "timestampMs": hex(ts_ms),
                            "baseFeePerGas": hex(80_000_000),
                        },
                    }
                ).encode("utf-8")
        return reorg_base(url, payload, timeout_s)

    with pytest.raises(ShadowRunnerError, match="reorg detected"):
        run_base_arb_shadow(
            out_dir=tmp_path / "reorg",
            mode="synthetic",
            ticks=2,
            interval_ms=0,
            max_requests=48,
            skip_uniswapx=True,
            bridge=bridge,
            transport=_reorg_transport,
            sleeper=_noop_sleep,
        )


def test_ssrf_and_tun_fake_ip_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    userinfo_url = "https://" + "u" + ":" + "p" + "@base-rpc.publicnode.com"
    for bad_url in (
        "http://base-rpc.publicnode.com",
        "https://localhost:8545",
        "https://127.0.0.1:8545",
        "https://10.0.0.1",
        userinfo_url,
    ):
        with pytest.raises(ShadowRunnerError):
            _validate_rpc_url(bad_url, enforce_public_host=True)

    # Private DNS rejected
    monkeypatch.setattr(
        "oasis_agent.fastpath.live_shadow_runner.socket.getaddrinfo",
        lambda *_a, **_k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.42", 443))],
    )
    with pytest.raises(ShadowRunnerError, match="private or loopback"):
        _validate_resolved_dns("base-rpc.publicnode.com", 443)

    # TUN Fake-IP (RFC 2544 / RFC 5180) permitted for DNS-resolved domain
    monkeypatch.setattr(
        "oasis_agent.fastpath.live_shadow_runner.socket.getaddrinfo",
        lambda *_a, **_k: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.0.15", 443)),
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001:2::15", 443, 0, 0)),
        ],
    )
    _validate_resolved_dns("base-rpc.publicnode.com", 443)


def test_uniswapx_orderbook_recording_and_error_degradation(
    tmp_path: Path, bridge: FastpathBridge
) -> None:
    sample_order: dict[str, Any] = {
        "type": "Dutch_V3",
        "orderStatus": "open",
        "chainId": BASE_CHAIN_ID,
        "orderHash": "0x" + "cc" * 32,
        "reactor": "0x" + "dd" * 20,
        "swapper": "0x" + "ee" * 20,
        "nonce": "1",
        "decayStartBlock": 57_800_000,
        "decayEndBlock": 57_800_020,
        "deadlineBlock": 57_800_050,
        "input": {
            "token": WETH,
            "startAmount": str(10**17),
            "endAmount": str(10**17),
        },
        "outputs": [
            {
                "token": USDC,
                "startAmount": str(300 * 10**6),
                "endAmount": str(295 * 10**6),
                "recipient": "0x" + "ee" * 20,
            }
        ],
    }
    ux_payload = canonical_json({"orders": [sample_order]})
    ux_client = UniswapXOrderbookClient(
        max_requests=2,
        transport=build_synthetic_uniswapx_transport([sample_order]),
        sleeper=_noop_sleep,
    )
    res_ok = run_base_arb_shadow(
        out_dir=tmp_path / "ux-ok",
        mode="synthetic",
        ticks=1,
        interval_ms=0,
        max_requests=20,
        skip_uniswapx=False,
        bridge=bridge,
        transport=build_synthetic_base_rpc_transport(),
        uniswapx_client=ux_client,
        sleeper=_noop_sleep,
    )
    assert res_ok.records[0]["uniswapx_status"] == "ok"
    assert res_ok.records[0]["uniswapx_open_orders"] == 1
    assert res_ok.records[0]["uniswapx_raw_sha256"] == sha256_hex(ux_payload)

    def _failing_ux_transport(_url: str, _timeout_s: float) -> bytes:
        raise UniswapXClientError("simulated api outage")

    res_deg = run_base_arb_shadow(
        out_dir=tmp_path / "ux-err",
        mode="synthetic",
        ticks=1,
        interval_ms=0,
        max_requests=20,
        skip_uniswapx=False,
        bridge=bridge,
        transport=build_synthetic_base_rpc_transport(),
        uniswapx_client=UniswapXOrderbookClient(
            max_requests=2,
            transport=_failing_ux_transport,
            sleeper=_noop_sleep,
        ),
        sleeper=_noop_sleep,
    )
    assert res_deg.records[0]["uniswapx_status"] == "error"
    assert res_deg.records[0]["uniswapx_open_orders"] is None
    assert res_deg.records[0]["uniswapx_raw_sha256"] is None
    assert "outage" in str(res_deg.records[0]["uniswapx_error"])


def test_overwrite_and_symlink_refusal(tmp_path: Path, bridge: FastpathBridge) -> None:
    out_dir = tmp_path / "immutable"
    run_base_arb_shadow(
        out_dir=out_dir,
        mode="synthetic",
        ticks=1,
        interval_ms=0,
        max_requests=20,
        skip_uniswapx=True,
        bridge=bridge,
        transport=build_synthetic_base_rpc_transport(),
        sleeper=_noop_sleep,
    )

    calls = 0
    inner = build_synthetic_base_rpc_transport()

    def _spy(url: str, payload: bytes, timeout_s: float) -> bytes:
        nonlocal calls
        calls += 1
        return inner(url, payload, timeout_s)

    with pytest.raises(FileExistsError, match="overwrite"):
        run_base_arb_shadow(
            out_dir=out_dir,
            mode="synthetic",
            ticks=1,
            interval_ms=0,
            max_requests=20,
            skip_uniswapx=True,
            bridge=bridge,
            transport=_spy,
            sleeper=_noop_sleep,
        )
    assert calls == 0

    real_dir = tmp_path / "real_target"
    real_dir.mkdir()
    sym_dir = tmp_path / "sym_out"
    sym_dir.symlink_to(real_dir)
    with pytest.raises(ShadowRunnerError, match="symlink"):
        run_base_arb_shadow(
            out_dir=sym_dir,
            mode="synthetic",
            ticks=1,
            interval_ms=0,
            max_requests=20,
            skip_uniswapx=True,
            bridge=bridge,
            transport=_spy,
            sleeper=_noop_sleep,
        )
    assert calls == 0


def test_cli_synthetic_main_and_subprocess(
    tmp_path: Path,
    bridge: FastpathBridge,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("OASIS_CORE_LIB_PATH", str(bridge.lib_path))
    out_main = tmp_path / "cli-main"
    rc = cli_module.main(
        [
            "--mode",
            "synthetic",
            "--ticks",
            "2",
            "--interval-ms",
            "0",
            "--max-requests",
            "48",
            "--priority-fee-wei",
            str(DEFAULT_PRIORITY_FEE_WEI),
            "--out",
            str(out_main),
        ]
    )
    assert rc == 0
    captured = capsys.readouterr()
    cli_summary = json.loads(captured.out.strip())
    assert cli_summary["mode"] == "synthetic"
    assert cli_summary["ticks_completed"] == 2
    assert cli_summary["execution_eligible"] is False
    assert (out_main / "shadow_summary.json").is_file()
    assert (out_main / "shadow_attribution.jsonl").is_file()

    # Re-run into same --out refuses overwrite with exit code 2
    rc_dup = cli_module.main(
        ["--mode", "synthetic", "--ticks", "1", "--interval-ms", "0", "--out", str(out_main)]
    )
    assert rc_dup == 2

    # Subprocess invocation using OASIS_FASTPATH_LIB (without OASIS_CORE_LIB_PATH)
    out_sub = tmp_path / "cli-sub"
    env = dict(os.environ)
    env.pop("OASIS_CORE_LIB_PATH", None)
    env["OASIS_FASTPATH_LIB"] = str(bridge.lib_path)
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--mode",
            "synthetic",
            "--ticks",
            "3",
            "--interval-ms",
            "0",
            "--max-requests",
            "48",
            "--skip-uniswapx",
            "--out",
            str(out_sub),
        ],
        cwd=REPO_ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    sub_summary = json.loads(proc.stdout.strip())
    assert sub_summary["ticks_completed"] == 3
    assert sub_summary["requests_used"] == 3 * RPC_CALLS_PER_TICK
    assert sub_summary["execution_eligible"] is False
    records = [
        json.loads(line)
        for line in (out_sub / "shadow_attribution.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(records) == 3
    assert verify_attribution_chain(records) == sub_summary["chain_head_sha256"]
