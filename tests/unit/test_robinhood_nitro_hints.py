import sqlite3
from pathlib import Path
from typing import Any, cast

import pytest

from oasis_agent.evidence.robinhood_nitro import (
    CHAIN_ID,
    DECODE_SCHEMA,
    OFFICIAL_RPC,
    RPC_SCHEMA,
    FeedReplay,
    NitroHintJournal,
    RpcAnchor,
    read_rpc_anchors,
)
from oasis_agent.models.common import canonical_json, sha256_hex


def block_hash(number: int) -> str:
    return f"0x{number:064x}"


def row(sequence: int, **changes: Any) -> dict[str, Any]:
    return {
        "sequence": sequence,
        "block_by_config": sequence,
        "feed_block_hash": block_hash(sequence),
        "feed_timestamp": sequence + 1000,
        "parent_chain_block": 25,
        "incoming_kind": 3,
        "delayed_messages_read": 100,
        "l2_payload_bytes": 13,
        "l2_payload_sha256": sha256_hex(bytes(str(sequence), "ascii")),
        "signed_eth_transactions": [{"hash": block_hash(sequence + 10_000)}],
        "signature_v2_bytes": 65,
        **changes,
    }


def replay(*rows: dict[str, Any], **summary: Any) -> FeedReplay:
    return FeedReplay.parse(
        canonical_json(
            {
                "summary": {
                    "schema": DECODE_SCHEMA,
                    "chain_id": CHAIN_ID,
                    "genesis_block_offset": 0,
                    "signatures_verified": 0,
                    "messages": len(rows),
                    "capture_sha256": sha256_hex(b"test capture"),
                    **summary,
                },
                "messages": rows,
            }
        )
    )


def hints(report: dict[str, object]) -> list[dict[str, Any]]:
    return cast("list[dict[str, Any]]", report["block_hints"])


def test_gap_is_buffered_recovered_and_deduped_across_restart(tmp_path: Path) -> None:
    checkpoint = tmp_path / "hints.sqlite3"
    journal = NitroHintJournal(checkpoint)
    try:
        first = journal.ingest((replay(row(10), row(12), row(10, frame=123)),), start=10)
        assert first["status"] == "waiting_for_gap"
        assert first["first_missing_range"] == [11, 11]
        assert first["buffered_messages"] == 1
        assert first["latest_hint"] is None
        assert [hint["sequence"] for hint in hints(first)] == [10]
        assert first["ingest_counts"] == {
            "added": 2,
            "duplicates": 1,
            "before_start": 0,
            "conflicts": 0,
        }
    finally:
        journal.close()
    journal = NitroHintJournal(checkpoint)
    try:
        resumed = journal.ingest((replay(row(9), row(11), row(12)),), limit=3)
        assert resumed["status"] == "contiguous"
        assert resumed["contiguous_through"] == 12
        assert resumed["next_sequence"] == 13
        assert [hint["sequence"] for hint in hints(resumed)] == [10, 11, 12]
        assert resumed["ingest_counts"] == {
            "added": 1,
            "duplicates": 1,
            "before_start": 1,
            "conflicts": 0,
        }
        with pytest.raises(ValueError, match="persisted cursor"):
            journal.ingest((), start=11)
        truncated = journal.ingest((), limit=1)
        assert [hint["sequence"] for hint in hints(truncated)] == [12]
        assert truncated["hints_omitted_by_limit"] == 2
    finally:
        journal.close()


@pytest.mark.parametrize(
    ("conflicting_feed", "reason"),
    [
        (row(10, feed_block_hash=block_hash(700)), "feed_sequence_conflict"),
        (row(10, parent_chain_block=26), "feed_sequence_conflict"),
        (row(12, feed_block_hash=block_hash(10)), "feed_block_hash_reused"),
    ],
)
def test_conflict_invalidates_hints_and_cannot_be_cleared_by_replay(
    tmp_path: Path,
    conflicting_feed: dict[str, Any],
    reason: str,
) -> None:
    checkpoint = tmp_path / "hints.sqlite3"
    journal = NitroHintJournal(checkpoint)
    try:
        assert journal.ingest((replay(row(10)),))["status"] == "contiguous"
        report = journal.ingest((replay(conflicting_feed, row(11)),))
        assert report["status"] == "invalidated"
        assert cast("dict[str, object]", report["invalidated_by"])["reason"] == reason
        assert cast("dict[str, int]", report["ingest_counts"])["conflicts"] == 1
        assert report["contiguous_through"] is None
        assert report["latest_hint"] is None
        assert hints(report) == []
    finally:
        journal.close()
    journal = NitroHintJournal(checkpoint)
    try:
        frozen = journal.ingest((replay(row(10), row(11)),))
        assert frozen["invalidated_by"] == report["invalidated_by"]
        assert hints(frozen) == []
    finally:
        journal.close()


