"""Fail-closed acceptance of a sustained account-observation window.

An operator loop reconciles the declared account at successive pinned blocks and
retains one accepted run per blocking. This module turns those runs into one
hash-linked window record and decides whether the canary precondition holds.

Nothing here fetches chain state, prices, signs or broadcasts. A window proves
only that one declared account was reconciled repeatedly, without unresolved
operations or a halt, at strictly advancing blocks and times, and under one
manifest and policy. It is a precondition, never an execution permit:
`execution_eligible` stays False everywhere in this repository.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..models.common import canonical_json, sha256_hex
from .account_risk import Digest, GlobalRiskPolicy, Positive, UInt

BLOCK_HASH = r"^0x[0-9a-f]{64}$"


class _Strict(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")


class ShadowRequirements(_Strict):
    """What a window must contain to count as a sustained observation.

    These are operator policy, not chain facts: the defaults below are the
    repository's stated minimum, and an operator may tighten them for a real
    canary. `min_span_seconds` of zero would accept a single burst of runs, so
    it is a deliberate, visible choice rather than an implicit default.
    """

    min_runs: Positive
    min_span_seconds: UInt
    max_gap_seconds: Positive
    max_staleness_seconds: Positive


# Stated operator minimum for a canary precondition. A production canary should
# use a longer window; this is the smallest one the gate will call sustained.
CANARY_WINDOW_REQUIREMENTS = ShadowRequirements(
    min_runs=3,
    min_span_seconds=120,
    max_gap_seconds=300,
    max_staleness_seconds=900,
)


class ObservationRun(_Strict):
    """One accepted reconciliation of the declared account at one pinned block.

    `state_digest` binds the ledger state this run produced, so a window is a
    chain of distinct observations rather than a repeated copy of one, and
    `report_digest` binds the retained report that carried it.
    """

    seq: Positive
    block_number: UInt
    block_hash: Annotated[str, Field(pattern=BLOCK_HASH)]
    timestamp: UInt
    state_digest: Digest
    report_digest: Digest
    cumulative_loss_usd_micros: UInt
    unresolved_operations: UInt
    halted: bool


class ShadowWindow(_Strict):
    """One contiguous, accepted observation window for exactly one account.

    Every summary value is derived from `runs`, so a window cannot claim a span
    or a block range its own runs do not support.
    """

    account_key: Annotated[str, Field(min_length=1, max_length=160)]
    manifest_digest: Digest
    policy_digest: Digest
    requirements: ShadowRequirements
    runs: tuple[ObservationRun, ...]

    @model_validator(mode="after")
    def sustained(self) -> Self:
        if not self.runs:
            raise ValueError("shadow window requires at least one accepted run")
        if len(self.runs) < self.requirements.min_runs:
            raise ValueError("shadow window has too few accepted runs")
        for index, run in enumerate(self.runs):
            if run.seq != index + 1:
                raise ValueError("shadow window run sequence must be contiguous")
            if run.unresolved_operations:
                raise ValueError("shadow window run has unresolved operations")
            if run.halted:
                raise ValueError("shadow window run is halted")
            if not index:
                continue
            previous = self.runs[index - 1]
            if run.block_number <= previous.block_number:
                raise ValueError("shadow window block regressed or repeated")
            if run.timestamp <= previous.timestamp:
                raise ValueError("shadow window time regressed or repeated")
            if run.timestamp - previous.timestamp > self.requirements.max_gap_seconds:
                raise ValueError("shadow window has a gap beyond the requirement")
            if run.state_digest == previous.state_digest:
                raise ValueError("shadow window repeats one ledger state")
        if self.span_seconds < self.requirements.min_span_seconds:
            raise ValueError("shadow window does not span the required duration")
        return self

    @property
    def first_block(self) -> int:
        return self.runs[0].block_number

    @property
    def last_block(self) -> int:
        return self.runs[-1].block_number

    @property
    def last_timestamp(self) -> int:
        return self.runs[-1].timestamp

    @property
    def span_seconds(self) -> int:
        return self.runs[-1].timestamp - self.runs[0].timestamp

    @property
    def cumulative_loss_usd_micros(self) -> int:
        return self.runs[-1].cumulative_loss_usd_micros

    @property
    def payload(self) -> bytes:
        """Self-describing record: the runs plus the summary they imply."""
        return canonical_json(
            {
                "schema": "oasis.account-shadow-window.v1",
                "account_key": self.account_key,
                "manifest_digest": self.manifest_digest,
                "policy_digest": self.policy_digest,
                "requirements": self.requirements.model_dump(mode="json"),
                "first_block": self.first_block,
                "last_block": self.last_block,
                "last_timestamp": self.last_timestamp,
                "span_seconds": self.span_seconds,
                "cumulative_loss_usd_micros": self.cumulative_loss_usd_micros,
                "runs": [run.model_dump(mode="json") for run in self.runs],
                "execution_eligible": False,
            }
        )

    @property
    def digest(self) -> str:
        return sha256_hex(self.payload)


def build_shadow_window(
    runs: tuple[ObservationRun, ...],
    *,
    account_key: str,
    manifest_digest: str,
    policy_digest: str,
    requirements: ShadowRequirements = CANARY_WINDOW_REQUIREMENTS,
) -> ShadowWindow:
    """Bind accepted runs into one window, or refuse an unqualified one.

    The caller supplies only what it observed; every summary and every
    acceptance rule is enforced by `ShadowWindow` itself.
    """
    if not isinstance(runs, tuple) or not runs:
        raise ValueError("shadow window requires a non-empty tuple of accepted runs")
    return ShadowWindow(
        account_key=account_key,
        manifest_digest=manifest_digest,
        policy_digest=policy_digest,
        requirements=requirements,
        runs=runs,
    )


# Exactly the fields `ShadowWindow.payload` writes: a record carrying anything
# else is not the record this module digests.
_WINDOW_FIELDS = frozenset(
    {
        "schema",
        "account_key",
        "manifest_digest",
        "policy_digest",
        "requirements",
        "first_block",
        "last_block",
        "last_timestamp",
        "span_seconds",
        "cumulative_loss_usd_micros",
        "runs",
        "execution_eligible",
    }
)


def load_shadow_window(path: Path) -> ShadowWindow:
    """Rebuild one retained window record, never trusting what the file claims.

    The record must carry exactly the schema's fields, and re-deriving it from its
    own runs must reproduce the canonical bytes it digested. A summary its runs do
    not support (`span_seconds`, the block range, the cumulative loss), an extra or
    missing field, and an attempt to flip `execution_eligible` are all refused. The
    runs themselves are bound by their own `state_digest` and `report_digest`, not
    re-proven here: this loader establishes that a record is internally consistent
    and unedited, not that the observation it records happened.
    """
    data = json.loads(path.read_bytes())
    if not isinstance(data, dict):
        raise ValueError("retained shadow window must be a JSON object")
    if set(data) != _WINDOW_FIELDS:
        raise ValueError("retained shadow window fields do not match its schema")
    window = build_shadow_window(
        tuple(data["runs"]),
        account_key=data["account_key"],
        manifest_digest=data["manifest_digest"],
        policy_digest=data["policy_digest"],
        requirements=ShadowRequirements.model_validate(data["requirements"]),
    )
    if window.payload != canonical_json(data):
        raise ValueError("retained shadow window was altered after it was recorded")
    return window


@dataclass(frozen=True, slots=True)
class CanaryAdmission:
    """The shadow precondition for a canary cycle. It permits no transaction.

    `admitted` means only that a fresh window exists for the current manifest and
    policy and that the declared canary cap fits the owner's capital and loss
    budgets. Capital authorization, signing policy, simulation and the operator
    broadcast gate all remain separate, unbypassed decisions.
    """

    admitted: bool
    reasons: tuple[str, ...]
    window_digest: str
    cap_usd_micros: int

    def __post_init__(self) -> None:
        if type(self.admitted) is not bool:
            raise ValueError("admission outcome must be boolean")
        if type(self.cap_usd_micros) is not int or self.cap_usd_micros <= 0:
            raise ValueError("canary cap must be a positive integer of USD micros")
        if self.admitted and self.reasons:
            raise ValueError("an admitted canary precondition cannot carry reasons")
        if not self.admitted and not self.reasons:
            raise ValueError("a refused canary precondition requires explicit reasons")


def canary_admission(
    window: ShadowWindow,
    *,
    policy: GlobalRiskPolicy,
    manifest_digest: str,
    cap_usd_micros: int,
    now: int,
) -> CanaryAdmission:
    """Decide whether a sustained window admits a canary cycle, with no I/O.

    Fail-closed by default: a window observed under another manifest or policy,
    an account outside the policy inventory, a stale, future, or under-sustained
    window, and a cap larger than the approved total capital, the daily loss
    budget, or the remaining cumulative loss budget are all refusals. The cap is
    charged against the loss budgets in full, because a canary can lose all of it.
    """
    if not isinstance(window, ShadowWindow):
        raise TypeError("typed shadow window required")
    if not isinstance(policy, GlobalRiskPolicy):
        raise TypeError("typed global risk policy required")
    if type(now) is not int or now < 0:
        raise ValueError("integer wall clock required")
    if type(cap_usd_micros) is not int or cap_usd_micros <= 0:
        raise ValueError("canary cap must be a positive integer of USD micros")
    reasons: list[str] = []
    if window.manifest_digest != manifest_digest:
        reasons.append("shadow window was observed under a different manifest")
    if window.policy_digest != policy.digest:
        reasons.append("shadow window was observed under a different policy")
    if window.account_key not in policy.inventory:
        reasons.append("shadow window account is outside the policy inventory")
    if now < window.last_timestamp:
        reasons.append("shadow window is in the future")
    elif now - window.last_timestamp > window.requirements.max_staleness_seconds:
        reasons.append("shadow window is stale")
    if cap_usd_micros > policy.total_capital_usd_micros:
        reasons.append("canary cap exceeds the approved total capital")
    if cap_usd_micros > policy.daily_loss_usd_micros:
        reasons.append("canary cap exceeds the daily loss budget")
    if window.cumulative_loss_usd_micros + cap_usd_micros > policy.cumulative_loss_usd_micros:
        reasons.append("canary cap exceeds the remaining cumulative loss budget")
    return CanaryAdmission(
        admitted=not reasons,
        reasons=tuple(reasons),
        window_digest=window.digest,
        cap_usd_micros=cap_usd_micros,
    )


__all__ = [
    "CANARY_WINDOW_REQUIREMENTS",
    "CanaryAdmission",
    "ObservationRun",
    "ShadowRequirements",
    "ShadowWindow",
    "build_shadow_window",
    "canary_admission",
    "load_shadow_window",
]
