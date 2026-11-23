import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from oasis_agent.fastpath import (
    AB_EXPERIMENT_SCHEMA,
    BlockPoolSnapshot,
    FastpathBridge,
    IncrementalCycleScanner,
    PoolKind,
    PoolNode,
    TickLiquidity,
    build_benchmark_34pool_universe,
    run_multihop_ab_experiment,
    scan_cycles,
)
from oasis_agent.fastpath import incremental_scanner as scanner_mod
from oasis_agent.fastpath.pool_graph import CycleCandidate, CycleScanReport
from scripts import run_multihop_ab_experiment as ab_cli

REPO_ROOT = Path(__file__).resolve().parents[2]


def _addr(byte_hex: str) -> str:
    return "0x" + byte_hex * 20


def _ensure_release_cdylib() -> Path:
    crates_dir = REPO_ROOT / "crates"
    release_lib = crates_dir / "target" / "release" / "liboasis_core.dylib"
    if release_lib.is_file():
        return release_lib
    so_lib = crates_dir / "target" / "release" / "liboasis_core.so"
    if so_lib.is_file():
        return so_lib
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
    return release_lib if release_lib.is_file() else so_lib


@pytest.fixture(scope="module")
def bridge() -> FastpathBridge:
    return FastpathBridge(lib_path=_ensure_release_cdylib())


def _sample_six_pool_universe(bridge: FastpathBridge) -> tuple[list[PoolNode], str, str]:
    weth = _addr("11")
    usdc = _addr("22")
    usdt = _addr("33")
    aero = _addr("44")
    sqrt_v3 = bridge.sqrt_price_at_tick(-196_258)
    sqrt_ss = bridge.sqrt_price_at_tick(-195_770)
    sqrt_v4 = bridge.sqrt_price_at_tick(283_255)

    pools = [
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
            tick=-196_258,
            liquidity=20_000_000_000_000_000,
            ticks=(
                TickLiquidity(tick=-196_800, liquidity_net=10_000_000_000_000_000),
                TickLiquidity(tick=-195_800, liquidity_net=-10_000_000_000_000_000),
            ),
            block_number=21_000_000,
            timestamp_ms=1_760_000_000_000,
        ),
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
            tick=-195_770,
            liquidity=20_000_000_000_000_000,
            ticks=(
                TickLiquidity(tick=-196_500, liquidity_net=5_000_000_000_000_000),
                TickLiquidity(tick=-195_200, liquidity_net=-5_000_000_000_000_000),
            ),
            block_number=21_000_000,
            timestamp_ms=1_760_000_000_000,
        ),
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
            reserve1=325_000_000_000,
            block_number=21_000_000,
            timestamp_ms=1_760_000_000_000,
        ),
        PoolNode(
            chain_id=8453,
            pool_id="0x" + "a5" * 32,
            kind=PoolKind.V4,
            token0=usdc,
            token1=aero,
            decimals0=6,
            decimals1=18,
            fee_pips=500,
            sqrt_price_x96=sqrt_v4,
            tick=283_255,
            liquidity=10**21,
            block_number=21_000_000,
            timestamp_ms=1_760_000_000_000,
        ),
        PoolNode(
            chain_id=8453,
            pool_id=_addr("a6"),
            kind=PoolKind.V2,
            token0=weth,
            token1=aero,
            decimals0=18,
            decimals1=18,
            fee_pips=3000,
            reserve0=50 * 10**18,
            reserve1=290_000 * 10**18,
            block_number=21_000_000,
            timestamp_ms=1_760_000_000_000,
        ),
    ]
    return pools, weth, aero


