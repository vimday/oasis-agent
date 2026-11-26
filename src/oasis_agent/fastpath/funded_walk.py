"""Fixed-input, distinct-venue walks over one caller-supplied quote snapshot."""

from collections import defaultdict, deque
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from heapq import heappush, heapreplace

MAX_HOPS = 400


@dataclass(frozen=True, slots=True)
class WalkEdge:
    venue: str
    token_in: str
    token_out: str


@dataclass(frozen=True, slots=True)
class FundedClosure:
    edges: tuple[WalkEdge, ...]
    amounts: tuple[int, ...]
    cost: int
    net: int


@dataclass(frozen=True, slots=True)
class FundedWalkResult:
    top: tuple[FundedClosure, ...]
    complete: bool
    edge_visits: int
    quotes: int
    missing_quotes: int
    zero_outputs: int
    closures: int
    positive_gross: int
    positive_net: int


def search_funded_walks(
    edges: Iterable[WalkEdge],
    *,
    anchor: str,
    amount_in: int,
    quote: Callable[[WalkEdge, int], int | None],
    cost_by_hops: tuple[int, ...],
    min_hops: int,
    max_edge_visits: int,
    top_k: int,
    revisit_assets: bool,
) -> FundedWalkResult:
    """Return the best visited net closures, each spending exactly the preceding output.

    Costs (including zero) are supplied in anchor units, indexed by hop count.
    Each venue must be an independent, single-use quote resource for this model.
    None is an unknown quote at that size. Completion refers to the supported
    graph and quoted paths. A deadline or unvisited edge cannot certify optimality.
    """
    maximum = len(cost_by_hops) - 1
    if (
        not isinstance(anchor, str)
        or not anchor
        or type(amount_in) is not int
        or not 0 < amount_in < 1 << 256
        or type(min_hops) is not int
        or not 2 <= min_hops <= maximum <= MAX_HOPS
        or type(revisit_assets) is not bool
        or type(max_edge_visits) is not int
        or max_edge_visits <= 0
        or type(top_k) is not int
        or top_k <= 0
        or any(type(c) is not int or not 0 <= c < 1 << 256 for c in cost_by_hops)
    ):
        raise ValueError("invalid fixed-input walk cost or search bounds")
    outgoing: dict[str, list[WalkEdge]] = defaultdict(list)
    incoming: dict[str, set[str]] = defaultdict(set)
    seen_edges: set[WalkEdge] = set()
    for candidate in edges:
        if (
            not all(
                isinstance(s, str) and s
                for s in (candidate.venue, candidate.token_in, candidate.token_out)
            )
            or candidate.token_in == candidate.token_out
            or candidate in seen_edges
        ):
            raise ValueError("empty identity, self-edge or duplicate directed venue")
        seen_edges.add(candidate)
        outgoing[candidate.token_in].append(candidate)
        incoming[candidate.token_out].add(candidate.token_in)
    for destinations in outgoing.values():
        destinations.sort(key=lambda e: (e.venue, e.token_out))
    # Ignoring size and used venues only shortens distance; this prune cannot cut a return.
    distance = {anchor: 0}
    queue = deque([anchor])
    while queue:
        token = queue.popleft()
        for predecessor in incoming.get(token, ()):
            if predecessor not in distance:
                distance[predecessor] = distance[token] + 1
                queue.append(predecessor)
    stack: list[Iterator[WalkEdge]] = [iter(outgoing.get(anchor, ()))]
    path: list[WalkEdge] = []
    amounts = [amount_in]
    venues: set[str] = set()
    tokens = {anchor}
    best: list[tuple[int, int, FundedClosure]] = []
    visits = quotes = missing = zeros = closures = gross_positive = net_positive = 0
    while stack:
        edge = next(stack[-1], None)
        if edge is None:
            stack.pop()
            if path:
                last = path.pop()
                venues.remove(last.venue)
                amounts.pop()
                if not revisit_assets:
                    tokens.remove(last.token_out)
            continue
        if visits == max_edge_visits:
            break
        visits += 1
        hops = len(path) + 1
        closes = edge.token_out == anchor
        if (
            edge.venue in venues
            or (not revisit_assets and not closes and edge.token_out in tokens)
            or (not revisit_assets and closes and hops < min_hops)
            or hops + distance.get(edge.token_out, maximum + 1) > maximum
        ):
            continue
        quotes += 1
        out = quote(edge, amounts[-1])
        if out is None:
            missing += 1
            continue
        if type(out) is not int or not 0 <= out < 1 << 256:
            raise ValueError("invalid exact quote output")
        if out == 0:
            zeros += 1
            continue
        if closes and hops >= min_hops:
            closures += 1
            cost = cost_by_hops[hops]
            net = out - amount_in - cost
            gross_positive += out > amount_in
            net_positive += net > 0
            if len(best) < top_k or net > best[0][0]:
                row = (net, -closures, FundedClosure((*path, edge), (*amounts, out), cost, net))
                if len(best) < top_k:
                    heappush(best, row)
                else:
                    heapreplace(best, row)
        if hops < maximum and (revisit_assets or not closes):
            path.append(edge)
            amounts.append(out)
            venues.add(edge.venue)
            if not revisit_assets:
                tokens.add(edge.token_out)
            stack.append(iter(outgoing.get(edge.token_out, ())))
    return FundedWalkResult(
        tuple(item[2] for item in sorted(best, reverse=True)),
        not stack,
        visits,
        quotes,
        missing,
        zeros,
        closures,
        gross_positive,
        net_positive,
    )
