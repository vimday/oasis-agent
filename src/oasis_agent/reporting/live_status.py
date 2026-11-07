"""Bounded, redacted projection of local canary state, never execution authority.

The JSON file is an operator-local declaration, not an authenticated journal.
Reference counts do not replay evidence. File mtime never establishes freshness.
"""

import json
import os
import stat
import time
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, ValidationError

from ..models.common import sha256_hex
from .live_bounded import BoundedRunStatus, read_bounded_run
from .live_pair import LivePairStatus, read_pair_status
from .live_release import LiveRelease, live_release
from .live_startup import LiveStartupReadiness, read_local_startup_readiness

UInt = Annotated[int, Field(strict=True, ge=0, lt=2**256)]
Usd = Annotated[FiniteFloat, Field(ge=0)]
Hash = Annotated[str, Field(pattern=r"^0x[0-9a-f]{64}$")]
Action = Literal["approve_usdc", "approve_permit2", "swap", "mint", "burn"]
MAX_STATE_BYTES = 2_000_000
MAX_OBSERVATION_AGE = 120


class LiveObservation(BaseModel):
    """Persisted only after an account observation has reached the loss check."""

    model_config = ConfigDict(extra="forbid", strict=True)
    chain_id: Literal[8453]
    account: Annotated[str, Field(pattern=r"^0x[0-9a-f]{40}$")]
    pool_id: Hash
    config_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    block: UInt
    block_hash: Hash
    block_timestamp: UInt
    observed_at: UInt
    nav_usd: Usd
    daily_loss_usd: Usd
    cumulative_loss_usd: Usd
    capital_limit_usd: Usd
    daily_loss_limit_usd: Usd
    cumulative_loss_limit_usd: Usd
    valuation_basis: Literal["legacy_principal_only", "two_feed_principal_and_fees"] = (
        "legacy_principal_only"
    )
    nav_micros: Annotated[str, Field(pattern=r"^(0|[1-9][0-9]{0,77})$")] | None = None
    uncollected_native_units: Annotated[str, Field(pattern=r"^(0|[1-9][0-9]{0,77})$")] | None = None
    uncollected_usdc_units: Annotated[str, Field(pattern=r"^(0|[1-9][0-9]{0,77})$")] | None = None
    # Recorded from the same retained proof used by execution, never renewed by display time.
    nav_valid_until: UInt | None = None
    eth_feed_max_age_seconds: UInt | None = None
    usdc_feed_max_age_seconds: UInt | None = None


