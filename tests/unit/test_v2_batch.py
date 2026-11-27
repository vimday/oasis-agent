from dataclasses import replace

import pytest

from oasis_agent.fastpath.v2_batch import (
    V2BatchOrder,
    V2BatchStatus,
    compare_v2_batch,
    compare_v2_route_repetitions,
    evaluate_v2_repeated_route,
    search_v2_batches,
    search_v2_full_output_switches,
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


def test_fixed_principal_prefixes_match_independent_full_prefix_replays() -> None:
    order = replace(_orders()[0], amount_in=29)
    report = evaluate_v2_repeated_route(
        order, initial_capital=400, batch_fixed_cost=64, max_rounds=200
    )
    assert report.stop_reason == "below_minimum_net" and 1 < report.best_rounds < 200
    assert report.prefixes[0].cumulative_net < 0 < report.best_net
    for prefix in report.prefixes:
        sequence = [replace(order, order_id=f"round-{i}") for i in range(prefix.rounds)]
        run = compare_v2_batch(sequence, initial_capital=336, anchor_token="A").ordered
        assert prefix.cumulative_net == run.final_capital - 400
        assert prefix.amount_out == run.results[-1].quote.amount_out
    assert sum(p.marginal_net for p in report.prefixes) == report.prefixes[-1].cumulative_net
    assert report.best_net == max(p.cumulative_net for p in report.prefixes)
    rejected = evaluate_v2_repeated_route(
        order, initial_capital=38, batch_fixed_cost=10, max_rounds=2
    )
    assert (rejected.stop_reason, rejected.best_net, rejected.prefixes) == (
        "insufficient_capital",
        0,
        (),
    )


def test_reinvestment_matches_separate_replays_from_the_original_pools() -> None:
    order = replace(_orders()[0], amount_in=29)
    compared = compare_v2_route_repetitions(
        order, initial_capital=400, batch_fixed_cost=64, max_rounds=25
    )
    sequence = []
    amount = order.amount_in
    reports = compared.fixed_principal, compared.full_output
    assert reports[0].prefixes[0] == reports[1].prefixes[0]
    assert len(compared.full_output.prefixes) >= 2
    for row in compared.full_output.prefixes:
        assert row.amount_in == amount
        sequence.append(replace(order, order_id=f"round-{row.rounds}", amount_in=amount))
        replay = compare_v2_batch(sequence, initial_capital=336, anchor_token="A").ordered
        quote = replay.results[-1].quote
        assert quote is not None
        assert row.amount_out == quote.amount_out
        assert row.capital_after == replay.final_capital
        assert row.cumulative_cost == 64 + replay.cost_in_anchor
        assert row.cumulative_net == replay.gross_profit - row.cumulative_cost
        amount = quote.amount_out
    reinvest = compared.full_output
    best = reinvest.prefixes[reinvest.best_rounds - 1]
    assert (
        best.cumulative_net
        == reinvest.best_net
        == max(0, *(row.cumulative_net for row in reinvest.prefixes))
    )
    assert best.cumulative_cost == reinvest.best_cost
    assert sum(row.marginal_net for row in reinvest.prefixes) == (
        reinvest.prefixes[-1].cumulative_net
    )


def test_reinvestment_stops_if_idle_capital_cannot_cover_the_full_next_input_and_cost() -> None:
    order = replace(_orders()[0], amount_in=29)
    compared = compare_v2_route_repetitions(
        order, initial_capital=44, batch_fixed_cost=8, max_rounds=3
    )
    reinvest = compared.full_output
    assert len(reinvest.prefixes) == 1
    assert len(compared.fixed_principal.prefixes) > 1
    first = reinvest.prefixes[0]
    assert first.capital_after - first.amount_out == 2
    assert reinvest.rejected_next_input == first.amount_out
    assert reinvest.stop_reason == "insufficient_capital" and reinvest.failed_hop is None
    assert reinvest.best_rounds == 1 and reinvest.best_cost == 13
    assert reinvest.best_net == first.cumulative_net

    hold = compare_v2_route_repetitions(
        order, initial_capital=35, batch_fixed_cost=8, max_rounds=3
    ).full_output
    assert not hold.prefixes and hold.rejected_next_input == order.amount_in
    assert hold.best_rounds == hold.best_net == hold.best_cost == 0


def _switch_orders() -> tuple[V2BatchOrder, ...]:
    pools = (
        V2Pool("p0", "A", "B", 2900, 3600, 997, 1000),
        V2Pool("p1", "A", "B", 2200, 1600, 997, 1000),
        V2Pool("p2", "A", "B", 1200, 500, 997, 1000),
    )
    return tuple(
        V2BatchOrder(
            f"p{i}-p{j}",
            V2Route((V2Leg(pools[i], "A"), V2Leg(pools[j], "B"))),
            30,
            2,
            3,
        )
        for i, j in ((0, 1), (0, 2), (1, 2))
    )


def test_switching_three_pools_beats_both_fixed_route_arms_and_replays_exactly() -> None:
    orders = _switch_orders()
    kwargs = {"initial_capital": 2000, "batch_fixed_cost": 8, "max_rounds": 8}
    switched = search_v2_full_output_switches(
        orders, anchor_token="A", max_leg_evaluations=200, **kwargs
    )
    best = switched.best
    fixed = [compare_v2_route_repetitions(order, **kwargs) for order in orders]
    assert best.best_net == 194 > max(item.full_output.best_net for item in fixed) == 167
    assert max(item.fixed_principal.best_net for item in fixed) == 153
    assert best.best_rounds == 4 and best.best_cost == 8 + 4 * 2
    assert best.stop_reason == "below_minimum_net" and best.unexecuted_next_input == 240
    assert [(row.order_id, row.amount_in, row.amount_out) for row in best.prefixes] == [
        ("p0-p2", 30, 80),
        ("p0-p2", 80, 166),
        ("p0-p1", 166, 221),
        ("p0-p2", 221, 240),
    ]
    assert not switched.budget_exhausted
    assert switched.quoted_legs == switched.search_leg_evaluations == 30
    assert switched.extensions_evaluated == 15

    by_id = {order.order_id: order for order in orders}
    sequence = []
    for row in best.prefixes:
        sequence.append(
            replace(by_id[row.order_id], order_id=f"round-{row.rounds}", amount_in=row.amount_in)
        )
        prefix = compare_v2_batch(sequence, initial_capital=1992, anchor_token="A").ordered
        quote = prefix.results[-1].quote
        assert quote is not None and quote.amount_out == row.amount_out
        assert prefix.final_capital == row.capital_after
        assert row.cumulative_net == prefix.final_capital - 2000
        assert row.cumulative_cost == 8 + prefix.cost_in_anchor


def test_switching_budget_drops_incomplete_rounds_and_breaks_exact_ties_stably() -> None:
    orders = _switch_orders()
    kwargs = {"initial_capital": 2000, "anchor_token": "A", "batch_fixed_cost": 8}
    unfunded = search_v2_full_output_switches(orders, max_rounds=3, max_leg_evaluations=5, **kwargs)
    assert unfunded.budget_exhausted and unfunded.extensions_evaluated == 0
    assert not unfunded.best.prefixes and unfunded.best.best_rounds == 0
    one = search_v2_full_output_switches(orders, max_rounds=3, max_leg_evaluations=11, **kwargs)
    assert one.budget_exhausted and one.extensions_evaluated == 3
    assert one.best.search_leg_evaluations == 6 and len(one.best.prefixes) == 1
    assert one.best.unexecuted_next_input == one.best.prefixes[0].amount_out

    route = orders[1]
    tie_and_other_lot = (
        replace(route, order_id="z"),
        replace(route, order_id="other-lot", amount_in=31),
        replace(route, order_id="a"),
    )
    tied = search_v2_full_output_switches(
        tie_and_other_lot, max_rounds=1, max_leg_evaluations=6, **kwargs
    )
    same, other = tied.groups
    assert same.initial_principal == 30 and same.menu_order_ids == ("a", "z")
    assert same.prefixes[0].order_id == "a"
    assert other.initial_principal == 31 and other.menu_order_ids == ("other-lot",)
    assert other.prefixes[0].amount_out == route.route.quote(31).amount_out
    assert tied == search_v2_full_output_switches(
        reversed(tie_and_other_lot), max_rounds=1, max_leg_evaluations=6, **kwargs
    )


def test_repeat_search_bounds_rounds_and_legs_and_preserves_shortest_tie() -> None:
    def route(n: int) -> V2Route:
        return V2Route(
            tuple(
                V2Leg(V2Pool(str(i), str(i), str((i + 1) % n), 10**20, 10**20, 1, 1), str(i))
                for i in range(n)
            )
        )

    def run(n: int, rounds: int):
        return evaluate_v2_repeated_route(
            V2BatchOrder("repeat", route(n), 1000, 0, 0, -100),
            initial_capital=10**8,
            batch_fixed_cost=0,
            max_rounds=rounds,
        )

    assert len(run(2, 200).prefixes) == 200
    capped = run(3, 200)
    assert len(capped.prefixes) == 512 // 3 and capped.stop_reason == "batch_leg_limit"
    assert capped.best_rounds == capped.best_net == 0
    switched = search_v2_full_output_switches(
        (V2BatchOrder("repeat", route(3), 1000, 0, 0, -100),),
        initial_capital=10**8,
        anchor_token="0",
        batch_fixed_cost=0,
        max_rounds=200,
        max_leg_evaluations=1000,
    ).best
    assert len(switched.prefixes) == 512 // 3 and switched.stop_reason == "batch_leg_limit"
    assert switched.prefixes[-1].cumulative_legs == 510 and switched.best_rounds == 0
    with pytest.raises(ValueError, match="maximum repeated rounds"):
        run(2, 201)


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
