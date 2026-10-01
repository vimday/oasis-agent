from dataclasses import replace

import pytest

from oasis_agent.farming.backtest import (
    ExperimentLock,
    LpReplayObservation,
    WalkForwardSpec,
    _expected_shortfall,
    backtest_strategy,
    observation_dataset_hash,
)
from oasis_agent.models.common import BlockRef, EvidenceRef


def evidence(block: BlockRef) -> EvidenceRef:
    return EvidenceRef(
        "reconciled-lp-event",
        "1" * 64,
        block,
        "archive-a",
        "2026-09-07T00:00:00+00:00",
        "2" * 64,
        "3" * 64,
        "4" * 64,
    )


def provider_evidence(block: BlockRef) -> tuple[EvidenceRef, ...]:
    first = evidence(block)
    return first, replace(first, source="archive-b", raw_response_hash="5" * 64)


def observation(number: int, **changes: int) -> LpReplayObservation:
    block = BlockRef(46630, number, "0x" + f"{number:064x}", number)
    values = {
        "block": block,
        "transaction_index": 0,
        "log_index": 0,
        "gross_routable_volume1": 1_000_000_000,
        "hold_pnl1": 10,
        "fixed_inventory_pnl1": 20,
        "active_inventory_pnl1": 30,
        "static_fee_pips": 500,
        "dynamic_fee_pips": 1_000,
        "fixed_liquidity_share_ppm": 100_000,
        "active_liquidity_share_ppm": 200_000,
        "elasticity_ppm_per_fee_pip": 100,
        "gas_cost_active1": 5,
        "rebalance_cost_active1": 5,
        "adverse_selection_fixed1": 10,
        "adverse_selection_active1": 10,
        "other_cost1": 0,
        "failed_tx_cost1": 2,
        "opportunity_cost1": 3,
        "unexplained_residual1": 0,
        "evidence": provider_evidence(block),
    }
    values.update(changes)
    return LpReplayObservation(**values)  # type: ignore[arg-type]


def lock(
    observations: tuple[LpReplayObservation, ...] | None = None, **changes: object
) -> ExperimentLock:
    if observations is None:
        observations = tuple(observation(number) for number in range(10, 22))
    values = {
        "experiment_id": "lp-001",
        "dataset_hash": observation_dataset_hash(observations),
        "policy_version": "policy-v1",
        "flow_response_model": "linear-routing-v1",
        "initial_capital1": 1_000_000,
        "trial_number": 1,
        "maximum_trials": 1,
        "minimum_evaluation_events": 4,
        "minimum_r11_excess1": 1,
        "maximum_drawdown1": 1_000,
        "maximum_expected_shortfall1": 1_000,
        "tail_fraction_ppm": 100_000,
        "locked_before_block": 9,
        "evidence": (
            replace(
                evidence(BlockRef(46630, 9, "0x" + "09" * 32, 9)),
                kind="experiment-lock",
            ),
        ),
    }
    values.update(changes)
    return ExperimentLock(**values)  # type: ignore[arg-type]


def test_four_cell_walk_forward_replay_computes_costs_and_flow_elasticity():
    observations = tuple(observation(number) for number in range(10, 22))
    result = backtest_strategy(observations, lock(observations), WalkForwardSpec(2, 2, 1, 2))
    assert result.metrics.sample_count == 4
    assert result.metrics.fold_count == 2
    assert result.accepted
    assert not result.execution_eligible
    comparison = result.comparison
    assert comparison.r00_static_fixed.fee_income1 == 4 * 50_000
    assert comparison.r11_dynamic_active.fee_income1 == 4 * 190_000
    assert comparison.r11_dynamic_active.gas_cost1 == 20
    assert comparison.r11_dynamic_active.rebalance_cost1 == 20
    assert comparison.r00_static_fixed.hold_net_pnl1 == 40
    assert comparison.r00_static_fixed.failed_tx_cost1 == 8
    assert comparison.r00_static_fixed.opportunity_cost1 == 12
    assert (
        comparison.r00_static_fixed.net_pnl1 == 200_020
    )  # fees 200k + inventory 80 - adverse 40 - failed 8 - opportunity 12
    assert comparison.r11_dynamic_active.net_pnl1 == (
        comparison.r11_dynamic_active.fee_income1
        + comparison.r11_dynamic_active.inventory_pnl1
        - comparison.r11_dynamic_active.gas_cost1
        - comparison.r11_dynamic_active.rebalance_cost1
        - comparison.r11_dynamic_active.adverse_selection_cost1
        - comparison.r11_dynamic_active.failed_tx_cost1
        - comparison.r11_dynamic_active.opportunity_cost1
    )


def test_precommitted_limits_reject_without_changing_results():
    observations = tuple(
        observation(number, active_inventory_pnl1=-500, gross_routable_volume1=0)
        for number in range(10, 22)
    )
    result = backtest_strategy(
        observations,
        lock(observations, minimum_r11_excess1=999_999, maximum_drawdown1=1),
        WalkForwardSpec(2, 2, 1, 2),
    )
    assert not result.accepted
    assert len(result.reasons) >= 2


