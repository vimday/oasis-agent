"""Offline bounds and bounded search for distinct-token, distinct-pool cycles.

The input is a fixed directed marginal graph. Topology bounds the number of
legs without estimating liquidity or the most profitable amount. A positive
marginal result still needs finite-size integer pricing and execution costs.
"""

from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from fractions import Fraction

from .marginal_graph import MarginalEdge, PositiveMarginalCycle


@dataclass(frozen=True, slots=True)
class CycleBlockTopology:
    token_count: int
    pool_count: int
    directed_edge_count: int

    @property
    def max_hops_upper(self) -> int:
        return min(self.token_count, self.pool_count)


@dataclass(frozen=True, slots=True)
class SimpleCycleTopology:
    token_count: int
    pool_count: int
    directed_edge_count: int
    strongly_connected_components: int
    cycle_blocks: tuple[CycleBlockTopology, ...]

    @property
    def max_hops_upper(self) -> int:
        """A necessary bound for every simple cycle of two or more legs."""
        return max((block.max_hops_upper for block in self.cycle_blocks), default=0)


@dataclass(frozen=True, slots=True)
class LongCycleSearch:
    topology: SimpleCycleTopology
    witness: PositiveMarginalCycle | None
    edge_visits: int
    budget_exhausted: bool


type _Link = tuple[str, str, str]


def _link(edge: MarginalEdge) -> _Link:
    lower, upper = sorted((edge.token_in, edge.token_out))
    return lower, upper, edge.pool


def _snapshot_edges(edges: Iterable[MarginalEdge]) -> tuple[MarginalEdge, ...]:
    seen: dict[tuple[str, str, str], MarginalEdge] = {}
    for edge in edges:
        if edge.token_in != edge.token_out:
            previous = seen.setdefault((edge.token_in, edge.token_out, edge.pool), edge)
            if previous.rate != edge.rate:
                raise ValueError("conflicting directed marginal rates for one pool")
    return tuple(seen.values())


def _strong_components(
    edges: tuple[MarginalEdge, ...],
) -> tuple[dict[str, int], int]:
    outgoing: dict[str, list[str]] = defaultdict(list)
    incoming: dict[str, list[str]] = defaultdict(list)
    for edge in edges:
        outgoing[edge.token_in].append(edge.token_out)
        incoming[edge.token_out].append(edge.token_in)
    tokens = sorted(outgoing.keys() | incoming.keys())
    seen: set[str] = set()
    order: list[str] = []
    for token in tokens:
        stack = [(token, False)]
        while stack:
            node, finish = stack.pop()
            if finish:
                order.append(node)
            elif node not in seen:
                seen.add(node)
                stack.append((node, True))
                stack.extend(
                    (neighbor, False) for neighbor in outgoing[node] if neighbor not in seen
                )
    components: dict[str, int] = {}
    count = 0
    for origin in reversed(order):
        if origin in components:
            continue
        components[origin] = count
        remaining = [origin]
        while remaining:
            node = remaining.pop()
            for neighbor in incoming[node]:
                if neighbor not in components:
                    components[neighbor] = count
                    remaining.append(neighbor)
        count += 1
    return components, count


def _biconnected_links(links: Iterable[_Link]) -> Iterator[tuple[_Link, ...]]:
    """Iterative low-link decomposition using physical multigraph edge identity."""
    neighbors: dict[str, list[tuple[str, _Link]]] = defaultdict(list)
    for link in links:
        lower, upper, _ = link
        neighbors[lower].append((upper, link))
        neighbors[upper].append((lower, link))
    discovery: dict[str, int] = {}
    low: dict[str, int] = {}
    path: list[_Link] = []
    for origin in sorted(neighbors):
        if origin in discovery:
            continue
        discovery[origin] = low[origin] = len(discovery)
        stack: list[tuple[str, _Link | None, Iterator[tuple[str, _Link]]]] = [
            (origin, None, iter(neighbors[origin]))
        ]
        while stack:
            node, parent_link, adjacent = stack[-1]
            following = next(adjacent, None)
            if following is None:
                stack.pop()
                if parent_link is not None:
                    parent = stack[-1][0]
                    low[parent] = min(low[parent], low[node])
                    if low[node] >= discovery[parent]:
                        partition = [path.pop()]
                        while partition[-1] != parent_link:
                            partition.append(path.pop())
                        if len(partition) > 1:
                            yield tuple(partition)
                continue
            neighbor, link = following
            if link == parent_link:
                continue
            if neighbor not in discovery:
                discovery[neighbor] = low[neighbor] = len(discovery)
                path.append(link)
                stack.append((neighbor, link, iter(neighbors[neighbor])))
            elif discovery[neighbor] < discovery[node]:
                low[node] = min(low[node], discovery[neighbor])
                path.append(link)


