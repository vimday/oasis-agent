from dataclasses import asdict, replace

import pytest

from oasis_agent.models.common import BlockRef, EvidenceRef, canonical_json
from oasis_agent.reporting.lp_readiness import (
    GateResult,
    GateStatus,
    LpGate,
    LpReleaseAssessment,
    ReleaseStage,
    parse_assessment,
)


def retained() -> EvidenceRef:
    return EvidenceRef(
        "gate",
        "1" * 64,
        BlockRef(46630, 100, "0x" + "ab" * 32, 1_000),
        "provider-a",
        "2026-09-07T00:00:00+00:00",
        "2" * 64,
        "3" * 64,
        "4" * 64,
    )


def dual() -> tuple[EvidenceRef, ...]:
    """Agreeing evidence from two distinct providers (release-gate standard)."""
    first = retained()
    return first, replace(
        first,
        source="provider-b",
        request_hash="5" * 64,
        raw_response_hash="6" * 64,
    )


def result(gate: LpGate, status: GateStatus = GateStatus.PASS) -> GateResult:
    reviewer = (
        "independent-reviewer"
        if gate
        in {
            LpGate.WP5_ADVERSARIAL,
            LpGate.WP7_CANARY,
            LpGate.WP8_PRODUCTION,
        }
        else ""
    )
    return GateResult(
        gate,
        status,
        dual() if status is GateStatus.PASS else (),
        () if status is GateStatus.PASS else ("missing required evidence",),
        "2027-01-01T00:00:00+00:00",
        reviewer,
    )


def assessment(gates) -> LpReleaseAssessment:
    return LpReleaseAssessment("2026-09-07T00:00:00+00:00", tuple(gates))


def test_all_gates_and_independent_reviews_are_required_for_execution():
    complete = assessment(result(gate) for gate in LpGate)
    assert complete.stage is ReleaseStage.BOUNDED_PRODUCTION
    assert complete.execution_eligible
    assert len(complete.digest) == 64

    blocked = assessment(
        result(
            gate, GateStatus.UNRESOLVED if gate is LpGate.WP6_SHADOW_ROUTING else GateStatus.PASS
        )
        for gate in LpGate
    )
    assert blocked.stage is ReleaseStage.SHADOW
    assert not blocked.execution_eligible
    assert blocked.blockers == ("wp6_shadow_routing: missing required evidence",)


def test_expired_evidence_and_out_of_order_later_passes_cannot_promote():
    gates = [result(gate) for gate in LpGate]
    gates[2] = replace(gates[2], expires_at="2026-09-06T00:00:00+00:00")
    outcome = assessment(gates)
    assert outcome.stage is ReleaseStage.RESEARCH
    assert "wp2_replay_accounting: evidence expired" in outcome.blockers


def test_missing_gate_duplicate_gate_and_self_review_fail_closed():
    gates = [result(gate) for gate in LpGate]
    with pytest.raises(ValueError, match="exactly one"):
        assessment(gates[:-1])
    with pytest.raises(ValueError, match="exactly one"):
        assessment((*gates[:-1], gates[0]))
    with pytest.raises(ValueError, match="independent"):
        replace(result(LpGate.WP5_ADVERSARIAL), independent_reviewer="")


def test_pass_cannot_hide_findings_or_use_incomplete_provenance():
    with pytest.raises(ValueError, match="no open findings"):
        replace(result(LpGate.WP0_CORRECTNESS), findings=("high severity",))
    with pytest.raises(ValueError, match="complete provenance"):
        replace(
            result(LpGate.WP0_CORRECTNESS),
            evidence=(replace(retained(), request_hash=""),),
        )


def test_pass_requires_agreeing_dual_provider_evidence():
    with pytest.raises(ValueError, match="two distinct providers"):
        replace(result(LpGate.WP0_CORRECTNESS), evidence=(retained(),))
    disagreeing = dual()
    disagreeing = (
        disagreeing[0],
        replace(
            disagreeing[1],
            content_hash="7" * 64,
            interpretation_hash="8" * 64,
        ),
    )
    with pytest.raises(ValueError, match="two distinct providers"):
        replace(result(LpGate.WP0_CORRECTNESS), evidence=disagreeing)
    assert result(LpGate.WP0_CORRECTNESS).status is GateStatus.PASS


def _manifest(gates) -> bytes:
    import json

    return json.dumps(
        {
            "gates": [
                {
                    "gate": gate.gate.value,
                    "status": gate.status.value,
                    "evidence": [asdict(item) for item in gate.evidence],
                    "findings": list(gate.findings),
                    "expires_at": gate.expires_at,
                    "independent_reviewer": gate.independent_reviewer,
                }
                for gate in gates
            ]
        }
    ).encode()


def test_parse_assessment_round_trip_and_machine_evaluator_cli(tmp_path):
    import json
    import subprocess
    import sys
    from pathlib import Path

    gates = [result(gate) for gate in LpGate]
    parsed = parse_assessment(_manifest(gates), as_of="2026-09-07T00:00:00+00:00")
    assert parsed.stage is ReleaseStage.BOUNDED_PRODUCTION
    assert parsed.execution_eligible
    assert not parsed.blockers
    assert parsed.digest == assessment(gates).digest

    manifest_path = tmp_path / "manifest.json"
    out_path = tmp_path / "assessment.json"
    manifest_path.write_bytes(_manifest(gates))
    root = Path(__file__).parents[2]
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.run_lp_readiness",
            "--input",
            str(manifest_path),
            "--out",
            str(out_path),
            "--as-of",
            "2026-09-07T00:00:00+00:00",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    stdout = json.loads(completed.stdout)
    assert stdout["stage"] == "bounded_production"
    assert stdout["execution_eligible"] is True
    assert stdout["digest"] == parsed.digest
    assert out_path.read_bytes() == parsed.payload + b"\n"


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        ("duplicate", "duplicate JSON key"),
        ("extra_key", "exactly the gates key"),
        ("missing_gate", "exactly one"),
        ("unknown_gate", "not a valid LpGate"),
        ("unknown_status", "not a valid GateStatus"),
        ("bad_evidence", "evidence block"),
        ("bad_expiry", "timezone"),
    ],
)
def test_parse_assessment_fails_closed(mutate, match):
    import json

    gates = [result(gate) for gate in LpGate]
    raw = json.loads(_manifest(gates))
    if mutate == "duplicate":
        payload = b'{"gates": [], "gates": []}'
    elif mutate == "extra_key":
        raw["extra"] = True
        payload = canonical_json(raw)
    elif mutate == "missing_gate":
        raw["gates"] = raw["gates"][:-1]
        payload = canonical_json(raw)
    elif mutate == "unknown_gate":
        raw["gates"][0]["gate"] = "wp99_unknown"
        payload = canonical_json(raw)
    elif mutate == "unknown_status":
        raw["gates"][0]["status"] = "maybe"
        payload = canonical_json(raw)
    elif mutate == "bad_evidence":
        raw["gates"][0]["evidence"][0]["block"] = "not-an-object"
        payload = canonical_json(raw)
    else:
        raw["gates"][0]["expires_at"] = "2026-01-01"
        payload = canonical_json(raw)
    with pytest.raises((ValueError, TypeError), match=match):
        parse_assessment(payload, as_of="2026-09-07T00:00:00+00:00")


def test_parse_assessment_requires_timezone():
    gates = [result(gate) for gate in LpGate]
    with pytest.raises(ValueError, match="timezone"):
        parse_assessment(_manifest(gates), as_of="2026-09-07T00:00:00")
