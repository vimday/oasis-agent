"""Rank new constant-product pool links by the loops they add to a retained graph."""

from collections import defaultdict, deque
from collections.abc import Iterable
from dataclasses import dataclass
from itertools import combinations


@dataclass(frozen=True, slots=True)
class PoolLink:
    pool_id: str
    token0: str
    token1: str
    # Optional common-denomination, same-snapshot capacity proxy supplied by the caller.
    depth: int | None = None

    def __post_init__(self) -> None:
        if (
            not all(
                isinstance(value, str) and value
                for value in (self.pool_id, self.token0, self.token1)
            )
            or self.token0 == self.token1
        ):
            raise ValueError("a pool link needs an identity and two distinct tokens")
        if self.depth is not None and (type(self.depth) is not int or self.depth < 0):
            raise ValueError("pool depth must be a nonnegative integer or unknown")


@dataclass(frozen=True, slots=True)
class PoolClosure:
    additions: tuple[str, ...]
    pools: tuple[str, ...]
    tokens: tuple[str, ...]
    route_depth: int | None


@dataclass(frozen=True, slots=True)
class PoolClosureScan:
    baseline_pools: int
    candidate_pools: int
    individual_closures: int
    two_addition_options: int
    unsupported_by_one_or_two_additions: int
    pair_checks: int
    pair_scan_complete: bool
    ranked: tuple[PoolClosure, ...]


def rank_pool_closures(
    baseline: Iterable[PoolLink],
    candidates: Iterable[PoolLink],
    *,
    result_limit: int,
    pair_budget: int,
) -> PoolClosureScan:
    """Find loops adding one or two pools; the rest of each loop comes from the baseline.

    One pool closes a cycle when its endpoints are already connected. Two new
    pools close a cycle when both connect the same two different components.
    Each witness uses the corresponding baseline spanning-tree paths. Its
    depth is a proxy for ranking and is specific to that witness.
    """
    if any(type(n) is not int or n < 1 for n in (result_limit, pair_budget)):
        raise ValueError("closure limits must be positive integers")

    existing, proposed = tuple(baseline), tuple(candidates)
    links = {row.pool_id: row for row in (*existing, *proposed)}
    if len(links) != len(existing) + len(proposed):
        raise ValueError("duplicate pool identity")
    representative = {token: token for row in links.values() for token in (row.token0, row.token1)}

    def root(token: str) -> str:
        while representative[token] != token:
            representative[token] = representative[representative[token]]
            token = representative[token]
        return token

    tree: dict[str, list[tuple[str, str]]] = defaultdict(list)

    # Building deeper links first gives a maximum-spanning forest on known depths.
    def preferred(row: PoolLink) -> tuple[bool, int, str]:
        return row.depth is None, -(row.depth or 0), row.pool_id

    for row in sorted(existing, key=preferred):
        a, b = root(row.token0), root(row.token1)
        if a != b:
            representative[a] = b
            tree[row.token0].append((row.token1, row.pool_id))
            tree[row.token1].append((row.token0, row.pool_id))

    def forest_path(start: str, target: str) -> tuple[list[str], list[str]]:
        frontier = deque([start])
        parents: dict[str, tuple[str, str]] = {}
        seen = {start}
        while frontier and target not in seen:
            token = frontier.popleft()
            for neighbor, pool_id in tree[token]:
                if neighbor not in seen:
                    seen.add(neighbor)
                    parents[neighbor] = token, pool_id
                    frontier.append(neighbor)
        if target not in seen:
            raise RuntimeError("pool closure endpoints were disconnected")
        tokens = [target]
        pools = []
        while tokens[-1] != start:
            parent, pool_id = parents[tokens[-1]]
            tokens.append(parent)
            pools.append(pool_id)
        return list(reversed(tokens)), list(reversed(pools))

    ranked: list[PoolClosure] = []
    supported: set[str] = set()

    def record(additions: tuple[str, ...], pools: list[str], tokens: list[str]) -> None:
        depths = [links[pool].depth for pool in pools]
        known = [depth for depth in depths if depth is not None]
        depth = min(known) if len(known) == len(depths) else None
        ranked.append(PoolClosure(additions, tuple(pools), tuple(tokens), depth))
        ranked.sort(
            key=lambda row: (
                row.route_depth is None,
                -(row.route_depth or 0),
                len(row.additions),
                len(row.pools),
                row.additions,
            )
        )
        del ranked[result_limit:]

    crossings: dict[tuple[str, str], list[tuple[PoolLink, str, str]]] = defaultdict(list)
    singles = 0
    for row in sorted(proposed, key=preferred):
        first, second = root(row.token0), root(row.token1)
        if first == second:
            tokens, pools = forest_path(row.token1, row.token0)
            record((row.pool_id,), [row.pool_id, *pools], [row.token0, *tokens])
            singles += 1
            supported.add(row.pool_id)
        elif first < second:
            crossings[first, second].append((row, row.token0, row.token1))
        else:
            crossings[second, first].append((row, row.token1, row.token0))

    pair_options = sum(len(group) * (len(group) - 1) // 2 for group in crossings.values())
    supported.update(
        row.pool_id for group in crossings.values() if len(group) >= 2 for row, _, _ in group
    )
    inspected = 0
    for group in crossings.values():
        for (first_pool, a, b), (second_pool, c, d) in combinations(group, 2):
            if inspected == pair_budget:
                return PoolClosureScan(
                    len(existing),
                    len(proposed),
                    singles,
                    pair_options,
                    len(proposed) - len(supported),
                    inspected,
                    False,
                    tuple(ranked),
                )
            second_tokens, second_pools = forest_path(b, d)
            first_tokens, first_pools = forest_path(c, a)
            record(
                (first_pool.pool_id, second_pool.pool_id),
                [first_pool.pool_id, *second_pools, second_pool.pool_id, *first_pools],
                [a, *second_tokens, *first_tokens],
            )
            inspected += 1
    return PoolClosureScan(
        len(existing),
        len(proposed),
        singles,
        pair_options,
        len(proposed) - len(supported),
        inspected,
        True,
        tuple(ranked),
    )
