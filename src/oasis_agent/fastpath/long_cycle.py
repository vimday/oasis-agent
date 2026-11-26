"""Offline bounds and bounded search for distinct-token, distinct-pool cycles.

The input is a fixed directed marginal graph. Topology bounds the number of
legs without estimating liquidity or the most profitable amount. A positive
marginal result still needs finite-size integer pricing and execution costs.
"""

from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from enum import StrEnum
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


class LongCycleStop(StrEnum):
    COMPLETE = "complete"
    EDGE_BUDGET = "edge_budget"
    WITNESS_BUDGET = "witness_budget"


@dataclass(frozen=True, slots=True)
class LongCycleWitness:
    cycle: PositiveMarginalCycle
    edge_visits: int


@dataclass(frozen=True, slots=True)
class LongCycleFrontier:
    topology: SimpleCycleTopology
    witnesses: tuple[LongCycleWitness, ...]
    edge_visits: int
    stop: LongCycleStop


@dataclass(slots=True)
class _SearchProgress:
    max_edge_visits: int
    max_witnesses: int
    edge_visits: int = 0
    witnesses: list[LongCycleWitness] = field(default_factory=list)


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
    required_pools: frozenset[str] | None,
    progress: _SearchProgress,
) -> LongCycleStop | None:
    adjacency: dict[str, list[MarginalEdge]] = defaultdict(list)
    for graph_edge in edges:
        adjacency[graph_edge.token_in].append(graph_edge)
    # Each oriented cycle has one smallest token, or one smallest changed pool
    # when searching only cycles that touch changed state.
    starts: Iterable[tuple[str, MarginalEdge | None]]
    if required_pools is None:
        starts = ((token, None) for token in sorted(adjacency))
    else:
        seeds = sorted(
            (edge for edge in edges if edge.pool in required_pools),
            key=lambda edge: (edge.pool, edge.token_in, edge.token_out),
        )
        starts = ((edge.token_in, edge) for edge in seeds)
    for origin, seed in starts:
        seen = {origin}
        pools: set[str] = set()
        path: list[MarginalEdge] = []
        products = [Fraction(1)]
        if seed is None:
            stack = [iter(adjacency[origin])]
        else:
            if progress.edge_visits == progress.max_edge_visits:
                return LongCycleStop.EDGE_BUDGET
            progress.edge_visits += 1
            seen.add(seed.token_out)
            pools.add(seed.pool)
            path.append(seed)
            products.append(seed.rate)
            stack = [iter(adjacency.get(seed.token_out, ()))]
        seed_depth = len(path)
        while stack:
            edge = next(stack[-1], None)
            if edge is None:
                stack.pop()
                if len(path) > seed_depth:
                    previous = path.pop()
                    seen.remove(previous.token_out)
                    pools.remove(previous.pool)
                    products.pop()
                continue
            if progress.edge_visits == progress.max_edge_visits:
                return LongCycleStop.EDGE_BUDGET
            progress.edge_visits += 1
            if edge.pool in pools:
                continue
            if (
                seed is not None
                and required_pools is not None
                and edge.pool in required_pools
                and edge.pool < seed.pool
            ):
                continue
            if edge.token_out == origin:
                if minimum <= len(path) + 1 and products[-1] * edge.rate > 1:
                    progress.witnesses.append(
                        LongCycleWitness(PositiveMarginalCycle((*path, edge)), progress.edge_visits)
                    )
                    if len(progress.witnesses) == progress.max_witnesses:
                        return LongCycleStop.WITNESS_BUDGET
            elif (
                len(path) + 1 < maximum
                and (seed is not None or edge.token_out > origin)
                and edge.token_out not in seen
            ):
                seen.add(edge.token_out)
                pools.add(edge.pool)
                path.append(edge)
                products.append(products[-1] * edge.rate)
                stack.append(iter(adjacency[edge.token_out]))
    return None


def search_long_marginal_frontier(
    edges: Iterable[MarginalEdge],
    *,
    min_hops: int,
    max_hops: int,
    max_edge_visits: int,
    max_witnesses: int,
    required_pools: frozenset[str] | None = None,
) -> LongCycleFrontier:
    """Collect positive simple cycles in traversal order up to either research budget.

    Edge visits count DFS work after topology decomposition. Reaching the
    witness cap stops immediately, even if it happened to be the last cycle.
    Only COMPLETE certifies enumeration of this marginal scope. The order
    is structural and gives no economic ranking or bound on unvisited paths.
    If required_pools is provided, each witness touches at least one of them.
    """
    if (
        any(type(value) is not int for value in (min_hops, max_hops, max_edge_visits))
        or not 2 <= min_hops <= max_hops
        or max_edge_visits < 1
    ):
        raise ValueError("require integer hop bounds 2 <= min <= max and a positive edge budget")
    if type(max_witnesses) is not int or max_witnesses < 1:
        raise ValueError("require a positive integer witness budget")
    if required_pools is not None and (
        not isinstance(required_pools, frozenset)
        or any(not isinstance(pool, str) or not pool for pool in required_pools)
    ):
        raise ValueError("required pools must be a frozenset of pool identities")
    topology, blocks = _decompose(edges)
    progress = _SearchProgress(max_edge_visits, max_witnesses)
    stop = LongCycleStop.COMPLETE
    for block, block_edges in blocks:
        if block.max_hops_upper < min_hops:
            continue
        reason = _bounded_dfs(
            block_edges,
            min_hops,
            min(max_hops, block.max_hops_upper),
            required_pools,
            progress,
        )
        if reason is not None:
            stop = reason
            break
    return LongCycleFrontier(topology, tuple(progress.witnesses), progress.edge_visits, stop)


def search_long_marginal_cycle(
    edges: Iterable[MarginalEdge],
    *,
    min_hops: int,
    max_hops: int,
    max_edge_visits: int,
    required_pools: frozenset[str] | None = None,
) -> LongCycleSearch:
    """Find the first positive simple cycle of the requested length, or report coverage.

    The edge budget applies after topology decomposition. If there is no
    witness and the edge budget was not exhausted, all eligible paths were
    excluded or checked. Work may be exponential in depth. If required_pools
    is provided, eligible cycles touch at least one of those pools. A full
    scan is needed after invalidation or missing observations.
    """
    frontier = search_long_marginal_frontier(
        edges,
        min_hops=min_hops,
        max_hops=max_hops,
        max_edge_visits=max_edge_visits,
        max_witnesses=1,
        required_pools=required_pools,
    )
    return LongCycleSearch(
        frontier.topology,
        frontier.witnesses[0].cycle if frontier.witnesses else None,
        frontier.edge_visits,
        frontier.stop is LongCycleStop.EDGE_BUDGET,
    )