def _decompose(
    edges: Iterable[MarginalEdge],
) -> tuple[SimpleCycleTopology, list[tuple[CycleBlockTopology, tuple[MarginalEdge, ...]]]]:
    graph = _snapshot_edges(edges)
    components, component_count = _strong_components(graph)
    within: dict[int, dict[_Link, list[MarginalEdge]]] = {}
    for edge in graph:
        src = components[edge.token_in]
        if src == components[edge.token_out]:
            within.setdefault(src, {}).setdefault(_link(edge), []).append(edge)
    blocks = []
    for connected in within.values():
        for partition in _biconnected_links(connected):
            block = CycleBlockTopology(
                token_count=len({token for u, v, _ in partition for token in (u, v)}),
                pool_count=len({pool for _, _, pool in partition}),
                directed_edge_count=sum(len(connected[link]) for link in partition),
            )
            if block.max_hops_upper >= 2:
                blocks.append(
                    (block, tuple(edge for link in partition for edge in connected[link]))
                )
    topology = SimpleCycleTopology(
        token_count=len(components),
        pool_count=len({edge.pool for edge in graph}),
        directed_edge_count=len(graph),
        strongly_connected_components=component_count,
        cycle_blocks=tuple(block for block, _ in blocks),
    )
    return topology, blocks


def bound_simple_cycles(edges: Iterable[MarginalEdge]) -> SimpleCycleTopology:
    """Bound hops by distinct tokens and pools in each SCC's undirected cycle blocks.

    A directed simple cycle lies in one strongly connected component. Its
    distinct physical edges lie in one undirected biconnected block, including
    two parallel edges for a two-pool cycle. Ignoring direction within each
    block can overestimate the true maximum.
    """
    topology, _ = _decompose(edges)
    return topology


def _bounded_dfs(
    edges: tuple[MarginalEdge, ...],
    minimum: int,
    maximum: int,
    budget: int,
) -> tuple[PositiveMarginalCycle | None, int, bool]:
    adjacency: dict[str, list[MarginalEdge]] = defaultdict(list)
    for graph_edge in edges:
        adjacency[graph_edge.token_in].append(graph_edge)
    visits = 0
    # A simple cycle has exactly one smallest token. Fixing it as the origin
    # preserves directions and distinct pool choices without duplicate rotations.
    for origin in sorted(adjacency):
        stack = [iter(adjacency[origin])]
        seen = {origin}
        pools: set[str] = set()
        path: list[MarginalEdge] = []
        products = [Fraction(1)]
        while stack:
            edge = next(stack[-1], None)
            if edge is None:
                stack.pop()
                if path:
                    previous = path.pop()
                    seen.remove(previous.token_out)
                    pools.remove(previous.pool)
                    products.pop()
                continue
            if visits == budget:
                return None, visits, True
            visits += 1
            if edge.pool in pools:
                continue
            if edge.token_out == origin:
                if minimum <= len(path) + 1 and products[-1] * edge.rate > 1:
                    return PositiveMarginalCycle((*path, edge)), visits, False
            elif len(path) + 1 < maximum and edge.token_out > origin and edge.token_out not in seen:
                seen.add(edge.token_out)
                pools.add(edge.pool)
                path.append(edge)
                products.append(products[-1] * edge.rate)
                stack.append(iter(adjacency[edge.token_out]))
    return None, visits, False


def search_long_marginal_cycle(
    edges: Iterable[MarginalEdge],
    *,
    min_hops: int,
    max_hops: int,
    max_edge_visits: int,
) -> LongCycleSearch:
    """Find the first positive simple cycle of the requested length, or report coverage.

    The edge budget applies to exact DFS after topology decomposition. A
    missing witness with budget_exhausted set leaves the result unresolved.
    With a missing witness and no exhaustion, every eligible path in the
    depth band was excluded or checked. Work may be exponential in depth.
    """
    if (
        any(type(value) is not int for value in (min_hops, max_hops, max_edge_visits))
        or not 2 <= min_hops <= max_hops
        or max_edge_visits < 1
    ):
        raise ValueError("require integer hop bounds 2 <= min <= max and a positive edge budget")
    topology, blocks = _decompose(edges)
    visits = 0
    for block, block_edges in blocks:
        if block.max_hops_upper < min_hops:
            continue
        witness, spent, exhausted = _bounded_dfs(
            block_edges, min_hops, min(max_hops, block.max_hops_upper), max_edge_visits - visits
        )
        visits += spent
        if witness is not None or exhausted:
            return LongCycleSearch(topology, witness, visits, exhausted)
    return LongCycleSearch(topology, None, visits, False)
