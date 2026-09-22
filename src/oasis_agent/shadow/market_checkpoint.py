"""Durable research cursors revalidated from raw evidence before every advance."""

import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path

from pydantic import TypeAdapter

from ..evidence.store import EvidenceStore
from ..models.common import EvidenceRef, canonical_json, sha256_hex
from ..models.v4 import PoolKey
from .market_window import MarketWindow, build_market_window, validate_market_successor


@dataclass(frozen=True, slots=True)
class MarketScope:
    chain_id: int
    pool: PoolKey
    pool_manager: str
    horizon_seconds: int
    max_lateness_seconds: int
    config_hash: str


class MarketCheckpoint:
    """Serializes writers; restart never trusts a cached head or claimed feature value.

    A missing retained DB or changed decoder output blocks recovery. The budget is
    a separate persistent ledger, and cannot be reset by this class. No RPC or signer.
    """

    def __init__(self, path: Path, scope: MarketScope):
        self.scope = scope
        self.binding = canonical_json(asdict(scope))
        self.db = sqlite3.connect(path, timeout=10)
        try:
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS scope (id INTEGER PRIMARY KEY, payload BLOB)"
            )
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS windows "
                "(seq INTEGER PRIMARY KEY, payload BLOB NOT NULL, digest TEXT NOT NULL)"
            )
            with self.db:
                self.db.execute("INSERT OR IGNORE INTO scope VALUES (1, ?)", (self.binding,))
            self.read()
        except Exception:
            self.db.close()
            raise

    def _replay(self, evidence_path: Path, payload: bytes) -> MarketWindow:
        if not evidence_path.is_absolute() or not evidence_path.is_file():
            raise ValueError("retained market evidence missing")
        data = json.loads(payload)
        references = TypeAdapter(tuple[EvidenceRef, ...]).validate_python(data["input_references"])
        if len(references) != 4:
            raise ValueError("market checkpoint requires four raw references")
        store = EvidenceStore(evidence_path)
        try:
            result = build_market_window(
                store,
                swap_references=(references[0], references[1]),
                price_references=(references[2], references[3]),
                pool=self.scope.pool,
                pool_manager=self.scope.pool_manager,
                horizon_seconds=self.scope.horizon_seconds,
                max_lateness_seconds=self.scope.max_lateness_seconds,
                mature_prefix=True,
            )
            if (
                result.chain_id != self.scope.chain_id
                or not result.markouts_complete
                or result.payload != payload
            ):
                raise ValueError("market checkpoint raw replay mismatch")
            return result
        finally:
            store.close()

    def _read(self) -> tuple[int, str, MarketWindow | None]:
        if self.db.execute("SELECT id, payload FROM scope").fetchall() != [(1, self.binding)]:
            raise ValueError("market checkpoint configuration changed")
        sequence, previous, window = 0, "0" * 64, None
        for seq, payload, digest in self.db.execute(
            "SELECT seq, payload, digest FROM windows ORDER BY seq"
        ):
            data = json.loads(payload)
            if (
                seq != sequence + 1
                or seq > 1000
                or sha256_hex(payload) != digest
                or canonical_json(data) != payload
                or set(data) != {"previous", "evidence_path", "window"}
                or data["previous"] != previous
            ):
                raise ValueError("market checkpoint audit chain corrupt")
            current = self._replay(Path(data["evidence_path"]), canonical_json(data["window"]))
            if window is not None:
                validate_market_successor(window, current)
            sequence, previous, window = seq, digest, current
        return sequence, previous, window

    def read(self) -> MarketWindow | None:
        try:
            self.db.execute("BEGIN")
            result = self._read()[2]
            self.db.commit()
            return result
        except Exception:
            self.db.rollback()
            raise

    def append(self, evidence_path: Path, window: MarketWindow) -> None:
        """Reconstruct proposed and retained windows while holding the writer lock."""
        try:
            self.db.execute("BEGIN IMMEDIATE")
            sequence, previous, last = self._read()
            path = evidence_path.resolve()
            current = self._replay(path, window.payload)
            if last is not None:
                if last.payload == current.payload:
                    self.db.commit()
                    return
                validate_market_successor(last, current)
            if sequence >= 1000:
                raise ValueError("market checkpoint capacity exhausted")
            payload = canonical_json(
                {
                    "previous": previous,
                    "evidence_path": str(path),
                    "window": json.loads(current.payload),
                }
            )
            self.db.execute(
                "INSERT INTO windows VALUES (?, ?, ?)", (sequence + 1, payload, sha256_hex(payload))
            )
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def close(self) -> None:
        self.db.close()
