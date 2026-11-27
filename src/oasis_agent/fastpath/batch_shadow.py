"""Reduce normalized whole-batch shadow evidence without granting authority.

Callers verify the raw RPC envelopes and account for all assets before constructing
source observations. This reducer checks their common scope, cash, and clocks. All
amounts use the scope's settlement asset in integer base units. Zero means known
zero, and None preserves an unobserved amount. A shadow policy can explicitly
charge conservative reserves; a real inclusion probability remains unobserved.
"""

from collections import Counter
from collections.abc import Sequence
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

type _Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
type _Hash32 = Annotated[str, Field(pattern=r"^0x[0-9a-f]{64}$")]
type _Provider = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")]
type _Clock = Annotated[int, Field(ge=0, lt=1 << 64)]
type _PositiveClock = Annotated[int, Field(gt=0, lt=1 << 64)]
type _Amount = Annotated[int, Field(ge=0, lt=1 << 256)]
type _SignedAmount = Annotated[int, Field(gt=-(1 << 256), lt=1 << 256)]
type BatchShadowKind = Literal["natural_forward", "fixed_control", "historical_replay", "fixture"]
type _SourceStatus = Literal[
    "succeeded", "reverted", "provider_refused", "transport_error", "invalid_response"
]
type _PhaseStatus = Literal[
    "succeeded",
    "reverted",
    "head_missing",
    "quorum_missing",
    "scope_mismatch",
    "clock_invalid",
    "provider_unavailable",
    "source_disagreement",
]
_SIMULATED = ("succeeded", "reverted")