def test_incremental_scanner_exact_parity_and_cache_hit(bridge: FastpathBridge) -> None:
    for dedup in (False, True):
        pools, weth, aero = _sample_six_pool_universe(bridge)
        scanner = IncrementalCycleScanner(bridge=bridge, full_scan_every_n_blocks=100)
        bases = [weth, aero]
        scan_kwargs: dict[str, Any] = {
            "max_hops": 4,
            "min_amount_in": 10**16,
            "max_amount_in": 5 * 10**18,
            "gas_cost_in_base": 10**14,
            "l1_fee_in_base": 5 * 10**13,
            "min_net_profit": 10**14,
            "per_hop_gas_cost_in_base": 15 * 10**13,
            "v2_hop_gas_cost_in_base": 15 * 10**13,
            "cl_hop_gas_cost_in_base": 25 * 10**13,
            "deduplicate_cycles": dedup,
        }

        # Block 0: initial full scan.
        res0 = scanner.scan_block(pools, bases, block_number=21_000_000, **scan_kwargs)
        full0 = scan_cycles(pools, bases, bridge=bridge, **scan_kwargs)
        assert res0.used_full_scan is True
        assert res0.fallback_reason == "initial_scan"
        assert res0.candidates == full0.candidates

        # Block 1: only block_number/timestamp_ms advance -> 0 changed pools -> pure cache hit.
        pools_b1 = [
            PoolNode(
                chain_id=p.chain_id,
                pool_id=p.pool_id,
                kind=p.kind,
                token0=p.token0,
                token1=p.token1,
                decimals0=p.decimals0,
                decimals1=p.decimals1,
                fee_pips=p.fee_pips,
                reserve0=p.reserve0,
                reserve1=p.reserve1,
                sqrt_price_x96=p.sqrt_price_x96,
                tick=p.tick,
                liquidity=p.liquidity,
                ticks=p.ticks,
                block_number=21_000_001,
                timestamp_ms=1_760_000_000_200,
            )
            for p in pools
        ]
        res1 = scanner.scan_block(pools_b1, bases, block_number=21_000_001, **scan_kwargs)
        assert res1.used_full_scan is False
        assert res1.changed_pools == ()
        assert res1.cycles_evaluated == 0
        assert res1.retained_cached_candidates == len(full0.candidates)
        assert res1.candidates == full0.candidates

        # Block 2: mutate Pool 6 (WETH/AERO V2) -> retains non-Pool-6 candidates, rescan subset.
        pools_b2 = list(pools_b1)
        old_p6 = pools_b2[5]
        assert old_p6.reserve1 is not None
        pools_b2[5] = PoolNode(
            chain_id=old_p6.chain_id,
            pool_id=old_p6.pool_id,
            kind=old_p6.kind,
            token0=old_p6.token0,
            token1=old_p6.token1,
            decimals0=old_p6.decimals0,
            decimals1=old_p6.decimals1,
            fee_pips=old_p6.fee_pips,
            reserve0=old_p6.reserve0,
            reserve1=old_p6.reserve1 + 5_000 * 10**18,
            block_number=21_000_002,
            timestamp_ms=1_760_000_000_400,
        )
        res2 = scanner.scan_block(pools_b2, bases, block_number=21_000_002, **scan_kwargs)
        full2 = scan_cycles(pools_b2, bases, bridge=bridge, **scan_kwargs)
        assert res2.used_full_scan is False
        assert res2.changed_pools == (_addr("a6"),)
        assert res2.cycles_evaluated < full2.cycles_evaluated
        assert res2.retained_cached_candidates > 0
        assert res2.candidates == full2.candidates


def test_incremental_scanner_fallback_triggers_and_checkpoint(bridge: FastpathBridge) -> None:
    pools, weth, aero = _sample_six_pool_universe(bridge)
    scanner = IncrementalCycleScanner(bridge=bridge, full_scan_every_n_blocks=3)
    bases = [weth, aero]
    base_kwargs: dict[str, Any] = {
        "max_hops": 4,
        "min_amount_in": 10**16,
        "max_amount_in": 5 * 10**18,
        "gas_cost_in_base": 2 * 10**14,
        "l1_fee_in_base": 5 * 10**13,
        "min_net_profit": 10**14,
        "deduplicate_cycles": True,
    }

    r0 = scanner.scan_block(pools, bases, **base_kwargs)
    assert r0.fallback_reason == "initial_scan"

    r1 = scanner.scan_block(pools, bases, **base_kwargs)
    assert r1.used_full_scan is False

    r2 = scanner.scan_block(pools, bases, **base_kwargs)
    assert r2.used_full_scan is False

    # Step 3 reaches periodic checkpoint (`_steps_since_full_scan + 1 >= 3`).
    r3 = scanner.scan_block(pools, bases, **base_kwargs)
    assert r3.used_full_scan is True
    assert r3.fallback_reason == "periodic_checkpoint"

    # Parameter change (higher gas_cost_in_base) forces full scan.
    r4 = scanner.scan_block(
        pools,
        bases,
        **{**base_kwargs, "gas_cost_in_base": 3 * 10**14},
    )
    assert r4.used_full_scan is True
    assert r4.fallback_reason == "scan_parameters_changed"
    full4 = scan_cycles(
        pools, bases, bridge=bridge, **{**base_kwargs, "gas_cost_in_base": 3 * 10**14}
    )
    assert r4.candidates == full4.candidates

    # Topology change (removing one pool) forces full scan.
    r5 = scanner.scan_block(
        pools[:-1],
        bases,
        **{**base_kwargs, "gas_cost_in_base": 3 * 10**14},
    )
    assert r5.used_full_scan is True
    assert r5.fallback_reason == "pool_topology_changed"

    # Explicit force_full_scan overrides incremental path.
    r6 = scanner.scan_block(
        pools[:-1],
        bases,
        force_full_scan=True,
        **{**base_kwargs, "gas_cost_in_base": 3 * 10**14},
    )
    assert r6.used_full_scan is True
    assert r6.fallback_reason == "explicit_force_full_scan"


