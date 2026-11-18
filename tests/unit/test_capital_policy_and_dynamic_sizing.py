"""Unit tests for configurable capital risk policy, headroom sizing, and notional solver."""

import json
import os
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from oasis_agent.models.common import canonical_json, sha256_hex
from oasis_agent.risk.capital_policy import (
    MAX_CONFIG_FILE_BYTES,
    SCHEMA_VERSION,
    AccountCapitalSnapshot,
    CapacityOptimizationResult,
    CapacityPoint,
    CapitalRiskConfig,
    HeadroomAllocation,
    compute_deployable_headroom,
    load_capital_risk_config,
    solve_optimal_notional,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_capital_risk_config_defaults_example_and_roundtrip(tmp_path: Path) -> None:
    cfg = CapitalRiskConfig()
    assert cfg.total_capital_usd_micros == 500_000_000
    assert cfg.max_daily_loss_usd_micros == 200_000_000
    assert cfg.max_cumulative_loss_usd_micros == 500_000_000
    assert cfg.reserve_buffer_bps == 1_000
    assert cfg.max_single_clip_share_bps == 5_000
    assert cfg.min_clip_usd_micros == 10_000_000
    assert cfg.assumed_tail_stress_bps == 250
    assert cfg.max_feed_age_seconds == 1_800
    assert cfg.usdc_price_usd_micros == 1_000_000

    with pytest.raises(FrozenInstanceError):
        cfg.total_capital_usd_micros = 999_000_000  # type: ignore[misc]

    as_dict = cfg.to_dict()
    assert as_dict["schema_version"] == SCHEMA_VERSION
    assert cfg.config_sha256 == sha256_hex(canonical_json(as_dict))
    assert CapitalRiskConfig.from_dict(as_dict) == cfg

    # Optional schema_version may be omitted
    without_schema = {k: v for k, v in as_dict.items() if k != "schema_version"}
    assert CapitalRiskConfig.from_dict(without_schema) == cfg

    # Committed example config matches defaults identically
    example_path = REPO_ROOT / "configs" / "profit-engine-risk.example.json"
    loaded_example = load_capital_risk_config(example_path)
    assert loaded_example == cfg
    assert loaded_example.config_sha256 == cfg.config_sha256

    # File roundtrip
    custom = CapitalRiskConfig(
        total_capital_usd_micros=5_000_000_000,
        max_daily_loss_usd_micros=750_000_000,
        max_cumulative_loss_usd_micros=1_500_000_000,
        reserve_buffer_bps=1_500,
        max_single_clip_share_bps=4_000,
        min_clip_usd_micros=25_000_000,
        assumed_tail_stress_bps=500,
        max_feed_age_seconds=900,
        usdc_price_usd_micros=999_500,
    )
    cfg_path = tmp_path / "risk.json"
    cfg_path.write_text(json.dumps(custom.to_dict(), indent=2) + "\n", encoding="utf-8")
    assert load_capital_risk_config(cfg_path) == custom


def test_capital_risk_config_validation_and_file_guards(tmp_path: Path) -> None:
    int_fields = (
        "total_capital_usd_micros",
        "max_daily_loss_usd_micros",
        "max_cumulative_loss_usd_micros",
        "reserve_buffer_bps",
        "max_single_clip_share_bps",
        "min_clip_usd_micros",
        "assumed_tail_stress_bps",
        "max_feed_age_seconds",
        "usdc_price_usd_micros",
    )
    for field_name in int_fields:
        with pytest.raises(ValueError, match=r"strict integer|positive|non-negative"):
            CapitalRiskConfig(**{field_name: True})  # type: ignore[arg-type]
        with pytest.raises(ValueError, match=r"strict integer|positive|non-negative"):
            CapitalRiskConfig(**{field_name: 100.5})  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="total_capital_usd_micros"):
        CapitalRiskConfig(total_capital_usd_micros=0)
    with pytest.raises(ValueError, match="usdc_price_usd_micros"):
        CapitalRiskConfig(usdc_price_usd_micros=0)
    with pytest.raises(ValueError, match="max_daily_loss_usd_micros"):
        CapitalRiskConfig(
            total_capital_usd_micros=500_000_000,
            max_daily_loss_usd_micros=300_000_000,
            max_cumulative_loss_usd_micros=250_000_000,
        )
    with pytest.raises(ValueError, match="total_capital_usd_micros"):
        CapitalRiskConfig(
            total_capital_usd_micros=400_000_000,
            max_daily_loss_usd_micros=200_000_000,
            max_cumulative_loss_usd_micros=500_000_000,
        )
    with pytest.raises(ValueError, match="reserve_buffer_bps"):
        CapitalRiskConfig(reserve_buffer_bps=9_501)
    with pytest.raises(ValueError, match="max_single_clip_share_bps"):
        CapitalRiskConfig(max_single_clip_share_bps=0)
    with pytest.raises(ValueError, match="max_single_clip_share_bps"):
        CapitalRiskConfig(max_single_clip_share_bps=10_001)
    with pytest.raises(ValueError, match="min_clip_usd_micros"):
        CapitalRiskConfig(min_clip_usd_micros=0)
    with pytest.raises(ValueError, match="min_clip_usd_micros"):
        CapitalRiskConfig(min_clip_usd_micros=500_000_001)
    with pytest.raises(ValueError, match="assumed_tail_stress_bps"):
        CapitalRiskConfig(assumed_tail_stress_bps=0)
    with pytest.raises(ValueError, match="assumed_tail_stress_bps"):
        CapitalRiskConfig(assumed_tail_stress_bps=10_001)
    with pytest.raises(ValueError, match="max_feed_age_seconds"):
        CapitalRiskConfig(max_feed_age_seconds=0)

    # Unknown keys & bad schema_version fail closed
    with pytest.raises(ValueError, match="unknown"):
        CapitalRiskConfig.from_dict({"unknown_key": 1})
    with pytest.raises(ValueError, match="schema_version"):
        CapitalRiskConfig.from_dict({"schema_version": "oasis.capital-risk-policy.v999"})

    # Symlink refusal
    target = tmp_path / "real.json"
    target.write_text(json.dumps(CapitalRiskConfig().to_dict()), encoding="utf-8")
    link = tmp_path / "link.json"
    os.symlink(target, link)
    with pytest.raises(ValueError, match="symlink"):
        load_capital_risk_config(link)

    # Oversized > 1MB refusal
    oversized = tmp_path / "big.json"
    oversized.write_bytes(b" " * (MAX_CONFIG_FILE_BYTES + 1))
    with pytest.raises(ValueError, match="maximum byte limit"):
        load_capital_risk_config(oversized)

    # Duplicate keys and NaN fail closed
    dup = tmp_path / "dup.json"
    dup.write_text(
        '{"total_capital_usd_micros": 500000000, "total_capital_usd_micros": 600000000}',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate"):
        load_capital_risk_config(dup)


def test_usdc_conversion_exact_integer_arithmetic() -> None:
    pegged = CapitalRiskConfig()
    assert pegged.usd_micros_to_usdc_raw(250_000_000) == 250_000_000
    assert pegged.usdc_raw_to_usd_micros(250_000_000) == 250_000_000

    off_peg = CapitalRiskConfig(usdc_price_usd_micros=999_000)  # $0.999 / USDC
    # $999.00 -> exactly 1,000 USDC raw
    assert off_peg.usd_micros_to_usdc_raw(999_000_000) == 1_000_000_000
    # 1,000 USDC raw -> $999.00
    assert off_peg.usdc_raw_to_usd_micros(1_000_000_000) == 999_000_000
    # Floor rounding on fractional conversion
    assert off_peg.usdc_raw_to_usd_micros(1) == 0

    with pytest.raises(ValueError):
        off_peg.usd_micros_to_usdc_raw(True)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        off_peg.usdc_raw_to_usd_micros(-1)


def test_compute_deployable_headroom_states_and_monotonic_tail_stress() -> None:
    cfg = CapitalRiskConfig()

    # 1. Full headroom with ceiling rounding check on odd reserve
    odd_cfg = CapitalRiskConfig(
        total_capital_usd_micros=500_000_001,
        reserve_buffer_bps=1_000,
    )
    odd_headroom = compute_deployable_headroom(
        odd_cfg,
        AccountCapitalSnapshot(cash_available_usd_micros=900_000_000),
    )
    # ceil(500_000_001 * 1000 / 10000) = 50_000_001
    assert odd_headroom.reserve_floor_usd_micros == 50_000_001
    assert odd_headroom.deployable_capital_usd_micros == 450_000_000
    assert odd_headroom.single_clip_cap_usd_micros == 225_000_000
    assert odd_headroom.max_clip_usd_micros == 225_000_000
    assert odd_headroom.can_deploy is True
    assert odd_headroom.block_reason is None
    assert odd_headroom.execution_eligible is False
    assert odd_headroom.to_dict()["execution_eligible"] is False

    # 2. Locked inventory + pending commitments reduce deployable capital & clip cap
    deducted = compute_deployable_headroom(
        cfg,
        AccountCapitalSnapshot(
            cash_available_usd_micros=500_000_000,
            locked_inventory_usd_micros=150_000_000,
            pending_commitments_usd_micros=100_000_000,
        ),
    )
    # 500M - 50M reserve - 150M locked - 100M pending = 200M; 50% clip = 100M
    assert deducted.deployable_capital_usd_micros == 200_000_000
    assert deducted.single_clip_cap_usd_micros == 100_000_000
    assert deducted.max_clip_usd_micros == 100_000_000
    assert deducted.can_deploy is True

    # 3. Unrealized drawdown throttles remaining daily loss & max clip
    throttled = compute_deployable_headroom(
        cfg,
        AccountCapitalSnapshot(
            cash_available_usd_micros=500_000_000,
            realized_daily_loss_usd_micros=50_000_000,
            unrealized_drawdown_usd_micros=147_000_000,
        ),
    )
    # Remaining daily = 200M - 197M = 3M; loss_throttled = 3M * 10000 / 250 = 120M
    assert throttled.remaining_daily_loss_usd_micros == 3_000_000
    assert throttled.loss_throttled_cap_usd_micros == 120_000_000
    assert throttled.max_clip_usd_micros == 120_000_000
    assert throttled.can_deploy is True

    # 4. Daily loss exhaustion freezes deployment
    daily_exhausted = compute_deployable_headroom(
        cfg,
        AccountCapitalSnapshot(
            cash_available_usd_micros=500_000_000,
            realized_daily_loss_usd_micros=120_000_000,
            unrealized_drawdown_usd_micros=80_000_000,
        ),
    )
    assert daily_exhausted.can_deploy is False
    assert daily_exhausted.max_clip_usd_micros == 0
    assert daily_exhausted.block_reason == "daily_loss_exhausted"

    # 5. Cumulative loss exhaustion freezes deployment
    cum_exhausted = compute_deployable_headroom(
        cfg,
        AccountCapitalSnapshot(
            cash_available_usd_micros=500_000_000,
            cumulative_loss_usd_micros=500_000_000,
        ),
    )
    assert cum_exhausted.can_deploy is False
    assert cum_exhausted.max_clip_usd_micros == 0
    assert cum_exhausted.block_reason == "cumulative_loss_exhausted"

    # 6. Unresolved residuals freeze deployment
    residuals = compute_deployable_headroom(
        cfg,
        AccountCapitalSnapshot(
            cash_available_usd_micros=500_000_000,
            unresolved_residuals=1,
        ),
    )
    assert residuals.can_deploy is False
    assert residuals.max_clip_usd_micros == 0
    assert residuals.block_reason == "unresolved_residuals"

    # 7. Authority halted freezes deployment with highest priority
    halted = compute_deployable_headroom(
        cfg,
        AccountCapitalSnapshot(
            cash_available_usd_micros=500_000_000,
            unresolved_residuals=1,
            halted=True,
        ),
    )
    assert halted.can_deploy is False
    assert halted.max_clip_usd_micros == 0
    assert halted.block_reason == "authority_halted"

    # 8. Below min clip triggers insufficient_deployable_headroom
    tiny = compute_deployable_headroom(
        cfg,
        AccountCapitalSnapshot(
            cash_available_usd_micros=60_000_000,  # 60M - 50M reserve = 10M deployable, 50% = 5M
        ),
    )
    assert tiny.can_deploy is False
    assert tiny.max_clip_usd_micros == 0
    assert tiny.block_reason == "insufficient_deployable_headroom"

    # 9. Increasing assumed_tail_stress_bps shrinks max clip monotonically
    snap = AccountCapitalSnapshot(
        cash_available_usd_micros=500_000_000,
        realized_daily_loss_usd_micros=190_000_000,  # remaining daily = 10M
    )
    clips: list[int] = []
    for stress_bps in (100, 250, 500, 1_000, 2_500, 5_000):
        stressed_cfg = CapitalRiskConfig(assumed_tail_stress_bps=stress_bps)
        alloc = compute_deployable_headroom(stressed_cfg, snap)
        clips.append(alloc.max_clip_usd_micros)
    assert clips == sorted(clips, reverse=True)
    assert clips[0] > clips[-1]

    # 10. Snapshot & HeadroomAllocation fail-closed input checks
    with pytest.raises(ValueError):
        AccountCapitalSnapshot(cash_available_usd_micros=-1)
    with pytest.raises(ValueError):
        AccountCapitalSnapshot(cash_available_usd_micros=True)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        AccountCapitalSnapshot(cash_available_usd_micros=100, halted="yes")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="execution_eligible"):
        HeadroomAllocation(
            deployable_capital_usd_micros=100,
            reserve_floor_usd_micros=10,
            single_clip_cap_usd_micros=50,
            remaining_daily_loss_usd_micros=100,
            remaining_cumulative_loss_usd_micros=100,
            loss_throttled_cap_usd_micros=100,
            max_clip_usd_micros=50,
            min_clip_usd_micros=10,
            can_deploy=True,
            block_reason=None,
            execution_eligible=True,  # type: ignore[arg-type]
        )


