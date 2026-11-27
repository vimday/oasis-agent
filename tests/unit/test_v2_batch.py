from dataclasses import replace

import pytest

from oasis_agent.fastpath.v2_batch import (
    V2BatchOrder,
    V2BatchStatus,
    compare_v2_batch,
    search_v2_batches,
)
from oasis_agent.fastpath.v2_sizing import V2Leg, V2Pool, V2Route


def _route() -> V2Route:
    first = V2Pool("first", "A", "B", 615, 1497, 997, 1000)
    second = V2Pool("second", "A", "B", 1730, 1521, 997, 1000)
    return V2Route((V2Leg(first, "A"), V2Leg(second, "B")))


def _orders() -> tuple[V2BatchOrder, V2BatchOrder]:
    route = _route()
    return (
        V2BatchOrder("first", route, 204, 5, 7),
        V2BatchOrder("second", route, 204, 5, 7),
    )


def test_reused_pools_erode_second_cycle_and_reverted_state_is_discarded() -> None:
    first, second = _orders()
    result = compare_v2_batch((first, second), initial_capital=300, anchor_token="A")
    batch, independent = result.ordered, result.independent
    assert [r.status for r in batch.results] == [
        V2BatchStatus.EXECUTED,
        V2BatchStatus.BELOW_MINIMUM_NET,
    ]
    assert batch.results[0].quote is not None
    assert batch.results[0].quote.amounts == (204, 372, 339)
    assert batch.results[1].quote is not None
    assert batch.results[1].quote.amounts == (204, 223, 146)
    assert (batch.gross_profit, batch.cost_in_anchor, batch.net_profit, batch.final_capital) == (
        135,
        12,
        123,
        423,
    )
    assert batch.success_count == 1
    assert [(p.reserve0, p.reserve1) for p in batch.final_pools] == [(819, 1125), (1391, 1893)]
    assert [result.capital_before for result in independent.results] == [300, 430]
    assert independent.net_profit == 260 and independent.success_count == 2
    assert [(p.reserve0, p.reserve1) for p in independent.final_pools] == [
        (615, 1497),
        (1730, 1521),
    ]
    assert [standalone.capital_before for standalone in result.standalone] == [300, 300]
    assert result.best_standalone_net == 130

    third = replace(second, order_id="accept-loss", min_net_in_anchor=-100)
    actual = compare_v2_batch((first, second, third), initial_capital=300, anchor_token="A")
    reference = compare_v2_batch((first, third), initial_capital=300, anchor_token="A")
    assert actual.ordered.results[-1].quote == reference.ordered.results[-1].quote
    assert actual.ordered.final_pools == reference.ordered.final_pools
    assert actual.ordered.final_capital == reference.ordered.final_capital - 7


def test_serial_bankroll_admits_following_order_without_resetting_risk() -> None:
    first, second = _orders()
    second = replace(second, amount_in=300)
    compare = compare_v2_batch((first, second), initial_capital=211, anchor_token="A")
    assert compare.standalone[1].status == V2BatchStatus.INSUFFICIENT_CAPITAL
    assert compare.ordered.results[1].status != V2BatchStatus.INSUFFICIENT_CAPITAL
    assert compare.independent.results[1].status != V2BatchStatus.INSUFFICIENT_CAPITAL
    assert compare.standalone[1].cost_in_anchor == 0
    assert compare.standalone[1].quote is None

    unfunded = compare_v2_batch((first,), initial_capital=210, anchor_token="A")
    assert unfunded.ordered.results[0].status == V2BatchStatus.INSUFFICIENT_CAPITAL
    assert unfunded.ordered.final_capital == 210 and unfunded.ordered.net_profit == 0


def test_zero_output_later_hop_charges_only_failure_cost_and_preserves_all_pools() -> None:
    route = _route()
    broken = V2Route((route.legs[0], V2Leg(V2Pool("dust", "A", "B", 1, 1730, 997, 1000), "B")))
    first = V2BatchOrder("break", broken, 204, 10, 9)
    salvage = V2BatchOrder("after", route, 204, 5, 7)
    run = compare_v2_batch((first, salvage), initial_capital=300, anchor_token="A").ordered
    rejected, followup = run.results
    assert rejected.quote is not None and rejected.quote.amounts == (204, 372, 0)
    assert (rejected.status, rejected.failure_hop, rejected.net_profit, rejected.gross_profit) == (
        V2BatchStatus.ZERO_OUTPUT,
        2,
        -9,
        0,
    )
    assert followup.status == V2BatchStatus.EXECUTED
    assert followup.quote == route.quote(204)
    assert run.final_capital == 421 and run.net_profit == run.gross_profit - run.cost_in_anchor
    dust = next(pool for pool in run.final_pools if pool.pool_id == "dust")
    assert dust == broken.legs[1].pool


