from dataclasses import replace
from decimal import Decimal

import pytest

from oasis_agent.models.common import BlockRef, EvidenceRef
from oasis_agent.models.v4 import SwapDirection
from oasis_agent.shadow.features import (
    AgreedInteger,
    LpFeatureWindow,
    SwapMarkout,
    agreed_value_content_hash,
    produce_pool_state,
)

Q96 = 1 << 96
Q192 = 1 << 192


def agreed(name: str, value: int, number: int) -> AgreedInteger:
    block = BlockRef(46630, number, "0x" + f"{number:064x}", 1_000 + number)
    content_hash = agreed_value_content_hash(name, value, block)
    references = tuple(
        EvidenceRef(
            name,
            content_hash,
            block,
            source,
            "2026-09-07T00:00:00+00:00",
            "2" * 64,
            raw,
            "4" * 64,
        )
        for source, raw in (("rpc-a", "3" * 64), ("rpc-b", "5" * 64))
    )
    return AgreedInteger(name, value, block, references)


def window() -> LpFeatureWindow:
    decision = BlockRef(46630, 12, "0x" + f"{12:064x}", 1_012)
    return LpFeatureWindow(
        decision,
        (agreed("sqrt", Q96, 10), agreed("sqrt", Q96 + Q96 // 100, 12)),
        agreed("inventory0", 3_000, 12),
        agreed("inventory1", 1_000, 12),
        agreed("oracle", Q192, 12),
        (SwapMarkout(SwapDirection.ZERO_FOR_ONE, 100, -110, agreed("future", Q192, 12)),),
    )


def test_producer_computes_features_without_binary_float_or_future_data():
    state = produce_pool_state(window())
    assert state.block == window().decision_block
    assert state.volatility == state.oracle_deviation
    assert state.volatility > Decimal("0.02")
    assert state.inventory_imbalance == Decimal("0.5")
    assert state.rebalance_incentive == Decimal("0.5")
    assert state.adverse_selection == Decimal("0.1")


def test_provider_disagreement_single_source_sign_and_future_inputs_fail_closed():
    value = agreed("sqrt", Q96, 10)
    with pytest.raises(ValueError, match="two distinct"):
        replace(value, evidence=value.evidence[:1])
    with pytest.raises(ValueError, match="one interpretation"):
        replace(
            value,
            evidence=(value.evidence[0], replace(value.evidence[1], interpretation_hash="9" * 64)),
        )
    with pytest.raises(ValueError, match="signs"):
        SwapMarkout(SwapDirection.ZERO_FOR_ONE, -100, 110, agreed("future", Q192, 12))
    with pytest.raises(ValueError, match="future"):
        replace(window(), reference_price_x192=agreed("oracle", Q192, 13))


def test_value_not_bound_to_evidence_content_fails_closed():
    value = agreed("sqrt", Q96, 10)
    with pytest.raises(ValueError, match="not bound to its evidence content"):
        replace(value, value=Q96 + 1)
    with pytest.raises(ValueError, match="not bound to its evidence content"):
        replace(
            value,
            evidence=(
                replace(value.evidence[0], content_hash="9" * 64),
                value.evidence[1],
            ),
        )
    assert agreed_value_content_hash("sqrt", Q96, value.block) == value.evidence[0].content_hash


def test_zero_inventory_and_no_swaps_are_valid_conservative_features():
    base = window()
    zero = replace(
        base,
        inventory0=agreed("inventory0", 0, 12),
        inventory1=agreed("inventory1", 0, 12),
        markouts=(),
    )
    state = produce_pool_state(zero)
    assert state.inventory_imbalance == 0
    assert state.adverse_selection == 0
