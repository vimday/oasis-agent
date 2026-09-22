"""Durable, account-bound loss latch over independently reconciled USD observations.

This journal verifies ordering and limits, not valuation truth. It grants no
signing authority. Its caller must reconstruct the referenced valuation evidence.
"""

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from ..models.common import BlockRef, canonical_json, normalize_address, sha256_hex
from .canary_instructions import CanaryOwnerInstructions, capital_limit_breaches

_ZERO = "0" * 64
_DAY_ZONE = timezone(timedelta(hours=8))


@dataclass(frozen=True, slots=True)
class CapitalObservation:
    block: BlockRef
    valuation_hash: str
    committed_capital_usd_micros: int
    daily_loss_usd_micros: int
    cumulative_loss_usd_micros: int

    def __post_init__(self) -> None:
        if self.block.timestamp is None:
            raise ValueError("timestamped valuation block required")
        if (
            not isinstance(self.valuation_hash, str)
            or len(self.valuation_hash) != 64
            or any(c not in "0123456789abcdef" for c in self.valuation_hash)
        ):
            raise ValueError("canonical valuation SHA-256 required")
        for value in (
            self.committed_capital_usd_micros,
            self.daily_loss_usd_micros,
            self.cumulative_loss_usd_micros,
        ):
            if type(value) is not int or value < 0:
                raise ValueError("nonnegative integer USD micros required")
        if self.daily_loss_usd_micros > self.cumulative_loss_usd_micros:
            raise ValueError("daily loss exceeds cumulative gross loss")

    @property
    def day(self) -> str:
        assert self.block.timestamp is not None
        return (
            datetime.fromtimestamp(self.block.timestamp, UTC)
            .astimezone(_DAY_ZONE)
            .date()
            .isoformat()
        )


@dataclass(frozen=True, slots=True)
class CapitalStatus:
    observations: int
    halted: bool
    reasons: tuple[str, ...]
    last_valuation_hash: str | None
    # Even a healthy loss budget is never an execution permit.
    execution_eligible: bool = False


class CapitalJournal:
    def __init__(self, path: Path, *, account: str, limits: CanaryOwnerInstructions) -> None:
        self.account = normalize_address(account)
        if int(self.account, 16) == 0 or limits.blockers:
            raise ValueError("nonzero dedicated account and complete owner limits required")
        self.limits = limits
        self._db = sqlite3.connect(path, isolation_level=None, timeout=5)
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS capital_policy "
            "(id INTEGER PRIMARY KEY, payload BLOB NOT NULL)"
        )
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS capital_observations "
            "(seq INTEGER PRIMARY KEY, payload BLOB NOT NULL, digest TEXT NOT NULL)"
        )
        policy = canonical_json(
            {
                "schema": "oasis.capital-latch.v1",
                "account": self.account,
                "limits_digest": limits.digest,
                "day_timezone": "UTC+08:00",
                "loss_semantics": "cumulative gross reconciled losses; gains do not refill budget",
            }
        )
        try:
            self._db.execute("BEGIN IMMEDIATE")
            row = self._db.execute("SELECT payload FROM capital_policy WHERE id=1").fetchone()
            if row is None:
                if self._db.execute("SELECT count(*) FROM capital_observations").fetchone()[0]:
                    raise ValueError("capital policy missing from existing journal")
                self._db.execute("INSERT INTO capital_policy VALUES (1, ?)", (policy,))
            elif bytes(row[0]) != policy:
                raise ValueError("capital account or owner policy changed")
            self._policy = policy
            self._read()
            self._db.commit()
        except BaseException:
            self._db.rollback()
            self._db.close()
            raise

    def _read(self) -> tuple[list[CapitalObservation], tuple[str, ...], str]:
        row = self._db.execute("SELECT payload FROM capital_policy WHERE id=1").fetchone()
        if row is None or bytes(row[0]) != self._policy:
            raise ValueError("capital policy changed")
        observations: list[CapitalObservation] = []
        reasons: set[str] = set()
        previous = _ZERO
        for expected, (seq, raw, digest) in enumerate(
            self._db.execute("SELECT seq, payload, digest FROM capital_observations ORDER BY seq"),
            1,
        ):
            data = json.loads(raw)
            if seq != expected or sha256_hex(bytes(raw)) != digest or data["previous"] != previous:
                raise ValueError("capital journal integrity failure")
            item = data["observation"]
            observation = CapitalObservation(**{**item, "block": BlockRef(**item["block"])})
            if bytes(raw) != canonical_json(
                {"previous": previous, "observation": asdict(observation)}
            ):
                raise ValueError("noncanonical capital journal record")
            self._validate_next(observations, observation)
            observations.append(observation)
            reasons.update(self._breaches(observation))
            previous = digest
        return observations, tuple(sorted(reasons)), previous

    def _breaches(self, observation: CapitalObservation) -> tuple[str, ...]:
        return capital_limit_breaches(
            self.limits,
            committed_capital_usd_micros=observation.committed_capital_usd_micros,
            daily_loss_usd_micros=observation.daily_loss_usd_micros,
            cumulative_loss_usd_micros=observation.cumulative_loss_usd_micros,
        )

    def _validate_next(self, previous: list[CapitalObservation], item: CapitalObservation) -> None:
        if item.block.chain_id != self.limits.chain_id:
            raise ValueError("capital observation chain mismatch")
        if any(old.valuation_hash == item.valuation_hash for old in previous):
            raise ValueError("duplicate valuation hash in capital history")
        if previous:
            last = previous[-1]
            if item.block.number <= last.block.number or item.day < last.day:
                raise ValueError("capital block/day regressed or reorged")
            assert item.block.timestamp is not None and last.block.timestamp is not None
            if item.block.timestamp < last.block.timestamp:
                raise ValueError("capital timestamp regressed")
            delta = item.cumulative_loss_usd_micros - last.cumulative_loss_usd_micros
            day_delta = (
                item.daily_loss_usd_micros - last.daily_loss_usd_micros
                if item.day == last.day
                else item.daily_loss_usd_micros
            )
            if delta < 0 or day_delta < 0 or delta < day_delta:
                raise ValueError("capital loss history regressed or omitted daily losses")

    @staticmethod
    def _status(items: list[CapitalObservation], reasons: tuple[str, ...]) -> CapitalStatus:
        blockers = reasons if items else ("valuation_missing",)
        return CapitalStatus(
            len(items), bool(blockers), blockers, items[-1].valuation_hash if items else None
        )

    def status(self) -> CapitalStatus:
        self._db.execute("BEGIN")
        try:
            items, reasons, _ = self._read()
            return self._status(items, reasons)
        finally:
            self._db.rollback()

    def record(self, observation: CapitalObservation) -> CapitalStatus:
        self._db.execute("BEGIN IMMEDIATE")
        try:
            items, reasons, previous = self._read()
            for old in items:
                if old.valuation_hash == observation.valuation_hash:
                    if old != observation:
                        raise ValueError("valuation hash replay has conflicting values")
                    self._db.commit()
                    return self._status(items, reasons)
            self._validate_next(items, observation)
            raw = canonical_json({"previous": previous, "observation": asdict(observation)})
            self._db.execute(
                "INSERT INTO capital_observations VALUES (?, ?, ?)",
                (len(items) + 1, raw, sha256_hex(raw)),
            )
            reasons = tuple(sorted(set(reasons) | set(self._breaches(observation))))
            self._db.commit()
            return self._status([*items, observation], reasons)
        except BaseException:
            self._db.rollback()
            raise

    def close(self) -> None:
        self._db.close()
