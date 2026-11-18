import json
import os
import random
import subprocess
import time
from pathlib import Path

import pytest

from oasis_agent.fastpath import (
    FastpathBridge,
    FastpathCallError,
    FastpathPoolGraph,
    FastpathUnavailableError,
    PoolKind,
    PoolNode,
    TickLiquidity,
    quote_pool,
    scan_cycles,
)
from oasis_agent.uniswap_v4 import liquidity_math

REPO_ROOT = Path(__file__).resolve().parents[2]


def _addr(byte_hex: str) -> str:
    return "0x" + byte_hex * 20


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
    lib_path = _ensure_release_cdylib()
    return FastpathBridge(lib_path=lib_path)


def test_bridge_missing_library_raises_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "missing_oasis_core.dylib"
    monkeypatch.setenv("OASIS_CORE_LIB_PATH", str(missing))
    with pytest.raises(FastpathUnavailableError):
        FastpathBridge()


def test_differential_tick_and_liquidity_math_bit_exact(bridge: FastpathBridge) -> None:
    ping = bridge.ping()
    assert ping["service"] == "oasis-core"

    boundary_ticks = [-887_272, -443_636, -1, 0, 1, 443_636, 887_272]
    rng = random.Random(20261118)
    random_ticks = [
        rng.randint(liquidity_math.MIN_TICK, liquidity_math.MAX_TICK) for _ in range(200)
    ]

    for tick in boundary_ticks + random_ticks:
        expected_sqrt = liquidity_math.sqrt_price_at_tick(tick)
        actual_sqrt = bridge.sqrt_price_at_tick(tick)
        assert actual_sqrt == expected_sqrt, f"tick={tick} mismatch"

    for tick in (liquidity_math.MIN_TICK - 1, liquidity_math.MAX_TICK + 1):
        with pytest.raises(FastpathCallError):
            bridge.sqrt_price_at_tick(tick)

    for idx in range(200):
        tick_a = rng.randint(liquidity_math.MIN_TICK, liquidity_math.MAX_TICK)
        tick_b = rng.randint(liquidity_math.MIN_TICK, liquidity_math.MAX_TICK)
        sqrt_a = liquidity_math.sqrt_price_at_tick(tick_a)
        sqrt_b = liquidity_math.sqrt_price_at_tick(tick_b)
        liq = rng.randint(1, (1 << 110) - 1) if idx % 10 != 0 else 0

        for round_up in (False, True):
            exp0 = liquidity_math.amount0_delta(sqrt_a, sqrt_b, liq, round_up=round_up)
            exp1 = liquidity_math.amount1_delta(sqrt_a, sqrt_b, liq, round_up=round_up)
            act0, act1 = bridge.amount_deltas(sqrt_a, sqrt_b, liq, round_up=round_up)
            assert act0 == exp0, f"amount0 mismatch case={idx} round_up={round_up}"
            assert act1 == exp1, f"amount1 mismatch case={idx} round_up={round_up}"

            lo, hi = sorted((sqrt_a, sqrt_b))
            if lo < hi:
                mid_tick = rng.randint(liquidity_math.MIN_TICK, liquidity_math.MAX_TICK)
                mid_sqrt = liquidity_math.sqrt_price_at_tick(mid_tick)
                exp_pair = liquidity_math.amounts_for_liquidity(
                    mid_sqrt, lo, hi, liq, round_up=round_up
                )
                act_pair = bridge.amounts_for_liquidity(mid_sqrt, lo, hi, liq, round_up=round_up)
                assert act_pair == exp_pair


