"""Configuration-bound append-only authoritative state snapshots with CAS writers.

The JSON state file is only a projection. A committed journal snapshot survives a
crash before projection replacement. Unknown signing attempts remain reserved;
there is no automatic nonce release, signature retry or broadcast retry.
"""

import json
import os
import sqlite3
import stat
from contextlib import closing
from pathlib import Path
from typing import Any

from ..models.common import canonical_json, sha256_hex

ZERO = "0" * 64
MAX_SNAPSHOT = 2_000_000


class JournalConflict(ValueError):
    pass


class LiveJournal:
    def __init__(self, path: Path, config_hash: str | None = None) -> None:
        self.path = path
        if path.is_symlink():
            raise ValueError("journal cannot be a symlink")
        if path.exists():
            info = path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise ValueError("journal must be an owned regular file")
        elif config_hash is None:
            raise ValueError("journal creation requires a configuration binding")
        self.config_hash = config_hash
        self.revision: str | None = None
        if config_hash is not None:
            if len(config_hash) != 64 or any(c not in "0123456789abcdef" for c in config_hash):
                raise ValueError("canonical configuration digest required")
            path.parent.mkdir(parents=True, exist_ok=True)
            with closing(self._connect()) as db:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY, "
                    "config TEXT NOT NULL, "
                    "parent TEXT NOT NULL, digest TEXT NOT NULL UNIQUE, payload BLOB NOT NULL)"
                )
                db.execute(
                    "CREATE TRIGGER IF NOT EXISTS no_update BEFORE UPDATE ON events "
                    "BEGIN SELECT RAISE(ABORT, 'append only'); END"
                )
                db.execute(
                    "CREATE TRIGGER IF NOT EXISTS no_delete BEFORE DELETE ON events "
                    "BEGIN SELECT RAISE(ABORT, 'append only'); END"
                )
            path.chmod(0o600)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA journal_mode=DELETE")
        return db

    def _read(self, db: sqlite3.Connection) -> tuple[str, bytes | None]:
        parent = ZERO
        latest = None
        binding = self.config_hash
        for expected, row in enumerate(
            db.execute("SELECT seq, config, parent, digest, payload FROM events ORDER BY seq"), 1
        ):
            seq, config, previous, digest, payload = row
            if binding is None:
                binding = config
            if not isinstance(payload, bytes) or len(payload) > MAX_SNAPSHOT:
                raise ValueError("invalid journal payload")
            actual = sha256_hex(
                canonical_json(
                    {
                        "seq": seq,
                        "config": config,
                        "parent": previous,
                        "state_hash": sha256_hex(payload),
                    }
                )
            )
            if seq != expected or config != binding or previous != parent or digest != actual:
                raise ValueError("journal chain or configuration changed")
            raw = json.loads(payload)
            if (
                canonical_json(raw) != payload
                or not isinstance(raw, dict)
                or raw.get("mode") != "live"
            ):
                raise ValueError("invalid canonical live journal state")
            parent, latest = digest, payload
        self.config_hash = binding
        return parent, latest

    def load(self, projection: bytes | None = None) -> bytes | None:
        with closing(self._connect()) as db:
            db.execute("BEGIN")
            self.revision, payload = self._read(db)
            if projection is not None:
                canonical = canonical_json(json.loads(projection))
                if (
                    db.execute(
                        "SELECT 1 FROM events WHERE payload = ? LIMIT 1", (canonical,)
                    ).fetchone()
                    is None
                ):
                    raise ValueError(
                        "state projection is absent from journal; modification or rollback"
                    )
            db.execute("COMMIT")
            return payload

    def append(self, state: dict[str, Any]) -> None:
        if self.config_hash is None or self.revision is None:
            raise ValueError("journal must be bound and read before appending")
        payload = canonical_json(state)
        if len(payload) > MAX_SNAPSHOT:
            raise ValueError("journal snapshot exceeds byte budget")
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            parent, latest = self._read(db)
            if parent != self.revision:
                raise JournalConflict("stale writer cannot overwrite journal state")
            if payload == latest:
                db.execute("COMMIT")
                return
            seq = db.execute("SELECT COUNT(*) FROM events").fetchone()[0] + 1
            digest = sha256_hex(
                canonical_json(
                    {
                        "seq": seq,
                        "config": self.config_hash,
                        "parent": parent,
                        "state_hash": sha256_hex(payload),
                    }
                )
            )
            db.execute(
                "INSERT INTO events VALUES (?, ?, ?, ?, ?)",
                (seq, self.config_hash, parent, digest, payload),
            )
            db.execute("COMMIT")
            self.revision = digest

    def pending_snapshot(self, nonce: int) -> bytes | None:
        """Read one unambiguous historical commitment without advancing writer CAS."""
        if type(nonce) is not int or not 0 <= nonce < 2**64 or self.config_hash is None:
            raise ValueError("bound journal and canonical nonce required")
        with closing(self._connect()) as db:
            db.execute("BEGIN")
            self._read(db)  # Validate the whole chain, including entries after the match.
            selected, commitment = None, None
            for (payload,) in db.execute("SELECT payload FROM events ORDER BY seq"):
                pending = json.loads(payload).get("pending")
                if not isinstance(pending, dict) or pending.get("nonce") != nonce:
                    continue
                if type(pending["nonce"]) is not int:
                    raise ValueError("historical commitment nonce invalid")
                encoded = canonical_json(pending)
                if commitment is not None and encoded != commitment:
                    raise ValueError("ambiguous historical commitment for nonce")
                selected, commitment = payload, encoded
            db.execute("COMMIT")
            return selected
