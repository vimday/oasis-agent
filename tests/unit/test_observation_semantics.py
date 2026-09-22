"""v4 core emits caller deltas: negative input and positive output (Pool.sol)."""

from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest

from oasis_agent.models.v4 import SwapObservation
from oasis_agent.shadow.producer import _realized_adverse_selection, build_observation

from .test_live_shadow_producer import (
    blocks,
    deployed_policy,
    evidence_ref,
    pool_key,
    strategy,
    swap_log,
)


def report(logs):
    snapshots = blocks(100, 3)
    data = SimpleNamespace(blocks=snapshots, logs=logs, start_block=100, end_block=102)
    return build_observation(
        dataset=data,
        window_evidence=evidence_ref(snapshots[-1].ref),
        pool=pool_key(),
        strategy=strategy(),
        min_benefit=Decimal(0),
        source="fixture",
        deployed_hook_policy=deployed_policy(),
    )


def log(amount0, amount1, *, index=0, sqrt=2**96):
    return swap_log(
        100,
        zero_for_one=amount0 < 0,
        amount0=amount0,
        amount1=amount1,
        sqrt_price=sqrt,
        liquidity=10**18,
        tick=0,
        fee=500,
        log_index=index,
    )


@pytest.mark.parametrize(
    "amount0,amount1,direction", [(-100, 90, "zero_for_one"), (90, -100, "one_for_zero")]
)
def test_canonical_event_signs_determine_direction(amount0, amount1, direction):
    comparison = report((log(amount0, amount1),)).comparisons[0]
    assert comparison.direction == direction
    assert comparison.amount0 == amount0
    assert comparison.amount1 == amount1


def test_later_swap_in_same_block_cannot_change_earlier_feature_or_quote():
    first = log(-100, 90)
    before = report((first,)).comparisons[0]
    after = report((first, log(-100, 90, index=1, sqrt=2**97))).comparisons[0]
    assert before.volatility == after.volatility
    assert before.quote_exact_input_fee_pips == after.quote_exact_input_fee_pips


@pytest.mark.parametrize("amount0,amount1,future_sqrt", [(-100, 90, 2**95), (90, -100, 2**97)])
def test_adverse_midprice_move_uses_pool_exposure_sign(amount0, amount1, future_sqrt):
    current = SwapObservation(
        pool_key().pool_id, "0x" + "1" * 40, amount0, amount1, 2**96, 100, 0, 500, 100
    )
    future = replace(current, sqrt_price_x96=future_sqrt, block_number=101)
    assert _realized_adverse_selection(current, future) > 0


def test_event_swap_fee_cannot_prove_lp_fee_without_protocol_split():
    assert report((log(-100, 90),)).comparisons[0].exact_fee_match is None
