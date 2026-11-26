import csv
import json

import pytest

from scripts.screen_multihop_archive import COLUMNS, V4, audit_queue, screen


def word(n, size=32):
    return "0x" + n.to_bytes(size, "big").hex()


def events(tx, pools):
    return [
        {
            "day": "2026-11-13",
            "block_number": 101,
            "block_hash": word(1001),
            "transaction_hash": word(tx),
            "transaction_index": tx,
            "log_index": tx * 100 + i,
            "address": word(777, 20),
            "sig": V4,
            "topic1": word(pool),
            "data": word(tx + i) + "00" * 160,
        }
        for i, pool in enumerate(pools)
    ]


def write_inputs(tmp_path, rows, first):
    data = tmp_path / "archive.csv"
    with data.open("w", newline="") as f:
        writer = csv.DictWriter(f, COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    ref = tmp_path / "ref.json"
    ref.write_text(
        json.dumps([dict(row, date=row["day"], topics=[V4, row["topic1"]]) for row in first])
    )
    return data, ref


def test_rank_by_multiplicity_and_preserve_controls_by_event_band(tmp_path):
    first = events(1, [1, 1, 2, 3, 4])
    equal = events(2, [1, 1, 2, 3, 4])
    reversed_order = events(3, [4, 3, 2, 1, 1])
    different = events(4, [1, 5, 6, 7, 8])
    long = events(5, [1] * 20)
    no_overlap = events(6, [9, 10, 11, 12, 13])
    args = write_inputs(
        tmp_path, first + equal + reversed_order + different + long + no_overlap, first
    )
    result = screen(*args, chain_id=1)
    day = result["days"]["2026-11-13"]
    assert result["reference_in_archive"] and result["event_rows"] == 45
    assert day["reference_comparisons"] == {
        "compared": 5,
        "nonzero": 4,
        "at_least_half": 2,
        "same_ordered_family": 1,
    }
    assert [
        (x["tx"], x["shared_observations"], x["union_observations"]) for x in day["closest"]
    ] == [(word(2), 5, 5), (word(3), 5, 5), (word(4), 1, 9)]
    assert day["bands"]["5-19"]["distinct_ordered_families"] == 4
    assert [x["tx"] for x in day["bands"]["5-19"]["selected"]] == [word(1), word(3), word(4)]
    assert [x["tx"] for x in day["bands"]["20-49"]["selected"]] == [word(5)]
    assert day["highest_event_baseline"][0]["tx"] == word(5)


@pytest.mark.parametrize(
    "problem",
    [
        "pool",
        "tx_order",
        "duplicate",
        "log_range",
        "reorg",
        "ref_gap",
        "mixed_ref",
        "columns",
        "hash_alias",
    ],
)
def test_reject_inconsistent_evidence(tmp_path, problem):
    first = events(1, range(5))
    second = events(2, range(5))
    rows = [r.copy() for r in first + second]
    if problem == "pool":
        rows[0]["topic1"] = word(20)
    elif problem == "tx_order":
        rows = rows[5:] + rows[:5]
    elif problem == "duplicate":
        rows[1]["log_index"] = rows[0]["log_index"]
    elif problem == "log_range":
        for r in rows[5:]:
            r["log_index"] -= 100
    elif problem == "reorg":
        for r in rows[5:]:
            r["block_hash"] = word(1002)
    elif problem == "ref_gap":
        first[-1]["log_index"] += 1
    elif problem == "mixed_ref":
        first[-1]["transaction_hash"] = word(3)
    elif problem == "hash_alias":
        for r in rows[5:]:
            r["block_number"] += 1
    data, ref = write_inputs(tmp_path, rows, first)
    if problem == "columns":
        data.write_text(data.read_text().replace("topic1,data", "removed,data"))
    with pytest.raises(ValueError):
        screen(data, ref, chain_id=1)


def test_reviewed_queue_coalesces_per_block_and_keeps_fork_and_chain_identity(tmp_path):
    reference = events(1, [1, 2, 3, 4, 5])
    args = write_inputs(
        tmp_path, reference + events(2, [1, 2, 3, 4, 4]) + events(3, [2, 4, 6, 8, 10]), reference
    )
    result = screen(*args, chain_id=1)
    seen = tmp_path / "seen.json"
    seen.write_text(
        json.dumps(
            [
                {"chain_id": 1, "tx": word(1), "block_hash": word(1001)},
                {"chain_id": 56, "tx": word(2), "block_hash": word(1001)},
                {"chain_id": 1, "tx": word(3), "block_hash": word(1002)},
            ]
        )
    )
    queue = audit_queue(result, seen)
    assert queue["selected_unique"] == 3 and queue["already_reviewed"] == 1
    assert queue["traces_to_check"] == 2 and queue["blocks_to_check"] == 1
    assert queue["max_reads_before_cache"] == 4
    assert [(r["tx"], r["selected_by"]) for r in queue["transactions"]] == [
        (word(2), ["reference_overlap", "5-19"]),
        (word(3), ["reference_overlap", "5-19"]),
    ]
