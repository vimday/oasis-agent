from fractions import Fraction

import pytest

from oasis_agent.fastpath.marginal_graph import (
    MarginalEdge,
    NoPositiveMarginalCycle,
    PositiveMarginalCycle,
    certify_marginal_graph,
)


@pytest.mark.parametrize("forward_gain", [Fraction(10001, 10000), Fraction(9999, 10000)])
def test_bidirectional_128_pool_ring(forward_gain: Fraction) -> None:
    n = 128
    fee_factor = Fraction(9975, 10000)
    edges = []
    for index in range(n):
        token_in, token_out = f"token-{index}", f"token-{(index + 1) % n}"
        pool = f"pool-{index}"
        forward_rate = forward_gain if index == n - 1 else Fraction(1)
        edges.extend(
            [
                MarginalEdge(token_in, token_out, pool, forward_rate),
                MarginalEdge(token_out, token_in, pool, fee_factor**2 / forward_rate),
            ]
        )

    result = certify_marginal_graph(edges)
    if forward_gain > 1:
        assert isinstance(result, PositiveMarginalCycle)
        assert result.gain == forward_gain
        assert len(result.edges) == len({edge.token_in for edge in result.edges}) == n
        assert len({edge.pool for edge in result.edges}) == n
        assert all(
            edge.token_out == result.edges[(i + 1) % n].token_in
            for i, edge in enumerate(result.edges)
        )
    else:
        assert isinstance(result, NoPositiveMarginalCycle)
        assert len(result.potentials) == n
        assert result.verifies(edges)


def test_exact_tie_and_parallel_edges_in_another_component() -> None:
    tied = [
        MarginalEdge("a", "b", "left", Fraction(5, 3)),
        MarginalEdge("b", "a", "right", Fraction(3, 5)),
    ]
    result = certify_marginal_graph(tied)
    assert isinstance(result, NoPositiveMarginalCycle)
    assert result.verifies(tied)

    # A disconnected component and parallel edge must remain in the search.
    other = [
        MarginalEdge("x", "y", "first", Fraction(1, 3)),
        MarginalEdge("x", "y", "better", Fraction(10, 21)),
        MarginalEdge("y", "x", "return", Fraction(11, 5)),
    ]
    result = certify_marginal_graph(tied + other)
    assert isinstance(result, PositiveMarginalCycle)
    assert {edge.pool for edge in result.edges} == {"better", "return"}
    assert result.gain == Fraction(22, 21)


def test_rate_must_be_positive() -> None:
    with pytest.raises(ValueError, match="positive Fraction"):
        MarginalEdge("a", "b", "pool", Fraction(0))
