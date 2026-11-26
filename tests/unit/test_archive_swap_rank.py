from itertools import repeat

import pytest

from scripts.rank_archive_swaps import V2, V4, rank_transaction

TX = "0x" + "ab" * 32
BLOCK = "0x" + "cd" * 32


def row(index: int, pool: int, amount_in: int, amount_out: int, *, manager: bool = False):
    words = [-amount_in, amount_out, 1, 2, 3, 4] if manager else [amount_in, 0, 0, amount_out]
    return {
        "transaction_hash": TX,
        "block_number": "20",
        "block_hash": BLOCK,
        "transaction_index": "4",
        "log_index": str(index),
        "address": "0x" + (1 if manager else pool).to_bytes(20).hex(),
        "sig": V4 if manager else V2,
        "topic1": "0x" + pool.to_bytes(32).hex(),
        "data": "0x" + "".join(w.to_bytes(32, signed=True).hex() for w in words),
    }


def test_ranks_unique_amounts_in_either_log_order_and_keeps_manager_pool_ids():
    rows = [row(5, 1, 40, 93, manager=True), row(7, 2, 30, 40, manager=True), row(9, 3, 93, 111)]
    result = rank_transaction(TX, iter(rows))
    assert (result.events, result.pools, result.amount_depth, result.indices) == (
        3,
        3,
        3,
        (7, 5, 9),
    )


def test_ambiguous_amount_and_repeated_pool_do_not_extend_depth():
    result = rank_transaction(TX, iter([row(5, 1, 2, 40), row(7, 2, 40, 11), row(9, 3, 40, 12)]))
    assert result.amount_depth == 1
    result = rank_transaction(TX, iter([row(5, 1, 2, 40), row(7, 1, 40, 11)]))
    assert (result.amount_depth, result.pools) == (1, 1)


def test_rejects_duplicate_log_positions_and_malformed_data():
    first = row(5, 1, 2, 40)
    with pytest.raises(ValueError, match="inclusion or order"):
        rank_transaction(TX, repeat(first, 2))
    with pytest.raises(ValueError, match="width"):
        rank_transaction(TX, iter([first | {"data": "0x"}]))
