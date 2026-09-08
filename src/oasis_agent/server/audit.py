"""Append-only operations audit journal for the observation server.

This database belongs to the server itself and is deliberately separate from
the read-only evidence, shadow and execution journals. Every attempted
operation (run trigger, gate approval, approval confirmation) is recorded
with a digest of its parameters; raw output tails are truncated and
redacted so credentials cannot leak into the audit trail.
"""

from __future__ import annotations

import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Self

from ..models.common import canonical_json, sha256_hex

_RUN_TAIL_LIMIT = 2_000
_DETAIL_LIMIT = 4_000
_MAX_TAIL_ROWS = 50


@dataclass(frozen=True, slots=True)
class OpsRun:
    run_id: str
    created_at: str
    command_digest: str
    status: str
    exit_code: int | None
    started_at: str | None
    finished_at: str | None
    stdout_tail: str
    stderr_tail: str


def _redact(text: str) -> str:
    redacted = re.sub(r"(?i)(token|secret|key|password)(\s*[=:]\s*)(\S+)", r"\1\2***", text)
    redacted = re.sub(r"(?i)(Bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1***", redacted)
    return redacted[-_DETAIL_LIMIT:] if len(redacted) > _DETAIL_LIMIT else redacted


def _tail(text: str) -> str:
    if len(text) <= _RUN_TAIL_LIMIT:
        return text
    return text[-_RUN_TAIL_LIMIT:]


