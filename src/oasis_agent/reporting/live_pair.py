"""Read a redacted offline inspection declaration, never open production journals.

The digest detects accidental edits; it is not authentication. The server does
not replay the journals or RPC evidence from which an operator exported it.
"""

import json
import os
import stat
import time
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..models.common import canonical_json, sha256_hex

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Timestamp = Annotated[int, Field(strict=True, ge=0, le=2**53 - 1)]
MAX_BYTES = 32_768
MAX_AGE = 300  # Age of the inspection, never a USD/NAV freshness allowance.
PairStatus = Literal[
    "unconfigured",
    "missing",
    "invalid",
    "stale",
    "configuration_mismatch",
    "projection_incomplete",
    "aligned",
]


class ServiceInspection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    config_hash: Digest
    journal_tip: Digest
    state_hash: Digest
    projection_hash: Digest
    projection_current: bool
    runtime_epoch_present: bool
    halted: bool
    has_pending: bool
    has_signing_attempt: bool
    legacy_gas_cost_incomplete: bool
    position_identity_hash: Digest

    @model_validator(mode="after")
    def coherent_projection(self) -> "ServiceInspection":
        if self.projection_current != (self.state_hash == self.projection_hash):
            raise ValueError("projection status contradicts its hashes")
        return self


class ServicePair(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    strategy: ServiceInspection
    authority: ServiceInspection


class PairInspection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_: Literal["oasis.live-pair-inspection.v1"] = Field(alias="schema")
    inspected_at: Timestamp
    expected_config_hash: Digest
    execution_eligible: Literal[False]
    historical_only: Literal[True]
    rpc_requests: Literal[0]
    services: ServicePair
    evidence_hash: Digest

    @model_validator(mode="before")
    @classmethod
    def exact_flags(cls, value: Any) -> Any:
        if (
            not isinstance(value, dict)
            or value.get("execution_eligible") is not False
            or value.get("historical_only") is not True
            or type(value.get("rpc_requests")) is not int
            or value["rpc_requests"] != 0
        ):
            raise ValueError("exact observation-only flags required")
        return value


class LivePairStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_: Literal["oasis.live-pair-status.v1"] = Field(
        default="oasis.live-pair-status.v1", alias="schema"
    )
    source: Literal["local_pair_inspection_declaration"] = "local_pair_inspection_declaration"
    execution_eligible: Literal[False] = False
    evidence_replayed: Literal[False] = False
    rpc_requests: Literal[0] = 0
    status: PairStatus
    blockers: list[str]
    inspection_age_seconds: int | None = None
    inspection: PairInspection | None = None


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate inspection key")
        result[key] = value
    return result


def read_pair_status(
    path: Path | None, *, now: int | None = None, startup_config_hash: str | None = None
) -> LivePairStatus:
    if path is None:
        return LivePairStatus(status="unconfigured", blockers=["pair_inspection_unconfigured"])
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_BYTES:
                raise ValueError("bounded regular inspection required")
            payload = handle.read(MAX_BYTES + 1)
            after = os.fstat(handle.fileno())
            if len(payload) > MAX_BYTES or any(
                getattr(before, field) != getattr(after, field)
                for field in ("st_size", "st_mtime_ns", "st_ctime_ns")
            ):
                raise ValueError("inspection changed during read")
        raw = json.loads(payload, object_pairs_hook=_unique)
        report = PairInspection.model_validate(raw)
        digest = raw.pop("evidence_hash")
        if sha256_hex(canonical_json(raw)) != digest:
            raise ValueError("inspection hash differs")
    except FileNotFoundError:
        return LivePairStatus(status="missing", blockers=["pair_inspection_missing"])
    except (OSError, ValueError, TypeError, RecursionError):
        return LivePairStatus(status="invalid", blockers=["pair_inspection_invalid"])
    clock = int(time.time()) if now is None else now
    age = clock - report.inspected_at
    if age < 0:
        return LivePairStatus(status="invalid", blockers=["pair_inspection_clock_invalid"])
    blockers = []
    for name in ("strategy", "authority"):
        service = getattr(report.services, name)
        if service.config_hash != report.expected_config_hash:
            blockers.append(f"{name}_configuration_mismatch")
        if not service.projection_current:
            blockers.append(f"{name}_projection_incomplete")
        blockers.extend(
            f"{name}_{flag}"
            for flag in (
                "halted",
                "has_pending",
                "has_signing_attempt",
                "legacy_gas_cost_incomplete",
            )
            if getattr(service, flag)
        )
    a, b = report.services.strategy, report.services.authority
    if a.config_hash != b.config_hash:
        blockers.append("service_configuration_disagreement")
    if a.position_identity_hash != b.position_identity_hash:
        blockers.append("service_position_disagreement")
    if startup_config_hash is None:
        blockers.append("start_configuration_unverified")
    elif report.expected_config_hash != startup_config_hash:
        blockers.append("start_configuration_mismatch")
    status: PairStatus = "aligned"
    if any(
        "configuration_mismatch" in code or "configuration_disagreement" in code
        for code in blockers
    ):
        status = "configuration_mismatch"
    elif not (a.projection_current and b.projection_current):
        status = "projection_incomplete"
    if age > MAX_AGE:
        status = "stale"
        blockers.append("pair_inspection_stale")
    return LivePairStatus(
        status=status,
        blockers=blockers,
        inspection_age_seconds=age,
        inspection=report,
    )
