"""Same-capital counterfactuals for ordered V2 cycles on one coherent snapshot.

This model executes caller-supplied fixed sizes and order. Each successful
route commits full-input reserve changes. A failed route leaves every pool
unchanged and pays its caller-supplied failure cost. Token transfer policy,
transaction inclusion, gas measurement, and shared transaction overhead
are outside this research model.
"""

from collections import ChainMap, Counter
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from heapq import nsmallest
from itertools import pairwise

from .v2_sizing import (
    _MAX_UINT112,
    _MAX_UINT256,
    V2Leg,
    V2Pool,
    V2Route,
    V2RouteQuote,
    _identity,
    _uint,
)

MAX_V2_BATCH_ORDERS = 64
MAX_V2_BATCH_LEGS = 512
MAX_V2_BATCH_SEARCH_LEGS = 100_000
MAX_V2_BATCH_BEAM_WIDTH = 128
V2_BATCH_SHORTLIST_SIZE = 8


@dataclass(frozen=True, slots=True)
class V2BatchOrder:
    order_id: str
    route: V2Route
    amount_in: int
    success_cost_in_anchor: int
    failure_cost_in_anchor: int
    min_net_in_anchor: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "order_id", _identity(self.order_id))
        _uint(self.amount_in, 1, _MAX_UINT256, "amount in")
        _uint(self.success_cost_in_anchor, 0, _MAX_UINT256, "success cost")
        _uint(self.failure_cost_in_anchor, 0, _MAX_UINT256, "failure cost")
        _uint(self.min_net_in_anchor, -_MAX_UINT256, _MAX_UINT256, "minimum net")


class V2BatchStatus(StrEnum):
    EXECUTED = "executed"
    INSUFFICIENT_CAPITAL = "insufficient_capital"
    ZERO_OUTPUT = "zero_output"
    RESERVE_OVERFLOW = "reserve_overflow"
    ANCHOR_OVERFLOW = "anchor_overflow"
    BELOW_MINIMUM_NET = "below_minimum_net"


@dataclass(frozen=True, slots=True)
class V2BatchEntry:
    """A modeled outcome. The quote on a failed attempt is diagnostic, and hops start at one."""

    order_id: str
    status: V2BatchStatus
    quote: V2RouteQuote | None
    capital_before: int
    capital_after: int
    cost_in_anchor: int
    failure_hop: int | None = None

    @property
    def gross_profit(self) -> int:
        if self.status == V2BatchStatus.EXECUTED and self.quote is not None:
            return self.quote.gross_profit
        return 0

    @property
    def net_profit(self) -> int:
        return self.capital_after - self.capital_before


@dataclass(frozen=True, slots=True)
class V2BatchRun:
    initial_capital: int
    final_capital: int
    results: tuple[V2BatchEntry, ...]
    final_pools: tuple[V2Pool, ...]

    @property
    def net_profit(self) -> int:
        return self.final_capital - self.initial_capital

    @property
    def gross_profit(self) -> int:
        return sum(item.gross_profit for item in self.results)

    @property
    def cost_in_anchor(self) -> int:
        return sum(item.cost_in_anchor for item in self.results)

    @property
    def success_count(self) -> int:
        return sum(item.status == V2BatchStatus.EXECUTED for item in self.results)


@dataclass(frozen=True, slots=True)
class V2BatchComparison:
    anchor_token: str
    ordered: V2BatchRun
    # This arm carries capital and costs forward but always prices the original market.
    independent: V2BatchRun
    # Each order starts from the original market and the original capital.
    standalone: tuple[V2BatchEntry, ...]

    @property
    def best_standalone_net(self) -> int:
        return max((entry.net_profit for entry in self.standalone), default=0)


