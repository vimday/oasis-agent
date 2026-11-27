import pytest

from oasis_agent.fastpath.base_policy import (
    BasePolicyCase,
    BasePolicySimulation,
    BasePolicyWindow,
    PreviousQuote,
    compare_base_policies,
)
from oasis_agent.fastpath.batch_shadow import BatchShadowHead

P_HASH, H_HASH, C_HASH = ("0x" + x * 64 for x in "123")
P = BatchShadowHead(height=20, block_hash=P_HASH, parent_hash="0x" + "0" * 64, observed_ns=100)
H = BatchShadowHead(height=21, block_hash=H_HASH, parent_hash=P_HASH, observed_ns=200)
C = BatchShadowHead(height=22, block_hash=C_HASH, parent_hash=H_HASH, observed_ns=300)
HASH = "f" * 64
QUOTE = PreviousQuote(
    head=P, envelope_sha256=HASH, available_ns=190, meets_minimum_output=True, modeled_net_wei=1
)
WIN = BasePolicySimulation(
    head=H,
    envelope_sha256=HASH,
    available_ns=299,
    disposition="settled",
    settlement_delta_wei=11,
    modeled_network_fee_wei=10,
)


def _case(**updates: object) -> BasePolicyCase:
    return BasePolicyCase.model_validate(
        {
            "kind": "natural",
            "envelope_sha256": HASH,
            "principal_wei": 1_000,
            "head": H,
            "child": C,
            "previous_quote": QUOTE,
            "head_simulation": WIN,
            "child_simulation": None,
            **updates,
        }
    )


def test_one_wei_win_requires_return_strictly_before_direct_child() -> None:
    row = compare_base_policies(_case())
    assert row["previous_block_decision"] == row["same_head_decision"] == "attempt"
    assert row["head_counterfactual_net_wei"] == 1
    late = WIN.model_copy(update={"available_ns": C.observed_ns})
    row = compare_base_policies(_case(head_simulation=late))
    assert row["same_head_decision"] == "expired"
    assert row["newly_forgone_positive"] is True
    assert row["avoided_failed_or_no_swap_fee_wei"] is None


@pytest.mark.parametrize("outcome", ["reverted", "no_swap"])
def test_avoided_fee_uses_exact_same_head_counterfactual(outcome: str) -> None:
    h = WIN.model_copy(update={"disposition": outcome, "settlement_delta_wei": 0})
    c = WIN.model_copy(update={"head": C, "available_ns": 350})
    row = compare_base_policies(_case(head_simulation=h, child_simulation=c))
    assert row["same_head_decision"] == "skip"
    assert row["newly_forgone_positive"] is False
    assert row["avoided_failed_or_no_swap_fee_wei"] == 10
    assert row["child_counterfactual_positive"] is True


def test_missing_simulation_or_deadline_stays_unknown() -> None:
    row = compare_base_policies(_case(head_simulation=None))
    assert row["same_head_decision"] == "unavailable"
    assert row["same_head_forgone_positive"] is None
    row = compare_base_policies(_case(child=None))
    assert row["previous_block_decision"] == row["same_head_decision"] == "deadline_missing"
    assert row["same_head_before_child"] is row["newly_forgone_positive"] is None


def test_baseline_rejects_below_minimum_even_with_positive_quote() -> None:
    quote = QUOTE.model_copy(update={"meets_minimum_output": False})
    row = compare_base_policies(_case(previous_quote=quote))
    assert row["previous_block_decision"] == "skip"
    assert row["newly_captured_positive"] is True


def test_previous_quote_returning_after_deadline_cannot_create_attempt() -> None:
    quote = QUOTE.model_copy(update={"available_ns": 312})
    row = compare_base_policies(_case(previous_quote=quote))
    assert row["previous_block_decision"] == "expired"
    assert row["same_head_decision"] == "attempt"


def test_wrong_envelope_or_branch_is_rejected() -> None:
    with pytest.raises(ValueError, match="frozen envelope"):
        _case(previous_quote=QUOTE.model_copy(update={"envelope_sha256": "e" * 64}))
    with pytest.raises(ValueError, match="directly observed child"):
        _case(child=C.model_copy(update={"parent_hash": P_HASH}))
    with pytest.raises(ValueError, match="different observation"):
        _case(child_simulation=WIN)


def test_window_is_replayable_and_duplicate_cases_do_not_count_twice() -> None:
    case = _case(kind="control")
    window = BasePolicyWindow(schema_version="oasis.base-shadow-policy-input.v1", cases=(case,))
    replayed = BasePolicyWindow.model_validate_json(window.model_dump_json())
    assert replayed.report() == window.report()
    assert replayed.report()["actual_paid_wei"] is None
    with pytest.raises(ValueError, match="duplicate"):
        window.model_copy(update={"cases": (case, case)}).report()


def test_same_child_hash_cannot_have_different_deadlines_across_cases() -> None:
    first = _case()
    other = _case(
        envelope_sha256="e" * 64,
        child=C.model_copy(update={"observed_ns": 500}),
        previous_quote=QUOTE.model_copy(update={"envelope_sha256": "e" * 64}),
        head_simulation=WIN.model_copy(update={"envelope_sha256": "e" * 64}),
    )
    window = BasePolicyWindow(
        schema_version="oasis.base-shadow-policy-input.v1", cases=(first, other)
    )
    with pytest.raises(ValueError, match="conflicting head observations"):
        window.report()
