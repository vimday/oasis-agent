"""Evidence-backed v4 pool access and routing decision."""

from dataclasses import dataclass, field
from enum import StrEnum

from ..models.common import EvidenceRef


class CheckStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    UNKNOWN = "unknown"


class PoolAccessDecision(StrEnum):
    ACCESSIBLE = "accessible"
    PERMISSIONED = "permissioned"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True, slots=True)
class PoolAccessCheck:
    name: str
    status: CheckStatus
    evidence: tuple[EvidenceRef, ...]
    detail: str

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.detail.strip():
            raise ValueError("pool access checks require a name and detail")
        if not isinstance(self.status, CheckStatus):
            raise TypeError("typed pool access status required")
        if self.status is not CheckStatus.UNKNOWN and not self.evidence:
            raise ValueError("confirmed access checks require retained evidence")
        sources: set[str] = set()
        for reference in self.evidence:
            if (
                reference.block is None
                or not reference.request_hash
                or not reference.raw_response_hash
                or not reference.interpretation_hash
            ):
                raise ValueError("pool access evidence must be complete and block-pinned")
            sources.add(reference.source)
        if self.status is not CheckStatus.UNKNOWN and len(sources) < 2:
            raise ValueError("confirmed access checks require two distinct providers")


REQUIRED_ACCESS_CHECKS = frozenset(
    {
        "deployment_identity",
        "pool_initialized",
        "hook_source_and_permissions",
        "token_behavior_and_authority",
        "external_lp_add",
        "owner_remove",
        "two_sided_routing",
        "router_whitelist",
    }
)


@dataclass(frozen=True, slots=True)
class PoolAccessAssessment:
    checks: tuple[PoolAccessCheck, ...]
    decision: PoolAccessDecision = field(init=False)
    reasons: tuple[str, ...] = field(init=False)
    execution_eligible: bool = field(init=False)

    def __post_init__(self) -> None:
        names = tuple(item.name for item in self.checks)
        if len(names) != len(set(names)) or set(names) != REQUIRED_ACCESS_CHECKS:
            raise ValueError("pool assessment requires exactly the canonical access checks")
        by_name = {item.name: item for item in self.checks}
        add = by_name["external_lp_add"]
        remove = by_name["owner_remove"]
        reasons = tuple(
            f"{item.name}: {item.detail}"
            for item in self.checks
            if item.status is not CheckStatus.PASS
        )
        if add.status is CheckStatus.FAIL and remove.status is CheckStatus.PASS:
            decision = PoolAccessDecision.PERMISSIONED
        elif all(item.status is CheckStatus.PASS for item in self.checks):
            decision = PoolAccessDecision.ACCESSIBLE
        else:
            decision = PoolAccessDecision.UNRESOLVED
        object.__setattr__(self, "decision", decision)
        object.__setattr__(self, "reasons", reasons)
        object.__setattr__(self, "execution_eligible", decision is PoolAccessDecision.ACCESSIBLE)


__all__ = [
    "REQUIRED_ACCESS_CHECKS",
    "CheckStatus",
    "PoolAccessAssessment",
    "PoolAccessCheck",
    "PoolAccessDecision",
]
