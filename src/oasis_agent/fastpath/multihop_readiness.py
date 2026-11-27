"""Offline assessment of a frozen, single-chain multihop shadow experiment.

This report is an input to independent release review. It checks local captures
and the operator-pinned plan, not publisher identity or a trusted time service.
It intentionally cannot produce a release permit.
"""

from collections.abc import Callable
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..models.common import canonical_json, sha256_hex

PLAN_SCHEMA = "oasis.multihop-shadow-plan.v1"
CAPTURE_SCHEMA = "oasis.multihop-shadow-evidence.v1"
ASSESSMENT_SCHEMA = "oasis.multihop-shadow-assessment.v1"

type Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
type Hash32 = Annotated[str, Field(pattern=r"^0x[a-f0-9]{64}$")]
type UInt64 = Annotated[int, Field(ge=0, lt=1 << 64)]
type Amount = Annotated[int, Field(ge=0, lt=1 << 256)]
type SignedAmount = Annotated[int, Field(gt=-(1 << 256), lt=1 << 256)]
type ProviderName = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")]
type EvidenceKind = Literal["natural_forward", "fixed_control", "historical_replay", "fixture"]


class _Strict(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")


class MultihopShadowPlan(_Strict):
    schema_version: Literal["oasis.multihop-shadow-plan.v1"]
    chain_id: Annotated[int, Field(gt=0, lt=1 << 64)]
    strategy_sha256: Digest
    cost_policy_sha256: Digest
    frozen_at_utc_ms: UInt64
    start_utc_ms: UInt64
    end_utc_ms: UInt64
    first_height: UInt64
    target_head_count: Annotated[int, Field(gt=0, le=100_000)]
    maximum_age_ms: Annotated[int, Field(gt=0, lt=1 << 64)]
    deadline_ms: Annotated[int, Field(gt=0, lt=1 << 64)]
    primary_source: ProviderName
    secondary_source: ProviderName
    settlement_asset: Annotated[str, Field(pattern=r"^0x[a-f0-9]{40}$")]
    decimals: Annotated[int, Field(ge=0, le=255)]
    minimum_priced_heads: Annotated[int, Field(gt=0, le=100_000)]
    maximum_unresolved_heads: Annotated[int, Field(ge=0, le=100_000)]
    minimum_positive_heads: Annotated[int, Field(gt=0, le=100_000)]
    minimum_timely_survivors: Annotated[int, Field(gt=0, le=100_000)]
    minimum_net_base_units: Annotated[int, Field(gt=0, lt=1 << 256)]

    @model_validator(mode="after")
    def validate_scope(self) -> Self:
        if self.primary_source == self.secondary_source:
            raise ValueError("the two provider IDs must differ")
        if not self.frozen_at_utc_ms < self.start_utc_ms < self.end_utc_ms:
            raise ValueError("freeze must precede the nonempty shadow window")
        if not (
            self.minimum_timely_survivors
            <= self.minimum_positive_heads
            <= self.minimum_priced_heads
            <= self.target_head_count
        ):
            raise ValueError("shadow head thresholds are inconsistent")
        if self.first_height + self.target_head_count >= 1 << 64:
            raise ValueError("shadow head range overflows")
        return self

    @property
    def digest(self) -> str:
        return sha256_hex(canonical_json(self.model_dump(mode="json")))


class _CashResult(_Strict):
    """One settlement unit. Explicit zeroes represent known non-applicable cost."""

    settlement_delta: SignedAmount
    gas: Amount
    l1_data: Amount
    operator: Amount
    inclusion: Amount
    financing: Amount
    failure_reserve: Amount
    opportunity: Amount
    residual: SignedAmount

    @property
    def net(self) -> int:
        return self.settlement_delta - (
            self.gas
            + self.l1_data
            + self.operator
            + self.inclusion
            + self.financing
            + self.failure_reserve
            + self.opportunity
        )


class _Block(_Strict):
    height: UInt64
    block_hash: Hash32
    parent_hash: Hash32


class _Trace(_Strict):
    source: ProviderName
    request_sha256: Digest
    response_sha256: Digest
    cash: _CashResult


class _Stage(_Block):
    scope_sha256: Digest
    envelope_sha256: Digest
    acquired_ns: UInt64
    returned_ns: UInt64
    primary: _Trace
    secondary: _Trace
    reconciliation_sha256: Digest

    @property
    def claim_digest(self) -> str:
        claim = self.model_dump(mode="json", exclude={"reconciliation_sha256"})
        return sha256_hex(canonical_json(claim))

    @property
    def request_digests(self) -> set[str]:
        return {self.primary.request_sha256, self.secondary.request_sha256}


class _Candidate(_Strict):
    envelope_sha256: Digest
    discovery: _Stage | None
    first_returned_head: _Stage | None
    direct_child: _Stage | None


class _Head(_Block):
    kind: EvidenceKind
    received_utc_ms: UInt64
    received_ns: UInt64
    child_received_ns: UInt64 | None
    discovery_sha256: Digest | None
    status: Literal["no_candidate", "candidate", "unresolved"]
    candidate: _Candidate | None

    @model_validator(mode="after")
    def validate_status(self) -> Self:
        if (self.status == "candidate") != (self.candidate is not None):
            raise ValueError("candidate status and candidate payload must agree")
        return self


class MultihopShadowEvidence(_Strict):
    schema_version: Literal["oasis.multihop-shadow-evidence.v1"]
    plan_sha256: Digest
    closed_at_utc_ms: UInt64
    heads: Annotated[list[_Head], Field(max_length=100_000)]


def _verified_net(
    plan: MultihopShadowPlan,
    plan_digest: str,
    stage: _Stage,
    envelope: str,
    contains: Callable[[str], bool],
) -> int | None:
    traces = (stage.primary, stage.secondary)
    if (
        stage.block_hash == stage.parent_hash
        or stage.scope_sha256 != plan_digest
        or stage.envelope_sha256 != envelope
        or stage.returned_ns < stage.acquired_ns
        or (stage.primary.source, stage.secondary.source)
        != (plan.primary_source, plan.secondary_source)
        or stage.primary.cash != stage.secondary.cash
        or stage.primary.cash.residual != 0
        or not -(1 << 256) < stage.primary.cash.net < 1 << 256
        or not contains(envelope)
        or stage.reconciliation_sha256 != stage.claim_digest
        or not contains(stage.reconciliation_sha256)
        or any(not contains(t.request_sha256) or not contains(t.response_sha256) for t in traces)
    ):
        return None
    return stage.primary.cash.net


def _stage_nets(
    plan: MultihopShadowPlan,
    plan_digest: str,
    head: _Head,
    contains: Callable[[str], bool],
) -> tuple[int | None, int | None, int | None]:
    candidate = head.candidate
    if candidate is None:
        return None, None, None
    discovery = candidate.discovery
    returned_head = candidate.first_returned_head
    child = candidate.direct_child
    if (
        discovery is None
        or (discovery.height, discovery.block_hash, discovery.parent_hash)
        != (head.height, head.block_hash, head.parent_hash)
        or discovery.acquired_ns < head.received_ns
    ):
        return None, None, None
    h_net = _verified_net(plan, plan_digest, discovery, candidate.envelope_sha256, contains)
    k_net = None
    if (
        returned_head is not None
        and returned_head.acquired_ns >= discovery.returned_ns
        and returned_head.height >= discovery.height
        and (returned_head.block_hash == discovery.block_hash)
        == (returned_head.height == head.height)
        and (
            (
                returned_head.parent_hash == discovery.parent_hash
                and returned_head.primary.cash == discovery.primary.cash
            )
            if returned_head.height == head.height
            else (
                head.child_received_ns is not None
                and returned_head.acquired_ns >= head.child_received_ns
                and discovery.request_digests.isdisjoint(returned_head.request_digests)
            )
        )
        and (
            child is None
            or returned_head.height <= child.height
            or (
                child.block_hash != returned_head.block_hash
                and child.request_digests.isdisjoint(returned_head.request_digests)
                and (
                    returned_head.height != child.height + 1
                    or returned_head.parent_hash == child.block_hash
                )
            )
        )
        and (
            returned_head.height != head.height + 1
            or (
                returned_head.parent_hash == head.block_hash
                and (
                    child is None
                    or (
                        returned_head.block_hash == child.block_hash
                        and returned_head.primary.cash == child.primary.cash
                    )
                )
            )
        )
    ):
        k_net = _verified_net(plan, plan_digest, returned_head, candidate.envelope_sha256, contains)
    c_net = None
    if (
        child is not None
        and head.child_received_ns is not None
        and head.received_ns < head.child_received_ns <= child.acquired_ns
        and child.height == head.height + 1
        and child.parent_hash == head.block_hash
        and child.request_digests.isdisjoint(discovery.request_digests)
    ):
        c_net = _verified_net(plan, plan_digest, child, candidate.envelope_sha256, contains)
    return h_net, k_net, c_net


def assess_multihop_shadow(
    plan: MultihopShadowPlan | None,
    evidence: MultihopShadowEvidence | None,
    *,
    expected_plan_sha256: str | None,
    now_utc_ms: int,
    contains: Callable[[str], bool],
) -> dict[str, object]:
    """Check scoped evidence without counting controls or unknown costs as positive."""
    blockers: list[str] = []
    counts = dict.fromkeys(("fixture", "fixed_control", "historical_replay", "natural_forward"), 0)
    targeted = priced = unresolved = positive = timely_survivors = 0
    plan_digest = plan.digest if plan is not None else None
    if plan is None or evidence is None:
        blockers.append("forward_shadow_evidence_missing")
    elif plan_digest != expected_plan_sha256 or evidence.plan_sha256 != plan_digest:
        blockers.append("frozen_plan_binding_mismatch")
    else:
        assert plan_digest is not None
        if not contains(plan.strategy_sha256) or not contains(plan.cost_policy_sha256):
            blockers.append("frozen_strategy_or_cost_policy_capture_missing")
        if not plan.end_utc_ms <= evidence.closed_at_utc_ms <= now_utc_ms:
            blockers.append("study_window_incomplete_or_future")
        if now_utc_ms - plan.end_utc_ms > plan.maximum_age_ms:
            blockers.append("study_window_stale")
        seen: set[int] = set()
        seen_hashes: set[str] = set()
        previous: _Head | None = None
        targeted = plan.target_head_count
        for row in evidence.heads:
            counts[row.kind] += 1
            if row.kind != "natural_forward":
                continue
            if (
                not plan.start_utc_ms <= row.received_utc_ms < plan.end_utc_ms
                or not plan.first_height <= row.height < plan.first_height + targeted
                or row.height in seen
                or row.block_hash in seen_hashes
                or row.block_hash == row.parent_hash
                or (
                    previous is not None
                    and (
                        row.height <= previous.height
                        or row.received_ns <= previous.received_ns
                        or (
                            row.height == previous.height + 1
                            and row.parent_hash != previous.block_hash
                        )
                    )
                )
            ):
                if "invalid_natural_forward_head_identity_or_window" not in blockers:
                    blockers.append("invalid_natural_forward_head_identity_or_window")
                continue
            seen.add(row.height)
            seen_hashes.add(row.block_hash)
            previous = row
            if (
                row.status == "unresolved"
                or row.discovery_sha256 is None
                or not contains(row.discovery_sha256)
            ):
                unresolved += 1
                continue
            if row.candidate is None:
                priced += 1
                continue
            h_net, k_net, c_net = _stage_nets(plan, plan_digest, row, contains)
            if h_net is None or k_net is None or c_net is None:
                unresolved += 1
            else:
                priced += 1
            if h_net is None or h_net < plan.minimum_net_base_units:
                continue
            positive += 1
            discovery = row.candidate.discovery
            if (
                discovery is not None
                and row.child_received_ns is not None
                and discovery.returned_ns < row.child_received_ns
                and discovery.returned_ns - row.received_ns <= plan.deadline_ms * 1_000_000
                and k_net is not None
                and k_net >= plan.minimum_net_base_units
                and c_net is not None
                and c_net >= plan.minimum_net_base_units
            ):
                timely_survivors += 1
        unresolved += targeted - len(seen)
        for blocked, reason in (
            (priced < plan.minimum_priced_heads, "priced_head_coverage_below_plan"),
            (unresolved > plan.maximum_unresolved_heads, "unresolved_heads_exceed_plan"),
            (positive < plan.minimum_positive_heads, "natural_full_cost_positive_heads_below_plan"),
            (timely_survivors < plan.minimum_timely_survivors, "timely_h_k_c_survivors_below_plan"),
        ):
            if blocked:
                blockers.append(reason)
    body: dict[str, object] = {
        "schema_version": ASSESSMENT_SCHEMA,
        "chain_id": plan.chain_id if plan is not None else None,
        "expected_plan_sha256": expected_plan_sha256,
        "assessed_at_utc_ms": now_utc_ms,
        "evidence_sha256": (
            sha256_hex(canonical_json(evidence.model_dump(mode="json")))
            if evidence is not None
            else None
        ),
        "evidence_kind_counts": counts,
        "natural_targeted_heads": targeted,
        "priced_heads": priced,
        "unresolved_heads": unresolved,
        "natural_full_cost_positive_heads": positive,
        "timely_h_k_c_survivors": timely_survivors if positive else None,
        "blocked_reasons": blockers,
        "shadow_thresholds_met_for_review": not blockers,
        "verification_scope": "local_artifact_integrity_and_normalized_claim_consistency",
        "external_review_items": [
            "trusted_precommit_time_and_archive_completeness",
            "provider_identity_and_independence",
            "wire_envelope_state_and_raw_cash_replay",
            "canonical_block_and_clock_provenance",
            "out_of_sample_risk_and_release",
        ],
        "independent_review_required": True,
        "release_permitted": False,
        "execution_eligible": False,
    }
    return {**body, "assessment_sha256": sha256_hex(canonical_json(body))}
