"""Unified Opportunity Radar CLI across all Oasis-Agent subsystems (v2).

Runs deterministic offline/shadow orchestration (never signs, broadcasts, or mutates
external hosts) across:
1. Rust `oasis-core` sub-ms multi-DEX cycle graph + Python differential & latency benchmark
   paired with `OasisAtomicExecutor` bundle encoding and tiered revert-budget telemetry;
2. Incremental EVM pool log state sync (`V2`/`V3`/`V4`) with reorg rollback + atomic
   flash-loan liquidation discovery (`Morpho Blue` / `Aave V3`);
3. Base Denim `BaseTime` (`0x4200...0030`) single-block predeploy verification probe;
4. Equation News (`eqn.news`) 14-channel catalyst intelligence + Base Denim
   `LaunchpadSafetyGate` & `SpotAgent` evaluator drill;
5. `UniswapX V3DutchOrderReactor` shadow filler + read-only `/v2/orders` orderbook poll;
6. `AlephTX` `ALEPHTX_SHM_V12` cross-language bridge + read-only WSL `aleph`
   lakehouse probe + `HedgeGroup` / CEX-DEX lead-lag / funding-basis evaluators;
7. Stage 7 Cross-Strategy Portfolio Capital Allocator with segregated reserves and shared
   failure-domain exposure caps.
"""

import argparse
import hashlib
import json
import os
import subprocess  # nosec B404 - bounded local oasis-fastpath benchmark invocation
import sys
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

from oasis_agent.cross_venue.aleph_bridge import (
    AlephShmReader,
    SyntheticBboSlot,
    probe_remote_aleph_lakehouse,
    write_synthetic_aleph_shm_v12,
)
from oasis_agent.cross_venue.funding_basis import SessionStatus, evaluate_funding_basis
from oasis_agent.cross_venue.hedge_group import (
    AuthoritativePositionMismatch,
    FillConfirmed,
    FillLeg,
    FillRecord,
    HedgeGroup,
    HedgeGroupState,
    HedgePolicy,
    PrimaryAck,
    ReserveCapital,
    StartQuoting,
    apply_hedge_event,
)
from oasis_agent.cross_venue.lead_lag import LeadLagDirection, evaluate_cex_dex_lead_lag
from oasis_agent.errors import RpcError
from oasis_agent.evidence.base_denim import BASE_TIME
from oasis_agent.evidence.base_time_probe import verify_base_time_predeploy
from oasis_agent.execution.atomic_bundle import (
    AtomicCycleIntent,
    PrecheckGuardSpec,
    SwapLegSpec,
    encode_execute_cycle_calldata,
)
from oasis_agent.fastpath.bridge import FastpathBridge
from oasis_agent.fastpath.liquidation import (
    BorrowerPositionSnapshot,
    FlashLiquidationConfig,
    FlashLiquidationScanner,
)
from oasis_agent.fastpath.pool_graph import (
    CycleCandidate,
    FastpathPoolGraph,
    PoolKind,
    PoolNode,
    TickLiquidity,
)
from oasis_agent.fastpath.state_sync import (
    V2_SYNC_TOPIC,
    V3_SWAP_TOPIC,
    EvmPoolLogEvent,
    IncrementalPoolStateSynchronizer,
    ReorgDetectedError,
)
from oasis_agent.intelligence.catalyst_scorer import classify_catalyst
from oasis_agent.intelligence.discovery_bridge import (
    EqnDiscoveryProvider,
    VerifiedSymbolEntry,
    VerifiedSymbolRegistry,
)
from oasis_agent.intelligence.eqn_catalog import EQN_CHANNEL_KEYS, parse_news_payload
from oasis_agent.intelligence.eqn_client import EqnClient, EqnFetchError
from oasis_agent.intents.dutch_v3 import (
    BASE_CHAIN_ID,
    DEPRECATED_PRIORITY_REACTOR_BASE,
    DutchV3Curve,
    DutchV3Order,
    DutchV3Output,
    validate_dutch_v3_reactor,
)
from oasis_agent.intents.filler_shadow import evaluate_dutch_v3_fill
from oasis_agent.intents.uniswapx_client import UniswapXClientError, UniswapXOrderbookClient
from oasis_agent.launch.launchpad_safety import (
    DecisionStatus,
    LaunchpadArchetype,
    LaunchpadSafetyEvidence,
    LaunchpadSafetyPolicy,
    evaluate_launchpad_safety,
)
from oasis_agent.launch.opportunity import Market
from oasis_agent.launch.spot_agent import make_launchpad_safety_evaluator
from oasis_agent.models.common import canonical_json, sha256_hex
from oasis_agent.portfolio.allocator import PortfolioAllocator, PortfolioAllocatorConfig
from oasis_agent.portfolio.models import (
    ExposureDomainCap,
    PortfolioReserves,
    StrategyTrackCandidate,
)
from oasis_agent.risk.revert_budget import (
    CYCLE_EXECUTED_TOPIC0,
    EARLY_ABORTED_TOPIC0,
    ReceiptClassificationInput,
    ReceiptLog,
    RollingRevertBudgetPolicy,
    RollingRevertBudgetTracker,
    classify_execution_receipt,
)
from oasis_agent.uniswap_v4 import liquidity_math

RADAR_SCHEMA_VERSION = "oasis-opportunity-radar-v2"
DEFAULT_BASE_RPC_URL = "https://base-rpc.publicnode.com"


def _addr(byte_hex: str) -> str:
    return "0x" + byte_hex * 20


def _topic_addr(byte_hex: str) -> str:
    return "0x" + ("00" * 12) + (byte_hex * 20)


def _u256_word(value: int) -> str:
    return value.to_bytes(32, "big").hex()


def _i256_word(value: int) -> str:
    return (value & ((1 << 256) - 1)).to_bytes(32, "big").hex()


def _hash32(label: bytes) -> str:
    return "0x" + hashlib.sha256(label).hexdigest()


def _write_new(path: Path, payload: bytes) -> None:
    if path.is_symlink():
        raise ValueError(f"refusing symlink output path: {path}")
    with path.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _sample_eqn_payload(now_ms: int) -> bytes:
    payload = {
        "items": [
            {
                "id": "radar-synth-oi-1",
                "no": 1,
                "path": "/monitor/1",
                "published_ts": now_ms - 15_000,
                "channels": ["oi", "binance"],
                "kind": "oi",
                "source": {"slug": "price_oi_move"},
                "i18n": {
                    "en": "[ETHUSDT] Binance Futures listing & OI +18.5%, price +7.2%",
                    "zh": "[ETHUSDT] 币安合约上新及持仓异动",
                },
                "coins": [{"symbol": "ETH"}],
                "data": {
                    "signal": "price_oi_move",
                    "symbol": "ETHUSDT",
                    "base": "ETH",
                    "exchange": "binance",
                    "market": "perp",
                    "window": 3600,
                    "price_pct": 7.2,
                    "oi_pct": 18.5,
                    "direction": "up",
                    "oi_usd": 120_000_000.0,
                    "market_cap": 300_000_000.0,
                    "oi_mc_ratio": 0.40,
                    "vol_24h_usd": 500_000_000.0,
                    "funding_rate_pct": -0.012,
                },
            },
            {
                "id": "radar-synth-sec-2",
                "no": 2,
                "path": "/main/2",
                "published_ts": now_ms - 5_000,
                "channels": ["main"],
                "kind": "news",
                "source": {"slug": "security_alert"},
                "i18n": {
                    "en": "URGENT: Protocol BadToken exploit & emergency pause freeze",
                    "zh": "紧急安全告警 BadToken 漏洞攻击并冻结",
                },
                "coins": [{"symbol": "BAD"}],
            },
        ],
        "next_cursor": None,
    }
    return canonical_json(payload)


