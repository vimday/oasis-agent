"""Durable offline quote work, atomic with canonical event coverage.

This is a historical research outbox. Leases authorize CPU replay only; window
cutoffs govern the retained market data, not a permission to quote or trade live.
"""

import json
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, TypeVar, cast

from ..evidence.chain_store import CanonicalChainStore
from ..evidence.ingestion import replay_block_range_evidence
from ..evidence.store import EvidenceStore
from ..models.chain import BlockSnapshot
from ..models.common import (
    BlockRef,
    EvidenceRef,
    canonical_json,
    evidence_ref_from_json,
    sha256_hex,
)
from .cycle_evidence import Export
from .cycle_plan import Plan
from .cycle_research import QuoteScope, evaluate_windows
from .cycle_shadow import ShadowWindow, replay_shadow, shadow_config_sha256

T = TypeVar("T")
MAX_REPORT_BYTES = 8 * 1024 * 1024


def _source_fingerprint() -> str:
    root = Path(__file__).resolve().parents[1]
    paths = [
        *sorted((root / "arbitrage").glob("cycle_*.py")),
        *(
            root / "evidence" / name
            for name in (
                "chain_store.py",
                "ingestion.py",
                "store.py",
                "range_validation.py",
                "rpc_validation.py",
            )
        ),
        root / "models" / "common.py",
        root / "models" / "chain.py",
        root / "uniswap_v4" / "events.py",
        root / "uniswap_v4" / "replay.py",
        root / "rpc" / "collector.py",
    ]
    return sha256_hex(
        canonical_json(
            {str(path.relative_to(root)): sha256_hex(path.read_bytes()) for path in paths}
        )
    )


LOADED_SOURCE_SHA256 = _source_fingerprint()


def _engine_sha() -> str:
    try:
        actual = _source_fingerprint()
    except OSError as exc:
        raise _IntegrityFault("process_source_unavailable") from exc
    if actual != LOADED_SOURCE_SHA256:
        raise _IntegrityFault("loaded_process_source_changed")
    return LOADED_SOURCE_SHA256


class QueueInvalidated(ValueError):
    """The frozen session is latched; its historical results are not current candidates."""


class LeaseRefused(ValueError):
    """An expired or superseded offline delivery cannot commit a result."""


class _IntegrityFault(ValueError):
    pass


class _CanonicalFault(_IntegrityFault):
    pass


@dataclass(frozen=True, slots=True)
class ReplayLease:
    job_id: str
    worker: str
    attempt: int
    expires_ms: int

    def __post_init__(self) -> None:
        _label(self.worker)
        if not isinstance(self.job_id, str) or re.fullmatch(r"[0-9a-f]{64}", self.job_id) is None:
            raise ValueError("invalid replay job ID")
        if type(self.attempt) is not int or not 1 <= self.attempt <= 8:
            raise ValueError("invalid replay attempt")
        if type(self.expires_ms) is not int or not 0 < self.expires_ms < 1 << 63:
            raise ValueError("invalid replay lease expiry")


def _label(value: str) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[a-zA-Z0-9_.-]{1,64}", value) is None:
        raise ValueError("invalid queue label")


def _payload(value: object) -> bytes:
    raw = canonical_json(value)
    if len(raw) > MAX_REPORT_BYTES:
        raise ValueError("queue payload limit")
    return raw


def _tasks(
    session: str, plan: Plan, window: ShadowWindow, report: dict[str, Any]
) -> dict[str, bytes]:
    result = {}
    for dispatch in report["quote_dispatches"]:
        if dispatch["status"] != "ready_for_replay":
            continue
        body = {
            "schema": "oasis-cycle-quote-replay-task-v1",
            "session": session,
            "cycle_config_sha256": window.cycle_config_sha256,
            "window_sha256": sha256_hex(canonical_json(window.model_dump(by_alias=True))),
            "chain_id": plan.chain_id,
            "dispatch": dispatch,
            "sizes_raw": list(plan.sizes_raw),
            "implementation_sha256": _engine_sha(),
            "mode": "offline_historical_quote_replay",
            "execution_eligible": False,
        }
        raw = _payload(body)
        result[sha256_hex(raw)] = raw
    if len(result) > 16:
        raise ValueError("bounded window task limit")
    return result