def _attempt(
    order: V2BatchOrder, capital: int, pools: Mapping[str, V2Pool]
) -> tuple[V2BatchEntry, dict[str, V2Pool]]:
    # Both outcomes must be fundable before attempting the route.
    commitment = order.amount_in + max(order.success_cost_in_anchor, order.failure_cost_in_anchor)
    if commitment > capital:
        return (
            V2BatchEntry(
                order.order_id,
                V2BatchStatus.INSUFFICIENT_CAPITAL,
                None,
                capital,
                capital,
                0,
            ),
            {},
        )

    legs = tuple(V2Leg(pools[leg.pool.pool_id], leg.token_in) for leg in order.route.legs)
    amounts = [order.amount_in]
    for leg in legs:
        amounts.append(leg.quote_exact_input(amounts[-1]))
    quote = V2RouteQuote(tuple(amounts))
    failure_cost = order.failure_cost_in_anchor

    def revert(status: V2BatchStatus, hop: int | None = None) -> V2BatchEntry:
        return V2BatchEntry(
            order.order_id, status, quote, capital, capital - failure_cost, failure_cost, hop
        )

    updates = {}
    for index, (leg, (amount_in, amount_out)) in enumerate(
        zip(legs, pairwise(quote.amounts), strict=True), start=1
    ):
        if amount_out == 0:
            return revert(V2BatchStatus.ZERO_OUTPUT, index), {}
        pool = leg.pool
        if leg.token_in == pool.token0:
            reserve0, reserve1 = pool.reserve0 + amount_in, pool.reserve1 - amount_out
        else:
            reserve0, reserve1 = pool.reserve0 - amount_out, pool.reserve1 + amount_in
        if max(reserve0, reserve1) > _MAX_UINT112:
            return revert(V2BatchStatus.RESERVE_OVERFLOW, index), {}
        updates[pool.pool_id] = replace(pool, reserve0=reserve0, reserve1=reserve1)

    net = quote.gross_profit - order.success_cost_in_anchor
    if net < order.min_net_in_anchor:
        return revert(V2BatchStatus.BELOW_MINIMUM_NET), {}
    if capital + net > _MAX_UINT256:
        return revert(V2BatchStatus.ANCHOR_OVERFLOW), {}
    return (
        V2BatchEntry(
            order.order_id,
            V2BatchStatus.EXECUTED,
            quote,
            capital,
            capital + net,
            order.success_cost_in_anchor,
        ),
        updates,
    )


def _run(
    orders: tuple[V2BatchOrder, ...],
    initial_capital: int,
    initial_pools: dict[str, V2Pool],
    *,
    carry_pool_state: bool,
) -> V2BatchRun:
    capital = initial_capital
    pools = initial_pools.copy()
    results = []
    for order in orders:
        result, updates = _attempt(order, capital, pools)
        capital = result.capital_after
        if carry_pool_state:
            pools.update(updates)
        results.append(result)
    return V2BatchRun(initial_capital, capital, tuple(results), tuple(pools.values()))


def compare_v2_batch(
    orders: Iterable[V2BatchOrder], *, initial_capital: int, anchor_token: str
) -> V2BatchComparison:
    """Compare identical order sizes, costs, and initial capital across three arms.

    The ordered arm prices new reserves after each success. The independent
    arm uses original reserves for each order while retaining capital changes.
    Standalone orders each receive the original capital and original reserves.
    A strategy may separately use a zero-net hold baseline. A passed model
    does not establish execution or settlement on chain.

    Input must be one coherent chain snapshot in integer token base units.
    Duplicate identities with distinct starting reserves or pool parameters
    are rejected before any arm starts.
    """
    _uint(initial_capital, 0, _MAX_UINT256, "initial capital")
    anchor = _identity(anchor_token)
    sequence = tuple(orders)
    pools = _batch_pools(sequence, anchor)
    return V2BatchComparison(
        anchor,
        _run(sequence, initial_capital, pools, carry_pool_state=True),
        _run(sequence, initial_capital, pools, carry_pool_state=False),
        tuple(_attempt(order, initial_capital, pools)[0] for order in sequence),
    )


