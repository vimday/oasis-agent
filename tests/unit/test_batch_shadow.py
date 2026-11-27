from typing import Literal

import pytest
from pydantic import ValidationError

from oasis_agent.fastpath.batch_shadow import (
    BatchShadowCash,
    BatchShadowHead,
    BatchShadowPhase,
    BatchShadowScope,
    BatchShadowSource,
    reduce_batch_shadow,
    summarize_batch_shadow,
)
from oasis_agent.models.common import sha256_hex


def _hash(label: str) -> str:
    return sha256_hex(label.encode())


SCOPE = BatchShadowScope(
    chain_id=8453,
    plan_sha256=_hash("plan"),
    envelope_sha256=_hash("envelope"),
    settlement_asset="0x" + "12" * 20,
    decimals=18,
    primary_source="primary",
    secondary_source="secondary",
    deadline_ns=200,
    minimum_net_base_units=30,
)
CASH = BatchShadowCash(
    settlement_delta=100,
    gas=10,
    l1_data=3,
    operator=2,
    inclusion=10,
    financing=4,
    failure_reserve=15,
    opportunity=1,
    residual=0,
)


def _head(height: int, name: str, parent: str, observed: int) -> BatchShadowHead:
    return BatchShadowHead(
        height=height,
        block_hash="0x" + _hash(name),
        parent_hash="0x" + _hash(parent),
        observed_ns=observed,
    )


def _result(
    head: BatchShadowHead,
    source: Literal["primary", "secondary"],
    returned: int,
    *,
    status: Literal["succeeded", "reverted", "provider_refused", "transport_error"] = "succeeded",
    cash: BatchShadowCash = CASH,
) -> BatchShadowSource:
    success = status in ("succeeded", "reverted")
    return BatchShadowSource(
        source=source,
        chain_id=SCOPE.chain_id,
        plan_sha256=SCOPE.plan_sha256,
        envelope_sha256=SCOPE.envelope_sha256,
        block_number=head.height,
        block_hash=head.block_hash,
        started_ns=head.observed_ns + 1,
        returned_ns=returned,
        request_sha256=_hash(f"{source}:{head.block_hash}:request"),
        response_sha256=(
            _hash(f"{source}:{head.block_hash}:response") if status != "transport_error" else None
        ),
        status=status,
        cash=cash if success else None,
        gas_used=21_000 if success else None,
    )


H_HEAD = _head(50, "h", "parent", 1_000)
C_HEAD = _head(51, "c", "h", 1_150)
H = BatchShadowPhase(
    head=H_HEAD,
    results=(_result(H_HEAD, "primary", 1_050), _result(H_HEAD, "secondary", 1_100)),
)
C = BatchShadowPhase(
    head=C_HEAD,
    results=(_result(C_HEAD, "primary", 1_220), _result(C_HEAD, "secondary", 1_250)),
)
K_SAME = BatchShadowPhase(
    head=H_HEAD.model_copy(update={"observed_ns": 1_130}), witness_started_ns=1_120
)


def test_hkc_reuses_same_hash_with_new_witness_clock_and_correct_integer_fees() -> None:
    report = reduce_batch_shadow(
        SCOPE,
        kind="natural_forward",
        discovery=H,
        first_return=K_SAME,
        child=C,
        independent_child_observed_ns=C_HEAD.observed_ns,
    )
    assert report.discovery.network_fee_net == 85
    assert report.discovery.policy_net == 55
    assert report.first_return.reused_from == "H"
    assert report.first_return.head is not None and report.first_return.head.observed_ns == 1_130
    assert report.first_return.quorum_return_ns == 1_100
    assert report.first_return_delay_ns == 50
    assert report.quorum_return_delay_ns == 100
    assert report.quorum_before_direct_child and report.timely_h_k_c_survivor
    assert report.execution_eligible is False and report.actual_inclusion_probability is None
    assert summarize_batch_shadow([report])["natural_positive_direct_child_still_positive"] == 1


def test_post_quorum_poll_does_not_qualify_even_with_three_profitable_states() -> None:
    report = reduce_batch_shadow(
        SCOPE, kind="natural_forward", discovery=H, first_return=K_SAME, child=C
    )
    assert report.full_cost_positive_on_discovery is True
    assert report.full_cost_positive_on_first_return is True
    assert report.full_cost_positive_on_direct_child is True
    assert report.quorum_within_deadline is True
    assert report.quorum_before_direct_child is None
    assert report.timely_h_k_c_survivor is None
    summary = summarize_batch_shadow([report])
    assert summary["natural_positive_direct_child_still_positive"] == 1
    assert summary["natural_timely_h_k_c_rated"] == 0
    assert summary["natural_timely_h_k_c_survivors"] is None