def test_solve_optimal_notional_concave_curve_and_infeasible_regions() -> None:
    # Concave profit curve in 6-decimal USDC raw units (1 USDC = 1_000_000):
    # Gross spread = 60 bps, linear slippage impact = 0.25 bps per USDC, fixed gas = $0.084.
    # Profit(Q_usd) = 0.006 * Q - 0.000025 * Q^2 - 0.084 -> vertex Q* = 120 USDC = 120_000_000.
    eval_calls: list[int] = []

    def concave_eval(notional_raw: int) -> tuple[int, float] | None:
        eval_calls.append(notional_raw)
        q_usd = notional_raw / 1_000_000.0
        net_profit_usd = 0.006 * q_usd - 0.000025 * (q_usd**2) - 0.084
        # Reject sizes below gas break-even when desired by caller
        if net_profit_usd <= 0.0:
            return None
        net_profit_micros = round(net_profit_usd * 1_000_000)
        net_edge_bps = (net_profit_usd / q_usd) * 10_000.0
        return (net_profit_micros, net_edge_bps)

    result = solve_optimal_notional(
        min_notional_raw=10_000_000,
        max_notional_raw=300_000_000,
        evaluate_fn=concave_eval,
        extra_knots=(5_000_000, 110_000_000, 350_000_000),  # out-of-range extra knots ignored
    )
    assert isinstance(result, CapacityOptimizationResult)
    assert result.optimal_notional_raw is not None
    assert result.best_net_profit_usd_micros is not None
    assert result.best_net_edge_bps is not None
    assert abs(result.optimal_notional_raw - 120_000_000) <= 1_000_000
    assert result.best_net_profit_usd_micros > 275_000
    # Memoization: no notional_raw evaluated twice
    assert len(eval_calls) == len(set(eval_calls))
    assert all(isinstance(pt, CapacityPoint) for pt in result.points_evaluated)
    assert all(pt.net_profit_usd_micros > 0 for pt in result.points_evaluated)

    # Without None filtering, optimizer also picks peak profit over negative tiny sizes
    def unfiltered_concave_eval(notional_raw: int) -> tuple[int, float]:
        q_usd = notional_raw / 1_000_000.0
        net_profit_usd = 0.006 * q_usd - 0.000025 * (q_usd**2) - 0.10
        net_profit_micros = round(net_profit_usd * 1_000_000)
        net_edge_bps = (net_profit_usd / q_usd) * 10_000.0
        return (net_profit_micros, net_edge_bps)

    pos_res = solve_optimal_notional(
        min_notional_raw=10_000_000,
        max_notional_raw=300_000_000,
        evaluate_fn=unfiltered_concave_eval,
    )
    assert pos_res.optimal_notional_raw is not None
    assert pos_res.best_net_profit_usd_micros is not None
    assert abs(pos_res.optimal_notional_raw - 120_000_000) <= 1_000_000
    assert pos_res.best_net_profit_usd_micros > 259_000

    # All infeasible -> (None, None, None, ())
    empty_res = solve_optimal_notional(
        min_notional_raw=10_000_000,
        max_notional_raw=100_000_000,
        evaluate_fn=lambda _q: None,
    )
    assert empty_res == CapacityOptimizationResult(None, None, None, ())

    # Degenerate single-point interval & tie-breaking smaller notional_raw
    single_res = solve_optimal_notional(
        min_notional_raw=25_000_000,
        max_notional_raw=25_000_000,
        evaluate_fn=lambda _q: (100_000, 40.0),
    )
    assert single_res.optimal_notional_raw == 25_000_000
    assert len(single_res.points_evaluated) == 1

    tie_res = solve_optimal_notional(
        min_notional_raw=10_000_000,
        max_notional_raw=100_000_000,
        evaluate_fn=lambda _q: (50_000, 5.0),
    )
    assert tie_res.optimal_notional_raw == 10_000_000

    # Input validation
    with pytest.raises(ValueError):
        solve_optimal_notional(
            min_notional_raw=0,
            max_notional_raw=100,
            evaluate_fn=lambda _q: None,
        )
    with pytest.raises(ValueError):
        solve_optimal_notional(
            min_notional_raw=200,
            max_notional_raw=100,
            evaluate_fn=lambda _q: None,
        )
    with pytest.raises(ValueError):
        solve_optimal_notional(
            min_notional_raw=10,
            max_notional_raw=100,
            evaluate_fn=lambda _q: None,
            extra_knots=(True,),
        )
