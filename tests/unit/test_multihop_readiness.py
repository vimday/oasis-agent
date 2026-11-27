import copy
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from oasis_agent.fastpath.multihop_readiness import (
    CAPTURE_SCHEMA,
    PLAN_SCHEMA,
    MultihopShadowEvidence,
    MultihopShadowPlan,
    assess_multihop_shadow,
)
from oasis_agent.models.common import canonical_json, sha256_hex
from scripts.check_multihop_readiness import main


def _hash(label: str) -> str:
    return sha256_hex(label.encode())


def _record_claim(stage: dict[str, Any], captures: dict[str, bytes]) -> None:
    claim = canonical_json(
        {key: value for key, value in stage.items() if key != "reconciliation_sha256"}
    )
    digest = sha256_hex(claim)
    captures[digest] = claim
    stage["reconciliation_sha256"] = digest


@pytest.fixture
def shadow_study() -> tuple[dict[str, Any], dict[str, Any], dict[str, bytes]]:
    scope = {
        "schema_version": PLAN_SCHEMA,
        "chain_id": 8453,
        "strategy_sha256": _hash("strategy"),
        "cost_policy_sha256": _hash("costs"),
        "frozen_at_utc_ms": 1000,
        "start_utc_ms": 1100,
        "end_utc_ms": 2200,
        "first_height": 10,
        "target_head_count": 1,
        "maximum_age_ms": 1000,
        "deadline_ms": 2,
        "primary_source": "first_provider",
        "secondary_source": "second_provider",
        "settlement_asset": "0x" + "ab" * 20,
        "decimals": 18,
        "minimum_priced_heads": 1,
        "maximum_unresolved_heads": 0,
        "minimum_positive_heads": 1,
        "minimum_timely_survivors": 1,
        "minimum_net_base_units": 15,
    }
    digest = MultihopShadowPlan.model_validate(scope).digest
    captures = {_hash("strategy"): b"strategy", _hash("costs"): b"costs"}

    def capture(label: str) -> str:
        result = _hash(label)
        captures[result] = label.encode()
        return result

    envelope = capture("envelope")

    def stage(height: int, block: str, parent: str, acquired_ns: int, label: str) -> dict[str, Any]:
        traces = {
            key: {
                "source": source,
                "request_sha256": capture(f"{label}:{key}:request"),
                "response_sha256": capture(f"{label}:{key}:response"),
                "cash": {
                    "settlement_delta": 100,
                    "gas": 50,
                    "l1_data": 5,
                    "operator": 5,
                    "inclusion": 5,
                    "financing": 5,
                    "failure_reserve": 10,
                    "opportunity": 5,
                    "residual": 0,
                },
            }
            for key, source in (("primary", "first_provider"), ("secondary", "second_provider"))
        }
        result = {
            "height": height,
            "block_hash": "0x" + _hash(block),
            "parent_hash": "0x" + _hash(parent),
            "scope_sha256": digest,
            "envelope_sha256": envelope,
            "acquired_ns": acquired_ns,
            "returned_ns": acquired_ns + 100,
            **traces,
        }
        _record_claim(result, captures)
        return result

    head = {
        "height": 10,
        "block_hash": "0x" + _hash("head"),
        "parent_hash": "0x" + _hash("parent"),
        "kind": "natural_forward",
        "received_utc_ms": 1500,
        "received_ns": 1000,
        "child_received_ns": 1300,
        "discovery_sha256": capture("discovery"),
        "status": "candidate",
        "candidate": {
            "envelope_sha256": envelope,
            "discovery": stage(10, "head", "parent", 1000, "H"),
            "first_returned_head": stage(10, "head", "parent", 1200, "K"),
            "direct_child": stage(11, "child", "head", 1400, "C"),
        },
    }
    return (
        scope,
        {
            "schema_version": CAPTURE_SCHEMA,
            "plan_sha256": digest,
            "closed_at_utc_ms": 2300,
            "heads": [head],
        },
        captures,
    )


def _assessment(
    inputs: tuple[dict[str, Any], dict[str, Any], dict[str, bytes]],
    *,
    now: int = 2500,
) -> dict[str, object]:
    plan, evidence, raw = inputs
    evidence_model = MultihopShadowEvidence.model_validate(evidence)
    return assess_multihop_shadow(
        MultihopShadowPlan.model_validate(plan),
        evidence_model,
        expected_plan_sha256=evidence_model.plan_sha256,
        now_utc_ms=now,
        contains=raw.__contains__,
    )


def test_scoped_same_head_k_can_meet_review_thresholds_but_never_authorizes(
    shadow_study: tuple[dict[str, Any], dict[str, Any], dict[str, bytes]],
) -> None:
    result = _assessment(shadow_study)
    assert result["shadow_thresholds_met_for_review"] is True
    assert result["release_permitted"] is result["execution_eligible"] is False
    assert result["natural_full_cost_positive_heads"] == result["timely_h_k_c_survivors"] == 1


@pytest.mark.parametrize("kind", ["historical_replay", "fixed_control", "fixture"])
def test_ineligible_sources_cannot_replace_frozen_forward_denominator(
    shadow_study: tuple[dict[str, Any], dict[str, Any], dict[str, bytes]], kind: str
) -> None:
    _, evidence, _ = shadow_study
    evidence["heads"][0]["kind"] = kind
    result = _assessment(shadow_study)
    assert result["natural_targeted_heads"] == result["unresolved_heads"] == 1
    assert result["natural_full_cost_positive_heads"] == 0
    assert result["timely_h_k_c_survivors"] is None
    assert result["shadow_thresholds_met_for_review"] is False


