"""Continuity and deduplication for offline Robinhood shadow hints."""

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from ..arbitrage.cycle_evidence import read_bounded
from ..models.common import canonical_json, normalize_hash32, sha256_hex
from ..rpc.protocol import response_envelope
from .rpc_validation import (
    rpc_quantity,
    strict_digest,
    strict_integer,
    strict_json_object,
    strict_list,
    strict_object,
    strict_string,
)

CHAIN_ID = 4663
OFFICIAL_RPC = "https://rpc.mainnet.chain.robinhood.com"
DECODE_SCHEMA = "oasis-robinhood-nitro-replay-v1"
RPC_SCHEMA = "oasis-robinhood-nitro-forward-rpc-verification-v1"
MAX_SEQUENCE = (1 << 63) - 2  # The next sequence must fit a SQLite integer.
MAX_DECODE_BYTES = 32 * 1024 * 1024
MAX_RPC_BYTES = 2 * 1024 * 1024
_APPLICATION_ID = 0x52484E54
_JOURNAL_VERSION = 1


def _uint(value: object, name: str) -> int:
    number = strict_integer(value, name)
    if not 0 <= number <= MAX_SEQUENCE:
        raise ValueError(f"{name} is outside the supported sequence range")
    return number


def _hash(value: object) -> str:
    normalized = normalize_hash32(strict_string(value, "block or transaction hash"))
    strict_digest(normalized[2:], "block or transaction hash")
    return normalized


@dataclass(frozen=True, slots=True)
class FeedMessage:
    sequence: int
    block_hash: str | None
    timestamp: int
    semantic_sha256: str
    transactions: tuple[str, ...]
    signature_v2_present: bool

    @classmethod
    def from_row(cls, value: object) -> FeedMessage:
        row = strict_object(value, "Nitro message")
        sequence = _uint(row.get("sequence"), "sequence")
        if _uint(row.get("block_by_config"), "mapped L2 block") != sequence:
            raise ValueError("Robinhood sequence/L2 block mismatch")
        if "feed_block_hash" not in row:
            raise ValueError("Nitro message lacks its block hash field")
        block_hash = None if row["feed_block_hash"] is None else _hash(row["feed_block_hash"])
        timestamp = _uint(row.get("feed_timestamp"), "feed timestamp")
        signed = strict_list(row.get("signed_eth_transactions"), "signed transaction hashes")
        tx_hashes = tuple(
            _hash(strict_object(tx, "signed transaction").get("hash")) for tx in signed
        )
        semantic = {
            key: _uint(row.get(key), key)
            for key in (
                "parent_chain_block",
                "incoming_kind",
                "delayed_messages_read",
                "l2_payload_bytes",
            )
        }
        payload_sha = strict_digest(row.get("l2_payload_sha256"), "L2 payload SHA-256")
        signature_bytes = _uint(row.get("signature_v2_bytes"), "signature V2 byte count")
        # Compare protocol content, not recv time, frame layout, or relay signatures.
        digest = sha256_hex(canonical_json([semantic, timestamp, payload_sha, tx_hashes]))
        return cls(
            sequence,
            block_hash,
            timestamp,
            digest,
            tuple(dict.fromkeys(tx_hashes)),
            signature_bytes > 0,
        )


@dataclass(frozen=True, slots=True)
class FeedReplay:
    decode_sha256: str
    capture_sha256: str
    messages: tuple[FeedMessage, ...]

    @classmethod
    def parse(cls, raw: bytes) -> FeedReplay:
        if len(raw) > MAX_DECODE_BYTES:
            raise ValueError("retained decode exceeds byte limit")
        obj = strict_json_object(raw)
        summary = strict_object(obj.get("summary"), "Nitro decode summary")
        if (
            summary.get("schema") != DECODE_SCHEMA
            or _uint(summary.get("chain_id"), "chain ID") != CHAIN_ID
            or _uint(summary.get("genesis_block_offset"), "genesis block offset") != 0
            or _uint(summary.get("signatures_verified"), "verified signatures") != 0
        ):
            raise ValueError("unsupported Robinhood decode scope")
        messages = tuple(
            FeedMessage.from_row(msg) for msg in strict_list(obj.get("messages"), "Nitro messages")
        )
        if len(messages) != _uint(summary.get("messages"), "message count"):
            raise ValueError("Nitro message count mismatch")
        return cls(
            sha256_hex(raw),
            strict_digest(summary.get("capture_sha256"), "raw capture SHA-256"),
            messages,
        )


