"""Configuration-bound append-only authoritative state snapshots with CAS writers.

The JSON state file is only a projection. A committed journal snapshot survives a
crash before projection replacement. Unknown signing attempts remain reserved;
there is no automatic nonce release, signature retry or broadcast retry.
"""

import json
import os
import sqlite3
import stat
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from ..models.common import canonical_json, sha256_hex
from .feed_epoch import transition_snapshot

ZERO = "0" * 64
MAX_SNAPSHOT = 2_000_000


class JournalConflict(ValueError):
    pass


class LiveJournal:
    def __init__(
        self, path: Path, config_hash: str | None = None, *, require_existing: bool = False
    ) -> None:
        self.path = path
        if path.is_symlink():
            raise ValueError("journal cannot be a symlink")
        if path.exists():
            info = path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise ValueError("journal must be an owned regular file")
        elif require_existing:
            raise ValueError("required execution journal missing")
        elif config_hash is None:
            raise ValueError("journal creation requires a configuration binding")
        self.config_hash = config_hash
        self.revision: str | None = None
        if config_hash is not None and (
            len(config_hash) != 64 or any(c not in "0123456789abcdef" for c in config_hash)
        ):
            raise ValueError("canonical configuration digest required")
        if config_hash is not None and not require_existing:
            path.parent.mkdir(parents=True, exist_ok=True)
            with closing(self._connect(create=True)) as db:
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

    def _connect(self, *, create: bool = False, read_only: bool = False) -> sqlite3.Connection:
        if create and read_only:
            raise ValueError("read-only journal cannot be created")
        db = sqlite3.connect(
            self.path
            if create
            else self.path.resolve().as_uri() + ("?mode=ro" if read_only else "?mode=rw"),
            uri=not create,
            timeout=5,
            isolation_level=None,
        )
        if not read_only:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("PRAGMA journal_mode=DELETE")
        return db

    def _read(self, db: sqlite3.Connection, *, schema: str = "main") -> tuple[str, bytes | None]:
        # The paired offline rearm validates both original chains inside one
        # ATTACH transaction. Never accept an operator-controlled SQL identifier.
        if schema not in {"main", "peer"}:
            raise ValueError("unsupported journal schema")
        parent = ZERO
        latest = None
        binding = None
        transitions = {}
        if db.execute(
            "SELECT 1 FROM main.sqlite_master WHERE type='table' AND name='config_transitions'"
            if schema == "main"
            else "SELECT 1 FROM peer.sqlite_master WHERE type='table' AND name='config_transitions'"
        ).fetchone():
            rows = db.execute(
                "SELECT seq, payload FROM main.config_transitions LIMIT 2"
                if schema == "main"
                else "SELECT seq, payload FROM peer.config_transitions LIMIT 2"
            ).fetchall()
            if len(rows) != 1:
                raise ValueError("exactly one supported configuration transition required")
            transitions = dict(rows)
        for expected, row in enumerate(
            db.execute(
                "SELECT seq, config, parent, digest, payload FROM main.events ORDER BY seq"
                if schema == "main"
                else "SELECT seq, config, parent, digest, payload FROM peer.events ORDER BY seq"
            ),
            1,
        ):
            seq, config, previous, digest, payload = row
            if (
                not isinstance(config, str)
                or len(config) != 64
                or any(c not in "0123456789abcdef" for c in config)
            ):
                raise ValueError("invalid journal configuration digest")
            if binding is None:
                binding = config
            if not isinstance(payload, bytes) or len(payload) > MAX_SNAPSHOT:
                raise ValueError("invalid journal payload")
            raw = json.loads(payload)
            if (
                canonical_json(raw) != payload
                or not isinstance(raw, dict)
                or raw.get("mode") != "live"
            ):
                raise ValueError("invalid canonical live journal state")
            commitment = {
                "seq": seq,
                "config": config,
                "parent": previous,
                "state_hash": sha256_hex(payload),
            }
            transition = transitions.pop(seq, None)
            if config != binding:
                if (
                    latest is None
                    or not isinstance(transition, bytes)
                    or len(transition) > MAX_SNAPSHOT
                ):
                    raise ValueError("configuration change lacks its transition record")
                record = json.loads(transition)
                if canonical_json(record) != transition:
                    raise ValueError("noncanonical transition record")
                target, snapshot = transition_snapshot(
                    json.loads(latest), record, binding=binding, parent=parent
                )
                if target != config or canonical_json(snapshot) != payload:
                    raise ValueError("transition state or configuration differs")
                commitment["transition_hash"] = sha256_hex(transition)
                binding = target
            elif transition is not None:
                raise ValueError("unexpected transition record")
            actual = sha256_hex(canonical_json(commitment))
            if seq != expected or previous != parent or digest != actual:
                raise ValueError("journal chain or configuration changed")
            parent, latest = digest, payload
        if transitions or (
            self.config_hash is not None and binding not in (None, self.config_hash)
        ):
            raise ValueError(
                "journal configuration changed; terminal configuration mismatch "
                "or orphan transition"
            )
        if binding is not None:
            self.config_hash = binding
        return parent, latest

    def transition(self, record: dict[str, Any]) -> bytes:
        """Append one reviewed strict-feed epoch atomically; never update old events.

        The caller must first replay raw proofs with feed_transition.apply_service.
        This storage operation cannot clear HALT and does not touch RPC budgets.
        """
        if self.config_hash is None or self.revision is None:
            raise ValueError("transition requires a loaded, bound journal")
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            parent, latest = self._read(db)
            if parent != self.revision or latest is None or record["source_tip"] != parent:
                raise JournalConflict("transition source changed")
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='config_transitions'").fetchone():
                raise ValueError("a transition already exists; no repeat or replacement")
            target, state = transition_snapshot(
                json.loads(latest), record, binding=self.config_hash, parent=parent
            )
            payload, encoded = canonical_json(state), canonical_json(record)
            if max(len(payload), len(encoded)) > MAX_SNAPSHOT:
                raise ValueError("transition exceeds byte bound")
            seq = db.execute("SELECT COUNT(*) FROM events").fetchone()[0] + 1
            digest = sha256_hex(
                canonical_json(
                    {
                        "seq": seq,
                        "config": target,
                        "parent": parent,
                        "state_hash": sha256_hex(payload),
                        "transition_hash": sha256_hex(encoded),
                    }
                )
            )
            db.execute(
                "CREATE TABLE config_transitions (seq INTEGER PRIMARY KEY, payload BLOB NOT NULL)"
            )
            for operation in ("UPDATE", "DELETE"):
                db.execute(
                    f"CREATE TRIGGER transition_no_{operation.lower()} BEFORE {operation} "
                    "ON config_transitions BEGIN SELECT RAISE(ABORT, 'append only'); END"
                )
            db.execute("INSERT INTO config_transitions VALUES (?, ?)", (seq, encoded))
            db.execute(
                "INSERT INTO events VALUES (?, ?, ?, ?, ?)", (seq, target, parent, digest, payload)
            )
            fresh = record["review"]["fresh_nav"]
            if not fresh["block"]["timestamp"] <= int(time.time()) <= fresh["valid_until"]:
                raise ValueError("transition proof expired while acquiring journal lock")
            db.execute("COMMIT")
        self.config_hash, self.revision = target, digest
        return payload

    def load(self, projection: bytes | None = None, *, read_only: bool = False) -> bytes | None:
        with closing(self._connect(read_only=read_only)) as db:
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
