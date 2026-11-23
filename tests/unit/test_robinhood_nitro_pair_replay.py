from pathlib import Path
from typing import cast

import pytest
from eth_abi import encode

from oasis_agent.evidence.public_receipts import ObservedReceipt
from oasis_agent.evidence.robinhood_nitro import (
    FeedMessage,
    FeedReplay,
    NitroHintJournal,
    RpcAnchor,
)
from oasis_agent.evidence.robinhood_pair import (
    POOL_MANAGER,
    SWAP_TOPIC,
    SWAP_TYPES,
    FeedArrival,
    receipt_swap_hints,
    replay_arrivals,
)
from oasis_agent.models.chain import LogRecord
from oasis_agent.models.common import BlockRef


def _hash(n: int) -> str:
    return f"0x{n:064x}"


def _arrival(source: str, ns: int, *sequences: int) -> FeedArrival:
    messages = tuple(FeedMessage(seq, _hash(seq), 100, "a" * 64, (), False) for seq in sequences)
    return FeedArrival(source, ns, ns, FeedReplay("b" * 64, "c" * 64, messages))


def test_interleaved_gap_and_restart_keep_historical_release_clock(tmp_path: Path) -> None:
    path = tmp_path / "replay.sqlite3"
    # Initial delayed observations can fill a missing message before the gap is
    # visible. The upcoming primary frame carries two messages atomically.
    arrivals = (
        _arrival("primary", 10, 100),
        _arrival("primary", 20, 101),
        _arrival("primary", 30, 102),
        _arrival("primary", 50, 103, 104),
        _arrival("delayed", 21, 101),
        _arrival("delayed", 60, 102, 103),
    )
    anchors = (RpcAnchor(103, _hash(103), 100, (), "d" * 64),)
    journal = NitroHintJournal(path)
    try:
        report = replay_arrivals(journal, arrivals, anchors, omit_primary=(101, 102))
    finally:
        journal.close()
    assert report["omitted_primary_sequences"] == [101, 102]
    assert report["anchor_prefix_release_ns"] == {103: 60}
    assert report["recovered_gaps"] == [
        {
            "first_missing_range": [102, 102],
            "started_ns": 50,
            "source_at_detection": "primary",
            "recovered_by": "delayed",
            "recovered_ns": 60,
            "observed_recovery_ns": 10,
            "released_through": 104,
        }
    ]
    journal = NitroHintJournal(path)
    try:
        repeated = replay_arrivals(journal, arrivals, anchors, omit_primary=(101, 102))
        assert repeated["recovered_gaps"] == []
        assert repeated["anchor_prefix_release_ns"] == {}
        assert cast(dict[str, int], repeated["ingest_counts"])["added"] == 0
        bad_anchor = (RpcAnchor(103, _hash(900), 100, (), "d" * 64),)
        invalidated = replay_arrivals(journal, arrivals, bad_anchor, omit_primary=(101, 102))
        assert cast(dict[str, object], invalidated["continuity"])["status"] == "invalidated"
        assert invalidated["anchor_prefix_release_ns"] == {}
    finally:
        journal.close()


def _receipt(*amounts: tuple[int, int, int]) -> ObservedReceipt:
    block = BlockRef(4663, 99, _hash(99), 100)
    tx_hash = _hash(500)
    logs = tuple(
        LogRecord(
            POOL_MANAGER,
            (SWAP_TOPIC, _hash(pool), _hash(90)),
            "0x" + encode(SWAP_TYPES, (d0, d1, 17, 27, -18, 125)).hex(),
            block.number,
            tx_hash,
            index,
        )
        for index, (pool, d0, d1) in enumerate(amounts)
    )
    return ObservedReceipt(block, tx_hash, 1, "0x" + "11" * 20, POOL_MANAGER, 1, 800, 2, logs)


def test_amount_hints_preserve_integer_amount_but_leave_currency_unknown() -> None:
    amount = (1 << 70) + 3
    hints = receipt_swap_hints(_receipt((1, amount, -123), (2, -amount, 890)))
    assert hints is not None
    assert hints["funding_verified"] is False
    assert hints["quote_state_verified"] is False
    assert hints["receipt_gas_cost_wei"] == 1600
    assert hints["exact_amount_hints"] == [
        {
            "caller": "0x" + _hash(90)[26:],
            "amount_raw": amount,
            "source_log": 0,
            "target_log": 1,
            "asset": None,
        }
    ]


@pytest.mark.parametrize(
    "trades",
    [
        ((1, -5, 7), (1, -7, 9)),
        ((1, -5, 7), (2, -7, 9), (3, -8, 7)),
    ],
)
def test_repeated_pool_and_ambiguous_amounts_are_excluded(
    trades: tuple[tuple[int, int, int], ...],
) -> None:
    hints = receipt_swap_hints(_receipt(*trades))
    assert hints is not None
    assert hints["exact_amount_hints"] == []