def test_oversize_reserve_update_is_charged_as_a_failed_attempt() -> None:
    biggest = (1 << 112) - 1
    original = _route().legs[0]
    large = V2Pool("second", "A", "B", biggest, biggest, 997, 1000)
    route = V2Route((original, V2Leg(large, "B")))
    order = V2BatchOrder("overflow", route, 204, 0, 3, min_net_in_anchor=-(10**9))
    run = compare_v2_batch((order,), initial_capital=300, anchor_token="A").ordered
    assert run.results[0].status == V2BatchStatus.RESERVE_OVERFLOW
    assert run.results[0].failure_hop == 2
    assert run.final_pools == (original.pool, large)
    assert run.net_profit == -3


def test_configured_exact_net_guard_and_invalid_snapshot_states() -> None:
    first, second = _orders()
    one = replace(first, min_net_in_anchor=130)
    two = replace(second, min_net_in_anchor=131)
    report = compare_v2_batch((one, two), initial_capital=300, anchor_token="A")
    assert [side.status for side in report.standalone] == [
        V2BatchStatus.EXECUTED,
        V2BatchStatus.BELOW_MINIMUM_NET,
    ]
    assert report.standalone[1].net_profit == -7

    changed = replace(first.route.legs[0].pool, reserve1=1498)
    other = V2Route((V2Leg(changed, "A"), first.route.legs[1]))
    with pytest.raises(ValueError, match="conflicting starting pool state"):
        compare_v2_batch(
            (first, replace(second, route=other)), initial_capital=300, anchor_token="A"
        )
    with pytest.raises(ValueError, match="declared anchor"):
        compare_v2_batch((first,), initial_capital=300, anchor_token="WRONG")
    with pytest.raises(ValueError, match="distinct IDs"):
        compare_v2_batch((first, first), initial_capital=300, anchor_token="A")


def test_batch_search_finds_member_that_fails_standalone_and_reprices_deletions() -> None:
    route = _route()
    reverse = V2Route((V2Leg(route.legs[1].pool, "A"), V2Leg(route.legs[0].pool, "B")))
    orders = (
        V2BatchOrder("a-reverse", reverse, 40, 1, 3, 1),
        V2BatchOrder("b-forward", route, 248, 5, 7, 1),
    )
    original = compare_v2_batch(orders, initial_capital=600, anchor_token="A")
    assert original.standalone[0].status == V2BatchStatus.BELOW_MINIMUM_NET

    result = search_v2_batches(
        orders,
        initial_capital=600,
        anchor_token="A",
        batch_fixed_cost=15,
        max_batch_orders=2,
        beam_width=4,
        max_leg_evaluations=40,
    )
    assert result.complete and result.best_net == 112
    assert result.winner is not None
    assert [item.order_id for item in result.winner.results] == ["b-forward", "a-reverse"]
    assert result.winner.results[1].quote is not None
    assert result.winner.results[1].quote.amounts == (40, 55, 42)
    assert result.winner.gross_profit - result.winner.cost_in_anchor - 15 == result.best_net
    assert [(item.order_ids, item.net_in_anchor) for item in result.shortlist] == [
        (("b-forward", "a-reverse"), 112),
        (("b-forward",), 111),
    ]
    forward, reverse_delta = result.marginals
    assert forward.incremental_net is None and forward.blocked_order_id == "a-reverse"
    assert forward.blocked_status == V2BatchStatus.BELOW_MINIMUM_NET
    assert (reverse_delta.without_order_net, reverse_delta.incremental_net) == (111, 1)


def test_batch_search_retains_below_overhead_prefix_and_reports_incomplete_search() -> None:
    orders = _orders()
    independent = V2Route(
        tuple(
            V2Leg(replace(leg.pool, pool_id=f"other-{leg.pool.pool_id}"), leg.token_in)
            for leg in orders[0].route.legs
        )
    )
    orders = (orders[0], replace(orders[1], route=independent))
    kwargs = {
        "initial_capital": 500,
        "anchor_token": "A",
        "batch_fixed_cost": 200,
        "max_batch_orders": 2,
    }
    result = search_v2_batches(orders, beam_width=2, max_leg_evaluations=8, **kwargs)
    assert result.complete and result.best_net == 60
    assert [item.order_ids for item in result.shortlist] == [
        ("first", "second"),
        ("second", "first"),
    ]
    assert [item.without_order_net for item in result.marginals] == [-70, -70]
    assert result.search_leg_evaluations == 8 and result.counterfactual_leg_evaluations == 4

    pruned = search_v2_batches(reversed(orders), beam_width=1, max_leg_evaluations=8, **kwargs)
    assert pruned.beam_pruned and not pruned.complete and pruned.best_net == 60
    exhausted = search_v2_batches(orders, beam_width=2, max_leg_evaluations=4, **kwargs)
    assert exhausted.budget_exhausted and not exhausted.complete
    assert exhausted.winner is None and exhausted.best_net == 0 and exhausted.shortlist == ()