def _batch_pools(orders: tuple[V2BatchOrder, ...], anchor: str) -> dict[str, V2Pool]:
    if len({order.order_id for order in orders}) != len(orders):
        raise ValueError("batch orders require distinct IDs")
    pools: dict[str, V2Pool] = {}
    for order in orders:
        if order.route.anchor != anchor:
            raise ValueError("all batch orders must return the declared anchor")
        for leg in order.route.legs:
            pool = leg.pool
            if pools.setdefault(pool.pool_id, pool) != pool:
                raise ValueError(f"conflicting starting pool state for {pool.pool_id}")

    return pools


@dataclass(frozen=True, slots=True)
class V2BatchMarginal:
    order_id: str
    without_order_net: int | None
    incremental_net: int | None
    blocked_order_id: str | None = None
    blocked_status: V2BatchStatus | None = None


@dataclass(frozen=True, slots=True)
class V2BatchCandidate:
    order_ids: tuple[str, ...]
    net_in_anchor: int

    @property
    def rank(self) -> tuple[int, int, tuple[str, ...]]:
        return -self.net_in_anchor, len(self.order_ids), self.order_ids


@dataclass(frozen=True, slots=True)
class V2BatchSearch:
    initial_capital: int
    fixed_cost: int
    winner: V2BatchRun | None
    shortlist: tuple[V2BatchCandidate, ...]
    marginals: tuple[V2BatchMarginal, ...]
    extension_status_counts: tuple[tuple[V2BatchStatus, int], ...]
    search_leg_evaluations: int
    counterfactual_leg_evaluations: int
    beam_pruned: bool
    budget_exhausted: bool

    @property
    def best_net(self) -> int:
        return 0 if self.winner is None else self.winner.final_capital - self.initial_capital

    @property
    def complete(self) -> bool:
        return not (self.beam_pruned or self.budget_exhausted)

    @property
    def extensions_evaluated(self) -> int:
        return sum(count for _, count in self.extension_status_counts)


@dataclass(frozen=True, slots=True)
class _BatchPrefix:
    ids: tuple[str, ...]
    capital: int
    entries: tuple[V2BatchEntry, ...]
    pool_updates: dict[str, V2Pool]
    legs: int

    @property
    def rank(self) -> tuple[int, int, tuple[str, ...]]:
        return -self.capital, len(self.ids), self.ids