@dataclass(frozen=True, slots=True)
class RpcAnchor:
    sequence: int
    block_hash: str
    timestamp: int
    transaction_hashes: tuple[str, ...]
    response_sha256: str


def _rpc_result(root: Path, number: int, method: str) -> tuple[list[object], object, str]:
    meta = strict_json_object(read_bounded(root / f"{number:02}.meta.json", 16384))
    request = read_bounded(root / f"{number:02}.request.json", 16384)
    response = read_bounded(root / f"{number:02}.response.json", MAX_RPC_BYTES)
    response_sha = sha256_hex(response)
    obj = strict_json_object(request)
    if (
        meta.get("http_status") != 200
        or meta.get("body_complete") is not True
        or meta.get("error") is not None
        or meta.get("request_sha256") != sha256_hex(request)
        or meta.get("response_sha256") != response_sha
        or strict_integer(meta.get("id"), "RPC metadata ID") != number
        or meta.get("method") != method
        or obj.get("jsonrpc") != "2.0"
        or strict_integer(obj.get("id"), "RPC request ID") != number
        or obj.get("method") != method
    ):
        raise ValueError("retained official RPC evidence disagrees")
    envelope = response_envelope(response, number)
    if "result" not in envelope:
        raise ValueError("retained official RPC read failed")
    return strict_list(obj.get("params"), "RPC params"), envelope["result"], response_sha


def read_rpc_anchors(root: Path, replay_digests: set[str]) -> tuple[RpcAnchor, ...]:
    """Extract same-operator observations from the archived read-only anchoring campaign."""
    scope = strict_json_object(read_bounded(root / "scope.json", 16384))
    if (
        scope.get("schema") != RPC_SCHEMA
        or scope.get("source") != "robinhood_official_public"
        or scope.get("endpoint") != OFFICIAL_RPC
        or _uint(scope.get("chain_id"), "RPC chain ID") != CHAIN_ID
        or strict_digest(scope.get("feed_decode_sha256"), "RPC feed decode SHA-256")
        not in replay_digests
    ):
        raise ValueError("retained official RPC scope does not match supplied feed replay")
    params, chain, _ = _rpc_result(root, 1, "eth_chainId")
    if params or rpc_quantity(chain) != CHAIN_ID:
        raise ValueError("retained official RPC chain ID mismatch")
    anchors = []
    for index, key in enumerate(("selected_sequence", "secondary_sequence"), start=2):
        sequence = _uint(scope.get(key), key)
        params, result, digest = _rpc_result(root, index, "eth_getBlockByNumber")
        block = strict_object(result, "official RPC block")
        if (
            len(params) != 2
            or params[1] is not False
            or rpc_quantity(params[0]) != sequence
            or rpc_quantity(block.get("number")) != sequence
        ):
            raise ValueError("retained official RPC block request/response height mismatch")
        transactions = tuple(
            _hash(tx) for tx in strict_list(block.get("transactions"), "official RPC transactions")
        )
        if len(transactions) != len(set(transactions)):
            raise ValueError("duplicate official RPC transaction hash")
        anchors.append(
            RpcAnchor(
                sequence,
                _hash(block.get("hash")),
                _uint(rpc_quantity(block.get("timestamp")), "block timestamp"),
                transactions,
                digest,
            )
        )
    if anchors[0].sequence == anchors[1].sequence:
        raise ValueError("two different official RPC block heights required")
    return tuple(anchors)


