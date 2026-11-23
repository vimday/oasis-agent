from dataclasses import replace

import pytest

from oasis_agent.fastpath.bridge import FastpathBridge
from oasis_agent.fastpath.pool_graph import CycleScanReport, PoolKind, PoolNode, scan_cycles
from oasis_agent.fastpath.resident_graph import AnchorScan, GraphBlock
from oasis_agent.fastpath.resident_shadow import ResidentShadowComparator, ResidentShadowError

_ANCHORS = (AnchorScan("a", 1, 32), AnchorScan("b", 1, 64, gas_cost_in_base=3))


def _block(height: int) -> GraphBlock:
    return GraphBlock(8453, height, f"0x{height:064x}", f"0x{height - 1:064x}")


def _pools(block: GraphBlock) -> tuple[PoolNode, ...]:
    return tuple(
        PoolNode(
            block.chain_id,
            pool_id,
            PoolKind.V2,
            "a",
            "b",
            18,
            6,
            3_000,
            block_number=block.block_number,
            timestamp_ms=block.block_number * 2_000,
            reserve0=100_000,
            reserve1=reserve1,
        )
        for pool_id, reserve1 in (("p", 200_000), ("q", 100_000))
    )


def _baseline(pools: tuple[PoolNode, ...], bridge: FastpathBridge) -> tuple[CycleScanReport, ...]:
    return tuple(
        scan_cycles(
            pools,
            [anchor.base_token],
            max_hops=anchor.max_hops,
            min_amount_in=anchor.min_amount_in,
            max_amount_in=anchor.max_amount_in,
            gas_cost_in_base=anchor.gas_cost_in_base,
            bridge=bridge,
        )
        for anchor in _ANCHORS
    )


def test_resident_shadow_checks_repeats_pricing_deltas_topology_and_gap() -> None:
    bridge = FastpathBridge()
    first = _block(20)
    first_pools = _pools(first)
    baseline = _baseline(first_pools, bridge)
    assert all(report.candidates for report in baseline)
    with ResidentShadowComparator(bridge=bridge) as shadow:
        opened = shadow.check(first, first_pools, _ANCHORS, baseline)
        assert (opened.transition, opened.submitted_pool_rows, opened.native_calls) == (
            "opened",
            2,
            3,
        )
        dupe = shadow.check(first, first_pools[::-1], _ANCHORS, baseline)
        assert (dupe.transition, dupe.submitted_pool_rows, dupe.native_calls) == ("repeated", 0, 1)
        second = _block(21)
        second_pools = _pools(second)
        unchanged = shadow.check(second, second_pools, _ANCHORS, baseline)
        assert (unchanged.transition, unchanged.submitted_pool_rows, unchanged.native_calls) == (
            "advanced",
            0,
            2,
        )
        third = _block(22)
        third_pools = _pools(third)
        repriced = (replace(third_pools[0], reserve1=90_000), third_pools[1])
        changed = shadow.check(third, repriced, _ANCHORS, _baseline(repriced, bridge))
        assert (changed.transition, changed.submitted_pool_rows, changed.native_calls) == (
            "advanced",
            1,
            2,
        )
        fourth = _block(23)
        fourth_pools = _pools(fourth)
        topology = (replace(fourth_pools[0], token1="c"), fourth_pools[1])
        replaced = shadow.check(fourth, topology, _ANCHORS, _baseline(topology, bridge))
        assert (replaced.transition, replaced.submitted_pool_rows, replaced.native_calls) == (
            "replaced_topology",
            2,
            2,
        )
        gap = _block(26)
        rebuilt = shadow.check(gap, _pools(gap), _ANCHORS, baseline)
        assert (rebuilt.transition, rebuilt.submitted_pool_rows, rebuilt.native_calls) == (
            "replaced_gap",
            2,
            2,
        )
        assert rebuilt.block == gap
        assert rebuilt.build_id == opened.build_id
        assert rebuilt.delta_ns >= 0 and rebuilt.apply_ns >= 0 and rebuilt.scan_ns >= 0


@pytest.mark.parametrize("conflict", ["hash", "state", "parent"])
def test_resident_shadow_latches_conflicting_state(conflict: str) -> None:
    bridge = FastpathBridge()
    first = _block(20)
    pools = _pools(first)
    baseline = _baseline(pools, bridge)
    with ResidentShadowComparator(bridge=bridge) as shadow:
        shadow.check(first, pools, _ANCHORS, baseline)
        if conflict == "hash":
            block = replace(first, block_hash=f"0x{999:064x}")
            expected = "same height"
        elif conflict == "parent":
            block = replace(_block(21), parent_hash=f"0x{999:064x}")
            pools = _pools(block)
            expected = "different parent"
        else:
            block = first
            pools = (replace(pools[0], fee_pips=2_000), pools[1])
            expected = "different pool state"
        with pytest.raises(ResidentShadowError, match=expected):
            shadow.check(block, pools, _ANCHORS, baseline)
        with pytest.raises(ResidentShadowError, match="has stopped"):
            shadow.check(first, _pools(first), _ANCHORS, baseline)


def test_resident_shadow_rejects_unverified_baseline_and_wrong_pool_block() -> None:
    bridge = FastpathBridge()
    first = _block(20)
    pools = _pools(first)
    baseline = _baseline(pools, bridge)
    with (
        ResidentShadowComparator(bridge=bridge) as shadow,
        pytest.raises(ResidentShadowError, match="unknown sizing coverage"),
    ):
        shadow.check(
            first, pools, _ANCHORS, (replace(baseline[0], sizing_residuals=None), baseline[1])
        )
    with (
        ResidentShadowComparator(bridge=bridge) as shadow,
        pytest.raises(ResidentShadowError, match="do not share the block pin"),
    ):
        shadow.check(_block(21), pools, _ANCHORS, baseline)
    with ResidentShadowComparator(bridge=bridge) as shadow:
        wrong = replace(baseline[0], cycles_evaluated=baseline[0].cycles_evaluated + 1)
        with pytest.raises(ResidentShadowError, match="differs from stateless baseline for a"):
            shadow.check(first, pools, _ANCHORS, (wrong, baseline[1]))
        with pytest.raises(ResidentShadowError, match="has stopped"):
            shadow.check(first, pools, _ANCHORS, baseline)
