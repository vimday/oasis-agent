"""Durable, hash-linked shadow health journal; faults latch across restarts.

A healthy observation only yields HOLD. Strategy qualification is separate and
cannot be inferred from a short health run or caller-supplied readiness flags.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict
from pathlib import Path

from ..models.chain import BlockSnapshot
from ..models.common import EvidenceRef, canonical_json


class ShadowJournal:
    def __init__(self, path: Path, *, chain_id: int, max_age_seconds: int):
        if (
            type(chain_id) is not int
            or chain_id <= 0
            or type(max_age_seconds) is not int
            or max_age_seconds <= 0
        ):
            raise ValueError("positive chain and age limits required")
        self.chain_id = chain_id
        self.max_age_seconds = max_age_seconds
        self.db = sqlite3.connect(path)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS observations (seq INTEGER PRIMARY KEY, payload BLOB NOT NULL, digest TEXT NOT NULL)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS policy (id INTEGER PRIMARY KEY CHECK(id=1), chain INTEGER NOT NULL, age INTEGER NOT NULL)"
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
        self.db.execute("BEGIN IMMEDIATE")
        try:
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
            self.db.execute(
                "INSERT INTO observations VALUES (?, ?, ?)",
                (result["seq"], payload, hashlib.sha256(payload).hexdigest()),
            )
            self.db.commit()
            return result
        except (ValueError, TypeError, KeyError, sqlite3.Error):
            self.db.rollback()
            raise

    def fault(self, *, now: int) -> dict[str, object]:
        """Record acquisition failure without retaining exception text or secrets."""
        if type(now) is not int or now < 0:
            raise ValueError("integer observation time required")
        self.db.execute("BEGIN IMMEDIATE")
        try:
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
            self.db.execute(
                "INSERT INTO observations VALUES (?, ?, ?)",
                (result["seq"], payload, hashlib.sha256(payload).hexdigest()),
            )
            self.db.commit()
            return result
        except (ValueError, TypeError, KeyError, sqlite3.Error):
            self.db.rollback()
            raise
