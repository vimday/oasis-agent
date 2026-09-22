"""Durable, hash-linked shadow health journal; faults latch across restarts.

A healthy observation only yields HOLD. Strategy qualification is separate and
cannot be inferred from a short health run or caller-supplied readiness flags.

HALT latch and reset
--------------------
A HALT health decision latches for the life of this journal file: every later
observation and decision appends ``halt_latched``/``decision_halt_latched``
reasons and can never become HOLD again in the same file. There is no in-place
reset. The only recovery path is operator journal rotation:

1. Preserve the halted journal file verbatim (append-only, hash-linked audit
   trail; never delete or edit it).
2. Start a fresh journal at a new path with a new observation window. A fresh
   journal has an empty chain, so the latch is absent by construction.
3. Because the consumer pins code/config identity (``lp_policy`` in
   ``LpShadowService``), the fresh window must pass the same reviewed-policy
   fingerprint check before any evaluation restarts.

Rotation is a manual, reviewed operation; no code path clears a latch in place.
"""

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import cast

from ..models.chain import BlockSnapshot
from ..models.common import EvidenceRef, canonical_json


class ShadowJournal:
    def __init__(self, path: Path, *, chain_id: int, max_age_seconds: int, read_only: bool = False):
        if (
            type(chain_id) is not int
            or chain_id <= 0
            or type(max_age_seconds) is not int
            or max_age_seconds <= 0
        ):
            raise ValueError("positive chain and age limits required")
        self.chain_id = chain_id
        self.max_age_seconds = max_age_seconds
        self.db = (
            sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
            if read_only
            else sqlite3.connect(path)
        )
        self._cycle_digest: str | None = None
        if read_only:
            self.db.execute("PRAGMA query_only=ON")
            try:
                if self.db.execute("SELECT chain, age FROM policy").fetchall() != [
                    (chain_id, max_age_seconds)
                ]:
                    raise ValueError("shadow reader policy mismatch")
            except BaseException:
                self.db.close()
                raise
            return
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS observations "
            "(seq INTEGER PRIMARY KEY, payload BLOB NOT NULL, digest TEXT NOT NULL)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS decisions "
            "(seq INTEGER PRIMARY KEY, payload BLOB NOT NULL, digest TEXT NOT NULL)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS policy "
            "(id INTEGER PRIMARY KEY CHECK(id=1), chain INTEGER NOT NULL, age INTEGER NOT NULL)"
        )
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO policy VALUES (1, ?, ?)", (chain_id, max_age_seconds)
            )
            if self.db.execute("SELECT chain, age FROM policy").fetchone() != (
                chain_id,
                max_age_seconds,
            ):
                raise ValueError("shadow policy cannot change on restart")

    def close(self) -> None:
        self.db.close()

    @contextmanager
    def atomic(self, *, cycle_digest: str | None = None, write: bool = True) -> Iterator[None]:
        """Nest operations using savepoints; only the outer cycle commits."""
        nested = self.db.in_transaction
        previous_cycle = self._cycle_digest
        self.db.execute(
            "SAVEPOINT shadow_operation" if nested else "BEGIN IMMEDIATE" if write else "BEGIN"
        )
        if cycle_digest is not None:
            self._cycle_digest = cycle_digest
        try:
            yield
            if nested:
                self.db.execute("RELEASE SAVEPOINT shadow_operation")
            else:
                self.db.commit()
        except BaseException:
            if nested:
                self.db.execute("ROLLBACK TO SAVEPOINT shadow_operation")
                self.db.execute("RELEASE SAVEPOINT shadow_operation")
            else:
                self.db.rollback()
            raise
        finally:
            self._cycle_digest = previous_cycle

    def _last(self) -> dict[str, object] | None:
        previous = "0" * 64
        last = None
        for seq, payload, digest in self.db.execute(
            "SELECT seq, payload, digest FROM observations ORDER BY seq"
        ):
            data = json.loads(payload)
            if (
                data["seq"] != seq
                or data["previous"] != previous
                or hashlib.sha256(payload).hexdigest() != digest
            ):
                raise ValueError("shadow audit chain corrupted")
            previous = digest
            last = data | {"digest": digest}
        return last

    def observe(
        self, header: BlockSnapshot, evidence: EvidenceRef, *, now: int
    ) -> dict[str, object]:
        if type(now) is not int or now < 0:
            raise ValueError("integer observation time required")
        if (
            evidence.block != header.ref
            or not evidence.request_hash
            or not evidence.raw_response_hash
            or not evidence.interpretation_hash
        ):
            raise ValueError("shadow observation needs raw block-pinned evidence")
        with self.atomic():
            last = self._last()
            reasons = []
            block = header.ref
            if block.chain_id != self.chain_id:
                reasons.append("chain_mismatch")
            if block.timestamp is None or not 0 <= now - block.timestamp <= self.max_age_seconds:
                reasons.append("stale_or_future_block")
            if last and last["block"] is not None:
                prior = last["block"]
                if not isinstance(prior, dict):
                    raise ValueError("corrupt prior block")
                if now < int(str(last["observed_at"])):
                    reasons.append("clock_regression")
                if block.number == prior["number"]:
                    if block.block_hash != prior["block_hash"]:
                        reasons.append("reorg")
                elif (
                    block.number != prior["number"] + 1 or header.parent_hash != prior["block_hash"]
                ):
                    reasons.append("lineage_gap_or_reorg")
            if last and last["decision"] == "HALT":
                reasons.append("halt_latched")
            result = {
                "schema": "oasis.shadow-health.v1",
                "seq": 1 if last is None else int(str(last["seq"])) + 1,
                "previous": "0" * 64 if last is None else last["digest"],
                "block": asdict(block),
                "observed_at": now,
                "evidence": asdict(evidence),
                "decision": "HALT" if reasons else "HOLD",
                "reasons": reasons,
                "execution_eligible": False,
                "strategy_qualification": "unresolved",
            }
            payload = canonical_json(result)
            digest = hashlib.sha256(payload).hexdigest()
            self.db.execute(
                "INSERT INTO observations VALUES (?, ?, ?)",
                (result["seq"], payload, digest),
            )
            return result | {"digest": digest}

    def fault(self, *, now: int) -> dict[str, object]:
        """Record acquisition failure without retaining exception text or secrets."""
        if type(now) is not int or now < 0:
            raise ValueError("integer observation time required")
        with self.atomic():
            last = self._last()
            result = {
                "schema": "oasis.shadow-health.v1",
                "seq": 1 if last is None else int(str(last["seq"])) + 1,
                "previous": "0" * 64 if last is None else last["digest"],
                "block": None if last is None else last["block"],
                "observed_at": now,
                "evidence": None,
                "decision": "HALT",
                "reasons": ["acquisition_failure"],
                "execution_eligible": False,
                "strategy_qualification": "unresolved",
            }
            payload = canonical_json(result)
            digest = hashlib.sha256(payload).hexdigest()
            self.db.execute(
                "INSERT INTO observations VALUES (?, ?, ?)",
                (result["seq"], payload, digest),
            )
            return result | {"digest": digest}

    def _last_decision(self) -> dict[str, object] | None:
        records = self.decision_records()
        return records[-1] if records else None

    def _validate_decision(self, data: dict[str, object]) -> None:
        if (
            data.get("schema") != "oasis.shadow-lp-decision.v1"
            or data.get("execution_eligible") is not False
            or data.get("strategy_qualification") != "unresolved"
        ):
            raise ValueError("shadow decision must remain observation-only")
        if data.get("decision") not in {"hold", "halt", "recenter", "remove", "reduce", "unwind"}:
            raise ValueError("unknown shadow decision")
        digest = data.get("health_digest")
        if not isinstance(digest, str):
            raise TypeError("decision needs a health observation digest")
        row = self.db.execute(
            "SELECT payload FROM observations WHERE digest=?", (digest,)
        ).fetchone()
        if row is None:
            raise ValueError("decision health observation missing")
        health = json.loads(row[0])
        if any(data.get(key) != health[key] for key in ("block", "observed_at", "evidence")):
            raise ValueError("decision and health snapshot disagree")
        if data.get("health") != health["decision"]:
            raise ValueError("decision health status mismatch")
        if health["decision"] == "HALT" and data["decision"] != "halt":
            raise ValueError("halted health cannot authorize a decision")

    def decision_records(self) -> tuple[dict[str, object], ...]:
        """Validate both chains and their links; external anchors detect truncation."""
        self._last()
        records = []
        previous = "0" * 64
        for seq, payload, digest in self.db.execute(
            "SELECT seq, payload, digest FROM decisions ORDER BY seq"
        ):
            data = json.loads(payload)
            if (
                data["seq"] != seq
                or data["previous"] != previous
                or hashlib.sha256(payload).hexdigest() != digest
            ):
                raise ValueError("shadow decision chain corrupted")
            self._validate_decision(data)
            previous = digest
            records.append(data | {"digest": digest})
        return tuple(records)

    def record_decision(self, payload: dict[str, object]) -> dict[str, object]:
        """Append one hash-linked LP decision record.

        The journal owns the chain position (``seq``/``previous``); callers
        supply the decision content only. ``previous`` is the digest of the
        prior decision record, so a forged or replayed decision cannot extend
        the audit trail undetected. Health context is linked by the caller
        through ``health_digest`` in the payload.
        """
        if payload.get("schema") != "oasis.shadow-lp-decision.v1":
            raise ValueError("decision payload schema required")
        if "seq" in payload or "previous" in payload:
            raise ValueError("decision chain position is journal-owned")
        with self.atomic():
            last = self._last_decision()
            self._validate_decision(payload)
            if last and last["decision"] == "halt" and payload["decision"] != "halt":
                raise ValueError("shadow decision halt is latched")
            seq = 1 if last is None else int(str(last["seq"])) + 1
            previous = "0" * 64 if last is None else last["digest"]
            record = {"seq": seq, "previous": previous, **payload}
            if self._cycle_digest is not None:
                record["cycle_digest"] = self._cycle_digest
            raw = canonical_json(record)
            digest = hashlib.sha256(raw).hexdigest()
            self.db.execute("INSERT INTO decisions VALUES (?, ?, ?)", (seq, raw, digest))
            return cast(dict[str, object], json.loads(raw)) | {"digest": digest}
