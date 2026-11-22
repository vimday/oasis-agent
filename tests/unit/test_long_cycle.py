from dataclasses import dataclass
from fractions import Fraction

import pytest

from oasis_agent.fastpath.long_cycle import bound_simple_cycles, search_long_marginal_cycle
from oasis_agent.fastpath.marginal_graph import (
    MarginalEdge,
    PositiveMarginalCycle,
    certify_marginal_graph,
)


@dataclass(frozen=True)
class _SyntheticPool:
    left: str
    right: str
    key: str
    reserve_left: int
    reserve_right: int
    fee_multiplier: int

    def rates(self) -> tuple[MarginalEdge, MarginalEdge]:
        ltr = Fraction(self.reserve_right * self.fee_multiplier, self.reserve_left * 10_000)
        rtl = Fraction(self.reserve_left * self.fee_multiplier, self.reserve_right * 10_000)
        return MarginalEdge(self.left, self.right, self.key, ltr), MarginalEdge(
            self.right, self.left, self.key, rtl
        )

    def output(self, token_in: str, input_raw: int) -> int:
        reserve_in, reserve_out = (
            (self.reserve_left, self.reserve_right)
            if token_in == self.left
            else (self.reserve_right, self.reserve_left)
        )
        post_fee = input_raw * self.fee_multiplier
        return post_fee * reserve_out // (reserve_in * 10_000 + post_fee)


def _ring(size: int, last_rate_numerator: int) -> list[_SyntheticPool]:
    scale = 10**22
    pools = []
    for index in range(size):
        fee = 9_975 if index % 2 else 9_970
        rate_numerator = last_rate_numerator if index == size - 1 else 10_000
        pools.append(
            _SyntheticPool(
                left=f"token-{index:03}",
                right=f"token-{(index + 1) % size:03}",
                key=f"pool-{index:03}",
                reserve_left=fee * scale * 10_000,
                reserve_right=rate_numerator * scale * 10_000,
                fee_multiplier=fee,
            )
        )
    return pools


def _input_to_output(
    witness: tuple[MarginalEdge, ...], pools: list[_SyntheticPool], amount: int
) -> int:
    pool_by_id = {pool.key: pool for pool in pools}
    for leg in witness:
        pool = pool_by_id[leg.pool]
        amount = pool.output(leg.token_in, amount)
    return amount


@pytest.mark.parametrize("size", [100, 200, 300])
@pytest.mark.parametrize("last_rate_numerator", [9_990, 10_010])
def test_long_ring_with_integer_execution_and_cost(size: int, last_rate_numerator: int) -> None:
    pools = _ring(size, last_rate_numerator)
    edges = [edge for pool in pools for edge in pool.rates()]
    assert bound_simple_cycles(edges).max_hops_upper == size
    result = search_long_marginal_cycle(
        edges, min_hops=size, max_hops=size, max_edge_visits=4 * size**2
    )
    assert not result.budget_exhausted
    initial = 10**9
    # Synthetic gas envelope, conversion to input-token raw units, and fixed financing.
    gas_units = 21_000 + 14_500 * size
    low_cost = (gas_units + 5) // 6 + 12_500
    high_cost = 2 * ((gas_units + 5) // 6) + 12_500
    forward = tuple(pool.rates()[0] for pool in pools)
    if last_rate_numerator < 10_000:
        assert result.witness is None
        assert _input_to_output(forward, pools, initial) < initial
        return
    assert result.witness is not None
    assert result.witness.gain == Fraction(last_rate_numerator, 10_000)
    assert (
        len(result.witness.edges) == len({edge.token_in for edge in result.witness.edges}) == size
    )
    assert len({edge.pool for edge in result.witness.edges}) == size
    gross = _input_to_output(result.witness.edges, pools, initial) - initial
    assert 0 < gross <= initial * (result.witness.gain - 1)
    assert gross - low_cost > 0
    assert (gross - high_cost < 0) == (size == 300)


def test_short_positive_witness_does_not_hide_long_ring() -> None:
    pools = _ring(100, 10_010)
    edges = [edge for pool in pools for edge in pool.rates()]
    gamma = Fraction(9_975, 10_000)
    edges.extend(
        [
            MarginalEdge("token-001", "token-000", "short-pool", Fraction(2)),
            MarginalEdge("token-000", "token-001", "short-pool", gamma**2 / 2),
        ]
    )
    first = certify_marginal_graph(edges)
    assert isinstance(first, PositiveMarginalCycle)
    assert len(first.edges) == 2
    long = search_long_marginal_cycle(edges, min_hops=100, max_hops=100, max_edge_visits=2_000)
    assert long.witness is not None and len(long.witness.edges) == 100
    capped = search_long_marginal_cycle(edges, min_hops=100, max_hops=100, max_edge_visits=1)
    assert capped.witness is None and capped.budget_exhausted and capped.edge_visits == 1


def test_parallel_two_pool_cycles_and_articulation_blocks() -> None:
    def directions(a: str, b: str, pool: str) -> list[MarginalEdge]:
        return [MarginalEdge(a, b, pool, Fraction(1)), MarginalEdge(b, a, pool, Fraction(1))]

    chain = directions("a", "b", "one") + directions("b", "c", "two")
    assert bound_simple_cycles(chain).max_hops_upper == 0
    multigraph = chain + directions("c", "a", "three") + directions("b", "c", "parallel")
    multigraph += (
        directions("c", "d", "four")
        + directions("d", "e", "five")
        + directions("e", "c", "six")
        + directions("x", "y", "other-first")
        + directions("x", "y", "other-second")
        + [MarginalEdge("e", "x", "unidirectional", Fraction(1))]
    )
    topology = bound_simple_cycles(multigraph)
    assert topology.max_hops_upper == 3
    assert topology.strongly_connected_components == 2
    assert sorted(block.max_hops_upper for block in topology.cycle_blocks) == [2, 3, 3]


def test_blocks_bound_shared_pool_count_and_conflicting_input() -> None:
    edges = [
        MarginalEdge("a", "b", "shared", Fraction(2)),
        MarginalEdge("b", "c", "shared", Fraction(2)),
        MarginalEdge("c", "a", "other", Fraction(2)),
    ]
    assert bound_simple_cycles(edges).max_hops_upper == 2
    result = search_long_marginal_cycle(edges, min_hops=2, max_hops=3, max_edge_visits=100)
    assert result.witness is None and not result.budget_exhausted
    with pytest.raises(ValueError, match="conflicting"):
        bound_simple_cycles([*edges, MarginalEdge("a", "b", "shared", Fraction(1))])