def _base_four_pool_universe(bridge: FastpathBridge) -> tuple[list[PoolNode], str]:
    weth = _addr("11")
    usdc = _addr("22")
    usdt = _addr("33")

    tick_v3 = -196_258
    tick_ss = -195_770
    sqrt_v3 = bridge.sqrt_price_at_tick(tick_v3)
    sqrt_ss = bridge.sqrt_price_at_tick(tick_ss)

    pools = [
        # Pool 1: V3 WETH/USDC (~3000 USDC/WETH)
        PoolNode(
            chain_id=8453,
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
            block_number=21_000_000,
            timestamp_ms=1_760_000_000_000,
        ),
        # Pool 2: AerodromeStable USDC/USDT (slightly imbalanced in favor of USDC->USDT)
        PoolNode(
            chain_id=8453,
            pool_id=_addr("a2"),
            kind=PoolKind.AERODROME_STABLE,
            token0=usdc,
            token1=usdt,
            decimals0=6,
            decimals1=6,
            fee_pips=100,
            reserve0=5_000_000_000_000,
            reserve1=5_050_000_000_000,
            block_number=21_000_000,
            timestamp_ms=1_760_000_000_000,
        ),
        # Pool 3: Slipstream WETH/USDC (~3150 USDC/WETH)
        PoolNode(
            chain_id=8453,
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
            block_number=21_000_000,
            timestamp_ms=1_760_000_000_000,
        ),
        # Pool 4: V2 WETH/USDT (3250 USDT/WETH)
        PoolNode(
            chain_id=8453,
            pool_id=_addr("a4"),
            kind=PoolKind.V2,
            token0=weth,
            token1=usdt,
            decimals0=18,
            decimals1=6,
            fee_pips=3000,
            reserve0=100 * 10**18,
            reserve1=325_000 * 10**6,
            block_number=21_000_000,
            timestamp_ms=1_760_000_000_000,
        ),
    ]
    return pools, weth


def test_single_pool_quotes_and_cl_exhaustion(bridge: FastpathBridge) -> None:
    pools, weth = _base_four_pool_universe(bridge)
    v2_pool = pools[3]
    amount_in = 10**18
    q = quote_pool(v2_pool, weth, amount_in, bridge=bridge)
    assert v2_pool.reserve0 is not None
    assert v2_pool.reserve1 is not None
    num = amount_in * (1_000_000 - v2_pool.fee_pips) * v2_pool.reserve1
    den = v2_pool.reserve0 * 1_000_000 + amount_in * (1_000_000 - v2_pool.fee_pips)
    assert q.amount_out == num // den

    # Exhausting CL liquidity beyond all ticks fails closed
    exhausted_v4 = PoolNode(
        chain_id=8453,
        pool_id=_addr("a9"),
        kind=PoolKind.V4,
        token0=_addr("11"),
        token1=_addr("22"),
        decimals0=18,
        decimals1=6,
        fee_pips=500,
        sqrt_price_x96=bridge.sqrt_price_at_tick(0),
        tick=0,
        liquidity=10**12,
        ticks=(TickLiquidity(tick=60, liquidity_net=-(10**12)),),
    )
    with pytest.raises(FastpathCallError, match="insufficient pool liquidity"):
        quote_pool(exhausted_v4, _addr("22"), 10**18, bridge=bridge)


