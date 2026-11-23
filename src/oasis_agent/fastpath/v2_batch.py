"""Same-capital counterfactuals for ordered V2 cycles on one coherent snapshot.

This model executes caller-supplied fixed sizes and order. Each successful
route commits full-input reserve changes. A failed route leaves every pool
unchanged and pays its caller-supplied failure cost. Token transfer policy,
transaction inclusion, gas measurement, and shared transaction overhead
are outside this research model.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
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
    route = order.route if legs == order.route.legs else V2Route(legs)
    quote = route.quote(order.amount_in)
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
    if len({order.order_id for order in sequence}) != len(sequence):
        raise ValueError("batch orders require distinct IDs")
    pools: dict[str, V2Pool] = {}
    for order in sequence:
        if order.route.anchor != anchor:
            raise ValueError("all batch orders must return the declared anchor")
        for leg in order.route.legs:
            pool = leg.pool
            if pools.setdefault(pool.pool_id, pool) != pool:
                raise ValueError(f"conflicting starting pool state for {pool.pool_id}")

    return V2BatchComparison(
        anchor,
        _run(sequence, initial_capital, pools, carry_pool_state=True),
        _run(sequence, initial_capital, pools, carry_pool_state=False),
        tuple(_attempt(order, initial_capital, pools)[0] for order in sequence),
    )
