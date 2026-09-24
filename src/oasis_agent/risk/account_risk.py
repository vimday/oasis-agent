"""Global reconciled-account risk ledger; inputs require a trusted independent reader.

No RPC, keys or execution. Hashes bind retained inputs, not their economic truth.
"""

import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..models.common import canonical_json, sha256_hex

UInt = Annotated[int, Field(strict=True, ge=0)]
Positive = Annotated[int, Field(strict=True, gt=0)]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Address = Annotated[str, Field(pattern=r"^0x[0-9a-f]{40}$")]
_DAY = timezone(timedelta(hours=8))


class Strict(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")


class AssetBalance(Strict):
    asset_id: Annotated[str, Field(min_length=1, max_length=160)]
    units: UInt
    decimals: Annotated[int, Field(strict=True, ge=0, le=36)]
    price_usd_micros_numerator: Positive
    price_denominator: Positive
    price_timestamp: UInt
    role: Literal["cash", "investment", "debt"]
    evidence_hash: Digest

    @property
    def value(self) -> int:
        numerator = self.units * self.price_usd_micros_numerator
        scale: int = 10**self.decimals
        denominator = self.price_denominator * scale
        return (
            -((numerator + denominator - 1) // denominator)
            if self.role == "debt"
            else numerator // denominator
        )


class AccountFrame(Strict):
    chain_id: Positive
    account: Address
    block_number: UInt
    block_hash: Annotated[str, Field(pattern=r"^0x[0-9a-f]{64}$")]
    timestamp: UInt
    assets: tuple[AssetBalance, ...]
    # Per-asset delta from the preceding frame, reconstructed from complete activity.
    movements: dict[str, int]
    # Non-trading flow in USD micros: attributed transfers (including between monitored
    # accounts) plus a declared native attribution. Gas is never credited here.
    non_trading_flow_usd_micros: int
    inventory_evidence_hash: Digest
    unresolved_operations: UInt

    @model_validator(mode="after")
    def unique_assets(self) -> "AccountFrame":
        if not self.assets or len({a.asset_id for a in self.assets}) != len(self.assets):
            raise ValueError("unique complete asset inventory required")
        if set(self.movements) != {a.asset_id for a in self.assets}:
            raise ValueError("every asset requires a movement reconciliation")
        if int(self.account, 16) == 0:
            raise ValueError("nonzero account required")
        return self

    @property
    def key(self) -> str:
        return f"{self.chain_id}:{self.account}"

    @property
    def nav(self) -> int:
        return sum(a.value for a in self.assets)

    @property
    def day(self) -> str:
        return datetime.fromtimestamp(self.timestamp, UTC).astimezone(_DAY).date().isoformat()


class PortfolioFrame(Strict):
    accounts: tuple[AccountFrame, ...]


class GlobalRiskPolicy(Strict):
    # Account key -> exact asset identifiers. Role/decimals are pinned at inception.
    inventory: dict[str, tuple[str, ...]]
    total_capital_usd_micros: Positive
    daily_loss_usd_micros: Positive
    cumulative_loss_usd_micros: Positive
    max_age_seconds: Positive
    max_skew_seconds: UInt

    @model_validator(mode="after")
    def valid(self) -> "GlobalRiskPolicy":
        if not self.inventory or self.daily_loss_usd_micros > self.cumulative_loss_usd_micros:
            raise ValueError("complete inventory and ordered loss limits required")
        for assets in self.inventory.values():
            if not assets or len(set(assets)) != len(assets):
                raise ValueError("unique policy asset inventory required")
        return self

    @property
    def digest(self) -> str:
        return sha256_hex(canonical_json(self.model_dump(mode="json")))


class Commitment(Strict):
    request_hash: Digest
    account_key: str
    usd_micros: Positive


class RiskState(Strict):
    frame: PortfolioFrame | None = None
    daily_losses: dict[str, int] = Field(default_factory=dict)
    cumulative_loss: UInt = 0
    reservations: tuple[Commitment, ...] = ()
    halted: bool = False


def reconcile(
    policy: GlobalRiskPolicy, old: RiskState, frame: PortfolioFrame, now: int
) -> RiskState:
    """Conservative gross loss: gains in another account never offset a loss."""
    if type(now) is not int or now < 0:
        raise ValueError("integer wall clock required")
    accounts = {a.key: a for a in frame.accounts}
    if len(accounts) != len(frame.accounts) or set(accounts) != set(policy.inventory):
        raise ValueError("complete unique cross-chain account coverage required")
    times = [a.timestamp for a in frame.accounts]
    if max(times) - min(times) > policy.max_skew_seconds:
        raise ValueError("cross-chain snapshot skew")
    prior = {a.key: a for a in old.frame.accounts} if old.frame else {}
    losses = dict(old.daily_losses)
    cumulative = old.cumulative_loss
    for key, account in accounts.items():
        if (
            account.unresolved_operations
            or not 0 <= now - account.timestamp <= policy.max_age_seconds
        ):
            raise ValueError("unresolved or stale account state")
        assets = {a.asset_id: a for a in account.assets}
        if set(assets) != set(policy.inventory[key]):
            raise ValueError("asset inventory mismatch")
        if any(not 0 <= now - a.price_timestamp <= policy.max_age_seconds for a in account.assets):
            raise ValueError("stale or future valuation")
        if key not in prior:
            if any(account.movements.values()) or account.non_trading_flow_usd_micros:
                raise ValueError("inception must be an explicit zero-flow baseline")
            continue
        previous = prior[key]
        if account == previous:
            continue
        if account.block_number <= previous.block_number or account.timestamp <= previous.timestamp:
            raise ValueError("account block or time regressed or reorged")
        for before in previous.assets:
            after = assets[before.asset_id]
            if (before.decimals, before.role) != (after.decimals, after.role):
                raise ValueError("asset units or role changed")
            if before.units + account.movements[before.asset_id] != after.units:
                raise ValueError("unreconciled per-asset residual")
        loss = max(0, previous.nav + account.non_trading_flow_usd_micros - account.nav)
        if account.day != previous.day:
            midnight = datetime.fromtimestamp(account.timestamp, UTC).astimezone(_DAY)
            if (midnight.hour, midnight.minute, midnight.second) != (0, 0, 0):
                raise ValueError("missing day-boundary reconciliation")
            day = previous.day
        else:
            day = account.day
        losses[day] = losses.get(day, 0) + loss
        cumulative += loss
    halted = (
        old.halted
        or cumulative >= policy.cumulative_loss_usd_micros
        or any(loss >= policy.daily_loss_usd_micros for loss in losses.values())
    )
    return RiskState(
        frame=frame,
        daily_losses=losses,
        cumulative_loss=cumulative,
        reservations=old.reservations,
        halted=halted,
    )


class GlobalRiskJournal:
    """Serialized account/loss/reservation updates. No timed or automatic release."""

    def __init__(self, path: Path, policy: GlobalRiskPolicy) -> None:
        self.policy = policy
        self._lock = threading.RLock()
        self.db = sqlite3.connect(path, timeout=5, isolation_level=None, check_same_thread=False)
        self.db.execute("PRAGMA synchronous=FULL")
        try:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS global_policy (id INTEGER PRIMARY KEY, digest TEXT)"
            )
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS global_events "
                "(seq INTEGER PRIMARY KEY, payload BLOB, digest TEXT)"
            )
            row = self.db.execute("SELECT digest FROM global_policy WHERE id=1").fetchone()
            if row is None:
                if self.db.execute("SELECT count(*) FROM global_events").fetchone()[0]:
                    raise ValueError("missing global policy")
                self.db.execute("INSERT INTO global_policy VALUES (1, ?)", (policy.digest,))
            self._read()
            self.db.commit()
        except BaseException:
            self.db.rollback()
            self.db.close()
            raise

    def _read(self) -> tuple[RiskState, int, str]:
        if self.db.execute("SELECT digest FROM global_policy WHERE id=1").fetchone() != (
            self.policy.digest,
        ):
            raise ValueError("global risk policy changed")
        state, seq, previous = RiskState(), 0, "0" * 64
        for number, raw, digest in self.db.execute(
            "SELECT seq,payload,digest FROM global_events ORDER BY seq"
        ):
            import_data = json.loads(raw)
            if (
                number != seq + 1
                or sha256_hex(raw) != digest
                or import_data["previous"] != previous
            ):
                raise ValueError("global risk journal integrity failure")
            state = RiskState.model_validate_json(canonical_json(import_data["state"]))
            seq, previous = number, digest
        return state, seq, previous

    def current_state(self) -> RiskState:
        """Read-only snapshot of the last committed state.

        Adds no write semantics and relaxes no check: it verifies the full
        policy digest and hash chain before returning. Callers may use the
        retained frame as the prior baseline for the next reconciliation, but
        must never treat it as a fresh observation.
        """
        with self._lock:
            return self._read()[0]

    def admit(self, frame: PortfolioFrame, commitment: Commitment, *, now: int) -> str:
        with self._lock:
            return self._admit(frame, commitment, now=now)

    def _admit(self, frame: PortfolioFrame, commitment: Commitment, *, now: int) -> str:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            old, seq, previous = self._read()
            state = reconcile(self.policy, old, frame, now)
            if commitment.account_key not in self.policy.inventory:
                raise ValueError("commitment account outside policy")
            existing = next(
                (r for r in state.reservations if r.request_hash == commitment.request_hash), None
            )
            if existing and existing != commitment:
                raise ValueError("request replay changed commitment")
            deployed = sum(
                max(0, a.value) for f in frame.accounts for a in f.assets if a.role == "investment"
            )
            pending = sum(r.usd_micros for r in state.reservations)
            rejected = (
                state.halted
                or deployed + pending + (0 if existing else commitment.usd_micros)
                > self.policy.total_capital_usd_micros
            )
            if not rejected and existing is None:
                state = state.model_copy(update={"reservations": (*state.reservations, commitment)})
            raw = canonical_json({"previous": previous, "state": state.model_dump(mode="json")})
            digest = sha256_hex(raw)
            self.db.execute("INSERT INTO global_events VALUES (?, ?, ?)", (seq + 1, raw, digest))
            self.db.commit()  # A loss-triggered denial must persist the loss latch.
        except BaseException:
            self.db.rollback()
            raise
        if rejected:
            raise ValueError("global loss latch or capital ceiling blocked admission")
        return digest

    def release(self, request_hash: str, *, reason: str) -> str:
        """Withdraw one admitted commitment that never produced a transaction.

        Idempotent by request hash: an unknown or already released commitment is a
        no-op. Released capital is available again, so a caller that keeps an
        unresolved signing latch must not release on its own initiative. Recorded
        losses and a recorded halt are never weakened.
        """
        if (
            not isinstance(request_hash, str)
            or len(request_hash) != 64
            or any(c not in "0123456789abcdef" for c in request_hash)
        ):
            raise ValueError("canonical request hash required")
        if (
            not isinstance(reason, str)
            or not 1 <= len(reason) <= 96
            or any(not (c.isascii() and (c.isalnum() or c in " _-")) for c in reason)
        ):
            raise ValueError("bounded ascii release reason required")
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                old, seq, previous = self._read()
                remaining = tuple(r for r in old.reservations if r.request_hash != request_hash)
                if len(remaining) == len(old.reservations):
                    self.db.rollback()
                    return previous
                state = old.model_copy(update={"reservations": remaining})
                raw = canonical_json(
                    {
                        "previous": previous,
                        "reason": reason,
                        "state": state.model_dump(mode="json"),
                    }
                )
                digest = sha256_hex(raw)
                self.db.execute(
                    "INSERT INTO global_events VALUES (?, ?, ?)", (seq + 1, raw, digest)
                )
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise
        return digest

    def close(self) -> None:
        self.db.close()
