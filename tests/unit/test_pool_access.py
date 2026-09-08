from dataclasses import replace

import pytest

from oasis_agent.evidence.pool_access import (
    REQUIRED_ACCESS_CHECKS,
    CheckStatus,
    PoolAccessAssessment,
    PoolAccessCheck,
    PoolAccessDecision,
)
from oasis_agent.models.common import BlockRef, EvidenceRef


def ref(source: str = "provider-a", raw: str = "3" * 64) -> EvidenceRef:
    return EvidenceRef(
        "pool-access",
        "1" * 64,
        BlockRef(4663, 100, "0x" + "ab" * 32, 1_000),
        source,
        "2026-09-07T00:00:00+00:00",
        "2" * 64,
        raw,
        "4" * 64,
    )


def checks(**statuses: CheckStatus):
    return tuple(
        PoolAccessCheck(
            name,
            statuses.get(name, CheckStatus.PASS),
            (ref(), ref("provider-b", "5" * 64)),
            "verified" if statuses.get(name, CheckStatus.PASS) is CheckStatus.PASS else "blocked",
        )
        for name in sorted(REQUIRED_ACCESS_CHECKS)
    )


def test_accessible_requires_every_identity_token_lifecycle_and_route_check():
    result = PoolAccessAssessment(checks())
    assert result.decision is PoolAccessDecision.ACCESSIBLE
    assert result.execution_eligible


def test_confirmed_add_denial_is_permissioned_when_owner_can_exit():
    result = PoolAccessAssessment(checks(external_lp_add=CheckStatus.FAIL))
    assert result.decision is PoolAccessDecision.PERMISSIONED
    assert not result.execution_eligible


def test_unknown_or_other_failure_is_unresolved_and_never_eligible():
    result = PoolAccessAssessment(checks(two_sided_routing=CheckStatus.UNKNOWN))
    assert result.decision is PoolAccessDecision.UNRESOLVED
    assert not result.execution_eligible
    assert result.reasons == ("two_sided_routing: blocked",)


def test_missing_checks_and_incomplete_confirmed_evidence_fail_closed():
    with pytest.raises(ValueError, match="canonical"):
        PoolAccessAssessment(checks()[:-1])
    with pytest.raises(ValueError, match="block-pinned"):
        replace(checks()[0], evidence=(replace(ref(), request_hash=""),))
    with pytest.raises(ValueError, match="two distinct"):
        replace(checks()[0], evidence=(ref(),))
