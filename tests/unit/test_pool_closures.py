from itertools import pairwise

import pytest

from oasis_agent.fastpath.pool_closures import PoolClosure, PoolLink, rank_pool_closures


def _validate(witness: PoolClosure, links: list[PoolLink]) -> None:
    assert witness.tokens[0] == witness.tokens[-1]
    assert len(set(witness.tokens[:-1])) == len(set(witness.pools)) == len(witness.pools)
    by_id = {link.pool_id: {link.token0, link.token1} for link in links}
    assert all(
        by_id[pid] == set(tokens)
        for pid, tokens in zip(witness.pools, pairwise(witness.tokens), strict=True)
    )


def test_two_long_tail_links_close_through_a_deep_baseline_path() -> None:
    existing = [
        PoolLink("a-b", "a", "b", 100),
        PoolLink("b-c", "b", "c", 90),
        PoolLink("shallow-shortcut", "a", "c", 5),
    ]
    candidates = [
        PoolLink("a-tail", "a", "tail", 50),
        PoolLink("c-tail", "c", "tail", 55),
        PoolLink("parallel", "a", "b", 20),
        PoolLink("only-one-link", "a", "leaf"),
    ]
    result = rank_pool_closures(existing, candidates, result_limit=10, pair_budget=10)
    assert result.individual_closures == result.two_addition_options == result.pair_checks == 1
    assert result.unsupported_by_one_or_two_additions == 1 and result.pair_scan_complete
    assert result.ranked[0].route_depth == 50
    assert set(result.ranked[0].pools) == {"a-b", "b-c", "a-tail", "c-tail"}
    assert result.ranked[1].pools == ("parallel", "a-b")
    for witness in result.ranked:
        _validate(witness, existing + candidates)


def test_pool_links_across_existing_components_leave_budget_visible() -> None:
    current = [PoolLink("a-x", "a", "x"), PoolLink("b-y", "b", "y", 100)]
    additions = [
        PoolLink(str(i), a, b, 100) for i, (a, b) in enumerate([("a", "b"), ("a", "y"), ("x", "b")])
    ]
    capped = rank_pool_closures(current, additions, result_limit=10, pair_budget=1)
    assert capped.individual_closures == capped.unsupported_by_one_or_two_additions == 0
    assert capped.two_addition_options == 3 and capped.pair_checks == 1
    assert capped.pair_scan_complete is False
    full = rank_pool_closures(current, additions, result_limit=10, pair_budget=4)
    assert full.pair_scan_complete and len(full.ranked) == 3
    assert [r.route_depth for r in full.ranked] == [100, None, None]
    for witness in full.ranked:
        _validate(witness, current + additions)
    with pytest.raises(ValueError, match="duplicate pool identity"):
        rank_pool_closures(current, current, result_limit=1, pair_budget=10)
