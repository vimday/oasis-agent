"""Read-only, version-bound access to evidence and hash-linked journals.

Connections are query-only and confined to the server event-loop thread.
Each journal cache probe uses SQLite data_version on the same persistent
connection inside a read transaction. Any external SQLite commit invalidates
validation, including edits that preserve row count and tail digest. A cold
read validates the entire chain in that transaction before publishing it.
Unchanged reads perform no table scans. Separate database files do not form
an atomic cross-database snapshot. Direct file mutation outside SQLite is not
supported; operators must stop the server before replacing journal files.
"""

import hashlib
import json
import sqlite3
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from ..models.common import canonical_json, sha256_hex
from ..reporting.lp_readiness import LpReleaseAssessment, parse_assessment
from .config import ServerConfig

SHADOW_HEALTH_SCHEMA = "oasis.shadow-health.v1"
SHADOW_DECISION_SCHEMA = "oasis.shadow-lp-decision.v1"
EXECUTION_SCHEMA_V1 = "oasis.lp-execution-journal.v1"
EXECUTION_SCHEMA_V2 = "oasis.lp-execution-journal.v2"
EXECUTION_SCHEMA_V3 = "oasis.lp-execution-journal.v3"
EXECUTION_EVENT_FIELDS_V1 = frozenset(
    {
        "schema",
        "seq",
        "previous",
        "policy_hash",
        "intent_hash",
        "nonce",
        "state",
        "transaction_hash",
        "evidence_hash",
        "intent",
    }
)
EXECUTION_EVENT_FIELDS_V2 = EXECUTION_EVENT_FIELDS_V1 | {"recovery"}
SHADOW_DECISION_VALUES = frozenset({"hold", "halt", "recenter", "remove", "reduce", "unwind"})
ZERO_HASH = "0" * 64

_EVIDENCE_COLUMNS = (
    "evidence_key, content_hash, kind, payload, chain_id, block_number, block_hash, "
    "source, retrieved_at, request_hash, raw_response_hash, interpretation_hash, "
    "schema_version"
)


class ReadOnlyError(Exception):
    """A data source is corrupt, missing or otherwise unusable; fail closed.

    ``code`` is a stable machine-readable classification (``unconfigured`` vs
    ``corrupt``) so the API handlers and the dashboard can render distinct
    states without string-matching error messages.
    """

    def __init__(self, message: str, *, code: str = "corrupt") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class DataSourceStatus:
    available: bool
    integrity: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class Page:
    items: tuple[dict[str, object], ...]
    total: int
    next_seq: int | None


@dataclass(frozen=True, slots=True)
class _ValidatedSnapshot:
    """Validated chain records plus the probe that made them authoritative."""

    probe: tuple[object, ...]
    observations: tuple[dict[str, object], ...] = ()
    decisions: tuple[dict[str, object], ...] = ()
    execution: tuple[tuple[dict[str, object], ...], str] = ((), "")