class _Strict(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")


class BatchShadowScope(_Strict):
    chain_id: _PositiveClock
    plan_sha256: _Digest
    envelope_sha256: _Digest
    settlement_asset: Annotated[str, Field(pattern=r"^0x[0-9a-f]{40}$")]
    decimals: Annotated[int, Field(ge=0, le=255)]
    primary_source: _Provider
    secondary_source: _Provider
    deadline_ns: _PositiveClock
    minimum_net_base_units: Annotated[int, Field(gt=0, lt=1 << 256)]

    @model_validator(mode="after")
    def distinct_sources(self) -> Self:
        if self.primary_source == self.secondary_source:
            raise ValueError("the configured sources must be distinct")
        return self


class BatchShadowHead(_Strict):
    height: _Clock
    block_hash: _Hash32
    parent_hash: _Hash32
    observed_ns: _Clock

    @model_validator(mode="after")
    def valid_header(self) -> Self:
        if self.block_hash == self.parent_hash:
            raise ValueError("the head cannot be its own parent")
        return self


class BatchShadowCash(_Strict):
    """Reconciled cash after route expenses and repayment, before these named costs."""

    settlement_delta: _SignedAmount | None = None
    gas: _Amount | None = None
    l1_data: _Amount | None = None
    operator: _Amount | None = None
    inclusion: _Amount | None = None
    financing: _Amount | None = None
    failure_reserve: _Amount | None = None
    opportunity: _Amount | None = None
    residual: _SignedAmount | None = None

    @property
    def unknown_fields(self) -> tuple[str, ...]:
        return tuple(name for name, value in self.model_dump().items() if value is None)

    def _net(self, costs: tuple[int | None, ...]) -> int | None:
        if self.settlement_delta is None or self.residual != 0 or None in costs:
            return None
        total = sum(cost for cost in costs if cost is not None)
        net = self.settlement_delta - total
        return net if total < 1 << 256 and -(1 << 256) < net < 1 << 256 else None

    @property
    def network_fee_net(self) -> int | None:
        return self._net((self.gas, self.l1_data, self.operator))

    @property
    def policy_net(self) -> int | None:
        return self._net(
            (
                self.gas,
                self.l1_data,
                self.operator,
                self.inclusion,
                self.financing,
                self.failure_reserve,
                self.opportunity,
            )
        )


class BatchShadowSource(_Strict):
    source: _Provider
    chain_id: _PositiveClock
    plan_sha256: _Digest
    envelope_sha256: _Digest
    block_number: _Clock
    block_hash: _Hash32
    started_ns: _Clock
    returned_ns: _Clock
    request_sha256: _Digest
    response_sha256: _Digest | None
    status: _SourceStatus
    cash: BatchShadowCash | None = None
    gas_used: _PositiveClock | None = None

    @model_validator(mode="after")
    def valid_outcome(self) -> Self:
        if self.returned_ns < self.started_ns:
            raise ValueError("source result precedes its request")
        if self.response_sha256 is None and self.status != "transport_error":
            raise ValueError("the source response digest is missing")
        if self.status not in _SIMULATED and (self.cash is not None or self.gas_used is not None):
            raise ValueError("a provider or transport failure cannot claim simulated cash")
        if (
            self.status == "reverted"
            and self.cash is not None
            and self.cash.settlement_delta not in (None, 0)
        ):
            raise ValueError("an EVM revert cannot commit settlement cash")
        return self


class BatchShadowPhase(_Strict):
    head: BatchShadowHead
    results: Annotated[tuple[BatchShadowSource, ...], Field(max_length=2)] = ()
    witness_started_ns: _Clock | None = None


class BatchShadowStage(_Strict):
    status: _PhaseStatus
    head: BatchShadowHead | None = None
    observations: tuple[BatchShadowSource, ...] = ()
    first_return_ns: _Clock | None = None
    quorum_return_ns: _Clock | None = None
    gas_used: _PositiveClock | None = None
    cash: BatchShadowCash | None = None
    network_fee_net: _SignedAmount | None = None
    policy_net: _SignedAmount | None = None
    reused_from: Literal["H", "C"] | None = None


class BatchShadowOutcome(_Strict):
    schema_version: Literal["oasis.whole-batch-shadow-outcome.v1"] = (
        "oasis.whole-batch-shadow-outcome.v1"
    )
    scope: BatchShadowScope
    kind: BatchShadowKind
    discovery: BatchShadowStage
    first_return: BatchShadowStage
    child: BatchShadowStage
    first_return_delay_ns: int | None
    quorum_return_delay_ns: int | None
    quorum_before_direct_child: bool | None
    quorum_within_deadline: bool | None
    full_cost_positive_on_discovery: bool | None
    full_cost_positive_on_first_return: bool | None
    full_cost_positive_on_direct_child: bool | None
    timely_h_k_c_survivor: bool | None
    actual_inclusion_probability: Literal[None] = None
    execution_eligible: Literal[False] = False


def _ref(head: BatchShadowHead) -> tuple[int, str, str]:
    return head.height, head.block_hash, head.parent_hash


def _references(phase: BatchShadowPhase) -> set[str]:
    return {result.request_sha256 for result in phase.results}


def _invalid(phase: BatchShadowPhase, status: _PhaseStatus) -> BatchShadowStage:
    return BatchShadowStage(status=status, head=phase.head, observations=phase.results)


def _stage(scope: BatchShadowScope, phase: BatchShadowPhase) -> BatchShadowStage:
    sources = {result.source for result in phase.results}
    configured = {scope.primary_source, scope.secondary_source}
    if len(sources) != len(phase.results) or not sources <= configured:
        return _invalid(phase, "scope_mismatch")
    for result in phase.results:
        if (
            result.chain_id,
            result.plan_sha256,
            result.envelope_sha256,
            result.block_number,
            result.block_hash,
        ) != (
            scope.chain_id,
            scope.plan_sha256,
            scope.envelope_sha256,
            phase.head.height,
            phase.head.block_hash,
        ):
            return _invalid(phase, "scope_mismatch")
    if any(result.started_ns < phase.head.observed_ns for result in phase.results):
        return _invalid(phase, "clock_invalid")

    valid_returns = [result.returned_ns for result in phase.results if result.status in _SIMULATED]
    first = min(valid_returns, default=None)
    if len(phase.results) != 2 or len(valid_returns) != 2:
        return BatchShadowStage(
            status=(
                "provider_unavailable"
                if len(valid_returns) != len(phase.results)
                else "quorum_missing"
            ),
            head=phase.head,
            observations=phase.results,
            first_return_ns=first,
        )

    left, right = phase.results
    if left.status != right.status or left.cash != right.cash or left.gas_used != right.gas_used:
        return BatchShadowStage(
            status="source_disagreement",
            head=phase.head,
            observations=phase.results,
            first_return_ns=first,
        )
    cash = left.cash
    return BatchShadowStage(
        status="succeeded" if left.status == "succeeded" else "reverted",
        head=phase.head,
        observations=phase.results,
        first_return_ns=first,
        quorum_return_ns=max(valid_returns),
        gas_used=left.gas_used,
        cash=cash,
        network_fee_net=cash.network_fee_net if cash is not None else None,
        policy_net=cash.policy_net if cash is not None else None,
    )


def _child_stage(
    scope: BatchShadowScope,
    discovery: BatchShadowPhase,
    child: BatchShadowPhase | None,
) -> BatchShadowStage:
    if child is None:
        return BatchShadowStage(status="head_missing")
    h, c = discovery.head, child.head
    if (
        c.height != h.height + 1
        or c.parent_hash != h.block_hash
        or c.block_hash == h.block_hash
        or not _references(discovery).isdisjoint(_references(child))
    ):
        return _invalid(child, "scope_mismatch")
    if c.observed_ns <= h.observed_ns:
        return _invalid(child, "clock_invalid")
    return _stage(scope, child)


def _latest_stage(
    scope: BatchShadowScope,
    discovery: BatchShadowPhase,
    first_return: BatchShadowPhase | None,
    child: BatchShadowPhase | None,
    h_stage: BatchShadowStage,
    c_stage: BatchShadowStage,
) -> BatchShadowStage:
    if first_return is None:
        return BatchShadowStage(status="head_missing")
    k, h = first_return.head, discovery.head
    if (
        h_stage.quorum_return_ns is None
        or first_return.witness_started_ns is None
        or not h_stage.quorum_return_ns <= first_return.witness_started_ns <= k.observed_ns
    ):
        return _invalid(first_return, "clock_invalid")
    if (
        k.height < h.height
        or (k.block_hash == h.block_hash) != (k.height == h.height)
        or (k.height == h.height and _ref(k) != _ref(h))
        or (k.height == h.height + 1 and k.parent_hash != h.block_hash)
        or (
            k.height != h.height
            and not _references(first_return).isdisjoint(_references(discovery))
        )
    ):
        return _invalid(first_return, "scope_mismatch")

    if child is not None and c_stage.status not in ("scope_mismatch", "clock_invalid"):
        c = child.head
        if (
            (k.height == c.height and _ref(k) != _ref(c))
            or (k.height != c.height and k.block_hash == c.block_hash)
            or (k.height == c.height + 1 and k.parent_hash != c.block_hash)
            or (
                k.height != c.height
                and not _references(first_return).isdisjoint(_references(child))
            )
        ):
            return _invalid(first_return, "scope_mismatch")
    else:
        c = None

    # A separately witnessed state may reuse an already verified same-envelope result.
    # The stage's head retains the new witness clock and its returns retain the originals.
    reused, label = (h_stage, "H") if k.height == h.height else (c_stage, "C")
    same_state = k.height == h.height or (c is not None and k.height == c.height)
    if same_state and reused.quorum_return_ns is not None:
        if not first_return.results:
            return reused.model_copy(update={"head": k, "reused_from": label})
        k_stage = _stage(scope, first_return)
        if k_stage.quorum_return_ns is not None and (
            k_stage.status,
            k_stage.cash,
            k_stage.gas_used,
        ) != (reused.status, reused.cash, reused.gas_used):
            return _invalid(first_return, "source_disagreement")
        return k_stage
    return _stage(scope, first_return)


def _positive(stage: BatchShadowStage, minimum: int) -> bool | None:
    if stage.status == "reverted":
        return False
    if stage.policy_net is None:
        return None
    return stage.policy_net >= minimum


def reduce_batch_shadow(
    scope: BatchShadowScope,
    *,
    kind: BatchShadowKind,
    discovery: BatchShadowPhase,
    first_return: BatchShadowPhase | None,
    child: BatchShadowPhase | None,
    independent_child_observed_ns: int | None = None,
) -> BatchShadowOutcome:
    """Bind H/K/C and compare timing only to a child observed independently of H."""
    h = _stage(scope, discovery)
    c = _child_stage(scope, discovery, child)
    k = _latest_stage(scope, discovery, first_return, child, h, c)
    start = discovery.head.observed_ns
    first = h.first_return_ns - start if h.first_return_ns is not None else None
    quorum = h.quorum_return_ns - start if h.quorum_return_ns is not None else None
    child_verified = c.head is not None and c.status not in ("scope_mismatch", "clock_invalid")
    before_child = (
        h.quorum_return_ns < independent_child_observed_ns
        if type(independent_child_observed_ns) is int
        and 0 < independent_child_observed_ns < 1 << 64
        and h.quorum_return_ns is not None
        and child_verified
        else None
    )
    within = quorum <= scope.deadline_ns if quorum is not None else None
    h_positive = _positive(h, scope.minimum_net_base_units)
    k_positive = _positive(k, scope.minimum_net_base_units)
    c_positive = _positive(c, scope.minimum_net_base_units)
    checks = (h_positive, k_positive, c_positive, before_child, within)
    timely = False if False in checks else (True if all(checks) else None)
    if kind == "natural_forward" and before_child is None:
        timely = None
    return BatchShadowOutcome(
        scope=scope,
        kind=kind,
        discovery=h,
        first_return=k,
        child=c,
        first_return_delay_ns=first,
        quorum_return_delay_ns=quorum,
        quorum_before_direct_child=before_child,
        quorum_within_deadline=within,
        full_cost_positive_on_discovery=h_positive,
        full_cost_positive_on_first_return=k_positive,
        full_cost_positive_on_direct_child=c_positive,
        timely_h_k_c_survivor=timely,
    )


def summarize_batch_shadow(outcomes: Sequence[BatchShadowOutcome]) -> dict[str, object]:
    """Count one admitted natural batch per discovery height and keep others separate."""
    if (
        len({tuple(row.scope.model_dump(exclude={"envelope_sha256"}).items()) for row in outcomes})
        > 1
    ):
        raise ValueError("shadow summary cannot combine different chain plans")
    kinds = Counter(row.kind for row in outcomes)
    natural = [row for row in outcomes if row.kind == "natural_forward"]
    heads = [row.discovery.head for row in natural]
    unique_height = {head.height for head in heads if head is not None}
    unique_hash = {head.block_hash for head in heads if head is not None}
    if len(unique_height) != len(natural) or len(unique_hash) != len(natural):
        raise ValueError("natural shadow denominators require distinct discovery heads")
    positive = [row for row in natural if row.full_cost_positive_on_discovery is True]
    rated = [row for row in positive if row.full_cost_positive_on_direct_child is not None]
    timely_rated = [row for row in positive if row.timely_h_k_c_survivor is not None]
    return {
        "evidence_kind_counts": {
            kind: kinds[kind]
            for kind in ("natural_forward", "fixed_control", "historical_replay", "fixture")
        },
        "natural_attempts": len(natural),
        "natural_discovery_unresolved": sum(
            row.full_cost_positive_on_discovery is None for row in natural
        ),
        "natural_full_cost_positive_heads": len(positive),
        "natural_positive_direct_child_rated": len(rated),
        "natural_positive_direct_child_still_positive": (
            sum(row.full_cost_positive_on_direct_child is True for row in rated) if rated else None
        ),
        "natural_timely_h_k_c_rated": len(timely_rated),
        "natural_timely_h_k_c_survivors": (
            sum(row.timely_h_k_c_survivor is True for row in timely_rated) if timely_rated else None
        ),
        "execution_eligible": False,
    }