def test_residual_order_leakage_and_incomplete_evidence_fail_closed():
    observations = tuple(observation(number) for number in range(10, 22))
    spec = WalkForwardSpec(2, 2, 1, 2)
    with pytest.raises(ValueError, match="residual"):
        backtest_strategy(
            (*observations[:-1], replace(observations[-1], unexplained_residual1=1)),
            lock((*observations[:-1], replace(observations[-1], unexplained_residual1=1))),
            spec,
        )
    with pytest.raises(ValueError, match="ordered"):
        backtest_strategy(tuple(reversed(observations)), lock(tuple(reversed(observations))), spec)
    with pytest.raises(ValueError, match="predate"):
        backtest_strategy(observations, lock(observations, locked_before_block=10), spec)
    with pytest.raises(ValueError, match="complete"):
        replace(
            observations[0],
            evidence=(replace(observations[0].evidence[0], request_hash=""),),
        )
    with pytest.raises(ValueError, match="two distinct"):
        replace(observations[0], evidence=(observations[0].evidence[0],))


def test_insufficient_folds_and_trial_p_hacking_are_rejected():
    short = tuple(observation(number) for number in range(10, 15))
    with pytest.raises(ValueError, match="folds"):
        backtest_strategy(
            short,
            lock(short, minimum_evaluation_events=1),
            WalkForwardSpec(2, 2, 1, 2),
        )
    with pytest.raises(ValueError, match="trial ledger"):
        lock(trial_number=2)
    with pytest.raises(ValueError, match="loss limits"):
        lock(maximum_drawdown1=1_000_001)
    with pytest.raises(ValueError, match="event count"):
        lock(minimum_evaluation_events=0)


def test_precommitted_dataset_hash_rejects_changed_observations():
    observations = tuple(observation(number) for number in range(10, 22))
    changed = (replace(observations[0], other_cost1=1), *observations[1:])
    with pytest.raises(ValueError, match="dataset hash"):
        backtest_strategy(changed, lock(observations), WalkForwardSpec(2, 2, 1, 2))


def test_positive_hold_excess_cannot_hide_inferior_static_lp():
    observations = tuple(
        observation(n, dynamic_fee_pips=100, elasticity_ppm_per_fee_pip=0) for n in range(10, 22)
    )
    result = backtest_strategy(observations, lock(observations), WalkForwardSpec(2, 2, 1, 2))
    assert result.metrics.r11_excess_lower_bound1 > 0
    assert not result.accepted
    assert any("R01" in reason for reason in result.reasons)


def test_losing_fold_vs_static_cannot_be_hidden_by_profitable_folds():
    observations = tuple(
        observation(n, dynamic_fee_pips=100 if n in (13, 14) else 1000) for n in range(10, 22)
    )
    result = backtest_strategy(observations, lock(observations), WalkForwardSpec(2, 2, 1, 2))
    assert not result.accepted
    assert any("R01" in reason for reason in result.reasons)


def test_walk_forward_cannot_split_one_block_between_calibration_and_evaluation():
    observations = tuple(replace(observation(10 + i // 5), log_index=i) for i in range(12))
    with pytest.raises(ValueError, match="block boundary"):
        backtest_strategy(observations, lock(observations), WalkForwardSpec(2, 2, 1, 2))


def test_expected_shortfall_rounds_loss_up():
    assert _expected_shortfall((-1, 0), 1_000_000) == 1


@pytest.mark.parametrize("timestamp", [None, 9, 10])
def test_observation_order_cannot_hide_missing_or_regressing_time(timestamp):
    items = [observation(n) for n in range(10, 22)]
    block = replace(items[1].block, timestamp=timestamp)
    items[1] = replace(items[1], block=block, evidence=provider_evidence(block))
    observations = tuple(items)
    with pytest.raises(ValueError, match=r"timestamped|time must strictly advance"):
        backtest_strategy(observations, lock(observations), WalkForwardSpec(2, 2, 1, 2))


def test_one_height_cannot_hide_a_fork_by_changing_log_index():
    first = observation(10)
    conflicting = replace(first.block, block_hash="0x" + "ff" * 32)
    observations = (
        first,
        replace(first, block=conflicting, log_index=1, evidence=provider_evidence(conflicting)),
    )
    with pytest.raises(ValueError, match="conflicting identities"):
        backtest_strategy(observations, lock(observations), WalkForwardSpec(1, 1, 0, 1))


def test_older_lock_height_cannot_hide_a_late_lock_timestamp():
    observations = tuple(observation(n) for n in range(10, 22))
    experiment = lock(observations)
    reference = experiment.evidence[0]
    experiment = replace(
        experiment, evidence=(replace(reference, block=replace(reference.block, timestamp=10)),)
    )
    with pytest.raises(ValueError, match="lock time must predate"):
        backtest_strategy(observations, experiment, WalkForwardSpec(2, 2, 1, 2))
