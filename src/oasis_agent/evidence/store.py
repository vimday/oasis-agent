import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ..models.common import (
    BlockRef,
    EvidenceRef,
    canonical_json,
    sha256_hex,
)

LEGACY_RETRIEVED_AT = "1970-01-01T00:00:00+00:00"


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    reference: EvidenceRef
    payload: bytes
    request_payload: bytes | None
    raw_response_payload: bytes
    interpretation_payload: bytes | None


def _envelope_bytes(value: object) -> bytes:
    return value if isinstance(value, bytes) else canonical_json(value)


class EvidenceStore:
    def __init__(self, path: Path, *, require_existing: bool = False) -> None:
        self._connection = sqlite3.connect(
            path.resolve().as_uri() + "?mode=rw" if require_existing else path,
            uri=require_existing,
        )
        try:
            if require_existing:
                self.validate_existing_schema()
            else:
                self._migrate()
        except Exception:
            self._connection.close()
            raise

    def validate_existing_schema(self) -> None:
        """Refuse lost or legacy runtime evidence; never repair it during startup."""
        expected = {
            "evidence_key",
            "content_hash",
            "kind",
            "payload",
            "chain_id",
            "block_number",
            "block_hash",
            "source",
            "retrieved_at",
            "request_hash",
            "raw_response_hash",
            "interpretation_hash",
            "request_payload",
            "raw_response_payload",
            "interpretation_payload",
            "schema_version",
        }
        columns = self._connection.execute("PRAGMA table_info(evidence)").fetchall()
        if (
            {row[1] for row in columns} != expected
            or not any(row[1] == "evidence_key" and row[5] == 1 for row in columns)
            or self._connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]
        ):
            raise ValueError("existing runtime evidence schema or integrity unresolved")

    def _migrate(self) -> None:
        columns = {
            row[1] for row in self._connection.execute("PRAGMA table_info(evidence)").fetchall()
        }
        if columns and "evidence_key" not in columns:
            self._connection.execute("ALTER TABLE evidence RENAME TO evidence_legacy")
            columns = set()
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS evidence (
                evidence_key TEXT PRIMARY KEY,
                content_hash TEXT NOT NULL,
                kind TEXT NOT NULL,
                payload BLOB NOT NULL,
                chain_id INTEGER,
                block_number INTEGER,
                block_hash TEXT,
                source TEXT NOT NULL,
                retrieved_at TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                raw_response_hash TEXT NOT NULL,
                interpretation_hash TEXT NOT NULL,
                request_payload BLOB,
                raw_response_payload BLOB,
                interpretation_payload BLOB,
                schema_version INTEGER NOT NULL,
                UNIQUE(
                    kind, content_hash, chain_id, block_number, block_hash, source,
                    retrieved_at, request_hash, raw_response_hash, interpretation_hash,
                    schema_version
                )
            )
            """
        )
        if columns:
            additions = {
                "retrieved_at": "TEXT NOT NULL DEFAULT ''",
                "request_hash": "TEXT NOT NULL DEFAULT ''",
                "raw_response_hash": "TEXT NOT NULL DEFAULT ''",
                "interpretation_hash": "TEXT NOT NULL DEFAULT ''",
                "request_payload": "BLOB",
                "raw_response_payload": "BLOB",
                "interpretation_payload": "BLOB",
                "schema_version": "INTEGER NOT NULL DEFAULT 1",
            }
            for name, definition in additions.items():
                if name not in columns:
                    self._connection.execute(f"ALTER TABLE evidence ADD COLUMN {name} {definition}")
        if self._table_exists("evidence_legacy"):
            for row in self._connection.execute(
                "SELECT content_hash, kind, payload, chain_id, block_number, block_hash, source "
                "FROM evidence_legacy"
            ).fetchall():
                content_hash, kind, payload, chain_id, block_number, block_hash, source = row
                block = None
                if chain_id is not None:
                    block = BlockRef(int(chain_id), int(block_number), str(block_hash))
                self._insert(
                    str(kind),
                    bytes(payload),
                    block,
                    str(source),
                    str(content_hash),
                    retrieved_at=LEGACY_RETRIEVED_AT,
                    request_hash="",
                    raw_response_hash=str(content_hash),
                    interpretation_hash="",
                    request_payload=None,
                    raw_response_payload=bytes(payload),
                    interpretation_payload=None,
                    schema_version=1,
                )
            self._connection.execute("DROP TABLE evidence_legacy")
        self._connection.commit()

    def _table_exists(self, name: str) -> bool:
        return (
            self._connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (name,)
            ).fetchone()
            is not None
        )

    @staticmethod
    def _key(reference: EvidenceRef) -> str:
        block = reference.block
        identity = {
            "kind": reference.kind,
            "content_hash": reference.content_hash,
            "chain_id": block.chain_id if block else None,
            "block_number": block.number if block else None,
            "block_hash": block.block_hash if block else None,
            "source": reference.source,
            "retrieved_at": reference.retrieved_at,
            "request_hash": reference.request_hash,
            "raw_response_hash": reference.raw_response_hash,
            "interpretation_hash": reference.interpretation_hash,
            "schema_version": reference.schema_version,
        }
        return hashlib.sha256(canonical_json(identity)).hexdigest()

    def _insert(
        self,
        kind: str,
        payload: bytes,
        block: BlockRef | None,
        source: str,
        content_hash: str,
        *,
        retrieved_at: str,
        request_hash: str,
        raw_response_hash: str,
        interpretation_hash: str,
        request_payload: bytes | None,
        raw_response_payload: bytes,
        interpretation_payload: bytes | None,
        schema_version: int,
    ) -> EvidenceRef:
        if sha256_hex(payload) != content_hash:
            raise ValueError("evidence payload hash does not match content_hash")
        reference = EvidenceRef(
            kind,
            content_hash,
            block,
            source,
            retrieved_at,
            request_hash,
            raw_response_hash,
            interpretation_hash,
            schema_version,
        )
        self._connection.execute(
            """
            INSERT OR IGNORE INTO evidence
                (evidence_key, content_hash, kind, payload, chain_id, block_number, block_hash,
                 source, retrieved_at, request_hash, raw_response_hash, interpretation_hash,
                 request_payload, raw_response_payload, interpretation_payload, schema_version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self._key(reference),
                content_hash,
                kind,
                payload,
                block.chain_id if block else None,
                block.number if block else None,
                block.block_hash if block else None,
                source,
                reference.retrieved_at,
                reference.request_hash,
                reference.raw_response_hash,
                reference.interpretation_hash,
                request_payload,
                raw_response_payload,
                interpretation_payload,
                reference.schema_version,
            ),
        )
        return reference

    def put(
        self,
        kind: str,
        payload: bytes,
        block: BlockRef | None,
        source: str,
        *,
        request: object | None = None,
        raw_response: object | None = None,
        interpretation: object | None = None,
        retrieved_at: str | None = None,
        schema_version: int = 1,
    ) -> EvidenceRef:
        if not kind or not source:
            raise ValueError("evidence kind and source are required")
        if schema_version <= 0:
            raise ValueError("evidence schema version must be positive")
        timestamp = retrieved_at or datetime.now(UTC).isoformat()
        request_payload = None if request is None else _envelope_bytes(request)
        raw_response_payload = payload if raw_response is None else _envelope_bytes(raw_response)
        interpretation_payload = None if interpretation is None else _envelope_bytes(interpretation)
        request_hash = "" if request_payload is None else sha256_hex(request_payload)
        raw_hash = sha256_hex(raw_response_payload)
        interpretation_hash = (
            "" if interpretation_payload is None else sha256_hex(interpretation_payload)
        )
        reference = self._insert(
            kind,
            payload,
            block,
            source,
            sha256_hex(payload),
            retrieved_at=timestamp,
            request_hash=request_hash,
            raw_response_hash=raw_hash,
            interpretation_hash=interpretation_hash,
            request_payload=request_payload,
            raw_response_payload=raw_response_payload,
            interpretation_payload=interpretation_payload,
            schema_version=schema_version,
        )
        self._connection.commit()
        return reference

    def get(self, reference: EvidenceRef) -> bytes:
        row = self._connection.execute(
            "SELECT payload FROM evidence WHERE evidence_key = ?", (self._key(reference),)
        ).fetchone()
        if row is None:
            raise KeyError(self._key(reference))
        payload = bytes(row[0])
        if sha256_hex(payload) != reference.content_hash:
            raise ValueError("evidence payload hash mismatch")
        return payload

    def get_record(self, reference: EvidenceRef) -> EvidenceRecord:
        row = self._connection.execute(
            "SELECT payload, request_payload, raw_response_payload, interpretation_payload "
            "FROM evidence WHERE evidence_key = ?",
            (self._key(reference),),
        ).fetchone()
        if row is None:
            raise KeyError(self._key(reference))
        payload = bytes(row[0])
        request_payload = None if row[1] is None else bytes(row[1])
        raw_response_payload = None if row[2] is None else bytes(row[2])
        interpretation_payload = None if row[3] is None else bytes(row[3])
        if sha256_hex(payload) != reference.content_hash:
            raise ValueError("evidence payload hash mismatch")
        if request_payload is not None and sha256_hex(request_payload) != reference.request_hash:
            raise ValueError("evidence request hash mismatch")
        if raw_response_payload is None:
            raise ValueError("evidence raw response payload is missing")
        if sha256_hex(raw_response_payload) != reference.raw_response_hash:
            raise ValueError("evidence raw response hash mismatch")
        if (
            interpretation_payload is not None
            and sha256_hex(interpretation_payload) != reference.interpretation_hash
        ):
            raise ValueError("evidence interpretation hash mismatch")
        return EvidenceRecord(
            reference,
            payload,
            request_payload,
            raw_response_payload,
            interpretation_payload,
        )

    def close(self) -> None:
        self._connection.close()


class ReadOnlyEvidenceStore(EvidenceStore):
    """One existing read snapshot; no creation, migration or evidence writes."""

    def __init__(self, path: Path) -> None:
        self._connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        self._connection.execute("PRAGMA query_only = ON")
        self._connection.execute("BEGIN")
