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

from ..farming.price_evidence import value_usd_micros
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
    def unique_assets(self) -> AccountFrame:
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
    def valid(self) -> GlobalRiskPolicy:
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


class Settlement(Strict):
    """The observed-deployment part of one admitted commitment.

    A reservation records an authorized worst-case spend; this records how much
    of it a reconciled frame has since shown as deployed investment units on the
    commitment's own account, at the block that showed it.
    """

    request_hash: Digest
    account_key: str
    usd_micros: Positive
    block_number: UInt
    block_hash: Annotated[str, Field(pattern=r"^0x[0-9a-f]{64}$")]


class RiskState(Strict):
    frame: PortfolioFrame | None = None
    daily_losses: dict[str, int] = Field(default_factory=dict)
    cumulative_loss: UInt = 0
    reservations: tuple[Commitment, ...] = ()
    settlements: tuple[Settlement, ...] = ()
    halted: bool = False


def _newly_deployed_usd_micros(previous: AccountFrame | None, current: AccountFrame) -> int:
    """Value the investment units an account gained between two frames, floored.

    Only a *unit* increase counts. A rise in the value of units the account
    already held is a price move rather than a deployment, so it must never
    settle a commitment; cash and debt units are never deployment either.
    """
    if previous is None:
        return 0
    before = {asset.asset_id: asset for asset in previous.assets}
    total = 0
    for asset in current.assets:
        if asset.role != "investment":
            continue
        held = before.get(asset.asset_id)
        delta = asset.units - (held.units if held is not None else 0)
        if delta <= 0:
            continue
        total += value_usd_micros(
            delta,
            numerator=asset.price_usd_micros_numerator,
            denominator=asset.price_denominator,
            decimals=asset.decimals,
        )
    return total


def _outstanding_usd_micros(reservation: Commitment, settled: tuple[Settlement, ...]) -> int:
    used = sum(s.usd_micros for s in settled if s.request_hash == reservation.request_hash)
    return max(0, reservation.usd_micros - used)


def _settlements(
    reservations: tuple[Commitment, ...],
    settled: tuple[Settlement, ...],
    previous: PortfolioFrame | None,
    frame: PortfolioFrame,
) -> tuple[Settlement, ...]:
    """Settle commitments whose capital is now observed as deployed investment.

    The ceiling counts deployed investment plus pending commitments, so a
    commitment whose capital has already been spent is charged twice and
    compresses the account's capacity permanently. A commitment settles only
    against investment units its own account gained between two consecutive
    reconciled frames, valued at the new frame's prices and floored, and each
    observed unit settles at most one unit of pending commitments, in
    (account, request hash) order, so no observation is consumed twice. Without a
    previous frame nothing is settled, and an unobserved remainder stays pending.
    """
    if previous is None:
        return ()
    accounts = {account.key: account for account in frame.accounts}
    prior = {account.key: account for account in previous.accounts}
    available = {
        key: _newly_deployed_usd_micros(prior.get(key), account)
        for key, account in accounts.items()
    }
    new: list[Settlement] = []
    for reservation in sorted(reservations, key=lambda r: (r.account_key, r.request_hash)):
        outstanding = _outstanding_usd_micros(reservation, settled)
        account = accounts.get(reservation.account_key)
        if outstanding <= 0 or account is None:
            continue
        budget = available[reservation.account_key]
        amount = min(outstanding, budget)
        if amount <= 0:
            continue
        available[reservation.account_key] = budget - amount
        new.append(
            Settlement(
                request_hash=reservation.request_hash,
                account_key=reservation.account_key,
                usd_micros=amount,
                block_number=account.block_number,
                block_hash=account.block_hash,
            )
        )
    return tuple(new)


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
        settlements=old.settlements,
        halted=halted,
    )


class GlobalRiskJournal:
    """Serialized account/loss/reservation updates. No timed or caller-driven release."""

    def __init__(
        self, path: Path, policy: GlobalRiskPolicy, *, require_existing: bool = False
    ) -> None:
        self.policy = policy
        self._lock = threading.RLock()
        self.db = sqlite3.connect(
            path.resolve().as_uri() + "?mode=rw" if require_existing else path,
            uri=require_existing,
            timeout=5,
            isolation_level=None,
            check_same_thread=False,
        )
        self.db.execute("PRAGMA synchronous=FULL")
        try:
            self.db.execute("BEGIN IMMEDIATE")
            if require_existing:
                # Observation cannot recreate an absent schema or policy and
                # silently establish a new zero-loss history.
                self._read()
                self.db.commit()
                return
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

    def observe(
        self, frame: PortfolioFrame, *, expected: RiskState, now: int, evidence_hash: str
    ) -> tuple[RiskState, str]:
        """Persist independent reconciliation without admitting or releasing capital.

        The trusted reader must derive the frame against ``expected``. Compare
        the entire baseline under the database write lock so a concurrent
        reservation, release or observation cannot be overwritten. An identical
        fresh frame is a no-op; losses and a halt survive retries and restart.
        Observed investment units settle commitments by the same conservative
        rule as admission; callers cannot request a release or settlement.
        """
        if (
            not isinstance(evidence_hash, str)
            or len(evidence_hash) != 64
            or any(c not in "0123456789abcdef" for c in evidence_hash)
        ):
            raise ValueError("canonical observation evidence hash required")
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                old, seq, previous = self._read()
                if old != expected:
                    raise ValueError("risk baseline changed during evidence replay")
                if old.frame is None:
                    raise ValueError("observation requires an established risk baseline")
                state = reconcile(self.policy, old, frame, now)
                settled = _settlements(old.reservations, old.settlements, old.frame, frame)
                if settled:
                    state = state.model_copy(update={"settlements": (*state.settlements, *settled)})
                if state == old:
                    self.db.rollback()
                    return old, previous
                raw = canonical_json(
                    {
                        "previous": previous,
                        "observation_sha256": evidence_hash,
                        "state": state.model_dump(mode="json"),
                    }
                )
                digest = sha256_hex(raw)
                self.db.execute(
                    "INSERT INTO global_events VALUES (?, ?, ?)", (seq + 1, raw, digest)
                )
                self.db.commit()
                return state, digest
            except BaseException:
                self.db.rollback()
                raise

    def _admit(self, frame: PortfolioFrame, commitment: Commitment, *, now: int) -> str:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            old, seq, previous = self._read()
            state = reconcile(self.policy, old, frame, now)
            if commitment.account_key not in self.policy.inventory:
                raise ValueError("commitment account outside policy")
            # Settlement is derived here, never requested by a caller: only proof
            # that the account itself gained investment units can free capital.
            settled = _settlements(old.reservations, old.settlements, old.frame, frame)
            if settled:
                state = state.model_copy(update={"settlements": (*state.settlements, *settled)})
            existing = next(
                (r for r in state.reservations if r.request_hash == commitment.request_hash), None
            )
            if existing and existing != commitment:
                raise ValueError("request replay changed commitment")
            deployed = sum(
                max(0, a.value) for f in frame.accounts for a in f.assets if a.role == "investment"
            )
            pending = sum(_outstanding_usd_micros(r, state.settlements) for r in state.reservations)
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
        losses and a recorded halt are never weakened. A commitment that a
        reconciled frame already showed as deployed investment is refused: its
        settlement is proof that a transaction was produced, so only the
        unexplained remainder stays pending instead of being withdrawn here.
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
                if any(s.request_hash == request_hash for s in old.settlements):
                    raise ValueError("a settled commitment cannot be released as unproduced")
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
