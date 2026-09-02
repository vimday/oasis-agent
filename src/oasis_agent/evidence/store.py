from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from ..models.common import BlockRef, EvidenceRef


class EvidenceStore:
    def __init__(self, path: Path) -> None:
        self._connection = sqlite3.connect(path)
        self._connection.execute("CREATE TABLE IF NOT EXISTS evidence (content_hash TEXT PRIMARY KEY, kind TEXT NOT NULL, payload BLOB NOT NULL, chain_id INTEGER, block_number INTEGER, block_hash TEXT, source TEXT NOT NULL)")
        self._connection.commit()

    def put(self, kind: str, payload: bytes, block: BlockRef | None, source: str) -> EvidenceRef:
        digest = hashlib.sha256(payload).hexdigest()
        chain_id = block.chain_id if block else None
        block_number = block.number if block else None
        block_hash = block.block_hash if block else None
        self._connection.execute("INSERT OR IGNORE INTO evidence VALUES (?, ?, ?, ?, ?, ?, ?)", (digest, kind, payload, chain_id, block_number, block_hash, source))
        self._connection.commit()
        return EvidenceRef(kind, digest, block, source)

    def get(self, reference: EvidenceRef) -> bytes:
        row = self._connection.execute("SELECT payload FROM evidence WHERE content_hash = ?", (reference.content_hash,)).fetchone()
        if row is None:
            raise KeyError(reference.content_hash)
        return bytes(row[0])

    def close(self) -> None:
        self._connection.close()
