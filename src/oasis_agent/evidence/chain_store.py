import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

from ..models.chain import BlockSnapshot, LogRecord
from ..models.common import BlockRef, EvidenceRef, canonical_json, normalize_hash32, sha256_hex
from ..rpc.collector import BlockRangeDataset


@dataclass(frozen=True, slots=True)
class ChainApplyResult:
    chain_id: int
    stream_id: str
    reorg_from: int | None
    removed_blocks: int
    inserted_blocks: int
    inserted_logs: int
    canonical_tip: BlockRef


class CanonicalChainStore:
    def __init__(self, path: Path) -> None:
        self._connection = sqlite3.connect(path)
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA synchronous = FULL")
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS canonical_streams (
                chain_id INTEGER NOT NULL,
                stream_id TEXT NOT NULL,
                scope_json TEXT NOT NULL,
                PRIMARY KEY (chain_id, stream_id)
            );
            CREATE TABLE IF NOT EXISTS canonical_blocks (
                chain_id INTEGER NOT NULL,
                block_number INTEGER NOT NULL,
                block_hash TEXT NOT NULL,
                parent_hash TEXT NOT NULL,
                timestamp INTEGER NOT NULL,
                PRIMARY KEY (chain_id, block_number),
                UNIQUE (chain_id, block_hash)
            );
            CREATE TABLE IF NOT EXISTS canonical_logs (
                stream_id TEXT NOT NULL,
                chain_id INTEGER NOT NULL,
                block_number INTEGER NOT NULL,
                block_hash TEXT NOT NULL,
                transaction_hash TEXT NOT NULL,
                transaction_index INTEGER NOT NULL,
                log_index INTEGER NOT NULL,
                address TEXT NOT NULL,
                topics_json TEXT NOT NULL,
                data TEXT NOT NULL,
                PRIMARY KEY (stream_id, chain_id, block_number, transaction_index, log_index),
                FOREIGN KEY (chain_id, block_number)
                    REFERENCES canonical_blocks(chain_id, block_number) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS canonical_coverage (
                chain_id INTEGER NOT NULL,
                stream_id TEXT NOT NULL,
                block_number INTEGER NOT NULL,
                content_hash TEXT NOT NULL,
                PRIMARY KEY (chain_id, stream_id, block_number),
                FOREIGN KEY (chain_id, block_number)
                    REFERENCES canonical_blocks(chain_id, block_number) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS canonical_captures (
                capture_id TEXT PRIMARY KEY,
                chain_id INTEGER NOT NULL,
                stream_id TEXT NOT NULL,
                source TEXT NOT NULL,
                start_block INTEGER NOT NULL,
                end_block INTEGER NOT NULL,
                content_hash TEXT NOT NULL,
                evidence_json TEXT
            );
            CREATE TABLE IF NOT EXISTS canonical_reorgs (
                seq INTEGER PRIMARY KEY,
                chain_id INTEGER NOT NULL,
                record_json TEXT NOT NULL
            );
            """
        )
        self._connection.commit()

    @contextmanager
    def atomic(self) -> Iterator[None]:
        """Serialize validation and mutation, including nested multi-filter ingestion."""
        nested = self._connection.in_transaction
        self._connection.execute("SAVEPOINT chain_apply" if nested else "BEGIN IMMEDIATE")
        try:
            yield
            if nested:
                self._connection.execute("RELEASE SAVEPOINT chain_apply")
            else:
                self._connection.commit()
        except BaseException:
            if nested:
                self._connection.execute("ROLLBACK TO SAVEPOINT chain_apply")
                self._connection.execute("RELEASE SAVEPOINT chain_apply")
            else:
                self._connection.rollback()
            raise

    def _tip_row(self, chain_id: int) -> tuple[int, str, int] | None:
        row = self._connection.execute(
            "SELECT block_number, block_hash, timestamp FROM canonical_blocks "
            "WHERE chain_id = ? ORDER BY block_number DESC LIMIT 1",
            (chain_id,),
        ).fetchone()
        if row is None:
            return None
        return int(row[0]), str(row[1]), int(row[2])

    def tip(self, chain_id: int) -> BlockRef | None:
        row = self._tip_row(chain_id)
        if row is None:
            return None
        return BlockRef(chain_id, int(row[0]), str(row[1]), int(row[2]))

    def apply(
        self, stream_id: str, dataset: BlockRangeDataset, *, evidence: EvidenceRef | None = None
    ) -> ChainApplyResult:
        with self.atomic():
            return self._apply(stream_id, dataset, evidence=evidence)

    def _apply(
        self, stream_id: str, dataset: BlockRangeDataset, *, evidence: EvidenceRef | None
    ) -> ChainApplyResult:
        if not stream_id:
            raise ValueError("canonical event stream ID is required")
        chain_id = dataset.chain_id
        first = dataset.blocks[0]
        existing_tip = self.tip(chain_id)
        reorg_from: int | None = None
        removed_blocks = 0
        if existing_tip is not None:
            if first.ref.number > existing_tip.number + 1:
                raise ValueError("canonical dataset would create a block gap")
            if first.ref.number == existing_tip.number + 1:
                if first.parent_hash != existing_tip.block_hash:
                    raise ValueError("appended dataset does not connect to canonical tip")
            else:
                anchor = self._connection.execute(
                    "SELECT block_hash FROM canonical_blocks "
                    "WHERE chain_id = ? AND block_number = ?",
                    (chain_id, first.ref.number),
                ).fetchone()
                if anchor is None or str(anchor[0]) != first.ref.block_hash:
                    raise ValueError("reorg dataset must begin with a retained common ancestor")
                for snapshot in dataset.blocks[1:]:
                    existing = self._connection.execute(
                        "SELECT block_hash FROM canonical_blocks "
                        "WHERE chain_id = ? AND block_number = ?",
                        (chain_id, snapshot.ref.number),
                    ).fetchone()
                    if existing is not None and str(existing[0]) != snapshot.ref.block_hash:
                        reorg_from = snapshot.ref.number
                        break

        if evidence is not None and (
            evidence.kind != "canonical_block_range"
            or evidence.source != dataset.source
            or evidence.block != dataset.blocks[-1].ref
            or evidence.content_hash != dataset.content_hash
        ):
            raise ValueError("coverage evidence differs from dataset")
        # All checks above run under the same write lock as the mutations below.
        scope = canonical_json({"address": dataset.address, "topics": dataset.topics}).decode()
        existing_scope = self._connection.execute(
            "SELECT scope_json FROM canonical_streams WHERE chain_id = ? AND stream_id = ?",
            (chain_id, stream_id),
        ).fetchone()
        if existing_scope is None:
            legacy = self._connection.execute(
                "SELECT 1 FROM canonical_logs WHERE chain_id = ? AND stream_id = ? LIMIT 1",
                (chain_id, stream_id),
            ).fetchone()
            if legacy is not None:
                raise ValueError("legacy stream scope is unknown; rebuild under a new stream ID")
            self._connection.execute(
                "INSERT INTO canonical_streams (chain_id, stream_id, scope_json) VALUES (?, ?, ?)",
                (chain_id, stream_id, scope),
            )
        elif existing_scope[0] != scope:
            raise ValueError("canonical stream query scope changed")
        if reorg_from is not None:
            assert existing_tip is not None
            self._connection.execute(
                "INSERT INTO canonical_reorgs (chain_id, record_json) VALUES (?, ?)",
                (
                    chain_id,
                    canonical_json(
                        {
                            "reason": "observed_hash_changed_after_retained_common_ancestor",
                            "from_block": reorg_from,
                            "prior_tip": asdict(existing_tip),
                            "stream_id": stream_id,
                            "source": dataset.source,
                            "incoming_content_hash": dataset.content_hash,
                        }
                    ).decode(),
                ),
            )
            row = self._connection.execute(
                "SELECT COUNT(*) FROM canonical_blocks WHERE chain_id = ? AND block_number >= ?",
                (chain_id, reorg_from),
            ).fetchone()
            assert row is not None
            removed_blocks = int(row[0])
            self._connection.execute(
                "DELETE FROM canonical_logs WHERE chain_id = ? AND block_number >= ?",
                (chain_id, reorg_from),
            )
            self._connection.execute(
                "DELETE FROM canonical_blocks WHERE chain_id = ? AND block_number >= ?",
                (chain_id, reorg_from),
            )

        inserted_blocks = 0
        for snapshot in dataset.blocks:
            existing_header = self._connection.execute(
                "SELECT block_hash, parent_hash, timestamp FROM canonical_blocks "
                "WHERE chain_id = ? AND block_number = ?",
                (chain_id, snapshot.ref.number),
            ).fetchone()
            if existing_header is not None and existing_header != (
                snapshot.ref.block_hash,
                snapshot.parent_hash,
                snapshot.ref.timestamp,
            ):
                raise ValueError("retained canonical block header conflicts with dataset")
            cursor = self._connection.execute(
                "INSERT OR IGNORE INTO canonical_blocks "
                "(chain_id, block_number, block_hash, parent_hash, timestamp) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    chain_id,
                    snapshot.ref.number,
                    snapshot.ref.block_hash,
                    snapshot.parent_hash,
                    snapshot.ref.timestamp,
                ),
            )
            inserted_blocks += cursor.rowcount

            block_logs = tuple(
                log for log in dataset.logs if log.block_number == snapshot.ref.number
            )
            content_hash = sha256_hex(canonical_json([asdict(log) for log in block_logs]))
            prior = self._connection.execute(
                "SELECT content_hash FROM canonical_coverage "
                "WHERE chain_id = ? AND stream_id = ? AND block_number = ?",
                (chain_id, stream_id, snapshot.ref.number),
            ).fetchone()
            if prior is not None and prior[0] != content_hash:
                raise ValueError("same-block stream log coverage conflicts")
            if prior is None:
                legacy_logs = tuple(
                    log
                    for log in self.logs(stream_id, chain_id)
                    if log.block_number == snapshot.ref.number
                )
                if legacy_logs and legacy_logs != block_logs:
                    raise ValueError("legacy same-block log coverage conflicts")
            self._connection.execute(
                "INSERT OR IGNORE INTO canonical_coverage VALUES (?, ?, ?, ?)",
                (chain_id, stream_id, snapshot.ref.number, content_hash),
            )

        self._connection.execute(
            "DELETE FROM canonical_logs WHERE stream_id = ? AND chain_id = ? "
            "AND block_number BETWEEN ? AND ?",
            (stream_id, chain_id, dataset.start_block, dataset.end_block),
        )
        for log in dataset.logs:
            assert log.block_hash is not None
            assert log.transaction_index is not None
            self._connection.execute(
                "INSERT INTO canonical_logs "
                "(stream_id, chain_id, block_number, block_hash, transaction_hash, "
                "transaction_index, log_index, address, topics_json, data) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    stream_id,
                    chain_id,
                    log.block_number,
                    log.block_hash,
                    log.transaction_hash,
                    log.transaction_index,
                    log.log_index,
                    log.address,
                    json.dumps(log.topics, separators=(",", ":")),
                    log.data,
                ),
            )
        evidence_json = canonical_json(asdict(evidence)).decode() if evidence else None
        capture_id = sha256_hex(
            canonical_json([stream_id, dataset.source, dataset.content_hash, evidence_json])
        )
        self._connection.execute(
            "INSERT OR IGNORE INTO canonical_captures VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                capture_id,
                chain_id,
                stream_id,
                dataset.source,
                dataset.start_block,
                dataset.end_block,
                dataset.content_hash,
                evidence_json,
            ),
        )

        tip = self.tip(chain_id)
        assert tip is not None
        return ChainApplyResult(
            chain_id,
            stream_id,
            reorg_from,
            removed_blocks,
            inserted_blocks,
            len(dataset.logs),
            tip,
        )

    def blocks(self, chain_id: int) -> tuple[BlockSnapshot, ...]:
        rows = self._connection.execute(
            "SELECT block_number, block_hash, parent_hash, timestamp FROM canonical_blocks "
            "WHERE chain_id = ? ORDER BY block_number",
            (chain_id,),
        ).fetchall()
        return tuple(
            BlockSnapshot(BlockRef(chain_id, int(row[0]), str(row[1]), int(row[3])), str(row[2]))
            for row in rows
        )

    def logs(self, stream_id: str, chain_id: int) -> tuple[LogRecord, ...]:
        rows = self._connection.execute(
            "SELECT address, topics_json, data, block_number, transaction_hash, log_index, "
            "block_hash, transaction_index FROM canonical_logs "
            "WHERE stream_id = ? AND chain_id = ? "
            "ORDER BY block_number, transaction_index, log_index",
            (stream_id, chain_id),
        ).fetchall()
        return tuple(
            LogRecord(
                str(row[0]),
                tuple(json.loads(str(row[1]))),
                str(row[2]),
                int(row[3]),
                str(row[4]),
                int(row[5]),
                normalize_hash32(str(row[6]), "block hash"),
                int(row[7]),
            )
            for row in rows
        )

    def close(self) -> None:
        self._connection.close()

    def coverage(self, stream_id: str, chain_id: int, start: int, end: int) -> dict[str, object]:
        """Coverage is relative to an explicit start; the global tip proves nothing here.

        Old databases have no coverage rows until replayed. Empty observed blocks are
        recorded; reorg deletion invalidates every affected stream via foreign keys.
        This describes retained ingestion, not provider honesty or current finality.
        """
        if any(type(v) is not int for v in (chain_id, start, end)) or not (
            chain_id > 0 and 0 <= start <= end and end - start < 100_000
        ):
            raise ValueError("invalid coverage bounds")
        rows = self._connection.execute(
            "SELECT block_number FROM canonical_coverage "
            "WHERE chain_id = ? AND stream_id = ? AND block_number BETWEEN ? AND ? "
            "ORDER BY block_number",
            (chain_id, stream_id, start, end),
        ).fetchall()
        observed = {int(row[0]) for row in rows}
        missing = [number for number in range(start, end + 1) if number not in observed]
        frontier = missing[0] - 1 if missing else end
        return {
            "start_block": start,
            "end_block": end,
            "contiguous_through": frontier if frontier >= start else None,
            "missing_blocks": missing,
            "observed_blocks": len(observed),
            "status": "complete" if not missing else "coverage_incomplete",
        }

    def captures(self, stream_id: str, chain_id: int) -> tuple[dict[str, object], ...]:
        """Original provenance survives deduplication and canonical reorg eviction."""
        rows = self._connection.execute(
            "SELECT capture_id, source, start_block, end_block, content_hash, evidence_json "
            "FROM canonical_captures WHERE chain_id = ? AND stream_id = ? ORDER BY capture_id",
            (chain_id, stream_id),
        ).fetchall()
        return tuple(
            {
                "capture_id": row[0],
                "source": row[1],
                "start_block": row[2],
                "end_block": row[3],
                "content_hash": row[4],
                "evidence": json.loads(row[5]) if row[5] else None,
            }
            for row in rows
        )

    def reorgs(self, chain_id: int) -> tuple[dict[str, object], ...]:
        return tuple(
            json.loads(row[0])
            for row in self._connection.execute(
                "SELECT record_json FROM canonical_reorgs WHERE chain_id = ? ORDER BY seq",
                (chain_id,),
            ).fetchall()
        )


__all__ = ["CanonicalChainStore", "ChainApplyResult"]