def test_earlier_independent_child_overrides_post_quorum_http_observation() -> None:
    report = reduce_batch_shadow(
        SCOPE,
        kind="natural_forward",
        discovery=H,
        first_return=K_SAME,
        child=C,
        independent_child_observed_ns=1_090,
    )
    assert report.child.head is not None and report.child.head.observed_ns == 1_150
    assert report.quorum_return_delay_ns == 100 and report.quorum_within_deadline
    assert report.quorum_before_direct_child is False
    assert report.timely_h_k_c_survivor is False


def test_missing_inclusion_and_opportunity_keep_network_net_only() -> None:
    costs = CASH.model_copy(update={"inclusion": None, "opportunity": None})
    h = BatchShadowPhase(
        head=H_HEAD,
        results=(
            _result(H_HEAD, "primary", 1_050, cash=costs),
            _result(H_HEAD, "secondary", 1_100, cash=costs),
        ),
    )
    report = reduce_batch_shadow(
        SCOPE, kind="natural_forward", discovery=h, first_return=K_SAME, child=C
    )
    assert report.discovery.cash is not None
    assert report.discovery.cash.unknown_fields == ("inclusion", "opportunity")
    assert report.discovery.network_fee_net == 85 and report.discovery.policy_net is None
    assert report.full_cost_positive_on_discovery is None
    assert summarize_batch_shadow([report])["natural_timely_h_k_c_survivors"] is None


@pytest.mark.parametrize("status", ["provider_refused", "transport_error"])
def test_failure_cannot_become_simulation_or_quorum(
    status: Literal["provider_refused", "transport_error"],
) -> None:
    h = BatchShadowPhase(
        head=H_HEAD,
        results=(
            H.results[0],
            _result(H_HEAD, "secondary", 1_100, status=status),
        ),
    )
    report = reduce_batch_shadow(
        SCOPE, kind="natural_forward", discovery=h, first_return=K_SAME, child=C
    )
    assert report.discovery.status == "provider_unavailable"
    assert report.discovery.observations[1].status == status
    assert report.discovery.policy_net is None
    assert report.first_return_delay_ns == 50 and report.quorum_return_delay_ns is None
    assert report.first_return.status == "clock_invalid" and report.timely_h_k_c_survivor is None


def test_evm_revert_charges_simulated_cost_but_cannot_survive() -> None:
    cash = CASH.model_copy(update={"settlement_delta": 0})
    child = BatchShadowPhase(
        head=C_HEAD,
        results=(
            _result(C_HEAD, "primary", 1_220, status="reverted", cash=cash),
            _result(C_HEAD, "secondary", 1_250, status="reverted", cash=cash),
        ),
    )
    report = reduce_batch_shadow(
        SCOPE,
        kind="natural_forward",
        discovery=H,
        first_return=K_SAME,
        child=child,
        independent_child_observed_ns=C_HEAD.observed_ns,
    )
    assert report.child.status == "reverted"
    assert report.child.policy_net == -45
    assert report.full_cost_positive_on_direct_child is False
    assert report.timely_h_k_c_survivor is False
    with pytest.raises(ValidationError, match="cannot commit settlement cash"):
        _result(C_HEAD, "primary", 1_220, status="reverted", cash=CASH)


@pytest.mark.parametrize(
    "change",
    [
        {"source": "primary"},
        {"envelope_sha256": _hash("other envelope")},
        {"block_number": 51},
        {"chain_id": 1},
        {"plan_sha256": _hash("other plan")},
    ],
)
def test_scope_mismatch_never_becomes_positive(change: dict[str, object]) -> None:
    bad = H.results[1].model_copy(update=change)
    phase = BatchShadowPhase(head=H_HEAD, results=(H.results[0], bad))
    report = reduce_batch_shadow(
        SCOPE, kind="natural_forward", discovery=phase, first_return=None, child=C
    )
    assert report.discovery.status == "scope_mismatch"
    assert report.full_cost_positive_on_discovery is None


