"""Machine-readable, fail-closed Phase-1 LP release assessment."""

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from ..models.common import BlockRef, EvidenceRef, canonical_json, sha256_hex


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    values: dict[str, object] = {}
    for key, value in pairs:
        if key in values:
            raise ValueError(f"duplicate JSON key: {key}")
        values[key] = value
    return values


def _strict_load(payload: bytes) -> dict[str, object]:
    try:
        data = json.loads(payload, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("assessment manifest is not valid JSON") from exc
    if not isinstance(data, dict):
        raise TypeError("assessment manifest must be a JSON object")
    return data


class LpGate(StrEnum):
    WP0_CORRECTNESS = "wp0_correctness"
    WP1_POOL_ACCESS = "wp1_pool_access"
    WP2_REPLAY_ACCOUNTING = "wp2_replay_accounting"
    WP3_RESEARCH = "wp3_research"
    WP4_HOOK_EXECUTOR = "wp4_hook_executor"
    WP5_ADVERSARIAL = "wp5_adversarial"
    WP6_SHADOW_ROUTING = "wp6_shadow_routing"
    WP7_CANARY = "wp7_canary"
    WP8_PRODUCTION = "wp8_production"


class GateStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    UNRESOLVED = "unresolved"


class ReleaseStage(StrEnum):
    REJECT = "reject"
    RESEARCH = "research"
    SHADOW = "shadow"
    CANARY = "canary"
    BOUNDED_PRODUCTION = "bounded_production"


@dataclass(frozen=True, slots=True)
class GateResult:
    gate: LpGate
    status: GateStatus
    evidence: tuple[EvidenceRef, ...]
    findings: tuple[str, ...]
    expires_at: str
    independent_reviewer: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.gate, LpGate) or not isinstance(self.status, GateStatus):
            raise TypeError("typed LP gate and status required")
        try:
            expiry = datetime.fromisoformat(self.expires_at)
        except ValueError as exc:
            raise ValueError("gate expiry must be ISO-8601") from exc
        if expiry.tzinfo is None or expiry.utcoffset() is None:
            raise ValueError("gate expiry must include a timezone")
        if self.status is GateStatus.PASS:
            if not self.evidence or self.findings:
                raise ValueError("a passing gate requires evidence and no open findings")
            for reference in self.evidence:
                if (
                    not reference.request_hash
                    or not reference.raw_response_hash
                    or not reference.interpretation_hash
                ):
                    raise ValueError("passing gate evidence must retain complete provenance")
            sources = {reference.source for reference in self.evidence}
            identities = {
                (reference.content_hash, reference.interpretation_hash)
                for reference in self.evidence
            }
            if len(sources) < 2 or len(identities) != 1:
                raise ValueError(
                    "a passing gate requires agreeing evidence from two distinct providers"
                )
        elif not self.findings:
            raise ValueError("failed or unresolved gate requires explicit findings")
        if (
            self.gate in {LpGate.WP5_ADVERSARIAL, LpGate.WP7_CANARY, LpGate.WP8_PRODUCTION}
            and self.status is GateStatus.PASS
            and not self.independent_reviewer.strip()
        ):
            raise ValueError("capital-critical gate requires an independent reviewer")