class CycleShadowQueue(CanonicalChainStore):
    def __init__(self, path: Path, *, clock_ms: Callable[[], int] | None = None) -> None:
        super().__init__(path)
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS cycle_queue_sessions (
                session_id TEXT PRIMARY KEY,
                chain_id INTEGER NOT NULL,
                start_block INTEGER NOT NULL,
                end_block INTEGER NOT NULL,
                config_json BLOB NOT NULL,
                window_json BLOB NOT NULL,
                binding_sha TEXT NOT NULL,
                state TEXT NOT NULL,
                reason TEXT,
                report_json BLOB,
                report_sha TEXT,
                ready_seen INTEGER NOT NULL DEFAULT 0,
                last_clock_ms INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS cycle_queue_jobs (
                job_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES cycle_queue_sessions(session_id),
                task_json BLOB NOT NULL,
                state TEXT NOT NULL,
                worker TEXT,
                attempt INTEGER NOT NULL DEFAULT 0,
                expires_ms INTEGER,
                result_json BLOB,
                result_sha TEXT
            );
            CREATE TABLE IF NOT EXISTS cycle_queue_events (
                seq INTEGER PRIMARY KEY,
                session_id TEXT NOT NULL,
                event TEXT NOT NULL,
                details_json TEXT NOT NULL
            );
            CREATE TRIGGER IF NOT EXISTS cycle_queue_reorg BEFORE DELETE ON canonical_blocks
            BEGIN
                INSERT INTO cycle_queue_events (session_id, event, details_json)
                    SELECT session_id, 'invalidated', '{"reason":"canonical_reorg"}'
                    FROM cycle_queue_sessions WHERE chain_id = OLD.chain_id
                    AND start_block <= OLD.block_number AND end_block >= OLD.block_number
                    AND state != 'invalidated';
                UPDATE cycle_queue_sessions SET state = 'invalidated', reason = 'canonical_reorg'
                    WHERE chain_id = OLD.chain_id AND start_block <= OLD.block_number
                    AND end_block >= OLD.block_number AND state != 'invalidated';
                UPDATE cycle_queue_jobs SET state = 'invalidated'
                    WHERE session_id IN (SELECT session_id FROM cycle_queue_sessions
                    WHERE chain_id = OLD.chain_id AND start_block <= OLD.block_number
                    AND end_block >= OLD.block_number);
            END;
            """
        )
        self._connection.commit()

    def _event(self, session: str, event: str, details: object) -> None:
        self._connection.execute(
            "INSERT INTO cycle_queue_events (session_id, event, details_json) VALUES (?, ?, ?)",
            (session, event, _payload(details).decode()),
        )

    def _row(self, session: str) -> dict[str, Any]:
        _label(session)
        cursor = self._connection.execute(
            "SELECT * FROM cycle_queue_sessions WHERE session_id = ?", (session,)
        )
        row = cursor.fetchone()
        if row is None:
            raise ValueError("unknown queue session")
        assert cursor.description is not None
        return dict(zip((v[0] for v in cursor.description), row, strict=True))

    def _invalidate(self, session: str, reason: str) -> None:
        if self._row(session)["state"] != "invalidated":
            self._event(session, "invalidated", {"reason": reason})
            self._connection.execute(
                "UPDATE cycle_queue_sessions SET state = 'invalidated', reason = ? "
                "WHERE session_id = ?",
                (reason, session),
            )
            self._connection.execute(
                "UPDATE cycle_queue_jobs SET state = 'invalidated' WHERE session_id = ?",
                (session,),
            )

    def fault(self, session: str) -> bool:
        """Latch a known session after normal-entry input/integrity failure; never reset."""
        with self.atomic():
            _label(session)
            if (
                self._connection.execute(
                    "SELECT 1 FROM cycle_queue_sessions WHERE session_id = ?", (session,)
                ).fetchone()
                is None
            ):
                return False
            self._invalidate(session, "input_or_evidence_refused")
            return True

    def _tick(self, session: str) -> int:
        now = self._clock_ms()
        if type(now) is not int or not 0 <= now < 1 << 63:
            raise _IntegrityFault("invalid_processing_clock")
        prior = self._row(session)["last_clock_ms"]
        if type(prior) is not int or prior < 0 or now < prior:
            raise _IntegrityFault("processing_clock_regressed")
        self._connection.execute(
            "UPDATE cycle_queue_sessions SET last_clock_ms = ? WHERE session_id = ?",
            (now, session),
        )
        return now

    def _bound(self, session: str) -> tuple[Plan, ShadowWindow]:
        row = self._row(session)
        if row["state"] == "invalidated":
            raise QueueInvalidated(row["reason"])
        if row["state"] not in {"collecting", "observed"}:
            raise _IntegrityFault("queue_state_corrupted")
        raw, window_raw = row["config_json"], row["window_json"]
        if sha256_hex(raw + b"\n" + window_raw + _engine_sha().encode()) != row["binding_sha"]:
            raise _IntegrityFault("queue_binding_corrupted")
        plan, window = Plan.model_validate_json(raw), ShadowWindow.model_validate_json(window_raw)
        if (plan.chain_id, window.start_block, window.end_block) != (
            row["chain_id"],
            row["start_block"],
            row["end_block"],
        ) or shadow_config_sha256(plan) != window.cycle_config_sha256:
            raise _IntegrityFault("queue_binding_corrupted")
        return plan, window

    def _run(
        self, session: str, action: Callable[[], T], *, initialize: Callable[[], None] | None = None
    ) -> T:
        failure = None
        with self.atomic():
            if initialize is not None:
                initialize()
            try:
                with self.atomic():
                    value = action()
            except _IntegrityFault as exc:
                failure = str(exc)
                targets = [session]
                if isinstance(exc, _CanonicalFault):
                    targets = [
                        r[0]
                        for r in self._connection.execute(
                            "SELECT session_id FROM cycle_queue_sessions WHERE chain_id = ?",
                            (self._row(session)["chain_id"],),
                        ).fetchall()
                    ]
                for target in targets:
                    self._invalidate(target, failure)
        if failure is not None:
            raise QueueInvalidated(failure)
        return value

    def publish(
        self,
        session: str,
        plan: Plan,
        window: ShadowWindow,
        store: EvidenceStore,
        refs: Mapping[str, EvidenceRef],
        *,
        quote_records: tuple[Export, ...] | None = None,
    ) -> dict[str, Any]:
        _label(session)
        raw = _payload(plan.model_dump(by_alias=True, mode="json"))
        window_raw = _payload(window.model_dump(by_alias=True, mode="json"))
        binding = sha256_hex(raw + b"\n" + window_raw + LOADED_SOURCE_SHA256.encode())

        def register() -> None:
            self._connection.execute(
                "INSERT OR IGNORE INTO cycle_queue_sessions "
                "(session_id, chain_id, start_block, end_block, config_json, window_json, "
                "binding_sha, state) VALUES (?, ?, ?, ?, ?, ?, ?, 'collecting')",
                (
                    session,
                    plan.chain_id,
                    window.start_block,
                    window.end_block,
                    raw,
                    window_raw,
                    binding,
                ),
            )

        def apply() -> dict[str, Any]:
            try:
                self._bound(session)
            except QueueInvalidated:
                raise
            except (ValueError, TypeError) as exc:
                raise _IntegrityFault("queue_binding_corrupted") from exc
            if self._row(session)["binding_sha"] != binding:
                raise _IntegrityFault("frozen_queue_binding_changed")
            try:
                report = replay_shadow(plan, window, store, refs, quote_records=quote_records)
                tasks = _tasks(session, plan, window, report)
                old = self._row(session)
                prior_jobs = dict(
                    self._connection.execute(
                        "SELECT job_id, task_json FROM cycle_queue_jobs WHERE session_id = ?",
                        (session,),
                    ).fetchall()
                )
                try:
                    for name, ref in sorted(refs.items()):
                        self.apply(
                            f"{window.cycle_config_sha256}:{name}",
                            replay_block_range_evidence(store, ref),
                            evidence=ref,
                        )
                    self._bound(session)
                except (ValueError, TypeError) as exc:
                    raise _CanonicalFault("canonical_cohort_conflict_or_reorg") from exc
                if old["ready_seen"] and (report["quote_missing_filters"] or prior_jobs != tasks):
                    raise _IntegrityFault("ready_window_regressed_or_changed")
                self._tick(session)
                report_raw = _payload(report)
                ready = not report["quote_missing_filters"]
                if old["report_sha"] != sha256_hex(report_raw):
                    self._event(
                        session,
                        "published",
                        {
                            "report_sha": sha256_hex(report_raw),
                            "jobs": sorted(tasks),
                            "ready": ready,
                            "implementation_sha256": _engine_sha(),
                        },
                    )
                self._connection.execute(
                    "UPDATE cycle_queue_sessions SET state = ?, report_json = ?, report_sha = ?, "
                    "ready_seen = ? WHERE session_id = ?",
                    (
                        "observed" if ready else "collecting",
                        report_raw,
                        sha256_hex(report_raw),
                        int(ready or old["ready_seen"]),
                        session,
                    ),
                )
                for key, payload in tasks.items():
                    self._connection.execute(
                        "INSERT OR IGNORE INTO cycle_queue_jobs "
                        "(job_id, session_id, task_json, state) "
                        "VALUES (?, ?, ?, 'pending')",
                        (key, session, payload),
                    )
                return report
            except QueueInvalidated as exc:
                raise _IntegrityFault(str(exc)) from exc
            except (ValueError, TypeError) as exc:
                if isinstance(exc, _IntegrityFault):
                    raise
                raise _IntegrityFault("input_or_evidence_refused") from exc

        report = self._run(session, apply, initialize=register)
        view = self.inspect(session, store)
        if view["state"] == "invalidated":
            raise QueueInvalidated(view["reason"])
        return {**report, "outbox": view}

    def _check(
        self, session: str, store: EvidenceStore
    ) -> tuple[Plan, ShadowWindow, dict[str, Any]]:
        try:
            plan, window = self._bound(session)
            row = self._row(session)
            raw = row["report_json"]
            if raw is None or sha256_hex(raw) != row["report_sha"]:
                raise _IntegrityFault("queue_report_corrupted_or_missing")
            saved = json.loads(raw)
            refs = {
                key: evidence_ref_from_json(value) for key, value in saved["evidence_refs"].items()
            }
            report = replay_shadow(plan, window, store, refs)
            excluded = {"quote_windows", "quote_status"}
            if canonical_json(
                {k: v for k, v in saved.items() if k not in excluded}
            ) != canonical_json({k: v for k, v in report.items() if k not in excluded}):
                raise _IntegrityFault("queue_report_replay_mismatch")
            for name, ref in refs.items():
                dataset = replay_block_range_evidence(store, ref)
                for block in dataset.blocks:
                    pinned = self._connection.execute(
                        "SELECT block_hash, parent_hash, timestamp FROM canonical_blocks "
                        "WHERE chain_id = ? AND block_number = ?",
                        (plan.chain_id, block.ref.number),
                    ).fetchone()
                    coverage = self._connection.execute(
                        "SELECT content_hash FROM canonical_coverage WHERE chain_id = ? "
                        "AND stream_id = ? AND block_number = ?",
                        (plan.chain_id, f"{window.cycle_config_sha256}:{name}", block.ref.number),
                    ).fetchone()
                    expected = sha256_hex(
                        canonical_json(
                            [
                                asdict(log)
                                for log in dataset.logs
                                if log.block_number == block.ref.number
                            ]
                        )
                    )
                    if pinned != (
                        block.ref.block_hash,
                        block.parent_hash,
                        block.ref.timestamp,
                    ) or coverage != (expected,):
                        raise _IntegrityFault("canonical_coverage_changed_or_missing")
            jobs = dict(
                self._connection.execute(
                    "SELECT job_id, task_json FROM cycle_queue_jobs WHERE session_id = ?",
                    (session,),
                ).fetchall()
            )
            if jobs != _tasks(session, plan, window, report):
                raise _IntegrityFault("queue_tasks_corrupted")
            ready = not report["quote_missing_filters"]
            if row["ready_seen"] != int(ready) or row["state"] != (
                "observed" if ready else "collecting"
            ):
                raise _IntegrityFault("queue_state_corrupted")
            for key, state, worker, attempt, expiry, result, digest in self._connection.execute(
                "SELECT job_id, state, worker, attempt, expires_ms, result_json, result_sha "
                "FROM cycle_queue_jobs WHERE session_id = ?",
                (session,),
            ).fetchall():
                if state == "pending":
                    if (worker, attempt, expiry, result, digest) != (None, 0, None, None, None):
                        raise _IntegrityFault("queue_delivery_state_corrupted")
                elif state in {"leased", "completed"}:
                    ReplayLease(key, worker, attempt, expiry)
                    if state == "completed":
                        if not isinstance(result, bytes) or sha256_hex(result) != digest:
                            raise _IntegrityFault("queue_result_corrupted")
                        value = json.loads(result)
                        if value["job_id"] != key or value["execution_eligible"] is not False:
                            raise _IntegrityFault("queue_result_corrupted")
                    elif result is not None or digest is not None:
                        raise _IntegrityFault("queue_delivery_state_corrupted")
                else:
                    raise _IntegrityFault("queue_delivery_state_corrupted")
            return plan, window, report
        except QueueInvalidated:
            raise
        except (ValueError, TypeError, KeyError) as exc:
            if isinstance(exc, _IntegrityFault):
                raise
            raise _IntegrityFault("retained_evidence_replay_refused") from exc

    def inspect(self, session: str, store: EvidenceStore) -> dict[str, Any]:
        def read() -> dict[str, Any]:
            row = self._row(session)
            if row["state"] != "invalidated":
                self._check(session, store)
            jobs = []
            for key, state, worker, attempt, expiry, result, digest in self._connection.execute(
                "SELECT job_id, state, worker, attempt, expires_ms, result_json, result_sha "
                "FROM cycle_queue_jobs WHERE session_id = ? ORDER BY job_id",
                (session,),
            ).fetchall():
                if result is not None and sha256_hex(result) != digest:
                    raise _IntegrityFault("queue_result_corrupted")
                jobs.append(
                    {
                        "job_id": key,
                        "state": state,
                        "worker": worker,
                        "attempt": attempt,
                        "expires_ms": expiry,
                        "result": json.loads(result)
                        if result is not None
                        and state == "completed"
                        and row["state"] != "invalidated"
                        else None,
                    }
                )
            return {
                "schema": "oasis-cycle-outbox-view-v1",
                "session": session,
                "state": row["state"],
                "reason": row["reason"],
                "jobs": jobs,
                "mode": "offline_historical_quote_replay",
                "execution_eligible": False,
            }

        try:
            return self._run(session, read)
        except QueueInvalidated:
            # Fault has committed. The second read exposes only the latched diagnostic.
            row = self._row(session)
            return {
                "schema": "oasis-cycle-outbox-view-v1",
                "session": session,
                "state": "invalidated",
                "reason": row["reason"],
                "jobs": [],
                "mode": "offline_historical_quote_replay",
                "execution_eligible": False,
            }

    def claim(
        self, session: str, store: EvidenceStore, worker: str, *, lease_ms: int = 30_000
    ) -> ReplayLease | None:
        _label(worker)
        if type(lease_ms) is not int or not 1 <= lease_ms <= 60_000:
            raise ValueError("invalid replay lease duration")

        def take() -> ReplayLease | None:
            self._check(session, store)
            now = self._tick(session)
            row = self._connection.execute(
                "SELECT job_id, attempt FROM cycle_queue_jobs WHERE session_id = ? "
                "AND (state = 'pending' OR (state = 'leased' AND expires_ms <= ?)) "
                "ORDER BY job_id LIMIT 1",
                (session, now),
            ).fetchone()
            if row is None:
                return None
            key, attempt = row
            if attempt >= 8:
                raise _IntegrityFault("replay_delivery_attempt_limit")
            expiry = now + lease_ms
            if expiry >= 1 << 63:
                raise _IntegrityFault("invalid_processing_clock")
            self._event(
                session,
                "claimed",
                {"job_id": key, "worker": worker, "attempt": attempt + 1, "expires_ms": expiry},
            )
            self._connection.execute(
                "UPDATE cycle_queue_jobs SET state = 'leased', worker = ?, "
                "attempt = ?, expires_ms = ? "
                "WHERE job_id = ?",
                (worker, attempt + 1, expiry, key),
            )
            return ReplayLease(key, worker, attempt + 1, expiry)

        return self._run(session, take)

    def definition(self, session: str, store: EvidenceStore) -> tuple[Plan, ShadowWindow]:
        return self._run(session, lambda: self._check(session, store)[:2])

    def complete(
        self, session: str, store: EvidenceStore, lease: ReplayLease, records: tuple[Export, ...]
    ) -> dict[str, Any]:
        """Compute the exact bounded offline replay, then fence its durable completion."""

        def finish() -> dict[str, Any]:
            plan, window, _ = self._check(session, store)
            now = self._tick(session)
            row = self._connection.execute(
                "SELECT task_json, state, worker, attempt, expires_ms, result_json, result_sha "
                "FROM cycle_queue_jobs WHERE session_id = ? AND job_id = ?",
                (session, lease.job_id),
            ).fetchone()
            if row is None or (row[2], row[3], row[4]) != (
                lease.worker,
                lease.attempt,
                lease.expires_ms,
            ):
                raise LeaseRefused("unknown_or_superseded_replay_lease")
            input_sha = sha256_hex(canonical_json([record.manifest() for record in records]))
            if row[1] == "completed":
                if sha256_hex(row[5]) != row[6]:
                    raise _IntegrityFault("queue_result_corrupted")
                prior = json.loads(row[5])
                if prior["quote_inputs_sha256"] != input_sha:
                    raise LeaseRefused("completed_replay_inputs_changed")
                return cast(dict[str, Any], prior)
            if row[1] != "leased" or now >= lease.expires_ms:
                raise LeaseRefused("expired_replay_lease")
            task = json.loads(row[0])
            block = task["dispatch"]["block"]
            snapshot = BlockSnapshot(BlockRef(**block["ref"]), block["parent_hash"])
            scopes = {
                snapshot.ref.number: QuoteScope(
                    snapshot, frozenset(tuple(path) for path in task["dispatch"]["paths"])
                )
            }
            result = {
                "schema": "oasis-cycle-outbox-result-v1",
                "job_id": lease.job_id,
                "implementation_sha256": _engine_sha(),
                "quote_inputs_sha256": input_sha,
                "windows": evaluate_windows(
                    plan, records, window.cycle_config_sha256, scopes=scopes
                ),
                "execution_eligible": False,
            }
            _engine_sha()
            if self._tick(session) >= lease.expires_ms:
                raise LeaseRefused("replay_lease_expired_during_evaluation")
            raw = _payload(result)
            self._event(
                session,
                "completed",
                {"job_id": lease.job_id, "attempt": lease.attempt, "result_sha": sha256_hex(raw)},
            )
            self._connection.execute(
                "UPDATE cycle_queue_jobs SET state = 'completed', result_json = ?, result_sha = ? "
                "WHERE job_id = ?",
                (raw, sha256_hex(raw), lease.job_id),
            )
            return cast(dict[str, Any], json.loads(raw))

        return self._run(session, finish)