class NitroHintJournal:
    """Persist the sequence prefix and gap buffer with one durable SQLite transaction."""

    def __init__(self, path: Path | None = None) -> None:
        self._db = sqlite3.connect(path if path is not None else ":memory:", isolation_level=None)
        self._db.row_factory = sqlite3.Row
        try:
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.execute("BEGIN IMMEDIATE")
            app_id = self._db.execute("PRAGMA application_id").fetchone()[0]
            version = self._db.execute("PRAGMA user_version").fetchone()[0]
            if (app_id, version) == (0, 0):
                if self._db.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone():
                    raise ValueError("existing database is not a Robinhood hint checkpoint")
                self._db.execute(f"PRAGMA application_id={_APPLICATION_ID}")
                self._db.execute(f"PRAGMA user_version={_JOURNAL_VERSION}")
            elif (app_id, version) != (_APPLICATION_ID, _JOURNAL_VERSION):
                raise ValueError("unsupported Robinhood hint checkpoint identity/version")
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS nitro_cursor (id INTEGER PRIMARY KEY CHECK(id=1), "
                "start_seq INTEGER NOT NULL, next_seq INTEGER NOT NULL, halt BLOB)"
            )
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS nitro_messages (seq INTEGER PRIMARY KEY, "
                "block_hash TEXT, timestamp INTEGER NOT NULL, identity TEXT NOT NULL, "
                "transactions BLOB NOT NULL, signature_present INTEGER NOT NULL, "
                "decode_sha TEXT NOT NULL, capture_sha TEXT NOT NULL, hash_decode_sha TEXT)"
            )
            self._db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS nitro_block_hash ON nitro_messages(block_hash)"
            )
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS nitro_anchors (seq INTEGER PRIMARY KEY, "
                "block_hash TEXT NOT NULL UNIQUE, timestamp INTEGER NOT NULL, "
                "transactions BLOB NOT NULL, response_sha TEXT NOT NULL)"
            )
            self._db.commit()
        except sqlite3.Error, ValueError:
            self._db.close()
            raise

    def close(self) -> None:
        self._db.close()

    def _find(self, table_is_anchor: bool, sequence: int) -> sqlite3.Row | None:
        statement = (
            "SELECT * FROM nitro_anchors WHERE seq=?"
            if table_is_anchor
            else "SELECT * FROM nitro_messages WHERE seq=?"
        )
        return cast("sqlite3.Row | None", self._db.execute(statement, (sequence,)).fetchone())

    def _halt(self, kind: str, sequence: int, prior: object, claim: object) -> None:
        self._db.execute(
            "UPDATE nitro_cursor SET halt=? WHERE id=1 AND halt IS NULL",
            (
                canonical_json(
                    {"reason": kind, "sequence": sequence, "prior": prior, "claim": claim}
                ),
            ),
        )

    def _compare_sources(self, sequence: int) -> bool:
        msg, anchor = self._find(False, sequence), self._find(True, sequence)
        if msg is None or anchor is None:
            return False
        if (msg["block_hash"] is not None and msg["block_hash"] != anchor["block_hash"]) or (
            msg["timestamp"] != anchor["timestamp"]
        ):
            self._halt(
                "feed_rpc_block_conflict",
                sequence,
                {
                    "rpc_response_sha": anchor["response_sha"],
                    "hash": anchor["block_hash"],
                    "timestamp": anchor["timestamp"],
                },
                {
                    "decode_sha": msg["hash_decode_sha"] or msg["decode_sha"],
                    "hash": msg["block_hash"],
                    "timestamp": msg["timestamp"],
                },
            )
            return True
        return False

    def _other_height(self, block_hash: str | None, sequence: int) -> int | None:
        result = self._db.execute(
            "SELECT seq FROM nitro_messages WHERE block_hash=? AND seq<>? UNION ALL "
            "SELECT seq FROM nitro_anchors WHERE block_hash=? AND seq<>? LIMIT 1",
            (block_hash, sequence, block_hash, sequence),
        ).fetchone()
        return None if result is None else cast(int, result["seq"])

    def _add_anchor(self, anchor: RpcAnchor) -> bool:
        previous = self._find(True, anchor.sequence)
        txs = canonical_json(anchor.transaction_hashes)
        if previous:
            if (previous["block_hash"], previous["timestamp"], previous["transactions"]) != (
                anchor.block_hash,
                anchor.timestamp,
                txs,
            ):
                self._halt(
                    "rpc_anchor_conflict",
                    anchor.sequence,
                    previous["response_sha"],
                    anchor.response_sha256,
                )
                return True
            return False
        earlier_height = self._other_height(anchor.block_hash, anchor.sequence)
        if earlier_height is not None:
            self._halt(
                "rpc_block_hash_reused",
                anchor.sequence,
                earlier_height,
                anchor.response_sha256,
            )
            return True
        self._db.execute(
            "INSERT INTO nitro_anchors VALUES (?,?,?,?,?)",
            (anchor.sequence, anchor.block_hash, anchor.timestamp, txs, anchor.response_sha256),
        )
        return self._compare_sources(anchor.sequence)

    def _add_message(self, message: FeedMessage, replay: FeedReplay) -> str:
        sequence = message.sequence
        prior = self._find(False, sequence)
        earlier_height = self._other_height(message.block_hash, sequence)
        if earlier_height is not None:
            self._halt("feed_block_hash_reused", sequence, earlier_height, replay.decode_sha256)
            return "conflicts"
        if prior is not None:
            hash_conflict = (
                prior["block_hash"] is not None
                and message.block_hash is not None
                and prior["block_hash"] != message.block_hash
            )
            if prior["identity"] != message.semantic_sha256 or hash_conflict:
                self._halt(
                    "feed_sequence_conflict",
                    sequence,
                    {
                        "decode_sha": prior["decode_sha"],
                        "hash": prior["block_hash"],
                        "semantic_sha": prior["identity"],
                    },
                    {
                        "decode_sha": replay.decode_sha256,
                        "hash": message.block_hash,
                        "semantic_sha": message.semantic_sha256,
                    },
                )
                return "conflicts"
            self._db.execute(
                "UPDATE nitro_messages SET signature_present=max(signature_present,?), "
                "block_hash=coalesce(block_hash,?), hash_decode_sha=coalesce(hash_decode_sha,?) "
                "WHERE seq=?",
                (
                    message.signature_v2_present,
                    message.block_hash,
                    replay.decode_sha256 if message.block_hash else None,
                    sequence,
                ),
            )
            result = "duplicates"
        else:
            self._db.execute(
                "INSERT INTO nitro_messages VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    sequence,
                    message.block_hash,
                    message.timestamp,
                    message.semantic_sha256,
                    canonical_json(message.transactions),
                    message.signature_v2_present,
                    replay.decode_sha256,
                    replay.capture_sha256,
                    replay.decode_sha256 if message.block_hash else None,
                ),
            )
            result = "added"
        if self._compare_sources(sequence):
            return "conflicts"
        return result

    def ingest(
        self,
        replays: tuple[FeedReplay, ...],
        anchors: tuple[RpcAnchor, ...] = (),
        *,
        start: int | None = None,
        limit: int = 1024,
    ) -> dict[str, object]:
        if type(limit) is not int or not 1 <= limit <= 4096:
            raise ValueError("hint limit must be between 1 and 4096")
        if start is not None:
            _uint(start, "start sequence")
        self._db.execute("BEGIN IMMEDIATE")
        try:
            result = self._ingest(replays, anchors, start, limit)
            self._db.commit()
            return result
        except BaseException:
            self._db.rollback()
            raise

    def _cursor(self) -> sqlite3.Row | None:
        return cast("sqlite3.Row | None", self._db.execute("SELECT * FROM nitro_cursor").fetchone())

    def _ingest(
        self,
        replays: tuple[FeedReplay, ...],
        anchors: tuple[RpcAnchor, ...],
        start: int | None,
        limit: int,
    ) -> dict[str, object]:
        initialized = self._cursor()
        if initialized is None:
            first = next(
                (replay.messages[0].sequence for replay in replays if replay.messages), None
            )
            start = first if start is None else start
            if start is None:
                raise ValueError("no feed messages or explicit start sequence")
            self._db.execute("INSERT INTO nitro_cursor VALUES (1,?,?,NULL)", (start, start))
        else:
            if start is not None and start != initialized["start_seq"]:
                raise ValueError("start sequence does not match persisted cursor")
            start = cast(int, initialized["start_seq"])
        counts = dict.fromkeys(("added", "duplicates", "before_start", "conflicts"), 0)
        for replay in replays:
            for message in replay.messages:
                cursor = self._cursor()
                assert cursor is not None
                if cursor["halt"] is not None:
                    break
                if message.sequence < start:
                    counts["before_start"] += 1
                    continue
                counts[self._add_message(message, replay)] += 1
        for anchor in anchors:
            cursor = self._cursor()
            assert cursor is not None
            if cursor["halt"] is not None:
                break
            if anchor.sequence >= start and self._add_anchor(anchor):
                counts["conflicts"] += 1
        cursor = self._cursor()
        assert cursor is not None
        expected = cast(int, cursor["next_seq"])
        halt = cursor["halt"]
        if halt is None:
            for (sequence,) in self._db.execute(
                "SELECT seq FROM nitro_messages WHERE seq>=? ORDER BY seq", (expected,)
            ):
                if sequence != expected:
                    break
                expected += 1
            self._db.execute("UPDATE nitro_cursor SET next_seq=? WHERE id=1", (expected,))
        return self._report(start, expected, halt, counts, limit)

    def _report(
        self,
        start: int,
        expected: int,
        halt: bytes | None,
        counts: dict[str, int],
        limit: int,
    ) -> dict[str, object]:
        pending = self._db.execute(
            "SELECT min(seq) AS first, count(*) AS count FROM nitro_messages WHERE seq>=?",
            (expected,),
        ).fetchone()
        first_pending = pending["first"]
        earliest = max(start, expected - limit)
        hints: list[dict[str, object]] = []
        if halt is None:
            rows = self._db.execute(
                "SELECT * FROM nitro_messages WHERE seq>=? AND seq<? ORDER BY seq",
                (earliest, expected),
            )
            hints = [self._hint(msg) for msg in rows]
        status = (
            "invalidated"
            if halt is not None
            else "waiting_for_gap"
            if first_pending is not None
            else "contiguous"
            if expected > start
            else "waiting_for_start"
        )
        return {
            "schema": "oasis-robinhood-nitro-shadow-hints-v1",
            "chain_id": CHAIN_ID,
            "status": status,
            "start_sequence": start,
            "next_sequence": expected,
            "contiguous_through": expected - 1 if expected > start and halt is None else None,
            "buffered_messages": pending["count"] if halt is None else None,
            "first_missing_range": [expected, first_pending - 1]
            if halt is None and first_pending is not None
            else None,
            "hints_omitted_by_limit": earliest - start if halt is None else None,
            "latest_hint": hints[-1] if status == "contiguous" else None,
            "block_hints": hints,
            "invalidated_by": json.loads(halt) if halt is not None else None,
            "ingest_counts": counts,
            "feed_signatures_verified": False,
            "official_rpc_independent": False,
            "finality_verified": False,
            "execution_eligible": False,
        }

    def _hint(self, msg: sqlite3.Row) -> dict[str, object]:
        anchor = self._find(True, cast(int, msg["seq"]))
        matched = anchor is not None and msg["block_hash"] is not None
        positions = (
            {tx: index for index, tx in enumerate(json.loads(anchor["transactions"]))}
            if matched and anchor
            else {}
        )
        return {
            "sequence": msg["seq"],
            "candidate_l2_block": msg["seq"],
            "feed_block_hash": msg["block_hash"],
            "feed_timestamp": msg["timestamp"],
            "feed_signature_v2_present": bool(msg["signature_present"]),
            "semantic_sha256": msg["identity"],
            "retained_decode_sha256": msg["decode_sha"],
            "raw_capture_sha256": msg["capture_sha"],
            "feed_hash_decode_sha256": msg["hash_decode_sha"],
            "anchor_status": (
                "matched_official_rpc"
                if matched
                else "official_rpc_feed_hash_missing"
                if anchor
                else "feed_hash_claim"
                if msg["block_hash"] is not None
                else "feed_height_only"
            ),
            "official_rpc_response_sha256": anchor["response_sha"] if anchor else None,
            "transactions": [
                {"hash": tx, "official_rpc_index": positions.get(tx)}
                for tx in json.loads(msg["transactions"])
            ],
        }
