import json
from itertools import permutations
from pathlib import Path

import pytest

from oasis_agent.fastpath.funded_walk import WalkEdge, search_funded_walks


def search(edges, **overrides):
    args = {
        "anchor": "A",
        "amount_in": 2,
        "quote": lambda e, n: n + 1,
        "cost_by_hops": (0, 0, 1, 1, 2, 2),
        "min_hops": 2,
        "max_edge_visits": 500,
        "top_k": 20,
        "revisit_assets": True,
    }
    return search_funded_walks(edges, **(args | overrides))


@pytest.mark.parametrize("repeat", [True, False])
def test_anchored_enumeration_matches_brute_force_with_shared_pool_and_gas(repeat):
    edges = [
        WalkEdge("p", "A", "B"),
        WalkEdge("p", "B", "A"),
        WalkEdge("q", "B", "A"),
        WalkEdge("r", "A", "C"),
        WalkEdge("s", "C", "A"),
        WalkEdge("t", "B", "C"),
    ]
    reference = {}
    for length in range(2, 6):
        for path in permutations(edges, length):
            tokens = ("A", *(e.token_out for e in path))
            if (
                tokens[-1] != "A"
                or any(e.token_in != t for e, t in zip(path, tokens[:-1], strict=True))
                or len({e.venue for e in path}) != length
                or (not repeat and len(set(tokens[:-1])) != length)
            ):
                continue
            reference[path] = length - (1 if length < 4 else 2)
    result = search(edges, revisit_assets=repeat)
    assert result.complete and result.closures == len(reference) == result.positive_net
    assert {r.edges: r.net for r in result.top} == reference
    assert search(reversed(edges), revisit_assets=repeat).top == result.top


def test_missing_zero_invalid_and_budget_termination():
    edges = [WalkEdge("a", "A", "B"), WalkEdge("b", "B", "A")]
    assert search(edges, max_edge_visits=1).complete is False
    assert search(edges, max_edge_visits=2, revisit_assets=False).complete
    assert search(edges, quote=lambda e, n: None).missing_quotes == 1
    assert search(edges, quote=lambda e, n: 0).zero_outputs == 1
    with pytest.raises(ValueError, match="quote"):
        search(edges, quote=lambda e, n: False)
    with pytest.raises(ValueError, match="duplicate"):
        search([edges[0], edges[0]])


def test_exhausted_top_k_continues_and_keeps_earliest_tie():
    edges = [WalkEdge("1", "A", "B"), *(WalkEdge(k, "B", "A") for k in ("2", "3", "4"))]
    result = search(
        edges,
        top_k=1,
        revisit_assets=False,
        quote=lambda e, n: n + (3 if e.venue in ("3", "4") else 1),
    )
    assert result.complete and result.closures == 3 and result.top[0].net == 3
    assert [e.venue for e in result.top[0].edges] == ["1", "3"]


def test_historical_54_leg_observed_amounts_return_to_usdc_seven_times():
    file = Path(__file__).resolve().parents[2] / "docs/verification/ETH_TWENTY_FLOW_20261127.json"
    legs = json.loads(file.read_bytes())["legs"][:54]
    quotes = {
        WalkEdge("/".join(filter(None, leg["pool"])), leg["token_in"], leg["token_out"]): (
            int(leg["amount_in"]),
            int(leg["amount_out"]),
        )
        for leg in legs
    }
    params = {
        "anchor": legs[0]["token_in"],
        "cost_by_hops": (0,) * 55,
        "quote": lambda e, n: quotes[e][1] if n == quotes[e][0] else None,
    }
    simple = search(quotes, **params, revisit_assets=False)
    walks = search(quotes, **params)
    assert simple.complete and walks.complete
    assert [(len(r.edges), r.amounts[-1]) for r in simple.top] == [(3, 16412)]
    assert [len(r.edges) for r in walks.top] == [54, 44, 32, 29, 27, 13, 3]
    assert walks.top[0].edges == tuple(quotes)
    assert walks.top[0].amounts == (2, *(int(leg["amount_out"]) for leg in legs))
