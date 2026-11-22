"""Exact certificates for a snapshot's directed marginal exchange rates.

For a standard pool whose swap output is bounded by its initial fee-adjusted
marginal rate, a cycle of distinct pools also obeys the product bound before
execution costs. The graph accepts caller-supplied edges and cannot check the
snapshot, liquidity, compatibility with an executable route, or state changes
from reusing a pool. A positive cycle is a candidate for finite-size pricing.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from fractions import Fraction
from math import prod
from types import MappingProxyType


@dataclass(frozen=True, slots=True)
class MarginalEdge:
    """A fixed fee-adjusted rate in out-token base units per in-token base unit."""

    token_in: str
    token_out: str
    pool: str
    rate: Fraction

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str) and value for value in (self.token_in, self.token_out, self.pool)
        ):
            raise ValueError("token and pool identities must be nonempty strings")
        if not isinstance(self.rate, Fraction) or self.rate <= 0:
            raise ValueError("the marginal rate must be a positive Fraction")


@dataclass(frozen=True, slots=True)
class NoPositiveMarginalCycle:
    """Positive potentials satisfying potential(out) >= rate * potential(in)."""

    potentials: Mapping[str, Fraction]

    def verifies(self, edges: Iterable[MarginalEdge]) -> bool:
        """A valid certificate telescopes to a rate product at most one on every closed walk."""
        for edge in edges:
            input_potential = self.potentials.get(edge.token_in)
            output_potential = self.potentials.get(edge.token_out)
            if (
                not isinstance(input_potential, Fraction)
                or not isinstance(output_potential, Fraction)
                or input_potential <= 0
                or output_potential < edge.rate * input_potential
            ):
                return False
        return True


@dataclass(frozen=True, slots=True)
class PositiveMarginalCycle:
    """A connected, closed sequence of directed edges with a product above one."""

    edges: tuple[MarginalEdge, ...]

    @property
    def gain(self) -> Fraction:
        return prod((edge.rate for edge in self.edges), start=Fraction(1))


@dataclass(frozen=True, slots=True)
class _Path:
    edge: MarginalEdge
    previous: _Path | None


def _positive_cycle(path: _Path) -> PositiveMarginalCycle:
    reverse_walk = []
    item: _Path | None = path
    while item is not None:
        reverse_walk.append(item.edge)
        item = item.previous
    walk = tuple(reversed(reverse_walk))
    seen = {walk[0].token_in: 0}
    for end, edge in enumerate(walk, start=1):
        if edge.token_out in seen:
            cycle = PositiveMarginalCycle(walk[seen[edge.token_out] : end])
            if cycle.gain <= 1:
                raise RuntimeError("a strictly improving walk contains a nonpositive cycle")
            return cycle
        seen[edge.token_out] = end
    raise RuntimeError("a strictly improving walk did not contain a cycle")


def certify_marginal_graph(
    edges: Iterable[MarginalEdge],
) -> NoPositiveMarginalCycle | PositiveMarginalCycle:
    """Run multiplicative Bellman-Ford from every vertex, with exact rational comparisons.

    A round computes the best products using at most one more edge. If a
    round is unchanged, its products are the telescoping potentials. If the
    |V|-th round strictly improves a product, the improving walk repeats a
    vertex. Removing a cycle of product at most one from that walk would
    contradict the strict improvement over all walks of length at most |V|-1.
    """
    graph = tuple(edges)
    tokens = sorted({token for edge in graph for token in (edge.token_in, edge.token_out)})
    potentials = dict.fromkeys(tokens, Fraction(1))
    paths: dict[str, _Path] = {}
    last_improvement: _Path | None = None

    for _ in tokens:
        next_potentials = potentials.copy()
        next_paths = paths.copy()
        last_improvement = None
        for edge in graph:
            candidate = potentials[edge.token_in] * edge.rate
            if candidate > next_potentials[edge.token_out]:
                next_potentials[edge.token_out] = candidate
                last_improvement = _Path(edge, paths.get(edge.token_in))
                next_paths[edge.token_out] = last_improvement
        if last_improvement is None:
            break
        potentials, paths = next_potentials, next_paths

    if last_improvement is not None:
        return _positive_cycle(last_improvement)
    return NoPositiveMarginalCycle(MappingProxyType(potentials))