def test_feed_missing_hash_enrichment_does_not_promote_to_verified_signature() -> None:
    hashes = [{"hash": block_hash(20)}, {"hash": block_hash(20)}, {"hash": block_hash(30)}]
    before = replay(row(10, feed_block_hash=None, signed_eth_transactions=hashes))
    after = replay(row(10, signed_eth_transactions=hashes, signature_v2_bytes=0))
    anchor = RpcAnchor(10, block_hash(10), 1010, (block_hash(1), block_hash(20)), "a" * 64)
    journal = NitroHintJournal()
    try:
        pending = hints(journal.ingest((before,), (anchor,)))[0]
        assert pending["anchor_status"] == "official_rpc_feed_hash_missing"
        assert pending["transactions"] == [
            {"hash": block_hash(20), "official_rpc_index": None},
            {"hash": block_hash(30), "official_rpc_index": None},
        ]
        final = journal.ingest((after,))
        hint = hints(final)[0]
        assert hint["anchor_status"] == "matched_official_rpc"
        assert hint["retained_decode_sha256"] == before.decode_sha256
        assert hint["feed_hash_decode_sha256"] == after.decode_sha256
        assert hint["feed_signature_v2_present"] is True
        assert hint["transactions"] == [
            {"hash": block_hash(20), "official_rpc_index": 1},
            {"hash": block_hash(30), "official_rpc_index": None},
        ]
        assert final["feed_signatures_verified"] is False
        assert final["official_rpc_independent"] is False
        assert final["finality_verified"] is False
        assert final["execution_eligible"] is False
    finally:
        journal.close()


@pytest.mark.parametrize(
    ("anchor", "reason"),
    [
        (RpcAnchor(10, block_hash(999), 1010, (), "a" * 64), "feed_rpc_block_conflict"),
        (RpcAnchor(10, block_hash(10), 999, (), "a" * 64), "feed_rpc_block_conflict"),
        (RpcAnchor(11, block_hash(10), 1011, (), "a" * 64), "rpc_block_hash_reused"),
    ],
)
def test_official_rpc_conflict_fails_closed(anchor: RpcAnchor, reason: str) -> None:
    journal = NitroHintJournal()
    try:
        result = journal.ingest((replay(row(10)),), (anchor,))
        assert result["status"] == "invalidated"
        assert cast("dict[str, object]", result["invalidated_by"])["reason"] == reason
        assert hints(result) == []
    finally:
        journal.close()


@pytest.mark.parametrize(
    "invalid_summary",
    [{"chain_id": 46630}, {"signatures_verified": 1}, {"genesis_block_offset": False}],
)
def test_replay_rejects_altered_chain_scope(invalid_summary: dict[str, object]) -> None:
    with pytest.raises(ValueError, match=r"scope|integer"):
        replay(row(10), **invalid_summary)


def test_checkpoint_does_not_overwrite_another_database(tmp_path: Path) -> None:
    checkpoint = tmp_path / "wrong.sqlite3"
    db = sqlite3.connect(checkpoint)
    try:
        db.execute("CREATE TABLE other(content TEXT)")
    finally:
        db.close()
    with pytest.raises(ValueError, match="not a Robinhood"):
        NitroHintJournal(checkpoint)


def test_rpc_anchors_are_bound_to_raw_exchanges_and_decode(tmp_path: Path) -> None:
    feed = replay(row(10), row(11))
    scope = {
        "schema": RPC_SCHEMA,
        "source": "robinhood_official_public",
        "endpoint": OFFICIAL_RPC,
        "chain_id": CHAIN_ID,
        "feed_decode_sha256": feed.decode_sha256,
        "selected_sequence": 10,
        "secondary_sequence": 11,
    }
    (tmp_path / "scope.json").write_bytes(canonical_json(scope))
    for number in range(1, 4):
        sequence = number + 8
        method = "eth_chainId" if number == 1 else "eth_getBlockByNumber"
        params = [] if number == 1 else [hex(sequence), False]
        data: object = (
            hex(CHAIN_ID)
            if number == 1
            else {
                "number": hex(sequence),
                "hash": block_hash(sequence),
                "timestamp": hex(sequence + 1000),
                "transactions": [block_hash(sequence + 10_000)],
            }
        )
        request = canonical_json(
            {"jsonrpc": "2.0", "id": number, "method": method, "params": params}
        )
        response = canonical_json({"jsonrpc": "2.0", "id": number, "result": data})
        metadata = {
            "id": number,
            "method": method,
            "http_status": 200,
            "body_complete": True,
            "error": None,
            "request_sha256": sha256_hex(request),
            "response_sha256": sha256_hex(response),
        }
        (tmp_path / f"{number:02}.request.json").write_bytes(request)
        (tmp_path / f"{number:02}.response.json").write_bytes(response)
        (tmp_path / f"{number:02}.meta.json").write_bytes(canonical_json(metadata))
    anchors = read_rpc_anchors(tmp_path, {feed.decode_sha256})
    assert [anchor.sequence for anchor in anchors] == [10, 11]
    with pytest.raises(ValueError, match="scope does not match"):
        read_rpc_anchors(tmp_path, {"f" * 64})
    (tmp_path / "02.response.json").write_bytes(b"{}")
    with pytest.raises(ValueError, match="official RPC evidence disagrees"):
        read_rpc_anchors(tmp_path, {feed.decode_sha256})


def test_hash_parser_rejects_signed_integer_text() -> None:
    with pytest.raises(ValueError, match="block or transaction hash"):
        replay(row(10, feed_block_hash="0x+" + "0" * 63))