def test_cash_conflict_and_inconsistent_child_never_become_quorum() -> None:
    other_costs = CASH.model_copy(update={"gas": 9, "financing": 5})
    bad = _result(H_HEAD, "secondary", 1_100, cash=other_costs)
    h = BatchShadowPhase(head=H_HEAD, results=(H.results[0], bad))
    wrong_child = BatchShadowPhase(head=_head(51, "orphan", "other", 1_500))
    report = reduce_batch_shadow(
        SCOPE, kind="natural_forward", discovery=h, first_return=K_SAME, child=wrong_child
    )
    assert report.discovery.status == "source_disagreement"
    assert report.discovery.quorum_return_ns is None
    assert report.child.status == "scope_mismatch"
    assert (
        report.quorum_before_direct_child is None and report.first_return.status == "clock_invalid"
    )


def test_budget_clock_uses_the_later_response_even_if_published_after_child() -> None:
    h = BatchShadowPhase(head=H_HEAD, results=(H.results[0], _result(H_HEAD, "secondary", 1_201)))
    k = K_SAME.model_copy(update={"witness_started_ns": 1_195})
    report = reduce_batch_shadow(
        SCOPE,
        kind="natural_forward",
        discovery=h,
        first_return=k,
        child=C,
        independent_child_observed_ns=C_HEAD.observed_ns,
    )
    assert report.first_return_delay_ns == 50 and report.quorum_return_delay_ns == 201
    assert report.quorum_before_direct_child is False and report.quorum_within_deadline is False
    assert report.first_return.status == "clock_invalid"
    assert report.timely_h_k_c_survivor is False


def test_latest_may_skip_child_but_cannot_reuse_a_different_state_request() -> None:
    later = _head(61, "latest", "latest-parent", 1_230)
    k = BatchShadowPhase(
        head=later,
        results=(_result(later, "primary", 1_250), _result(later, "secondary", 1_300)),
        witness_started_ns=1_200,
    )
    report = reduce_batch_shadow(
        SCOPE,
        kind="natural_forward",
        discovery=H,
        first_return=k,
        child=C,
        independent_child_observed_ns=C_HEAD.observed_ns,
    )
    assert report.first_return.status == "succeeded" and report.timely_h_k_c_survivor

    reused_request = k.results[0].model_copy(update={"request_sha256": C.results[0].request_sha256})
    contradictory_k = k.model_copy(update={"results": (reused_request, k.results[1])})
    report = reduce_batch_shadow(
        SCOPE, kind="natural_forward", discovery=H, first_return=contradictory_k, child=C
    )
    assert report.first_return.status == "scope_mismatch"
    assert report.full_cost_positive_on_first_return is None
    assert report.timely_h_k_c_survivor is None


def test_canonical_matching_latest_child_reuses_c_without_inventing_return_time() -> None:
    k = BatchShadowPhase(
        head=C_HEAD.model_copy(update={"observed_ns": 1_180}), witness_started_ns=1_125
    )
    outcome = reduce_batch_shadow(SCOPE, kind="fixed_control", discovery=H, first_return=k, child=C)
    assert outcome.first_return.reused_from == "C"
    assert outcome.first_return.quorum_return_ns == C.results[1].returned_ns
    assert outcome.first_return.head is not None and outcome.first_return.head.observed_ns == 1_180
    assert summarize_batch_shadow([outcome])["natural_full_cost_positive_heads"] == 0


def test_controls_do_not_count_and_unknown_child_never_becomes_zero_survival() -> None:
    controls = [
        reduce_batch_shadow(SCOPE, kind="fixed_control", discovery=H, first_return=K_SAME, child=C)
    ]
    summary = summarize_batch_shadow(controls)
    assert summary["natural_attempts"] == 0
    assert summary["natural_positive_direct_child_still_positive"] is None
    unresolved = reduce_batch_shadow(
        SCOPE, kind="natural_forward", discovery=H, first_return=K_SAME, child=None
    )
    summary = summarize_batch_shadow([*controls, unresolved])
    assert summary["natural_full_cost_positive_heads"] == 1
    assert summary["natural_positive_direct_child_rated"] == 0
    assert summary["natural_positive_direct_child_still_positive"] is None
    assert summary["natural_timely_h_k_c_rated"] == 0
    assert summary["natural_timely_h_k_c_survivors"] is None
    with pytest.raises(ValueError, match="distinct discovery heads"):
        summarize_batch_shadow([unresolved, unresolved])
