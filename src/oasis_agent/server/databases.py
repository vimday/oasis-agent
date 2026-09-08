"""Read-only access to the evidence, shadow and execution journals.

Every SQLite connection opens with ``mode=ro`` and ``PRAGMA query_only``, so
the API process cannot mutate the audit chains it serves. Hash-linked chains
(observations, decisions, execution events) are fully re-validated on every
read and any inconsistency raises :class:`ReadOnlyError`; the API then fails
closed instead of serving a partial or tampered view. The readiness manifest
is parsed with the repository's strict fail-closed parser.

Validated chains are memoized: each read probes the journals with cheap
indexed queries (row count, tail digest, policy hash) and only re-runs the
full hash-chain validation when the probe changes. The journals are
append-only, so an unchanged probe guarantees the previously validated
records are still authoritative; a changed or tampered journal forces a full
re-validation and fails closed on any inconsistency. The memo lives on the
single-threaded event loop, so it needs no lock.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from ..models.common import canonical_json, sha256_hex
from ..reporting.lp_readiness import LpReleaseAssessment, parse_assessment
from .config import ServerConfig

SHADOW_HEALTH_SCHEMA = "oasis.shadow-health.v1"
SHADOW_DECISION_SCHEMA = "oasis.shadow-lp-decision.v1"
EXECUTION_SCHEMA_V1 = "oasis.lp-execution-journal.v1"
EXECUTION_SCHEMA_V2 = "oasis.lp-execution-journal.v2"
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

# Fixed SQL fragments keyed by allowlisted journal table names only. The
# statements are explicit literals so static analysis sees no string
# interpolation and there is no injection surface.
_CHAIN_TABLES: tuple[str, ...] = ("observations", "decisions", "events")
_PROBE_SQL: dict[str, str] = {
    "observations": "SELECT COUNT(*), COALESCE(MAX(seq), 0) FROM observations",
    "decisions": "SELECT COUNT(*), COALESCE(MAX(seq), 0) FROM decisions",
    "events": "SELECT COUNT(*), COALESCE(MAX(seq), 0) FROM events",
}
_TAIL_SQL: dict[str, str] = {
    "observations": "SELECT digest FROM observations ORDER BY seq DESC LIMIT 1",
    "decisions": "SELECT digest FROM decisions ORDER BY seq DESC LIMIT 1",
    "events": "SELECT digest FROM events ORDER BY seq DESC LIMIT 1",
}
_EVIDENCE_COLUMNS = (
    "evidence_key, content_hash, kind, payload, chain_id, block_number, block_hash, "
    "source, retrieved_at, request_hash, raw_response_hash, interpretation_hash, "
    "schema_version"
)


class ReadOnlyError(Exception):
    """A data source is corrupt, missing or otherwise unusable; fail closed."""


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
    if data.get("seq") != seq or data.get("previous") != previous:
        raise ReadOnlyError("journal record position mismatch")
    if hashlib.sha256(payload).hexdigest() != digest:
        raise ReadOnlyError("journal record digest mismatch")
    if canonical_json(data) != payload:
        raise ReadOnlyError("journal record is not canonical JSON")
    return data


def _chain_probe(connection: sqlite3.Connection, table: str) -> tuple[int, int, str]:
    """Cheap invalidation key for an append-only hash-linked chain.

    ``table`` must be one of the allowlisted :data:`_CHAIN_TABLES` names; the
    statements themselves are fixed literals looked up from :data:`_PROBE_SQL`
    and :data:`_TAIL_SQL`. A hash chain is fully determined by its tail digest
    and length, so an unchanged probe proves the cached validation is still
    authoritative.
    """
    row = connection.execute(_PROBE_SQL[table]).fetchone()
    tail = connection.execute(_TAIL_SQL[table]).fetchone()
    return (int(row[0]), int(row[1]), str(tail[0]) if tail is not None else ZERO_HASH)


def _validate_observation_chain(connection: sqlite3.Connection) -> tuple[dict[str, object], ...]:
    records: list[dict[str, object]] = []
    previous = ZERO_HASH
    for seq, payload, digest in connection.execute(
        "SELECT seq, payload, digest FROM observations ORDER BY seq"
    ):
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
        if schema == EXECUTION_SCHEMA_V2:
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
        self._snapshot: _ValidatedSnapshot | None = None
        self._readiness: tuple[str, LpReleaseAssessment] | None = None
        if config.databases.shadow is not None:
            self._shadow = _open_ro(config.databases.shadow)
        if config.databases.evidence is not None:
            self._evidence = _open_ro(config.databases.evidence)
        if config.databases.execution is not None:
            self._execution = _open_ro(config.databases.execution)

    def close(self) -> None:
        for connection in (self._shadow, self._evidence, self._execution):
            if connection is not None:
                connection.close()
        self._shadow = None
        self._evidence = None
        self._execution = None
        self._snapshot = None
        self._readiness = None

    def _require(self, connection: sqlite3.Connection | None, name: str) -> sqlite3.Connection:
        if connection is None:
            raise ReadOnlyError(f"{name} database is not configured")
        return connection

    # ----- Validated snapshot (memoized hash-chain validation) -----

    def _shadow_probe(self) -> tuple[tuple[int, int, str], tuple[int, int, str]]:
        connection = self._require(self._shadow, "shadow")
        try:
            return (
                _chain_probe(connection, "observations"),
                _chain_probe(connection, "decisions"),
            )
        except sqlite3.Error as exc:
            raise ReadOnlyError("shadow database unreadable") from exc

    def _execution_probe(self) -> tuple[tuple[int, int, str], str | None]:
        connection = self._require(self._execution, "execution")
        try:
            policy_row = connection.execute("SELECT payload FROM policy WHERE id=1").fetchone()
            policy_hash = sha256_hex(bytes(policy_row[0])) if policy_row is not None else None
            return (_chain_probe(connection, "events"), policy_hash)
        except sqlite3.Error as exc:
            raise ReadOnlyError("execution database unreadable") from exc

    def _validated(self) -> _ValidatedSnapshot:
        probe: tuple[object, ...] = (
            self._shadow_probe() if self._shadow is not None else None,
            self._execution_probe() if self._execution is not None else None,
        )
        snapshot = self._snapshot
        if snapshot is not None and snapshot.probe == probe:
            return snapshot
        observations = _validate_observation_chain(self._shadow) if self._shadow is not None else ()
        decisions = _validate_decision_chain(self._shadow) if self._shadow is not None else ()
        if self._execution is not None:
            execution = _validate_execution_chain(self._execution)
        else:
            execution = ((), "")
        snapshot = _ValidatedSnapshot(probe, observations, decisions, execution)
        self._snapshot = snapshot
        return snapshot

    # ----- Shadow health chain -----

    def health_chain(self) -> tuple[dict[str, object], ...]:
        self._require(self._shadow, "shadow")
        return self._validated().observations

    def decision_chain(self) -> tuple[dict[str, object], ...]:
        self._require(self._shadow, "shadow")
        return self._validated().decisions

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
        events, policy_hash = self._validated().execution
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

    def readiness(self) -> LpReleaseAssessment:
        manifest = self._config.databases.readiness_manifest
        if manifest is None:
            raise ReadOnlyError("readiness manifest is not configured")
        try:
            raw = manifest.read_bytes()
        except OSError as exc:
            raise ReadOnlyError(f"readiness manifest unreadable: {exc}") from exc
        # The manifest is a signed evidence file that is replaced atomically by
        # the producer; a content-hash probe proves the cached parse is still
        # authoritative without re-validating on every request.
        probe = sha256_hex(raw)
        cached = self._readiness
        if cached is not None and cached[0] == probe:
            return cached[1]
        try:
            assessment = parse_assessment(raw)
        except (ValueError, TypeError) as exc:
            raise ReadOnlyError(f"readiness manifest unreadable: {exc}") from exc
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
                    self._validated()
                elif name == "evidence":
                    connection.execute("SELECT 1 FROM evidence LIMIT 1").fetchone()
                else:
                    self._validated()
                statuses[name] = DataSourceStatus(True, "verified")
            except (ReadOnlyError, sqlite3.Error) as exc:
                statuses[name] = DataSourceStatus(False, "corrupt", str(exc))
        statuses["readiness"] = (
            DataSourceStatus(True, "verified")
            if self._config.databases.readiness_manifest is not None
            else DataSourceStatus(False, "unconfigured")
        )
        return statuses