@dataclass(frozen=True, slots=True)
class LpReleaseAssessment:
    as_of: str
    gates: tuple[GateResult, ...]
    stage: ReleaseStage = field(init=False)
    blockers: tuple[str, ...] = field(init=False)
    execution_eligible: bool = field(init=False)

    def __post_init__(self) -> None:
        try:
            now = datetime.fromisoformat(self.as_of)
        except ValueError as exc:
            raise ValueError("assessment time must be ISO-8601") from exc
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("assessment time must include a timezone")
        if len(self.gates) != len(LpGate) or {item.gate for item in self.gates} != set(LpGate):
            raise ValueError("assessment requires exactly one result for every LP work package")
        by_gate = {item.gate: item for item in self.gates}
        ordered = tuple(by_gate[gate] for gate in LpGate)
        blockers: list[str] = []
        effective_pass: dict[LpGate, bool] = {}
        for result in ordered:
            expired = datetime.fromisoformat(result.expires_at) <= now
            effective_pass[result.gate] = result.status is GateStatus.PASS and not expired
            if result.status is not GateStatus.PASS:
                blockers.extend(f"{result.gate.value}: {finding}" for finding in result.findings)
            elif expired:
                blockers.append(f"{result.gate.value}: evidence expired")

        consecutive = 0
        for gate in LpGate:
            if not effective_pass[gate]:
                break
            consecutive += 1
        if consecutive == len(LpGate):
            stage = ReleaseStage.BOUNDED_PRODUCTION
        elif consecutive >= 7:
            stage = ReleaseStage.CANARY
        elif consecutive >= 6:
            stage = ReleaseStage.SHADOW
        elif consecutive >= 1:
            stage = ReleaseStage.RESEARCH
        else:
            stage = ReleaseStage.REJECT
        object.__setattr__(self, "stage", stage)
        object.__setattr__(self, "blockers", tuple(blockers))
        object.__setattr__(self, "execution_eligible", stage is ReleaseStage.BOUNDED_PRODUCTION)

    @property
    def payload(self) -> bytes:
        return canonical_json(
            {
                "schema": "oasis.lp-release-assessment.v1",
                "as_of": self.as_of,
                "stage": self.stage.value,
                "execution_eligible": self.execution_eligible,
                "blockers": self.blockers,
                "gates": [asdict(item) for item in self.gates],
            }
        )

    @property
    def digest(self) -> str:
        return sha256_hex(self.payload)


def parse_assessment(payload: bytes, *, as_of: str | None = None) -> LpReleaseAssessment:
    """Strictly parse a machine-readable gate manifest and fail closed.

    The manifest is exactly ``{"gates": [...]}``; any extra or missing key,
    duplicate key, unknown gate/status string, or malformed evidence reference
    raises instead of silently promoting. ``as_of`` must be ISO-8601 with a
    timezone; when omitted the current UTC time is used.
    """
    data = _strict_load(payload)
    if set(data) != {"gates"}:
        raise ValueError("assessment manifest requires exactly the gates key")
    raw_gates = data["gates"]
    if not isinstance(raw_gates, list):
        raise TypeError("assessment manifest gates must be a list")
    gates: list[GateResult] = []
    for raw in raw_gates:
        if not isinstance(raw, dict):
            raise TypeError("each gate result must be an object")
        expected = {"gate", "status", "evidence", "findings", "expires_at", "independent_reviewer"}
        if set(raw) != expected:
            raise ValueError(
                "gate result requires exactly gate, status, evidence, findings, "
                "expires_at, independent_reviewer"
            )
        gate = LpGate(raw["gate"])
        status = GateStatus(raw["status"])
        raw_evidence = raw["evidence"]
        if not isinstance(raw_evidence, list):
            raise TypeError("gate evidence must be a list")
        evidence = []
        for item in raw_evidence:
            if not isinstance(item, dict):
                raise TypeError("evidence reference must be an object")
            block = item.pop("block")
            if not isinstance(block, dict):
                raise TypeError("evidence block must be an object")
            evidence.append(EvidenceRef(**{**item, "block": BlockRef(**block)}))
        findings = raw["findings"]
        if not isinstance(findings, list) or not all(
            isinstance(finding, str) for finding in findings
        ):
            raise TypeError("gate findings must be a list of strings")
        reviewer = raw["independent_reviewer"]
        if not isinstance(reviewer, str):
            raise TypeError("independent reviewer must be a string")
        gates.append(
            GateResult(
                gate,
                status,
                tuple(evidence),
                tuple(findings),
                raw["expires_at"],
                reviewer,
            )
        )
    if as_of is None:
        as_of = datetime.now(UTC).isoformat()
    return LpReleaseAssessment(as_of, tuple(gates))


__all__ = [
    "GateResult",
    "GateStatus",
    "LpGate",
    "LpReleaseAssessment",
    "ReleaseStage",
    "parse_assessment",
]