def _open_ro(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    connection.execute("PRAGMA query_only=ON")
    return connection


def _decode(payload: bytes, seq: int, previous: str, digest: str) -> dict[str, object]:
    try:
        data = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReadOnlyError("journal record is not valid JSON") from exc
    if not isinstance(data, dict):
        raise ReadOnlyError("journal record is not a JSON object")
    if (
        type(data.get("seq")) is not int
        or data.get("seq") != seq
        or data.get("previous") != previous
    ):
        raise ReadOnlyError("journal record position mismatch")
    if hashlib.sha256(payload).hexdigest() != digest:
        raise ReadOnlyError("journal record digest mismatch")
    if canonical_json(data) != payload:
        raise ReadOnlyError("journal record is not canonical JSON")
    return data


def _validate_observation_chain(connection: sqlite3.Connection) -> tuple[dict[str, object], ...]:
    records: list[dict[str, object]] = []
    previous = ZERO_HASH
    for seq, payload, digest in connection.execute(
        "SELECT seq, payload, digest FROM observations ORDER BY seq"
    ):
        if seq != len(records) + 1:
            raise ReadOnlyError("observation journal sequence gap")
        data = _decode(bytes(payload), int(seq), previous, str(digest))
        if (
            data.get("schema") != SHADOW_HEALTH_SCHEMA
            or data.get("execution_eligible") is not False
        ):
            raise ReadOnlyError("shadow health record schema violated")
        records.append(data | {"digest": digest})
        previous = str(digest)
    return tuple(records)


def _validate_decision_chain(connection: sqlite3.Connection) -> tuple[dict[str, object], ...]:
    records: list[dict[str, object]] = []
    previous = ZERO_HASH
    for seq, payload, digest in connection.execute(
        "SELECT seq, payload, digest FROM decisions ORDER BY seq"
    ):
        if seq != len(records) + 1:
            raise ReadOnlyError("decision journal sequence gap")
        data = _decode(bytes(payload), int(seq), previous, str(digest))
        if (
            data.get("schema") != SHADOW_DECISION_SCHEMA
            or data.get("execution_eligible") is not False
            or data.get("decision") not in SHADOW_DECISION_VALUES
            or not isinstance(data.get("health_digest"), str)
        ):
            raise ReadOnlyError("shadow decision record schema violated")
        records.append(data | {"digest": digest})
        previous = str(digest)
    return tuple(records)


def _validate_execution_chain(
    connection: sqlite3.Connection,
) -> tuple[tuple[dict[str, object], ...], str]:
    policy_row = connection.execute("SELECT payload FROM policy WHERE id=1").fetchone()
    if policy_row is None:
        raise ReadOnlyError("execution journal policy missing")
    policy_payload = bytes(policy_row[0])
    policy_hash = sha256_hex(policy_payload)
    records: list[dict[str, object]] = []
    previous = ZERO_HASH
    sequence = 0
    for seq, payload, digest in connection.execute(
        "SELECT seq, payload, digest FROM events ORDER BY seq"
    ):
        if int(seq) != sequence + 1:
            raise ReadOnlyError("execution journal sequence gap")
        data = _decode(bytes(payload), int(seq), previous, str(digest))
        schema = data.get("schema")
        allowed_fields = EXECUTION_EVENT_FIELDS_V1 if schema == EXECUTION_SCHEMA_V1 else None
        if schema in {EXECUTION_SCHEMA_V2, EXECUTION_SCHEMA_V3}:
            allowed_fields = EXECUTION_EVENT_FIELDS_V2
        if allowed_fields is None or set(data) != allowed_fields:
            raise ReadOnlyError("execution journal schema violated")
        if data.get("policy_hash") != policy_hash:
            raise ReadOnlyError("execution journal policy mismatch")
        nonce = data.get("nonce")
        if not isinstance(nonce, int) or nonce < 0:
            raise ReadOnlyError("execution journal nonce violated")
        if not isinstance(data.get("state"), str):
            raise ReadOnlyError("execution journal state violated")
        records.append(data | {"digest": digest})
        previous = str(digest)
        sequence = int(seq)
    return tuple(records), policy_hash


def _evidence_filters(
    kind: str | None, chain_id: int | None, source: str | None
) -> tuple[list[str], list[object]]:
    """Return allowlisted WHERE fragments and their bound parameters."""
    clauses: list[str] = []
    params: list[object] = []
    if kind is not None:
        clauses.append("kind = ?")
        params.append(kind)
    if chain_id is not None:
        clauses.append("chain_id = ?")
        params.append(chain_id)
    if source is not None:
        clauses.append("source = ?")
        params.append(source)
    return clauses, params


def _row_to_record(row: sqlite3.Row | tuple[object, ...]) -> dict[str, object]:
    # sqlite3.Row is not a plain tuple to the type checker, so normalize first;
    # every column is then narrowed explicitly below and malformed storage fails
    # closed instead of producing a half-typed record.
    values: tuple[object, ...] = tuple(row)
    (
        key,
        content_hash,
        kind_value,
        payload,
        chain_id_value,
        block_number,
        block_hash,
        source_value,
        retrieved_at,
        request_hash,
        raw_response_hash,
        interpretation_hash,
        schema_version,
    ) = values
    if not isinstance(payload, bytes):
        raise ReadOnlyError("evidence payload not bytes")
    if sha256_hex(payload) != str(content_hash):
        raise ReadOnlyError("evidence payload hash mismatch")
    block: dict[str, object] | None = None
    chain_id: int | None = None
    if chain_id_value is not None and block_number is not None:
        if not isinstance(chain_id_value, int) or not isinstance(block_number, int):
            raise ReadOnlyError("evidence block fields not integers")
        chain_id = chain_id_value
        block = {
            "chain_id": chain_id,
            "number": block_number,
            "block_hash": str(block_hash),
        }
    if not isinstance(schema_version, int):
        raise ReadOnlyError("evidence schema_version not integer")
    return {
        "evidence_key": str(key),
        "content_hash": str(content_hash),
        "kind": str(kind_value),
        "chain_id": chain_id,
        "block": block,
        "source": str(source_value),
        "retrieved_at": str(retrieved_at),
        "request_hash": str(request_hash),
        "raw_response_hash": str(raw_response_hash),
        "interpretation_hash": str(interpretation_hash),
        "schema_version": schema_version,
        "payload": _maybe_decode_payload(payload),
    }


def _maybe_decode_payload(payload: bytes) -> object:
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return payload.hex()
    return value


class ReadOnlyDatabases:
    def __init__(self, config: ServerConfig) -> None:
        self._config = config
        self._shadow: sqlite3.Connection | None = None
        self._evidence: sqlite3.Connection | None = None
        self._execution: sqlite3.Connection | None = None
        self._snapshots: dict[str, _ValidatedSnapshot] = {}
        self._readiness: tuple[str, LpReleaseAssessment] | None = None
        try:
            if config.databases.shadow is not None:
                self._shadow = _open_ro(config.databases.shadow)
            if config.databases.evidence is not None:
                self._evidence = _open_ro(config.databases.evidence)
            if config.databases.execution is not None:
                self._execution = _open_ro(config.databases.execution)
        except sqlite3.Error:
            self.close()
            raise

    def close(self) -> None:
        for connection in (self._shadow, self._evidence, self._execution):
            if connection is not None:
                connection.close()
        self._shadow = None
        self._evidence = None
        self._execution = None
        self._snapshots.clear()
        self._readiness = None

    def _require(self, connection: sqlite3.Connection | None, name: str) -> sqlite3.Connection:
        if connection is None:
            raise ReadOnlyError(f"{name} database is not configured", code="unconfigured")
        return connection

    # ----- Validated snapshot (memoized hash-chain validation) -----

    def _validated(self, source: Literal["shadow", "execution"]) -> _ValidatedSnapshot:
        # Source ownership is independent: a corrupt execution journal cannot
        # conceal a verified shadow HALT, nor can it validate the other source.
        connection = self._require(self._shadow if source == "shadow" else self._execution, source)
        try:
            with ExitStack() as transaction:
                connection.execute("BEGIN")
                transaction.callback(connection.rollback)
                probe = (connection.execute("PRAGMA data_version").fetchone()[0],)
                snapshot = self._snapshots.get(source)
                if snapshot is not None and snapshot.probe == probe:
                    return snapshot
                self._snapshots.pop(source, None)
                if source == "shadow":
                    observations = _validate_observation_chain(connection)
                    decisions = _validate_decision_chain(connection)
                    health_digests = {record["digest"] for record in observations}
                    if any(record["health_digest"] not in health_digests for record in decisions):
                        raise ReadOnlyError("decision health evidence is missing")
                    snapshot = _ValidatedSnapshot(probe, observations, decisions)
                else:
                    snapshot = _ValidatedSnapshot(
                        probe, execution=_validate_execution_chain(connection)
                    )
            self._snapshots[source] = snapshot
            return snapshot
        except sqlite3.Error as exc:
            self._snapshots.pop(source, None)
            raise ReadOnlyError("journal database unreadable") from exc

    # ----- Shadow health chain -----

    def health_chain(self) -> tuple[dict[str, object], ...]:
        self._require(self._shadow, "shadow")
        return self._validated("shadow").observations

    def decision_chain(self) -> tuple[dict[str, object], ...]:
        self._require(self._shadow, "shadow")
        return self._validated("shadow").decisions

    # ----- Evidence -----

    def evidence_page(
        self,
        *,
        kind: str | None = None,
        chain_id: int | None = None,
        source: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Page:
        """Serve a validated evidence page with filtering pushed into SQL."""
        connection = self._require(self._evidence, "evidence")
        clauses, params = _evidence_filters(kind, chain_id, source)
        # WHERE fragments come only from the allowlist in _evidence_filters;
        # every filter value is bound as a parameter, never interpolated.
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        try:
            count_statement = "SELECT COUNT(*) FROM evidence" + where  # nosec B608
            total = int(connection.execute(count_statement, params).fetchone()[0])
            rows = connection.execute(
                "SELECT "  # nosec B608 - fixed allowlisted columns and fragments
                + _EVIDENCE_COLUMNS
                + " FROM evidence"
                + where
                + " ORDER BY retrieved_at DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        except sqlite3.Error as exc:
            raise ReadOnlyError("evidence database unreadable") from exc
        return Page(items=tuple(_row_to_record(row) for row in rows), total=total, next_seq=None)

    def evidence_by_key(self, evidence_key: str) -> dict[str, object] | None:
        connection = self._require(self._evidence, "evidence")
        try:
            row = connection.execute(
                "SELECT "  # nosec B608 - allowlisted columns, key bound as parameter
                + _EVIDENCE_COLUMNS
                + " FROM evidence WHERE evidence_key = ?",
                (evidence_key,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise ReadOnlyError("evidence database unreadable") from exc
        return _row_to_record(row) if row is not None else None

    # ----- Execution journal -----

    def execution_events(self) -> tuple[tuple[dict[str, object], ...], str]:
        self._require(self._execution, "execution")
        events, policy_hash = self._validated("execution").execution
        return events, policy_hash

    def execution_summary(self) -> dict[str, object]:
        events, policy_hash = self.execution_events()
        by_intent: dict[str, dict[str, object]] = {}
        for event in events:
            identity = str(event["intent_hash"])
            by_intent[identity] = event
        open_records = tuple(
            event
            for event in by_intent.values()
            if str(event["state"]) not in {"closed", "expired"}
        )
        return {
            "policy_hash": policy_hash,
            "total_events": len(events),
            "intent_count": len(by_intent),
            "open_intents": len(open_records),
            "latest": events[-1] | {"digest": events[-1]["digest"]} if events else None,
        }

    # ----- Readiness -----

    def readiness(self, *, as_of: str | None = None) -> LpReleaseAssessment:
        manifest = self._config.databases.readiness_manifest
        if manifest is None:
            raise ReadOnlyError("readiness manifest is not configured", code="unconfigured")
        try:
            raw = manifest.read_bytes()
        except OSError as exc:
            raise ReadOnlyError(f"readiness manifest unreadable: {exc}") from exc
        # Cache parsing, never time-dependent eligibility. A content hash does
        # not stop evidence from expiring, and is not a signature verification.
        probe = sha256_hex(raw)
        cached = self._readiness
        try:
            evaluated_at = as_of if as_of is not None else datetime.now(UTC).isoformat()
            assessment = (
                LpReleaseAssessment(evaluated_at, cached[1].gates)
                if cached is not None and cached[0] == probe
                else parse_assessment(raw, as_of=evaluated_at)
            )
        except (ValueError, TypeError, KeyError) as exc:
            self._readiness = None
            raise ReadOnlyError("readiness manifest unreadable") from exc
        self._readiness = (probe, assessment)
        return assessment

    # ----- Health -----

    def sources_status(self) -> dict[str, DataSourceStatus]:
        statuses: dict[str, DataSourceStatus] = {}
        for name, connection in (
            ("shadow", self._shadow),
            ("evidence", self._evidence),
            ("execution", self._execution),
        ):
            if connection is None:
                statuses[name] = DataSourceStatus(False, "unconfigured")
                continue
            try:
                if name == "shadow":
                    self._validated("shadow")
                elif name == "evidence":
                    connection.execute("SELECT 1 FROM evidence LIMIT 1").fetchone()
                else:
                    self._validated("execution")
                statuses[name] = DataSourceStatus(True, "verified")
            except (ReadOnlyError, sqlite3.Error) as exc:
                statuses[name] = DataSourceStatus(False, "corrupt", str(exc))
        try:
            self.readiness()
            statuses["readiness"] = DataSourceStatus(True, "verified")
        except ReadOnlyError as exc:
            statuses["readiness"] = DataSourceStatus(False, exc.code)
        return statuses