def test_multihop_ab_experiment_parity_and_cli_execution(
    tmp_path: Path, bridge: FastpathBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    pools, bases = build_benchmark_34pool_universe(bridge)
    assert len(pools) == 34

    # Execute both scans, with fixed timings so a shared runner cannot change the gate result.
    with monkeypatch.context() as timing:
        timing.setattr(scanner_mod, "_time_scan", lambda scan: (scan(), 1_000_000))
        report = run_multihop_ab_experiment(
            num_blocks=25,
            changed_pools_per_block=1,
            max_hops=4,
            full_scan_every_n_blocks=50,
            min_p99_reduction_ratio=0.0,
            seed=20261122,
            bridge=bridge,
        )
    assert report.schema == AB_EXPERIMENT_SCHEMA
    assert report.execution_eligible is False
    assert report.release_permitted is False
    assert report.total_blocks == 25
    assert report.exact_bit_for_bit_blocks == 25
    assert report.recall == 1.0
    assert report.missed_candidates_count == 0
    assert report.missed_net_profit_raw == 0
    assert report.false_positive_count == 0
    assert report.arm_b.full_scan_blocks == 1
    assert report.arm_b.incremental_blocks == 24
    assert report.cycles_evaluated_reduction_ratio >= 0.75
    assert report.p99_reduction_ratio == 0.0
    assert report.hypothesis_a_passed is True

    # Verify CLI --out output & --require-pass exit code 2 on impossible reduction threshold.
    out_json = tmp_path / "ab_report.json"
    rc_ok = ab_cli.main(
        [
            "--blocks",
            "6",
            "--changed-pools-per-block",
            "1",
            "--max-hops",
            "3",
            "--out",
            str(out_json),
        ]
    )
    assert rc_ok == 0
    payload = json.loads(out_json.read_text(encoding="utf-8"))
    assert payload["schema"] == AB_EXPERIMENT_SCHEMA
    assert payload["exact_bit_for_bit_blocks"] == 6
    assert payload["recall"] == 1.0
    assert payload["execution_eligible"] is False
    assert payload["release_permitted"] is False

    # Refuse symlink --out fail-closed.
    sym_out = tmp_path / "sym_ab.json"
    sym_out.symlink_to(out_json)
    with pytest.raises(ValueError, match="symlink"):
        ab_cli.main(["--blocks", "2", "--out", str(sym_out)])

    # Check exit code 2 when --require-pass fails.
    impossible_proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.run_multihop_ab_experiment",
            "--blocks",
            "4",
            "--min-p99-reduction",
            "0.999",
            "--require-pass",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert impossible_proc.returncode == 2
    impossible_payload = json.loads(impossible_proc.stdout)
    assert impossible_payload["hypothesis_a_passed"] is False

    # Custom BlockPoolSnapshot list path.
    custom_snap = BlockPoolSnapshot(
        block_number=1,
        timestamp_ms=1000,
        pools=pools[:6],
        base_tokens=bases,
        gas_cost_in_base=10**14,
        l1_fee_in_base=5 * 10**13,
    )
    single_report = run_multihop_ab_experiment(
        [custom_snap],
        max_hops=3,
        min_p99_reduction_ratio=0.0,
        bridge=bridge,
    )
    assert single_report.exact_bit_for_bit_blocks == 1


def test_recall_empty_and_missed_amounts_keep_their_denominations(
    bridge: FastpathBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    pools, weth, _ = _sample_six_pool_universe(bridge)
    usdc = pools[0].token1
    snap = BlockPoolSnapshot(1, 1000, tuple(pools), (weth, usdc), 0, 0)
    empty_result = scanner_mod.IncrementalCycleScanResult(
        candidates=(),
        cycles_evaluated=1,
        rust_elapsed_ns=1,
        wall_elapsed_ns=1,
        used_full_scan=True,
        fallback_reason="initial_scan",
        changed_pools=(),
        retained_cached_candidates=0,
        fresh_candidates=0,
        sizing_residuals=(),
    )
    monkeypatch.setattr(IncrementalCycleScanner, "scan_block", lambda *_a, **_kw: empty_result)
    ref = CycleScanReport((), 1, 1, ())
    monkeypatch.setattr(scanner_mod, "scan_cycles", lambda *_a, **_kw: ref)
    empty = run_multihop_ab_experiment([snap], bridge=bridge, min_p99_reduction_ratio=0)
    assert empty.recall is None
    assert empty.hypothesis_a_passed is False
    ref = replace(
        ref,
        candidates=tuple(
            CycleCandidate(8453, token, (), 1, 1 + n, 0, 0, n)
            for token, n in ((weth, 31), (usdc, 19))
        ),
    )
    report = run_multihop_ab_experiment([snap], bridge=bridge)
    assert report.recall == 0
    assert report.missed_net_profit_raw is None
    assert report.to_dict()["missed_net_profit_by_asset"] == [
        {"chain_id": 8453, "token": weth, "decimals": 18, "net_raw": "31"},
        {"chain_id": 8453, "token": usdc, "decimals": 6, "net_raw": "19"},
    ]
    blocked = run_multihop_ab_experiment([replace(snap, headroom_blocked=True)], bridge=bridge)
    assert blocked.arm_a.total_candidates == blocked.arm_b.total_candidates == 0
    assert blocked.recall is None
    assert blocked.hypothesis_a_passed is False