def search_v2_batches(
    orders: Iterable[V2BatchOrder],
    *,
    initial_capital: int,
    anchor_token: str,
    batch_fixed_cost: int,
    max_batch_orders: int,
    beam_width: int,
    max_leg_evaluations: int,
) -> V2BatchSearch:
    """Search fixed-size ordered subsets, carrying shared reserves and bankroll.

    An extension must execute in its prefix state. Standalone profitability is
    not an input filter. Every nonempty plan reserves the same modeled shared
    cost; hold costs zero. Completeness is relative to the frozen candidate
    menu, maximum orders and maximum total legs, and requires an unpruned search.
    """
    _uint(initial_capital, 1, _MAX_UINT256, "initial capital")
    _uint(batch_fixed_cost, 0, initial_capital - 1, "batch fixed cost")
    _uint(max_batch_orders, 1, MAX_V2_BATCH_ORDERS, "maximum batch orders")
    _uint(beam_width, 1, MAX_V2_BATCH_BEAM_WIDTH, "beam width")
    _uint(max_leg_evaluations, 1, MAX_V2_BATCH_SEARCH_LEGS, "leg evaluation budget")
    sequence = tuple(sorted(orders, key=lambda order: order.order_id))
    _uint(len(sequence), 1, MAX_V2_BATCH_ORDERS, "candidate count")
    pools = _batch_pools(sequence, _identity(anchor_token))
    working_capital = initial_capital - batch_fixed_cost
    frontier = [_BatchPrefix((), working_capital, (), {}, 0)]
    winner = None
    shortlist: list[V2BatchCandidate] = []
    statuses: Counter[V2BatchStatus] = Counter()
    search_legs = 0
    beam_pruned = budget_exhausted = False

    def extensions(prefixes: list[_BatchPrefix]) -> Iterator[_BatchPrefix]:
        nonlocal search_legs, budget_exhausted
        for prefix in prefixes:
            state = ChainMap(prefix.pool_updates, pools)
            for order in sequence:
                legs = len(order.route.legs)
                if order.order_id in prefix.ids or prefix.legs + legs > MAX_V2_BATCH_LEGS:
                    continue
                if search_legs + legs > max_leg_evaluations:
                    budget_exhausted = True
                    return
                search_legs += legs
                result, updates = _attempt(order, prefix.capital, state)
                statuses[result.status] += 1
                if result.status == V2BatchStatus.EXECUTED:
                    yield _BatchPrefix(
                        (*prefix.ids, order.order_id),
                        result.capital_after,
                        (*prefix.entries, result),
                        prefix.pool_updates | updates,
                        prefix.legs + legs,
                    )

    depth = min(max_batch_orders, len(sequence))
    for layer in range(1, depth + 1):
        # Keep the shortlist and one extra survivor to detect beam pruning.
        frontier = nsmallest(
            max(beam_width + 1, V2_BATCH_SHORTLIST_SIZE),
            extensions(frontier),
            key=lambda item: item.rank,
        )
        if frontier:
            candidate = frontier[0]
            if candidate.capital > initial_capital and (
                winner is None or candidate.rank < winner.rank
            ):
                winner = candidate
            shortlist.extend(
                V2BatchCandidate(item.ids, item.capital - initial_capital)
                for item in frontier
                if item.capital > initial_capital
            )
            shortlist = nsmallest(V2_BATCH_SHORTLIST_SIZE, shortlist, key=lambda item: item.rank)
        if not frontier or budget_exhausted:
            break
        if layer < depth and len(frontier) > beam_width:
            beam_pruned = True
        frontier = frontier[:beam_width]

    run = None
    marginals: tuple[V2BatchMarginal, ...] = ()
    counterfactual_legs = 0
    if winner is not None:
        run = V2BatchRun(
            working_capital,
            winner.capital,
            winner.entries,
            tuple((pools | winner.pool_updates).values()),
        )
        by_id = {order.order_id: order for order in sequence}
        marginals, counterfactual_legs = _batch_marginals(
            tuple(by_id[order_id] for order_id in winner.ids),
            initial_capital=initial_capital,
            batch_fixed_cost=batch_fixed_cost,
            winner_net=winner.capital - initial_capital,
            pools=pools,
        )
    return V2BatchSearch(
        initial_capital,
        batch_fixed_cost,
        run,
        tuple(shortlist),
        marginals,
        tuple(sorted(statuses.items())),
        search_legs,
        counterfactual_legs,
        beam_pruned,
        budget_exhausted,
    )


def _batch_marginals(
    orders: tuple[V2BatchOrder, ...],
    *,
    initial_capital: int,
    batch_fixed_cost: int,
    winner_net: int,
    pools: dict[str, V2Pool],
) -> tuple[tuple[V2BatchMarginal, ...], int]:
    results = []
    legs = 0
    for removed in orders:
        remaining = tuple(order for order in orders if order.order_id != removed.order_id)
        if not remaining:
            results.append(V2BatchMarginal(removed.order_id, 0, winner_net))
            continue
        legs += sum(len(order.route.legs) for order in remaining)
        counterfactual = _run(
            remaining, initial_capital - batch_fixed_cost, pools, carry_pool_state=True
        )
        blocked = next(
            (item for item in counterfactual.results if item.status != V2BatchStatus.EXECUTED),
            None,
        )
        if blocked is not None:
            results.append(
                V2BatchMarginal(removed.order_id, None, None, blocked.order_id, blocked.status)
            )
        else:
            net = counterfactual.final_capital - initial_capital
            results.append(V2BatchMarginal(removed.order_id, net, winner_net - net))
    return tuple(results), legs
