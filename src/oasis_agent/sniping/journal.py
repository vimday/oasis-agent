"""Append-only execution journal with a restart-stable hash chain.

Every attempted buy, sell and monitor decision is journaled before and after
execution. Reads re-verify the full chain and the immutable policy binding;
tampered or truncated chains raise instead of returning data.
"""

import json
import sqlite3
from pathlib import Path
from typing import Any, Literal

from ..models.common import canonical_json, sha256_hex

Policy = Literal["oasis.launch-execution-policy.v1"]


class ExecutionJournal:
    def __init__(self, path: Path, policy: dict[str, Any]):
        self.path = Path(path)
        self.policy = canonical_json(policy)
        self.policy_digest = sha256_hex(self.policy)
        self._verified_head: tuple[int, str] | None = None
        self._verified_version = -1
        self._verified_changes = -1
        self.db = sqlite3.connect(self.path)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS execution_policy "
            "(id INTEGER PRIMARY KEY CHECK(id=1), payload BLOB NOT NULL)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS execution_events "
            "(seq INTEGER PRIMARY KEY, kind TEXT NOT NULL, payload BLOB NOT NULL, "
            "digest TEXT NOT NULL)"
        )
        try:
            with self.db:
                self.db.execute(
                    "INSERT OR IGNORE INTO execution_policy VALUES(1, ?)", (self.policy,)
                )
                row = self.db.execute("SELECT payload FROM execution_policy").fetchone()
                if row != (self.policy,):
                    raise ValueError("execution policy cannot change on restart")
                self._verify()
        except ValueError, sqlite3.Error:
            self.db.close()
            raise
        self.policy_digest = sha256_hex(self.policy)

    def _verify(self) -> list[dict[str, Any]]:
        previous = "0" * 64
        records: list[dict[str, Any]] = []
        for seq, kind, raw, digest in self.db.execute(
            "SELECT seq, kind, payload, digest FROM execution_events ORDER BY seq"
        ):
            record = json.loads(raw)
            if not isinstance(record, dict):
                raise ValueError("execution event must be an object")
            if (
                seq != len(records) + 1
                or record.get("seq") != seq
                or record.get("kind") != kind
                or record.get("previous") != previous
                or record.get("policy_digest") != self.policy_digest
                or sha256_hex(raw) != digest
                or canonical_json(record) != raw
            ):
                raise ValueError("execution journal chain corrupted")
            records.append(record | {"digest": digest})
            previous = digest
        return records

    def read(self) -> list[dict[str, Any]]:
        return self._verify()

    def append(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(kind, str) or not kind or not isinstance(payload, dict):
            raise ValueError("kind must be a non-empty string, payload an object")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            # The reserved write lock prevents another writer committing between
            # this version check and our append. Compare data_version only on
            # this same connection; own writes are tracked by total_changes.
            version = int(self.db.execute("PRAGMA data_version").fetchone()[0])
            if self.db.execute("SELECT payload FROM execution_policy WHERE id=1").fetchone() != (
                self.policy,
            ):
                raise ValueError("execution policy changed")
            if (
                self._verified_head is None
                or version != self._verified_version
                or self.db.total_changes != self._verified_changes
            ):
                records = self._verify()
                self._verified_head = (len(records), records[-1]["digest"] if records else "0" * 64)
            seq, previous = self._verified_head
            record = payload | {
                "kind": kind,
                "seq": seq + 1,
                "previous": previous,
                "policy_digest": self.policy_digest,
            }
            raw = canonical_json(record)
            digest = sha256_hex(raw)
            changes_before_insert = self.db.total_changes
            self.db.execute(
                "INSERT INTO execution_events VALUES (?, ?, ?, ?)",
                (record["seq"], kind, raw, digest),
            )
            if self.db.total_changes - changes_before_insert != 1:
                # Unexpected SQL triggers must not turn our own append into an
                # unverified rewrite of historical records.
                raise ValueError("unexpected additional journal writes")
            self.db.commit()
            self._verified_head = (seq + 1, digest)
            # Keep the pre-commit version: a writer committing immediately after
            # us must invalidate this head on the next append.
            self._verified_version = version
            self._verified_changes = self.db.total_changes
            return record | {"digest": digest}
        except ValueError, TypeError, KeyError, sqlite3.Error:
            self._verified_head = None
            self.db.rollback()
            raise

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> ExecutionJournal:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