class ServerOpsJournal:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS ops (
                seq INTEGER PRIMARY KEY,
                ts TEXT NOT NULL,
                action TEXT NOT NULL,
                actor TEXT NOT NULL,
                params_digest TEXT NOT NULL,
                result TEXT NOT NULL,
                detail TEXT NOT NULL
            )
            """
        )
        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                command_digest TEXT NOT NULL,
                status TEXT NOT NULL,
                exit_code INTEGER,
                started_at TEXT,
                finished_at TEXT,
                stdout_tail TEXT NOT NULL,
                stderr_tail TEXT NOT NULL
            )
            """
        )
        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS approvals (
                id INTEGER PRIMARY KEY,
                gate TEXT NOT NULL,
                status TEXT NOT NULL,
                requested_by TEXT NOT NULL,
                requested_at TEXT NOT NULL,
                requested_reason TEXT NOT NULL,
                confirmed_by TEXT,
                confirmed_at TEXT
            )
            """
        )
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    def record(
        self, *, action: str, actor: str, params: dict[str, object], result: str, detail: str
    ) -> int:
        params_digest = sha256_hex(canonical_json(params))
        with self.db:
            cursor = self.db.execute(
                "INSERT INTO ops (ts, action, actor, params_digest, result, detail) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    self._now(),
                    action,
                    actor,
                    params_digest,
                    result,
                    _redact(detail),
                ),
            )
        rowid = cursor.lastrowid
        if rowid is None:
            raise RuntimeError("audit insert did not return a rowid")
        return int(rowid)

    def recent_ops(self, *, limit: int) -> tuple[dict[str, object], ...]:
        rows = self.db.execute(
            "SELECT seq, ts, action, actor, params_digest, result, detail "
            "FROM ops ORDER BY seq DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return tuple(
            {
                "seq": int(seq),
                "ts": str(ts),
                "action": str(action),
                "actor": str(actor),
                "params_digest": str(params_digest),
                "result": str(result),
                "detail": str(detail),
            }
            for seq, ts, action, actor, params_digest, result, detail in rows
        )

    def create_run(self, *, command: tuple[str, ...]) -> OpsRun:
        run_id = uuid.uuid4().hex
        command_digest = sha256_hex(canonical_json(list(command)))
        now = self._now()
        with self.db:
            self.db.execute(
                "INSERT INTO runs (run_id, created_at, command_digest, status, exit_code, "
                "started_at, finished_at, stdout_tail, stderr_tail) "
                "VALUES (?, ?, ?, 'queued', NULL, NULL, NULL, '', '')",
                (run_id, now, command_digest),
            )
        return OpsRun(run_id, now, command_digest, "queued", None, None, None, "", "")

    def _update_run(self, run_id: str, **fields: object) -> None:
        allowed = {
            "status",
            "exit_code",
            "started_at",
            "finished_at",
            "stdout_tail",
            "stderr_tail",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unknown run field(s): {sorted(unknown)}")
        # Identifiers come only from the allowlist above; values are bound as
        # parameters below, never interpolated into the statement text.
        clauses = ", ".join(f"{name} = ?" for name in fields)
        # nosec B608 - the SET identifiers are allowlisted and the only
        # variable operand is a fixed fragment built from that allowlist.
        statement = "UPDATE runs SET " + clauses + " WHERE run_id = ?"  # nosec B608
        with self.db:
            self.db.execute(statement, (*fields.values(), run_id))

    def mark_started(self, run_id: str) -> None:
        self._update_run(run_id, status="running", started_at=self._now())

    def mark_finished(self, run_id: str, *, exit_code: int, stdout: str, stderr: str) -> None:
        self._update_run(
            run_id,
            status="succeeded" if exit_code == 0 else "failed",
            exit_code=exit_code,
            finished_at=self._now(),
            stdout_tail=_tail(_redact(stdout)),
            stderr_tail=_tail(_redact(stderr)),
        )

    def get_run(self, run_id: str) -> OpsRun | None:
        row = self.db.execute(
            "SELECT run_id, created_at, command_digest, status, exit_code, started_at, "
            "finished_at, stdout_tail, stderr_tail FROM runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        (
            run_id_value,
            created_at,
            command_digest,
            status,
            exit_code,
            started_at,
            finished_at,
            stdout_tail,
            stderr_tail,
        ) = row
        return OpsRun(
            run_id=str(run_id_value),
            created_at=str(created_at),
            command_digest=str(command_digest),
            status=str(status),
            exit_code=int(exit_code) if exit_code is not None else None,
            started_at=str(started_at) if started_at is not None else None,
            finished_at=str(finished_at) if finished_at is not None else None,
            stdout_tail=str(stdout_tail),
            stderr_tail=str(stderr_tail),
        )

    def list_runs(self, *, limit: int) -> tuple[dict[str, object], ...]:
        rows = self.db.execute(
            "SELECT run_id, created_at, command_digest, status, exit_code, started_at, "
            "finished_at FROM runs ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return tuple(
            {
                "run_id": str(run_id),
                "created_at": str(created_at),
                "command_digest": str(command_digest),
                "status": str(status),
                "exit_code": int(exit_code) if exit_code is not None else None,
                "started_at": str(started_at) if started_at is not None else None,
                "finished_at": str(finished_at) if finished_at is not None else None,
            }
            for run_id, created_at, command_digest, status, exit_code, started_at, finished_at in rows
        )

    def request_approval(self, *, gate: str, actor: str, reason: str) -> dict[str, object]:
        with self.db:
            cursor = self.db.execute(
                "INSERT INTO approvals (gate, status, requested_by, requested_at, "
                "requested_reason) VALUES (?, 'pending', ?, ?, ?)",
                (gate, actor, self._now(), _redact(reason)),
            )
        rowid = cursor.lastrowid
        if rowid is None:
            raise RuntimeError("approval insert did not return a rowid")
        return {
            "id": int(rowid),
            "gate": gate,
            "status": "pending",
            "requested_by": actor,
            "requested_reason": _redact(reason),
        }

    def confirm_approval(self, *, approval_id: int, actor: str) -> dict[str, object] | None:
        row = self.db.execute(
            "SELECT id, gate, status, requested_by FROM approvals WHERE id = ?",
            (approval_id,),
        ).fetchone()
        if row is None:
            return None
        approval_id_value, gate, status, requested_by = row
        if str(status) == "pending" and str(actor) != str(requested_by):
            with self.db:
                self.db.execute(
                    "UPDATE approvals SET status = 'approved', confirmed_by = ?, "
                    "confirmed_at = ? WHERE id = ?",
                    (actor, self._now(), approval_id_value),
                )
            return {
                "id": int(approval_id_value),
                "gate": str(gate),
                "status": "approved",
                "requested_by": str(requested_by),
                "confirmed_by": actor,
            }
        return {
            "id": int(approval_id_value),
            "gate": str(gate),
            "status": str(status),
            "requested_by": str(requested_by),
        }

    def list_approvals(self, *, gate: str | None = None) -> tuple[dict[str, object], ...]:
        if gate is None:
            rows = self.db.execute(
                "SELECT id, gate, status, requested_by, requested_at, requested_reason, "
                "confirmed_by, confirmed_at FROM approvals ORDER BY id DESC LIMIT ?",
                (_MAX_TAIL_ROWS,),
            ).fetchall()
        else:
            rows = self.db.execute(
                "SELECT id, gate, status, requested_by, requested_at, requested_reason, "
                "confirmed_by, confirmed_at FROM approvals WHERE gate = ? "
                "ORDER BY id DESC LIMIT ?",
                (gate, _MAX_TAIL_ROWS),
            ).fetchall()
        return tuple(
            {
                "id": int(approval_id),
                "gate": str(gate_value),
                "status": str(status),
                "requested_by": str(requested_by),
                "requested_at": str(requested_at),
                "requested_reason": str(requested_reason),
                "confirmed_by": str(confirmed_by) if confirmed_by is not None else None,
                "confirmed_at": str(confirmed_at) if confirmed_at is not None else None,
            }
            for approval_id, gate_value, status, requested_by, requested_at, requested_reason, confirmed_by, confirmed_at in rows
        )