def _run_rust_and_executor_engine(
    *,
    bench_iters: int,
    now_ms: int,
    bridge: FastpathBridge | None = None,
) -> dict[str, Any]:
    active_bridge = bridge if bridge is not None else FastpathBridge()
    weth = _addr("11")
    usdc = _addr("22")
    usdt = _addr("33")

    tick_v3 = -196_258
    tick_ss = -195_770
    sqrt_v3 = active_bridge.sqrt_price_at_tick(tick_v3)
    sqrt_ss = active_bridge.sqrt_price_at_tick(tick_ss)

    pools = [
        PoolNode(
            chain_id=BASE_CHAIN_ID,
            pool_id=_addr("a1"),
            kind=PoolKind.V3,
            token0=weth,
            token1=usdc,
            decimals0=18,
            decimals1=6,
            fee_pips=500,
            sqrt_price_x96=sqrt_v3,
            tick=tick_v3,
            liquidity=20_000_000_000_000_000,
            ticks=(
                TickLiquidity(tick=-196_800, liquidity_net=10_000_000_000_000_000),
                TickLiquidity(tick=-195_800, liquidity_net=-10_000_000_000_000_000),
            ),
            block_number=57_800_000,
            timestamp_ms=now_ms,
        ),
        PoolNode(
            chain_id=BASE_CHAIN_ID,
            pool_id=_addr("a2"),
            kind=PoolKind.AERODROME_STABLE,
            token0=usdc,
            token1=usdt,
            decimals0=6,
            decimals1=6,
            fee_pips=100,
            reserve0=5_000_000_000_000,
            reserve1=5_050_000_000_000,
            block_number=57_800_000,
            timestamp_ms=now_ms,
        ),
        PoolNode(
            chain_id=BASE_CHAIN_ID,
            pool_id=_addr("a3"),
            kind=PoolKind.SLIPSTREAM,
            token0=weth,
            token1=usdc,
            decimals0=18,
            decimals1=6,
            fee_pips=500,
            sqrt_price_x96=sqrt_ss,
            tick=tick_ss,
            liquidity=20_000_000_000_000_000,
            ticks=(
                TickLiquidity(tick=-196_500, liquidity_net=5_000_000_000_000_000),
                TickLiquidity(tick=-195_200, liquidity_net=-5_000_000_000_000_000),
            ),
            block_number=57_800_000,
            timestamp_ms=now_ms,
        ),
        PoolNode(
            chain_id=BASE_CHAIN_ID,
            pool_id=_addr("a4"),
            kind=PoolKind.V2,
            token0=weth,
            token1=usdt,
            decimals0=18,
            decimals1=6,
            fee_pips=3000,
            reserve0=100 * 10**18,
            reserve1=325_000 * 10**6,
            block_number=57_800_000,
            timestamp_ms=now_ms,
        ),
    ]

    graph = FastpathPoolGraph(bridge=active_bridge)
    for pool in pools:
        graph.upsert_pool(pool)

    gas_l2 = 2 * 10**14
    gas_l1 = 5 * 10**13
    report = graph.scan_cycles(
        [weth],
        max_hops=3,
        min_amount_in=10**16,
        max_amount_in=5 * 10**18,
        gas_cost_in_base=gas_l2,
        l1_fee_in_base=gas_l1,
        min_net_profit=10**14,
        block_number=57_800_000,
        timestamp_ns=now_ms * 1_000_000,
    )

    # Bit-exact differential check + isolated timing: Python vs Rust FFI & native binary
    sample_ticks = [-887_272, -196_258, -1, 0, 1, 195_770, 887_272]
    mismatches = 0
    for tick in sample_ticks:
        sq_py = liquidity_math.sqrt_price_at_tick(tick)
        sq_rs = active_bridge.sqrt_price_at_tick(tick)
        a0_py = liquidity_math.amount0_delta(
            sq_py, sq_py + 1_000_000_000_000, 10**18, round_up=True
        )
        a0_rs = active_bridge.amount0_delta(sq_rs, sq_rs + 1_000_000_000_000, 10**18, round_up=True)
        if sq_rs != sq_py or a0_rs != a0_py:
            mismatches += 1

    t_py0 = time.perf_counter_ns()
    for _ in range(bench_iters):
        for tick in sample_ticks:
            sq = liquidity_math.sqrt_price_at_tick(tick)
            liquidity_math.amount0_delta(sq, sq + 1_000_000_000_000, 10**18, round_up=True)
    py_ns = max(1, time.perf_counter_ns() - t_py0)

    t_rs0 = time.perf_counter_ns()
    for _ in range(bench_iters):
        for tick in sample_ticks:
            sq_rs = active_bridge.sqrt_price_at_tick(tick)
            active_bridge.amount0_delta(sq_rs, sq_rs + 1_000_000_000_000, 10**18, round_up=True)
    rs_ffi_ns = max(1, time.perf_counter_ns() - t_rs0)

    fastpath_bin = (
        Path(__file__).resolve().parents[1] / "crates" / "target" / "release" / "oasis-fastpath"
    )
    native_ns_per_scan: int | None = None
    if fastpath_bin.is_file():
        proc = subprocess.run(  # nosec B603 - fixed local compiled binary path
            [str(fastpath_bin), "benchmark", "--iterations", str(bench_iters), "--hops", "3"],
            shell=False,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        bench_out = json.loads(proc.stdout)
        raw_ns = bench_out.get("ns_per_scan")
        if type(raw_ns) is int:
            native_ns_per_scan = raw_ns

    top: CycleCandidate = report.candidates[0]
    legs = tuple(
        SwapLegSpec(
            pool=hop.pool_id,
            token_in=hop.token_in,
            token_out=hop.token_out,
            zero_for_one=(hop.token_in < hop.token_out),
            sqrt_price_limit_x96=0,
            v2_fee_bps=30 if hop.kind is PoolKind.V2 else 0,
        )
        for hop in top.hops
    )
    executor_addr = _addr("e1")
    intent = AtomicCycleIntent(
        chain_id=BASE_CHAIN_ID,
        executor_address=executor_addr,
        executor_code_hash=_hash32(b"oasis-atomic-executor-v1"),
        nonce=1,
        deadline=(now_ms // 1000) + 12,
        profit_token=top.base_token,
        initial_amount=top.optimal_amount_in,
        min_net_profit=top.net_profit,
        guard=PrecheckGuardSpec(
            pool=pools[0].pool_id,
            min_sqrt_price_x96=max(1, sqrt_v3 - (sqrt_v3 // 100)),
            max_sqrt_price_x96=sqrt_v3 + (sqrt_v3 // 100),
        ),
        legs=legs,
    )
    calldata, calldata_sha256 = encode_execute_cycle_calldata(intent)

    tracker = RollingRevertBudgetTracker(
        RollingRevertBudgetPolicy(
            max_hourly_gas_burn_usd_micros=5_000_000,
            max_deep_reverts_per_hour=3,
            max_early_abort_gas=65_000,
        )
    )
    early_outcome = classify_execution_receipt(
        ReceiptClassificationInput(
            executor_address=executor_addr,
            status=1,
            gas_used=62_422,
            logs=(
                ReceiptLog(
                    address=executor_addr,
                    topics=(EARLY_ABORTED_TOPIC0,),
                    data="0x",
                ),
            ),
        ),
        tracker.policy,
    )
    success_outcome = classify_execution_receipt(
        ReceiptClassificationInput(
            executor_address=executor_addr,
            status=1,
            gas_used=138_000,
            logs=(
                ReceiptLog(
                    address=executor_addr,
                    topics=(CYCLE_EXECUTED_TOPIC0,),
                    data="0x",
                ),
            ),
        ),
        tracker.policy,
    )
    now_s = now_ms // 1000
    tracker.record(
        timestamp_s=now_s,
        outcome=early_outcome,
        gas_cost_usd_micros=15_000,
        realized_profit_usd_micros=0,
    )
    snap = tracker.record(
        timestamp_s=now_s + 1,
        outcome=success_outcome,
        gas_cost_usd_micros=35_000,
        realized_profit_usd_micros=450_000,
    )

    return {
        "cycles_evaluated": report.cycles_evaluated,
        "candidates_found": len(report.candidates),
        "rust_scan_elapsed_ns": report.elapsed_ns,
        "top_candidate": {
            "base_token": top.base_token,
            "hops": [hop.pool_id for hop in top.hops],
            "optimal_amount_in": str(top.optimal_amount_in),
            "amount_out": str(top.amount_out),
            "net_profit": str(top.net_profit),
        },
        "differential": {
            "bench_iters": bench_iters,
            "samples_checked": len(sample_ticks),
            "mismatches": mismatches,
            "bit_exact": mismatches == 0,
            "python_pure_ns": py_ns,
            "rust_ffi_ns": rs_ffi_ns,
            "rust_native_ns_per_cycle_scan": native_ns_per_scan,
        },
        "atomic_executor_bundle": {
            "intent_hash": intent.intent_hash,
            "calldata_bytes": len(calldata),
            "calldata_sha256": calldata_sha256,
            "revert_budget_status": snap.status.value,
            "early_abort_count": snap.early_abort_count,
            "net_gas_burn_usd_micros": snap.net_gas_burn_usd_micros,
        },
    }


def _run_eqn_and_launchpad_engine(
    *,
    mode: str,
    now_ms: int,
    eqn_channel: str,
    eqn_limit: int,
    eqn_client: EqnClient | None = None,
) -> tuple[dict[str, Any], bytes | None]:
    raw_bytes: bytes | None = None
    fetch_status = "synthetic_replay"
    fetch_error: str | None = None

    if mode == "public-shadow":
        try:
            if eqn_client is not None:
                batch = eqn_client.fetch_news(
                    channel=eqn_channel, limit=eqn_limit, received_ts_ms=now_ms
                )
            else:
                with EqnClient(max_requests=1, timeout_s=10.0) as client:
                    batch = client.fetch_news(
                        channel=eqn_channel, limit=eqn_limit, received_ts_ms=now_ms
                    )
            fetch_status = "ok"
            raw_bytes = canonical_json(
                {
                    "capture_id": batch.capture_id,
                    "channel": batch.channel,
                    "raw_sha256": batch.raw_sha256,
                    "item_count": len(batch.items),
                    "items": [asdict(item) for item in batch.items],
                }
            )
        except (EqnFetchError, ValueError, TypeError) as exc:
            fetch_status = "error"
            fetch_error = str(exc)
            batch = None
    else:
        raw_bytes = _sample_eqn_payload(now_ms)
        batch = parse_news_payload(
            raw_bytes,
            channel=eqn_channel,
            request_url="https://eqn.news/api/v1/news",
            received_ts_ms=now_ms,
        )

    registry = VerifiedSymbolRegistry(
        entries=(
            VerifiedSymbolEntry(
                chain_id=BASE_CHAIN_ID,
                symbol="ETH",
                token_address=_addr("11"),
                code_sha256=sha256_hex(b"verified-weth-base"),
            ),
            VerifiedSymbolEntry(
                chain_id=BASE_CHAIN_ID,
                symbol="BAD",
                token_address=_addr("ba"),
                code_sha256=sha256_hex(b"verified-bad-token"),
            ),
        )
    )

    eqn_summary: dict[str, Any]
    if batch is not None:
        signals = tuple(classify_catalyst(item) for item in batch.items)
        provider = EqnDiscoveryProvider(
            registry=registry,
            items=batch.items,
            max_age_ms=86_400_000,
            clock_ms=lambda: now_ms,
        )
        nominated = provider.nominate(BASE_CHAIN_ID)
        counts = dict(sorted(Counter(sig.kind.value for sig in signals).items()))
        top_items = [
            {
                "id": sig.item_id,
                "kind": sig.kind.value,
                "direction": sig.direction.value,
                "confidence_bps": sig.confidence_bps,
                "symbols": list(sig.symbols),
                "channels": list(sig.channels),
                "risk_freeze": sig.risk_freeze,
                "actionable": (not sig.risk_freeze and sig.confidence_bps >= 6_000),
                "reason": sig.reason,
            }
            for sig in sorted(signals, key=lambda s: (-s.confidence_bps, s.item_id))[:20]
        ]
        eqn_summary = {
            "status": fetch_status,
            "error": fetch_error,
            "catalog_channels": len(EQN_CHANNEL_KEYS),
            "channel": eqn_channel,
            "raw_sha256": batch.raw_sha256,
            "item_count": len(batch.items),
            "counts_by_kind": counts,
            "risk_freeze_count": sum(1 for sig in signals if sig.risk_freeze),
            "nominated_verified_addresses": nominated,
            "frozen_symbols": list(provider.frozen_symbols),
            "unmapped_symbols": list(provider.unmapped_symbols),
            "top_items": top_items,
        }
    else:
        eqn_summary = {
            "status": fetch_status,
            "error": fetch_error,
            "catalog_channels": len(EQN_CHANNEL_KEYS),
            "channel": eqn_channel,
            "raw_sha256": None,
            "item_count": 0,
            "counts_by_kind": {},
            "risk_freeze_count": 0,
            "nominated_verified_addresses": [],
            "frozen_symbols": [],
            "unmapped_symbols": [],
            "top_items": [],
        }

    # Base Denim LaunchpadSafetyGate triad: active decay (DEFER), safe entry (APPROVED), soft rug
    block_s = (now_ms // 1000) - 2
    genesis_ms = (block_s - 2) * 1000
    defer_evidence = LaunchpadSafetyEvidence(
        chain_id=BASE_CHAIN_ID,
        token=_addr("d1"),
        pool=_addr("d2"),
        archetype=LaunchpadArchetype.DOPPLER_MULTICURVE,
        block_number=57_800_010,
        block_hash=_hash32(b"denim-block-defer"),
        block_timestamp_s=block_s,
        block_timestamp_ms=block_s * 1000 + 400,
        observed_at_ms=block_s * 1000 + 600,
        source_count=2,
        buy_tax_bps=6_000,
        sell_tax_bps=120,
        sell_enabled=True,
        unlock_timestamp_ms=genesis_ms,
        genesis_timestamp_ms=genesis_ms,
        decay_start_fee_bps=8_000,
        decay_end_fee_bps=120,
        decay_duration_ms=10_000,
        simulated_buy_in=1_000_000,
        simulated_buy_out=395_000,
        simulated_sell_out=390_000,
        exit_liquidity_usd_micros=60_000_000_000,
        creator_same_block_buy_bps=150,
        bundled_cluster_bps=400,
        creator_prior_rug_count=0,
        raw_sha256=sha256_hex(b"doppler-defer"),
    )
    approved_evidence = LaunchpadSafetyEvidence(
        chain_id=BASE_CHAIN_ID,
        token=_addr("d1"),
        pool=_addr("d2"),
        archetype=LaunchpadArchetype.DOPPLER_MULTICURVE,
        block_number=57_800_060,
        block_hash=_hash32(b"denim-block-approved"),
        block_timestamp_s=block_s + 12,
        block_timestamp_ms=(block_s + 12) * 1000 + 200,
        observed_at_ms=(block_s + 12) * 1000 + 350,
        source_count=2,
        buy_tax_bps=120,
        sell_tax_bps=120,
        sell_enabled=True,
        unlock_timestamp_ms=genesis_ms,
        genesis_timestamp_ms=genesis_ms,
        decay_start_fee_bps=8_000,
        decay_end_fee_bps=120,
        decay_duration_ms=10_000,
        simulated_buy_in=1_000_000,
        simulated_buy_out=988_000,
        simulated_sell_out=972_000,
        exit_liquidity_usd_micros=60_000_000_000,
        creator_same_block_buy_bps=150,
        bundled_cluster_bps=400,
        creator_prior_rug_count=0,
        raw_sha256=sha256_hex(b"doppler-approved"),
    )
    rug_evidence = LaunchpadSafetyEvidence(
        chain_id=BASE_CHAIN_ID,
        token=_addr("d3"),
        pool=_addr("d4"),
        archetype=LaunchpadArchetype.CLANKER_V4,
        block_number=57_800_061,
        block_hash=_hash32(b"denim-block-rug"),
        block_timestamp_s=block_s + 12,
        block_timestamp_ms=(block_s + 12) * 1000 + 400,
        observed_at_ms=(block_s + 12) * 1000 + 500,
        source_count=2,
        buy_tax_bps=100,
        sell_tax_bps=100,
        sell_enabled=True,
        unlock_timestamp_ms=genesis_ms,
        genesis_timestamp_ms=genesis_ms,
        decay_start_fee_bps=8_000,
        decay_end_fee_bps=100,
        decay_duration_ms=10_000,
        simulated_buy_in=1_000_000,
        simulated_buy_out=985_000,
        simulated_sell_out=970_000,
        exit_liquidity_usd_micros=60_000_000_000,
        creator_same_block_buy_bps=1_200,
        bundled_cluster_bps=4_500,
        creator_prior_rug_count=1,
        raw_sha256=sha256_hex(b"clanker-soft-rug"),
    )
    policy = LaunchpadSafetyPolicy()
    d_defer = evaluate_launchpad_safety(defer_evidence, policy)
    d_ok = evaluate_launchpad_safety(approved_evidence, policy)
    d_rug = evaluate_launchpad_safety(rug_evidence, policy)

    evidence_by_token_and_block = {
        (defer_evidence.token, block_s): defer_evidence,
        (approved_evidence.token, block_s + 12): approved_evidence,
        (rug_evidence.token, block_s + 13): rug_evidence,
    }
    spot_evaluator = make_launchpad_safety_evaluator(
        policy,
        lambda market, ts: evidence_by_token_and_block.get((market.token, ts)),
        fail_closed_on_missing=True,
    )
    drill_defer = spot_evaluator(
        Market(
            token=defer_evidence.token,
            symbol="DOPL",
            observed_at=block_s,
            liquidity_usd_micros=60_000_000_000,
            market_cap_usd_micros=500_000_000_000,
            holders=300,
            top10_bps=2_200,
            unique_traders=90,
            buys=60,
            sells=30,
            first_trade_at=genesis_ms // 1000,
        ),
        block_s,
    )
    drill_ok = spot_evaluator(
        Market(
            token=approved_evidence.token,
            symbol="DOPL",
            observed_at=block_s + 12,
            liquidity_usd_micros=60_000_000_000,
            market_cap_usd_micros=500_000_000_000,
            holders=300,
            top10_bps=2_200,
            unique_traders=90,
            buys=60,
            sells=30,
            first_trade_at=genesis_ms // 1000,
        ),
        block_s + 12,
    )
    drill_rug = spot_evaluator(
        Market(
            token=rug_evidence.token,
            symbol="RUG",
            observed_at=block_s + 13,
            liquidity_usd_micros=60_000_000_000,
            market_cap_usd_micros=500_000_000_000,
            holders=300,
            top10_bps=5_500,
            unique_traders=90,
            buys=60,
            sells=30,
            first_trade_at=genesis_ms // 1000,
        ),
        block_s + 13,
    )

    return (
        {
            "eqn_news": eqn_summary,
            "launchpad_safety_gate": {
                "defer_case": {
                    "status": d_defer.status.value,
                    "earliest_safe_entry_ms": d_defer.earliest_safe_entry_ms,
                    "reasons": list(d_defer.reasons),
                },
                "approved_case": {
                    "status": d_ok.status.value,
                    "effective_buy_tax_bps": d_ok.effective_buy_tax_bps,
                    "round_trip_loss_bps": d_ok.round_trip_loss_bps,
                },
                "soft_rug_case": {
                    "status": d_rug.status.value,
                    "reasons": list(d_rug.reasons),
                },
                "spot_agent_gate_drill": {
                    "defer_status": drill_defer.status.value if drill_defer else None,
                    "approved_status": drill_ok.status.value if drill_ok else None,
                    "rug_status": drill_rug.status.value if drill_rug else None,
                    "parity_ok": (
                        drill_defer is not None
                        and drill_defer.status is DecisionStatus.DEFER_UNTIL_DECAY
                        and drill_ok is not None
                        and drill_ok.status is DecisionStatus.APPROVED
                        and drill_rug is not None
                        and drill_rug.status is DecisionStatus.REJECTED
                    ),
                },
                "all_expected_statuses_ok": (
                    d_defer.status is DecisionStatus.DEFER_UNTIL_DECAY
                    and d_ok.status is DecisionStatus.APPROVED
                    and d_rug.status is DecisionStatus.REJECTED
                ),
            },
        },
        raw_bytes,
    )


def _run_uniswapx_intent_engine(
    *,
    mode: str,
    now_ms: int,
    uniswapx_client: UniswapXOrderbookClient | None = None,
) -> dict[str, Any]:
    reactor_addr = _addr("31")
    validate_dutch_v3_reactor(reactor_addr, chain_id=BASE_CHAIN_ID)
    deprecated_rejected = False
    try:
        validate_dutch_v3_reactor(DEPRECATED_PRIORITY_REACTOR_BASE, chain_id=BASE_CHAIN_ID)
    except ValueError:
        deprecated_rejected = True

    order = DutchV3Order(
        order_hash=_hash32(b"radar-uniswapx-v3-order"),
        chain_id=BASE_CHAIN_ID,
        reactor_address=reactor_addr,
        swapper=_addr("41"),
        nonce=11,
        decay_start_block=57_800_000,
        decay_end_block=57_800_030,
        deadline_block=57_800_045,
        exclusivity_end_block=57_800_010,
        exclusive_filler=_addr("51"),
        exclusivity_override_bps=50,
        input_token=_addr("11"),
        input_start_amount=10**18,
        input_end_amount=10**18,
        outputs=(
            DutchV3Output(
                token=_addr("22"),
                start_amount=2_540_000_000,
                end_amount=2_470_000_000,
                recipient=_addr("41"),
                curve=DutchV3Curve(
                    relative_blocks=(10, 30),
                    relative_amounts=(40_000_000, 70_000_000),
                ),
            ),
        ),
    )

    decision = evaluate_dutch_v3_fill(
        order=order,
        fill_block=57_800_022,
        filler_address=_addr("61"),
        input_token_decimals=18,
        input_hedge_bid_usd_micros=2_505_000_000,
        output_token_decimals=(6,),
        output_hedge_ask_usd_micros=(1_000_000,),
        hedge_fee_bps=2,
        hedge_slippage_bps=3,
        reactor_callback_gas_usd_micros=15_000,
        quote_latency_ms=180,
        amm_baseline_fee_bps=5,
        sla_budget_ms=500,
        min_net_edge_bps=5,
    )
    if mode == "public-shadow":
        try:
            active_client = (
                uniswapx_client
                if uniswapx_client is not None
                else UniswapXOrderbookClient(max_requests=1, timeout_s=8.0)
            )
            poll = active_client.poll_open_orders(limit=20, polled_at_ms=now_ms)
            live_poll = {
                "status": "ok",
                "order_count": len(poll.orders),
                "raw_sha256": poll.raw_sha256,
                "request_url": poll.request_url,
                "error": None,
            }
        except (UniswapXClientError, OSError, ValueError) as exc:
            live_poll = {
                "status": "error",
                "order_count": 0,
                "raw_sha256": None,
                "request_url": None,
                "error": str(exc),
            }
    else:
        live_poll = {
            "status": "synthetic_skip",
            "order_count": 0,
            "raw_sha256": None,
            "request_url": None,
            "error": None,
        }

    return {
        "deprecated_priority_reactor_rejected": deprecated_rejected,
        "order_hash": decision.order_hash,
        "fill_block": decision.fill_block,
        "approved": decision.approved,
        "worst_case_net_usd_micros": decision.worst_case_net_usd_micros,
        "net_edge_bps": decision.net_edge_bps,
        "saved_amm_fee_bps": decision.saved_amm_fee_bps,
        "saved_amm_fee_usd_micros": decision.saved_amm_fee_usd_micros,
        "quote_latency_ms": decision.quote_latency_ms,
        "sla_budget_ms": decision.sla_budget_ms,
        "live_orderbook_poll": live_poll,
        "execution_eligible": decision.execution_eligible,
    }


def _run_cross_venue_and_aleph_engine(
    *,
    mode: str,
    out_dir: Path,
    now_ms: int,
    ssh_host: str,
    bridge: FastpathBridge | None = None,
) -> dict[str, Any]:
    active_bridge = bridge if bridge is not None else FastpathBridge()
    shm_path = out_dir / "aleph_shm_v12_probe.bin"
    now_ns = now_ms * 1_000_000
    slot_ts_ns = now_ns - 15_000_000  # 15 ms fresh

    write_synthetic_aleph_shm_v12(
        shm_path,
        num_symbols=16,
        slots=(
            SyntheticBboSlot(
                symbol_id=0,
                exchange_id=1,
                timestamp_ns=slot_ts_ns,
                bid_price=2_504.50,
                bid_size=12.0,
                ask_price=2_505.00,
                ask_size=10.0,
            ),
            SyntheticBboSlot(
                symbol_id=0,
                exchange_id=6,
                timestamp_ns=slot_ts_ns,
                bid_price=2_505.25,
                bid_size=25.0,
                ask_price=2_505.50,
                ask_size=20.0,
            ),
        ),
    )

    py_reader = AlephShmReader(shm_path, num_symbols=16)
    try:
        py_binance = py_reader.read_slot(0, 6, now_ns=now_ns, max_age_ns=1_000_000_000)
        py_hl = py_reader.read_slot(0, 1, now_ns=now_ns, max_age_ns=1_000_000_000)
    finally:
        py_reader.close()

    rs_binance = active_bridge.read_aleph_bbo(shm_path, num_symbols=16, symbol_id=0, exchange_id=6)
    rs_hl = active_bridge.read_aleph_bbo(shm_path, num_symbols=16, symbol_id=0, exchange_id=1)

    lead_lag = evaluate_cex_dex_lead_lag(
        instrument="ETH-USDC",
        direction=LeadLagDirection.DEX_BUY_CEX_SELL,
        qty_base=2 * 10**18,
        base_decimals=18,
        dex_quote_usd_micros=2_498_000_000,
        dex_executable_depth_base=10 * 10**18,
        dex_quote_age_ms=80,
        cex_bbo=py_binance,
        dex_pool_fee_bps=5,
        cex_taker_fee_bps=3,
        hedge_slippage_bps=2,
        base_l2_gas_usd_micros=10_000,
        base_l1_gas_usd_micros=8_000,
        latency_drift_bps=2,
        min_net_edge_bps=5,
    )

    basis = evaluate_funding_basis(
        instrument="XYZ-NVDA-PERP",
        proceeds_bid_s_usd_micros=10_050_000_000,
        cost_ask_b_usd_micros=10_000_000_000,
        expected_funding_usd_micros=8_000_000,
        fees_usd_micros=6_000_000,
        borrow_usd_micros=2_000_000,
        gas_usd_micros=500_000,
        hedge_slippage_usd_micros=2_000_000,
        gap_reserve_usd_micros=3_000_000,
        tail_charge_usd_micros=2_000_000,
        session_status=SessionStatus.REGULAR,
        liquidation_distance_bps=3_500,
        min_liquidation_distance_bps=1_500,
        min_net_edge_bps=10,
    )

    # HedgeGroup saga: duplicate fill dedup + stale-position phantom-delta HALT drill
    policy = HedgePolicy(
        max_child_order_base=5 * 10**18,
        min_hedge_notional_usd_micros=100_000_000,
        max_accumulate_wait_ms=1_000,
        max_unhedged_notional_ms=30_000 * 5_000_000_000,
        base_decimals=18,
    )
    group = HedgeGroup(
        group_id="radar-saga-1",
        instrument="ETH-USDC",
        primary_venue="base_slipstream",
        hedge_venue="hyperliquid",
        target_qty_base=2 * 10**18,
    )
    group, _ = apply_hedge_event(group, ReserveCapital(now_ms, "res-1", 6_000_000_000), policy)
    group, _ = apply_hedge_event(group, StartQuoting(now_ms + 1, "ord-p1"), policy)
    group, d_ack = apply_hedge_event(group, PrimaryAck(now_ms + 2, "ord-p1", 2 * 10**18), policy)
    fill_evt = FillConfirmed(
        now_ms + 10,
        FillRecord(
            "base_slipstream",
            "ord-p1",
            "fill-1",
            FillLeg.PRIMARY,
            10**18,
            2_500_000_000,
            now_ms + 10,
        ),
    )
    group, d_fill1 = apply_hedge_event(group, fill_evt, policy)
    group, d_dup = apply_hedge_event(group, fill_evt, policy)
    group, d_halt = apply_hedge_event(
        group,
        AuthoritativePositionMismatch(
            event_ms=now_ms + 20,
            authoritative_primary_base=3 * 10**18,
            authoritative_hedge_base=0,
            reconciliation_version=1,
        ),
        policy,
    )

    if mode == "public-shadow":
        lakehouse = probe_remote_aleph_lakehouse(ssh_host=ssh_host, timeout_s=5)
        lakehouse_dict = {
            "probed": True,
            "ssh_host": lakehouse.ssh_host,
            "reachable": lakehouse.reachable,
            "subdirectories": lakehouse.subdirectories,
            "error": lakehouse.error,
        }
    else:
        lakehouse_dict = {
            "probed": False,
            "ssh_host": ssh_host,
            "reachable": None,
            "subdirectories": {},
            "error": None,
        }

    shm_sha256 = sha256_hex(shm_path.read_bytes())

    return {
        "aleph_shm_v12": {
            "file_name": shm_path.name,
            "file_sha256": shm_sha256,
            "python_binance_bid_micros": py_binance.bid_usd_micros if py_binance else None,
            "rust_binance_bid_price": rs_binance["bid_price"],
            "python_hl_bid_micros": py_hl.bid_usd_micros if py_hl else None,
            "rust_hl_bid_price": rs_hl["bid_price"],
            "cross_language_match": (
                py_binance is not None
                and py_hl is not None
                and py_binance.bid_usd_micros == round(float(rs_binance["bid_price"]) * 1_000_000)
                and py_hl.bid_usd_micros == round(float(rs_hl["bid_price"]) * 1_000_000)
            ),
        },
        "remote_lakehouse_probe": lakehouse_dict,
        "cex_dex_lead_lag": {
            "approved": lead_lag.approved,
            "gross_spread_usd_micros": lead_lag.gross_spread_usd_micros,
            "worst_case_net_usd_micros": lead_lag.worst_case_net_usd_micros,
            "edge_bps": lead_lag.edge_bps,
        },
        "funding_basis": {
            "approved": basis.approved,
            "net_edge_usd_micros": basis.net_edge_usd_micros,
            "edge_bps": basis.edge_bps,
        },
        "hedge_group_saga": {
            "ack_generated_hedge": len(d_ack) > 0,
            "first_fill_generated_hedge": len(d_fill1) == 1,
            "duplicate_fill_was_noop": len(d_dup) == 0,
            "phantom_delta_state": group.state.value,
            "halt_directives": len(d_halt),
            "halted_cleanly": group.state is HedgeGroupState.HALTED,
        },
    }


def _run_evm_sync_and_liquidation_engine(
    *,
    now_ms: int,
    bridge: FastpathBridge | None = None,
) -> dict[str, Any]:
    active_bridge = bridge if bridge is not None else FastpathBridge()
    weth = _addr("11")
    usdc = _addr("22")
    pool_v2_id = _addr("f1")
    pool_v3_id = _addr("f2")

    head0 = 57_800_000
    hash0 = _hash32(b"radar-evm-sync-b0")
    hash1 = _hash32(b"radar-evm-sync-b1")
    hash2_fork = _hash32(b"radar-evm-sync-b2-fork")
    tick_init = -196_000
    sqrt_init = active_bridge.sqrt_price_at_tick(tick_init)

    sync = IncrementalPoolStateSynchronizer(
        initial_pools=(
            PoolNode(
                chain_id=BASE_CHAIN_ID,
                pool_id=pool_v2_id,
                kind=PoolKind.V2,
                token0=weth,
                token1=usdc,
                decimals0=18,
                decimals1=6,
                fee_pips=3000,
                reserve0=100 * 10**18,
                reserve1=320_000 * 10**6,
                block_number=head0,
                timestamp_ms=now_ms,
            ),
            PoolNode(
                chain_id=BASE_CHAIN_ID,
                pool_id=pool_v3_id,
                kind=PoolKind.V3,
                token0=weth,
                token1=usdc,
                decimals0=18,
                decimals1=6,
                fee_pips=500,
                sqrt_price_x96=sqrt_init,
                tick=tick_init,
                liquidity=10_000_000_000_000_000,
                ticks=(
                    TickLiquidity(tick=-197_000, liquidity_net=10_000_000_000_000_000),
                    TickLiquidity(tick=-195_000, liquidity_net=-10_000_000_000_000_000),
                ),
                block_number=head0,
                timestamp_ms=now_ms,
            ),
        ),
        head_block_number=head0,
        head_block_hash=hash0,
    )

    tick_post = -195_950
    sqrt_post = active_bridge.sqrt_price_at_tick(tick_post)
    events_b1 = (
        EvmPoolLogEvent(
            chain_id=BASE_CHAIN_ID,
            block_number=head0 + 1,
            block_hash=hash1,
            parent_hash=hash0,
            tx_index=0,
            log_index=0,
            pool_id=pool_v2_id,
            topic0=V2_SYNC_TOPIC,
            topics=(V2_SYNC_TOPIC,),
            data_hex="0x" + _u256_word(120 * 10**18) + _u256_word(408_000 * 10**6),
        ),
        EvmPoolLogEvent(
            chain_id=BASE_CHAIN_ID,
            block_number=head0 + 1,
            block_hash=hash1,
            parent_hash=hash0,
            tx_index=1,
            log_index=0,
            pool_id=pool_v3_id,
            topic0=V3_SWAP_TOPIC,
            topics=(V3_SWAP_TOPIC, _topic_addr("aa"), _topic_addr("bb")),
            data_hex=(
                "0x"
                + _i256_word(-(10**18))
                + _i256_word(3_400 * 10**6)
                + _u256_word(sqrt_post)
                + _u256_word(10_000_000_000_000_000)
                + _i256_word(tick_post)
            ),
        ),
    )
    applied_b1 = sync.apply_block_logs(head0 + 1, hash1, hash0, events_b1)
    sync.apply_block_logs(head0 + 2, hash2_fork, hash1, ())

    reorg_detected = False
    try:
        sync.apply_block_logs(head0 + 3, _hash32(b"radar-evm-sync-b3-conflict"), hash1, ())
    except ReorgDetectedError:
        reorg_detected = True
    sync.rollback_to_block(head0 + 1, hash1)

    synced_graph = sync.to_fastpath_graph(bridge=active_bridge)
    oracle_weth_in_usdc_wad = 3_150 * 10**6  # 1e18 WETH = 3,150e6 USDC
    positions = (
        BorrowerPositionSnapshot(
            protocol="morpho_blue",
            market_id=_hash32(b"morpho-weth-usdc-base"),
            borrower=_addr("71"),
            debt_token=usdc,
            collateral_token=weth,
            debt_amount_raw=20_000 * 10**6,
            collateral_amount_raw=8 * 10**18,
            health_factor_wad=92 * 10**16,
            close_factor_bps=5_000,
            liquidation_bonus_bps=500,
            oracle_price_collateral_in_debt_wad=oracle_weth_in_usdc_wad,
            block_number=head0 + 1,
            block_hash=hash1,
        ),
        BorrowerPositionSnapshot(
            protocol="aave_v3",
            market_id=_addr("82"),
            borrower=_addr("72"),
            debt_token=usdc,
            collateral_token=weth,
            debt_amount_raw=5_000 * 10**6,
            collateral_amount_raw=5 * 10**18,
            health_factor_wad=115 * 10**16,  # healthy -> skipped
            close_factor_bps=5_000,
            liquidation_bonus_bps=500,
            oracle_price_collateral_in_debt_wad=oracle_weth_in_usdc_wad,
            block_number=head0 + 1,
            block_hash=hash1,
        ),
    )
    liq_candidates = FlashLiquidationScanner.scan(
        positions,
        synced_graph,
        FlashLiquidationConfig(
            flash_fee_bps=0,
            gas_cost_in_debt_raw=100_000,
            min_net_profit_debt_raw=1_000_000,
            max_repay_debt_raw=15_000 * 10**6,
        ),
    )
    top_liq = liq_candidates[0] if liq_candidates else None
    return {
        "head_block_number": sync.head_block_number,
        "head_block_hash": sync.head_block_hash,
        "sync_events_applied": applied_b1,
        "reorg_detected": reorg_detected,
        "rollback_restored_head": sync.head_block_number == head0 + 1
        and sync.head_block_hash == hash1,
        "positions_evaluated": len(positions),
        "liquidation_candidates_found": len(liq_candidates),
        "top_liquidation": (
            {
                "protocol": top_liq.protocol,
                "borrower": top_liq.borrower,
                "repay_debt_raw": top_liq.repay_debt_raw,
                "seized_collateral_raw": top_liq.seized_collateral_raw,
                "swap_pool_id": top_liq.swap_pool_id,
                "swap_out_debt_raw": top_liq.swap_out_debt_raw,
                "net_profit_debt_raw": top_liq.net_profit_debt_raw,
                "execution_eligible": top_liq.execution_eligible,
            }
            if top_liq is not None
            else None
        ),
        "execution_eligible": False,
    }


def _synthetic_base_time_transport(now_ms: int) -> Callable[[str, bytes, float], bytes]:
    sec = now_ms // 1000
    millis_part = 400
    ts_ms = sec * 1000 + millis_part
    block_num = 57_800_000
    block_hash = _hash32(b"radar-basetime-synthetic-block")

    def _transport(_url: str, body: bytes, _timeout: float) -> bytes:
        decoded = json.loads(body.decode("utf-8"))
        if isinstance(decoded, dict):
            return json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": decoded["id"],
                    "result": {
                        "number": hex(block_num),
                        "hash": block_hash,
                        "timestamp": hex(sec),
                        "timestampMs": hex(ts_ms),
                    },
                }
            ).encode("utf-8")
        return json.dumps(
            [
                {"jsonrpc": "2.0", "id": 2, "result": "0x" + _u256_word(ts_ms)},
                {"jsonrpc": "2.0", "id": 3, "result": "0x" + _u256_word(millis_part)},
            ]
        ).encode("utf-8")

    return _transport


def _run_base_time_predeploy_engine(
    *,
    mode: str,
    now_ms: int,
    base_rpc_url: str,
    transport: Callable[[str, bytes, float], bytes] | None = None,
) -> dict[str, Any]:
    active_transport = (
        transport
        if transport is not None
        else (_synthetic_base_time_transport(now_ms) if mode == "synthetic" else None)
    )
    try:
        receipt = verify_base_time_predeploy(
            base_rpc_url,
            block_tag="latest",
            timeout_s=8.0,
            max_requests=4,
            transport=active_transport,
        )
        return {
            "status": "synthetic_verified" if mode == "synthetic" else "ok",
            "predeploy_address": BASE_TIME,
            "chain_id": receipt.chain_id,
            "rpc_origin": receipt.rpc_origin,
            "block_number": receipt.block_number,
            "block_hash": receipt.block_hash,
            "header_timestamp": receipt.header_timestamp,
            "header_timestamp_ms": receipt.header_timestamp_ms,
            "predeploy_timestamp_ms": receipt.predeploy_timestamp_ms,
            "predeploy_millis_part": receipt.predeploy_millis_part,
            "header_matches_predeploy": (
                receipt.header_timestamp_ms == receipt.predeploy_timestamp_ms
                if receipt.header_timestamp_ms is not None
                else None
            ),
            "requests_used": receipt.requests_used,
            "max_requests": receipt.max_requests,
            "header_request_sha256": receipt.header_request_sha256,
            "header_response_sha256": receipt.header_response_sha256,
            "batch_request_sha256": receipt.batch_request_sha256,
            "batch_response_sha256": receipt.batch_response_sha256,
            "error": None,
            "execution_eligible": False,
        }
    except (RpcError, OSError, ValueError) as exc:
        return {
            "status": "error",
            "predeploy_address": BASE_TIME,
            "chain_id": BASE_CHAIN_ID,
            "rpc_origin": base_rpc_url,
            "block_number": None,
            "block_hash": None,
            "header_timestamp": None,
            "header_timestamp_ms": None,
            "predeploy_timestamp_ms": None,
            "predeploy_millis_part": None,
            "header_matches_predeploy": None,
            "requests_used": 0,
            "max_requests": 4,
            "header_request_sha256": None,
            "header_response_sha256": None,
            "batch_request_sha256": None,
            "batch_response_sha256": None,
            "error": str(exc),
            "execution_eligible": False,
        }


def _run_stage7_portfolio_allocator_engine() -> dict[str, Any]:
    allocator = PortfolioAllocator(
        PortfolioAllocatorConfig(
            min_lcb_edge_bps=5,
            max_tail_loss_99_bps=1_500,
            max_single_strategy_share_bps=5_000,
        )
    )
    reserves = PortfolioReserves(
        lp_unwind_gas_usd_micros=25_000_000,
        perp_margin_buffer_usd_micros=50_000_000,
        incident_cash_usd_micros=50_000_000,
        ops_reserve_usd_micros=25_000_000,
    )
    domain_caps = (
        ExposureDomainCap("chain", "base", 300_000_000),
        ExposureDomainCap("sequencer", "base", 300_000_000),
        ExposureDomainCap("stablecoin", "usdc", 320_000_000),
        ExposureDomainCap("venue", "uniswapx", 175_000_000),
    )
    tracks = (
        StrategyTrackCandidate(
            strategy_id="base_dutch_v3_filler_shadow",
            stage=3,
            current_allocation_usd_micros=100_000_000,
            capacity_usd_micros=200_000_000,
            max_step_up_usd_micros=75_000_000,
            lcb_net_edge_bps=24,
            expected_drawdown_bps=120,
            tail_loss_99_bps=350,
            evidence_age_seconds=120,
            max_evidence_age_seconds=900,
            exposures=(("chain", "base"), ("sequencer", "base"), ("venue", "uniswapx")),
        ),
        StrategyTrackCandidate(
            strategy_id="base_v4_atomic_liq_shadow",
            stage=2,
            current_allocation_usd_micros=50_000_000,
            capacity_usd_micros=180_000_000,
            max_step_up_usd_micros=75_000_000,
            lcb_net_edge_bps=18,
            expected_drawdown_bps=150,
            tail_loss_99_bps=400,
            evidence_age_seconds=60,
            max_evidence_age_seconds=900,
            exposures=(("chain", "base"), ("sequencer", "base"), ("stablecoin", "usdc")),
        ),
        StrategyTrackCandidate(
            strategy_id="aleph_cross_venue_basis_shadow",
            stage=4,
            current_allocation_usd_micros=50_000_000,
            capacity_usd_micros=150_000_000,
            max_step_up_usd_micros=60_000_000,
            lcb_net_edge_bps=14,
            expected_drawdown_bps=180,
            tail_loss_99_bps=500,
            evidence_age_seconds=45,
            max_evidence_age_seconds=900,
            exposures=(("stablecoin", "usdc"), ("venue", "hyperliquid")),
        ),
        StrategyTrackCandidate(
            strategy_id="base_launchpad_unvetted_legacy",
            stage=6,
            current_allocation_usd_micros=25_000_000,
            capacity_usd_micros=100_000_000,
            max_step_up_usd_micros=10_000_000,
            lcb_net_edge_bps=-15,
            expected_drawdown_bps=600,
            tail_loss_99_bps=2_500,
            evidence_age_seconds=100,
            max_evidence_age_seconds=900,
            exposures=(("chain", "base"),),
        ),
    )
    report = allocator.allocate(
        total_nav_usd_micros=500_000_000,
        reserves=reserves,
        tracks=tracks,
        domain_caps=domain_caps,
    )
    return {
        "total_nav_usd_micros": report.total_nav_usd_micros,
        "segregated_reserves_usd_micros": report.segregated_reserves_usd_micros,
        "deployable_budget_usd_micros": report.deployable_budget_usd_micros,
        "allocated_total_usd_micros": report.allocated_total_usd_micros,
        "unallocated_cash_usd_micros": report.unallocated_cash_usd_micros,
        "reserve_deficit_usd_micros": report.reserve_deficit_usd_micros,
        "domain_utilization_usd_micros": report.domain_utilization_usd_micros,
        "report_sha256": report.report_sha256,
        "decisions": [
            {
                **asdict(d),
                "reasons": list(d.reasons),
                "binding_constraints": list(d.binding_constraints),
            }
            for d in report.decisions
        ],
        "execution_eligible": report.execution_eligible,
    }


def run_radar(
    *,
    mode: str,
    out_dir: Path,
    ssh_host: str = "aleph",
    eqn_channel: str = "all",
    eqn_limit: int = 20,
    now_ms: int | None = None,
    bench_iters: int = 50,
    base_rpc_url: str = DEFAULT_BASE_RPC_URL,
    bridge: FastpathBridge | None = None,
    eqn_client: EqnClient | None = None,
    base_time_transport: Callable[[str, bytes, float], bytes] | None = None,
    uniswapx_client: UniswapXOrderbookClient | None = None,
) -> dict[str, Any]:
    """Run all Oasis-Agent subsystems into an immutable read-only/shadow directory."""
    if mode not in {"synthetic", "public-shadow"}:
        raise ValueError(f"unsupported mode: {mode!r}")
    if out_dir.is_symlink():
        raise ValueError(f"refusing symlink output directory: {out_dir}")
    out_dir.mkdir(parents=True, exist_ok=True)
    for fname in ("radar_report.json", "evidence_manifest.json", "aleph_shm_v12_probe.bin"):
        if (out_dir / fname).exists():
            raise FileExistsError(f"refusing to overwrite existing output file: {fname}")

    obs_ms = int(time.time() * 1000) if now_ms is None else now_ms
    if type(obs_ms) is not int or obs_ms <= 0:
        raise ValueError("now_ms must be a positive integer")

    rust_report = _run_rust_and_executor_engine(
        bench_iters=bench_iters, now_ms=obs_ms, bridge=bridge
    )
    evm_sync_liq_report = _run_evm_sync_and_liquidation_engine(now_ms=obs_ms, bridge=bridge)
    base_time_report = _run_base_time_predeploy_engine(
        mode=mode,
        now_ms=obs_ms,
        base_rpc_url=base_rpc_url,
        transport=base_time_transport,
    )
    eqn_report, eqn_raw_bytes = _run_eqn_and_launchpad_engine(
        mode=mode,
        now_ms=obs_ms,
        eqn_channel=eqn_channel,
        eqn_limit=eqn_limit,
        eqn_client=eqn_client,
    )
    intent_report = _run_uniswapx_intent_engine(
        mode=mode,
        now_ms=obs_ms,
        uniswapx_client=uniswapx_client,
    )
    cross_venue_report = _run_cross_venue_and_aleph_engine(
        mode=mode,
        out_dir=out_dir,
        now_ms=obs_ms,
        ssh_host=ssh_host,
        bridge=bridge,
    )
    portfolio_report = _run_stage7_portfolio_allocator_engine()

    if eqn_raw_bytes is not None and not (out_dir / "eqn_snapshot.json").exists():
        _write_new(out_dir / "eqn_snapshot.json", eqn_raw_bytes)

    report: dict[str, Any] = {
        "schema_version": RADAR_SCHEMA_VERSION,
        "mode": mode,
        "observed_at_ms": obs_ms,
        "execution_eligible": False,
        "rust_fastpath_and_atomic_executor": rust_report,
        "evm_log_state_sync_and_liquidation": evm_sync_liq_report,
        "base_time_predeploy_verification": base_time_report,
        "eqn_intelligence_and_launchpad_gate": eqn_report,
        "uniswapx_dutch_v3_intent_shadow": intent_report,
        "aleph_cross_venue_and_saga": cross_venue_report,
        "stage7_portfolio_allocator": portfolio_report,
    }
    report_bytes = canonical_json(report) + b"\n"
    report_sha256 = sha256_hex(report_bytes)
    _write_new(out_dir / "radar_report.json", report_bytes)

    manifest = {
        "schema_version": "oasis-opportunity-radar-manifest-v1",
        "mode": mode,
        "observed_at_ms": obs_ms,
        "execution_eligible": False,
        "radar_report_sha256": report_sha256,
        "aleph_shm_probe_sha256": cross_venue_report["aleph_shm_v12"]["file_sha256"],
        "eqn_raw_sha256": eqn_report["eqn_news"]["raw_sha256"],
        "base_time_batch_response_sha256": base_time_report["batch_response_sha256"],
        "portfolio_allocation_sha256": portfolio_report["report_sha256"],
    }
    _write_new(out_dir / "evidence_manifest.json", canonical_json(manifest) + b"\n")
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("synthetic", "public-shadow"),
        default="synthetic",
        help="Offline synthetic mode or bounded read-only public-shadow mode.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        required=True,
        help="Output directory for immutable radar_report.json and evidence_manifest.json.",
    )
    parser.add_argument("--ssh-host", default="aleph", help="Read-only AlephTX WSL host alias.")
    parser.add_argument("--eqn-channel", default="all", help="Equation News channel or 'all'.")
    parser.add_argument("--eqn-limit", type=int, default=20, help="EQN items limit (1..100).")
    parser.add_argument("--now-ms", type=int, default=None, help="Explicit ms timestamp.")
    parser.add_argument("--bench-iters", type=int, default=50, help="Differential bench iters.")
    parser.add_argument(
        "--base-rpc-url",
        default=DEFAULT_BASE_RPC_URL,
        help="Read-only Base HTTPS JSON-RPC URL for bounded BaseTime predeploy verification.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        manifest = run_radar(
            mode=args.mode,
            out_dir=args.out,
            ssh_host=args.ssh_host,
            eqn_channel=args.eqn_channel,
            eqn_limit=args.eqn_limit,
            now_ms=args.now_ms,
            bench_iters=args.bench_iters,
            base_rpc_url=args.base_rpc_url,
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError, EqnFetchError) as exc:
        print(f"opportunity radar failed: {exc}", file=sys.stderr)
        return 2
    print(
        f"opportunity-radar mode={manifest['mode']} "
        f"report_sha256={manifest['radar_report_sha256']} execution_eligible=false"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