class PendingView(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    kind: Action
    tx_hash: Hash
    nonce: UInt
    signed_at: UInt


class PositionView(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    token_id: UInt
    lower: Annotated[int, Field(ge=-887272, le=887272)]
    upper: Annotated[int, Field(ge=-887272, le=887272)]
    liquidity: UInt


class LightCheckView(BaseModel):
    """Two-provider tick/liquidity hold between full observations; not a NAV or loss check."""

    model_config = ConfigDict(extra="forbid", strict=True)
    block: UInt
    block_timestamp: UInt
    observed_at: UInt
    tick: Annotated[int, Field(ge=-887272, le=887272)]


class _Event(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    event: Annotated[str, Field(max_length=80)]
    tx: Hash | None = None
    native_gas_units: UInt | None = None
    cost_evidence: Annotated[list[dict[str, Any]], Field(max_length=4)] = []
    cash_evidence: Annotated[list[dict[str, Any]], Field(max_length=4)] = []
    position_evidence: Annotated[list[dict[str, Any]], Field(max_length=4)] = []


class _State(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    mode: Literal["live"]
    halted: Annotated[str, Field(max_length=4096)] | None
    pending: PendingView | None
    position: PositionView | None
    gas_spent_wei: UInt
    legacy_gas_cost_incomplete: bool | None = None
    consecutive_reverts: UInt
    actions_today: UInt
    last_observation: LiveObservation | None = None
    signing_attempt: dict[str, Any] | None = None
    last_light_check: LightCheckView | None = None
    history: Annotated[list[_Event], Field(max_length=200)]


class ActionView(BaseModel):
    action: Action | Literal["reverted"]
    transaction_hash: str | None
    native_gas_units: str | None
    cost_references: int
    cash_references: int
    position_references: int


class LiveStatusResponse(BaseModel):
    release: LiveRelease = Field(default_factory=live_release)
    startup: LiveStartupReadiness
    bounded_run: BoundedRunStatus = Field(default_factory=lambda: read_bounded_run(None))
    pair: LivePairStatus = Field(default_factory=lambda: read_pair_status(None))
    schema_: Literal["oasis.live-status.v1"] = Field(default="oasis.live-status.v1", alias="schema")
    execution_eligible: Literal[False] = False
    source: Literal["local_state_declaration"] = "local_state_declaration"
    evidence_replayed: Literal[False] = False
    profitability: Literal["unresolved"] = "unresolved"
    status: Literal[
        "unconfigured",
        "missing",
        "invalid",
        "signing_unknown",
        "halted",
        "pending",
        "stale",
        "observed",
    ]
    blockers: list[str]
    state_hash: str | None = None
    observation_age_seconds: int | None = None
    observation: LiveObservation | None = None
    light_check: LightCheckView | None = None
    pending: PendingView | None = None
    signing_unresolved: bool | None = None
    position: PositionView | None = None
    native_gas_units: str | None = None
    actions_today: int | None = None
    consecutive_reverts: int | None = None
    recent_actions: list[ActionView] = []


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate state key")
        result[key] = value
    return result


def _read(path: Path) -> bytes:
    # A configured symlink or FIFO must not disclose another file or hang the reader.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_STATE_BYTES:
            raise ValueError("bounded regular state file required")
        payload = handle.read(MAX_STATE_BYTES + 1)
        after = os.fstat(handle.fileno())
        if (
            len(payload) > MAX_STATE_BYTES
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
        ):
            raise ValueError("state changed during read")
        return payload


def read_live_status(
    path: Path | None,
    *,
    now: int | None = None,
    pair_inspection: Path | None = None,
    bounded_run: Path | None = None,
) -> LiveStatusResponse:
    """Read one atomic local snapshot; missing/corrupt inputs never become zero NAV."""
    release = live_release()
    startup = read_local_startup_readiness(release=release)
    pair = read_pair_status(
        pair_inspection, now=now, startup_config_hash=startup.configuration_hash
    )
    bounded = read_bounded_run(bounded_run, now=now)
    if path is None:
        return LiveStatusResponse(
            release=release,
            startup=startup,
            pair=pair,
            bounded_run=bounded,
            status="unconfigured",
            blockers=["live_state_unconfigured"],
        )
    try:
        payload = _read(path)
        state = _State.model_validate(json.loads(payload, object_pairs_hook=_unique))
    except FileNotFoundError:
        return LiveStatusResponse(
            release=release,
            startup=startup,
            pair=pair,
            bounded_run=bounded,
            status="missing",
            blockers=["live_state_missing"],
        )
    except (OSError, ValueError, TypeError, RecursionError, ValidationError):
        return LiveStatusResponse(
            release=release,
            startup=startup,
            pair=pair,
            bounded_run=bounded,
            status="invalid",
            blockers=["live_state_invalid"],
        )
    clock = int(time.time()) if now is None else now
    observation = state.last_observation
    blockers = []
    age = None
    if observation is None:
        blockers.append("observation_missing")
    else:
        age = clock - observation.observed_at
        block_age = clock - observation.block_timestamp
        if age < 0 or block_age < 0 or observation.block_timestamp > observation.observed_at:
            blockers.append("observation_clock_invalid")
        elif max(age, block_age) > MAX_OBSERVATION_AGE:
            # A recent in-range tick check supplies no new wallet, USD feed or NAV proof.
            # Keep it visible separately, without renewing the account observation's age.
            blockers.append("observation_stale")
        limits = (observation.eth_feed_max_age_seconds, observation.usdc_feed_max_age_seconds)
        deadline = observation.nav_valid_until
        if (
            observation.valuation_basis != "two_feed_principal_and_fees"
            or deadline is None
            or None in limits
            or not observation.block_timestamp
            <= deadline
            <= observation.block_timestamp + MAX_OBSERVATION_AGE
        ):
            blockers.append("nav_validity_unverified")
        elif any(limit is not None and not 1 <= limit <= 1800 for limit in limits):
            blockers.append("usd_feed_policy_mismatch")
        elif clock > deadline:
            blockers.append("nav_validity_expired")
        if (
            observation.daily_loss_usd >= observation.daily_loss_limit_usd
            or observation.cumulative_loss_usd >= observation.cumulative_loss_limit_usd
        ):
            blockers.append("recorded_loss_budget_exhausted")
    if state.halted:
        blockers.append("state_halted")
    if state.pending:
        blockers.append("pending_requires_reconciliation")
    signing_unresolved = state.signing_attempt is not None
    if signing_unresolved:
        blockers.append("signing_outcome_unresolved")
    if state.legacy_gas_cost_incomplete or (
        state.legacy_gas_cost_incomplete is None and state.gas_spent_wei > 0
    ):
        blockers.append("historical_native_cost_incomplete")
    actions = [
        ActionView(
            action=item.event,  # type: ignore[arg-type]  # narrowed by the explicit allowlist below
            transaction_hash=item.tx,
            native_gas_units=str(item.native_gas_units)
            if item.native_gas_units is not None
            else None,
            cost_references=len(item.cost_evidence),
            cash_references=len(item.cash_evidence),
            position_references=len(item.position_evidence),
        )
        for item in reversed(state.history)
        if item.event in {"approve_usdc", "approve_permit2", "swap", "mint", "burn", "reverted"}
    ][:20]
    status: Literal["signing_unknown", "halted", "pending", "stale", "observed"] = (
        "signing_unknown"
        if signing_unresolved
        else "halted"
        if state.halted
        else "pending"
        if state.pending
        else "stale"
        if blockers
        else "observed"
    )
    return LiveStatusResponse(
        release=release,
        startup=startup,
        pair=pair,
        bounded_run=bounded,
        status=status,
        blockers=blockers,
        state_hash=sha256_hex(payload),
        observation_age_seconds=age,
        observation=observation,
        light_check=state.last_light_check if observation is not None else None,
        pending=state.pending,
        signing_unresolved=signing_unresolved,
        position=state.position,
        native_gas_units=str(state.gas_spent_wei),
        actions_today=state.actions_today,
        consecutive_reverts=state.consecutive_reverts,
        recent_actions=actions,
    )