def test_four_pool_base_cycle_scan_and_rejection(bridge: FastpathBridge) -> None:
    pools, weth = _base_four_pool_universe(bridge)
    graph = FastpathPoolGraph(bridge=bridge)
    for pool in pools:
        graph.upsert_pool(pool)

    gas_l2 = 2 * 10**14  # 0.0002 WETH
    gas_l1 = 5 * 10**13  # 0.00005 WETH
    min_profit = 10**14  # 0.0001 WETH

    report = graph.scan_cycles(
        [weth],
        max_hops=3,
        min_amount_in=10**16,
        max_amount_in=5 * 10**18,
        gas_cost_in_base=gas_l2,
        l1_fee_in_base=gas_l1,
        min_net_profit=min_profit,
    )

    assert report.cycles_evaluated == 6
    assert len(report.candidates) >= 1

    best = report.candidates[0]
    assert best.chain_id == 8453
    assert best.base_token == weth
    assert best.net_profit >= min_profit
    assert best.amount_out - best.optimal_amount_in - gas_l2 - gas_l1 == best.net_profit
    assert best.hops[0].amount_in == best.optimal_amount_in
    assert best.hops[-1].amount_out == best.amount_out
    for prev_hop, next_hop in zip(best.hops[:-1], best.hops[1:], strict=True):
        assert prev_hop.token_out == next_hop.token_in
        assert prev_hop.amount_out == next_hop.amount_in

    # Confirm optimal_amount_in beats or equals local +/- 1% perturbations
    for pct in (99, 101):
        perturbed_in = (best.optimal_amount_in * pct) // 100
        if perturbed_in >= 10**16:
            cur_amt = perturbed_in
            for hop in best.hops:
                q = graph.quote_pool(best.chain_id, hop.pool_id, hop.token_in, cur_amt)
                cur_amt = q.amount_out
            perturbed_net = cur_amt - perturbed_in - gas_l2 - gas_l1
            assert best.net_profit >= perturbed_net

    # Negative-EV rejection when L1 + L2 gas exceeds gross edge
    rejected = scan_cycles(
        pools,
        [weth],
        max_hops=3,
        min_amount_in=10**16,
        max_amount_in=5 * 10**18,
        gas_cost_in_base=2 * 10**18,
        l1_fee_in_base=10**18,
        min_net_profit=1,
        bridge=bridge,
    )
    assert rejected.candidates == ()


def test_ffi_scan_microbenchmark(bridge: FastpathBridge) -> None:
    pools, weth = _base_four_pool_universe(bridge)
    t0 = time.perf_counter_ns()
    last_report = None
    for _ in range(25):
        last_report = scan_cycles(
            pools,
            [weth],
            max_hops=3,
            min_amount_in=10**16,
            max_amount_in=5 * 10**18,
            gas_cost_in_base=2 * 10**14,
            l1_fee_in_base=5 * 10**13,
            min_net_profit=10**14,
            bridge=bridge,
        )
    wall_ns = time.perf_counter_ns() - t0
    assert last_report is not None
    assert last_report.cycles_evaluated == 6
    assert len(last_report.candidates) >= 1
    assert last_report.elapsed_ns > 0
    assert wall_ns > 0


def test_oasis_fastpath_cli_benchmark_and_scan(tmp_path: Path, bridge: FastpathBridge) -> None:
    bin_path = REPO_ROOT / "crates" / "target" / "release" / "oasis-fastpath"
    assert bin_path.is_file()

    bench = subprocess.run(
        [str(bin_path), "benchmark", "--iterations", "10", "--hops", "3"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    bench_json = json.loads(bench.stdout)
    assert bench_json["iterations"] == 10
    assert bench_json["cycles_evaluated_per_scan"] == 6
    assert bench_json["candidates_found"] >= 1
    assert int(bench_json["top_net_profit"]) > 0
    assert bench_json["ns_per_scan"] > 0

    pools, weth = _base_four_pool_universe(bridge)
    scan_input = tmp_path / "scan_input.json"
    scan_input.write_text(
        json.dumps(
            {
                "pools": [p.to_wire() for p in pools],
                "base_tokens": [weth],
                "max_hops": 3,
                "min_amount_in": str(10**16),
                "max_amount_in": str(5 * 10**18),
                "gas_cost_in_base": str(2 * 10**14),
                "l1_fee_in_base": str(5 * 10**13),
                "min_net_profit": str(10**14),
            }
        )
    )
    scan_proc = subprocess.run(
        [str(bin_path), "scan", "--input", str(scan_input)],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    scan_json = json.loads(scan_proc.stdout)
    assert scan_json["cycles_evaluated"] == 6
    assert len(scan_json["candidates"]) >= 1
    assert int(scan_json["candidates"][0]["net_profit"]) >= 10**14