@pytest.mark.parametrize(
    ("phase", "field", "value"),
    [
        ("discovery", "envelope_sha256", _hash("wrong envelope")),
        ("first_returned_head", "acquired_ns", 1099),
        ("direct_child", "parent_hash", "0x" + _hash("unrelated parent")),
        ("direct_child", "height", 12),
    ],
)
def test_same_envelope_clock_and_direct_parent_are_required(
    shadow_study: tuple[dict[str, Any], dict[str, Any], dict[str, bytes]],
    phase: str,
    field: str,
    value: str | int,
) -> None:
    stage = shadow_study[1]["heads"][0]["candidate"][phase]
    stage[field] = value
    _record_claim(stage, shadow_study[2])
    result = _assessment(shadow_study)
    assert result["unresolved_heads"] == 1
    assert result["shadow_thresholds_met_for_review"] is False


def test_cost_disagreement_missing_capture_and_lateness_remain_distinct(
    shadow_study: tuple[dict[str, Any], dict[str, Any], dict[str, bytes]],
) -> None:
    disagreement = copy.deepcopy(shadow_study)
    stage = disagreement[1]["heads"][0]["candidate"]["discovery"]
    stage["secondary"]["cash"]["gas"] += 1
    _record_claim(stage, disagreement[2])
    assert _assessment(disagreement)["natural_full_cost_positive_heads"] == 0

    known_loss = copy.deepcopy(shadow_study)
    cash = known_loss[1]["heads"][0]["candidate"]["direct_child"]
    for provider in ("primary", "secondary"):
        cash[provider]["cash"]["gas"] += 16
    _record_claim(cash, known_loss[2])
    loss = _assessment(known_loss)
    assert loss["priced_heads"] == 1 and loss["timely_h_k_c_survivors"] == 0

    missing = copy.deepcopy(shadow_study)
    missing[2].pop(missing[1]["heads"][0]["candidate"]["direct_child"]["reconciliation_sha256"])
    assert _assessment(missing)["unresolved_heads"] == 1

    late = copy.deepcopy(shadow_study)
    late[1]["heads"][0]["child_received_ns"] = 1100
    late_result = _assessment(late)
    assert late_result["unresolved_heads"] == 0
    assert late_result["natural_full_cost_positive_heads"] == 1
    assert late_result["timely_h_k_c_survivors"] == 0


def test_same_raw_request_cannot_witness_distinct_states(
    shadow_study: tuple[dict[str, Any], dict[str, Any], dict[str, bytes]],
) -> None:
    candidate = shadow_study[1]["heads"][0]["candidate"]
    candidate["direct_child"]["primary"]["request_sha256"] = candidate["discovery"]["primary"][
        "request_sha256"
    ]
    _record_claim(candidate["direct_child"], shadow_study[2])
    result = _assessment(shadow_study)
    assert result["unresolved_heads"] == 1
    assert result["timely_h_k_c_survivors"] == 0


def test_raw_reconciliation_digest_binds_cash_and_timestamps(
    shadow_study: tuple[dict[str, Any], dict[str, Any], dict[str, bytes]],
) -> None:
    stage = shadow_study[1]["heads"][0]["candidate"]["discovery"]
    for source in ("primary", "secondary"):
        stage[source]["cash"]["gas"] -= 10
    stage["returned_ns"] += 1
    result = _assessment(shadow_study)
    assert result["unresolved_heads"] == 1
    assert result["natural_full_cost_positive_heads"] == 0


def test_window_stale_and_duplicate_heads_block_review(
    shadow_study: tuple[dict[str, Any], dict[str, Any], dict[str, bytes]],
) -> None:
    shadow_study[1]["heads"] += copy.deepcopy(shadow_study[1]["heads"])
    result = _assessment(shadow_study, now=3201)
    assert result["blocked_reasons"] == [
        "study_window_stale",
        "invalid_natural_forward_head_identity_or_window",
    ]
    assert result["natural_targeted_heads"] == 1


def test_unknown_cost_and_bool_amount_are_rejected(
    shadow_study: tuple[dict[str, Any], dict[str, Any], dict[str, bytes]],
) -> None:
    evidence = shadow_study[1]
    cash = evidence["heads"][0]["candidate"]["discovery"]["primary"]["cash"]
    for invalid in (None, True, "5"):
        cash["gas"] = invalid
        with pytest.raises(ValidationError, match="valid integer"):
            MultihopShadowEvidence.model_validate(evidence)


def test_cli_checks_retained_content_and_never_overwrites(
    shadow_study: tuple[dict[str, Any], dict[str, Any], dict[str, bytes]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, evidence, captures = shadow_study
    plan_path, evidence_path, output = (tmp_path / name for name in ("plan", "evidence", "result"))
    capture_dir = tmp_path / "raw"
    capture_dir.mkdir()
    plan_path.write_bytes(canonical_json(plan))
    evidence_path.write_bytes(canonical_json(evidence))
    for digest, raw in captures.items():
        (capture_dir / digest).write_bytes(raw)
    monkeypatch.setattr("scripts.check_multihop_readiness.time.time_ns", lambda: 2_500_000_000)
    args = [
        "--plan",
        str(plan_path),
        "--plan-sha256",
        evidence["plan_sha256"],
        "--evidence",
        str(evidence_path),
        "--capture-dir",
        str(capture_dir),
        "--require-review-thresholds",
    ]
    assert main([*args, "--out", str(output)]) == 0
    before = output.read_bytes()
    assert main([*args, "--out", str(output)]) == 2
    assert output.read_bytes() == before

    pinned_reconciliation = evidence["heads"][0]["candidate"]["direct_child"][
        "reconciliation_sha256"
    ]
    (capture_dir / pinned_reconciliation).write_bytes(b"different")
    rejected = tmp_path / "rejected"
    assert main([*args, "--out", str(rejected)]) == 2
    assert json.loads(rejected.read_bytes())["unresolved_heads"] == 1
