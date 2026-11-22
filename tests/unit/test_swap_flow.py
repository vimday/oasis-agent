from collections.abc import Iterable
from itertools import pairwise

import pytest

from oasis_agent.arbitrage.cycle_protocols import topic
from oasis_agent.evidence.public_receipts import ObservedReceipt
from oasis_agent.evidence.swap_flow import (
    TRANSFER,
    FundingLink,
    ObservedSwap,
    SwapLeg,
    analyze_swap_funding,
    direct_pool_funding,
    profile_swap_call_bundles,
    profile_swap_receipt,
)
from oasis_agent.models.chain import LogRecord
from oasis_agent.models.common import BlockRef

A, B, C, D, E = (f"0x{i:040x}" for i in range(1, 6))
P, Q = ("0x" + c * 40 for c in "ab")
BLOCK = "0x" + "f" * 64
TX = "0x" + "e" * 64
V3 = topic("Swap(address,address,int256,int256,uint160,uint128,int24)")
V4 = topic("Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)")
INFINITY = topic("Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24,uint16)")


def word(n: int) -> str:
    return f"0x{n:064x}"


def log(index: int, target: str, topic0: str, data_words: int, topic1: str = word(8)) -> LogRecord:
    return LogRecord(
        target, (topic0, topic1, word(9)), "0x" + "00" * 32 * data_words, 20, TX, index
    )


def receipt(logs: Iterable[LogRecord]) -> ObservedReceipt:
    return ObservedReceipt(BlockRef(1, 20, BLOCK), TX, 0, A, D, 1, 21_000, 9, tuple(logs))


def leg(index: int, token_in: str, token_out: str, qty_in: int, qty_out: int) -> SwapLeg:
    return SwapLeg(ObservedSwap(index, (P, None), "v3"), token_in, token_out, qty_in, qty_out)


def test_pool_manager_counts_separate_pool_id_from_repeated_events() -> None:
    tx = receipt([log(17, P, V4, 6), log(19, P, V4, 6), log(20, P, INFINITY, 7, word(11))])
    result = profile_swap_receipt(tx)
    assert len(result.events) == 3
    assert (result.distinct_emitters, result.distinct_pools, result.repeated_pool_events) == (
        1,
        2,
        1,
    )
    with pytest.raises(ValueError, match="malformed"):
        profile_swap_receipt(receipt([log(20, P, INFINITY, 6)]))


def test_direct_transfer_validates_callback_funding_order_and_duplicate_movements() -> None:
    transfer_topics = (TRANSFER, word(int(P, 16)), word(int(Q, 16)))
    transfer = LogRecord(B, transfer_topics, word(8), 20, TX, 4)
    tx = receipt([transfer, log(5, Q, V3, 5), log(7, P, V3, 5)])
    first, second = profile_swap_receipt(tx).events[::-1]
    legs = [SwapLeg(first, A, B, 7, 8), SwapLeg(second, B, C, 8, 9)]
    links = direct_pool_funding(tx, legs)
    report = analyze_swap_funding(legs, links)
    assert report.links == 1
    assert report.longest_witnessed_path == (7, 5)
    second_transfer = LogRecord(B, transfer_topics, word(8), 20, TX, 6)
    duplicate = receipt([transfer, tx.logs[1], second_transfer, tx.logs[2]])
    assert direct_pool_funding(duplicate, legs) == ()


def test_split_join_and_unresolved_financing_cycle() -> None:
    legs = (leg(1, A, B, 10, 10), leg(2, B, C, 6, 4), leg(3, B, C, 4, 3), leg(4, C, A, 7, 11))
    links = [
        FundingLink(1, 2, B, 6, "a"),
        FundingLink(1, 3, B, 4, "b"),
        FundingLink(2, 4, C, 4, "c"),
        FundingLink(3, 4, C, 3, "d"),
    ]
    report = analyze_swap_funding(legs, links)
    assert (
        report.witnessed_depth,
        report.linked_swaps,
        report.branch_swaps,
        report.join_swaps,
    ) == (
        3,
        4,
        1,
        1,
    )
    with pytest.raises(ValueError, match="overallocated"):
        analyze_swap_funding(legs, [*links, FundingLink(1, 3, B, 1, "e")])
    with pytest.raises(ValueError, match="duplicate"):
        analyze_swap_funding(legs, [*links, links[0]])
    cycle = analyze_swap_funding(legs, [*links, FundingLink(4, 1, A, 10, "repay")])
    assert cycle.cyclic_allocations and cycle.witnessed_depth is None
    assert cycle.longest_witnessed_path == ()


def test_funding_tracks_true_400_leg_depth_even_when_events_are_reverse_order() -> None:
    assets = (A, B, C, D, E)
    legs = [
        leg(400 - i, assets[i % len(assets)], assets[(i + 1) % len(assets)], 10, 10)
        for i in range(400)
    ]
    links = [
        FundingLink(a.event.log_index, b.event.log_index, a.token_out, 10, str(i))
        for i, (a, b) in enumerate(pairwise(legs))
    ]
    report = analyze_swap_funding(legs, links)
    assert report.witnessed_depth == 400
    assert report.longest_witnessed_path == tuple(range(400, 0, -1))


def test_call_bundle_count_requires_ancestors_and_matches_emitter_multiplicity() -> None:
    tx = receipt([log(5, Q, V3, 5), log(7, P, V3, 5), log(9, P, V3, 5)])
    selector = topic("swap(address,bool,int256,uint160,bytes)")[:10]
    branch_root = ((), A, "", False)
    frames = [
        branch_root,
        ((0,), A, "", False),
        ((0, 0), Q, selector, False),
        ((0, 1), P, selector, False),
        ((1,), P, selector, False),
        ((2,), B, "", True),
        ((2, 0), P, selector, False),
    ]
    bundle = profile_swap_call_bundles(tx, frames)
    assert bundle.complete_for_address_pool_events
    assert bundle.matched_address_pool_swap_calls == 3
    assert bundle.outer_branch_swap_counts == ((0, 2), (1, 1))
    assert not profile_swap_call_bundles(tx, frames[:-3]).complete_for_address_pool_events
    with pytest.raises(ValueError, match="ancestry"):
        profile_swap_call_bundles(tx, [branch_root, frames[-1]])
