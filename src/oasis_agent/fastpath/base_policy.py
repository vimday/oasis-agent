"""Compare a previous-block quote with a same-head simulation in a Base shadow window."""

from collections import Counter
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from .batch_shadow import BatchShadowHead, _Amount, _Digest, _PositiveClock, _SignedAmount, _Strict

type CaseKind = Literal["natural", "fixed_route", "control"]
type Decision = Literal["attempt", "skip", "expired", "unavailable", "deadline_missing"]


class PreviousQuote(_Strict):
    head: BatchShadowHead
    envelope_sha256: _Digest
    available_ns: _PositiveClock
    meets_minimum_output: bool
    modeled_net_wei: _SignedAmount | None

    @model_validator(mode="after")
    def after_head(self) -> Self:
        if self.available_ns < self.head.observed_ns:
            raise ValueError("quote was available before its block was observed")
        return self


class BasePolicySimulation(_Strict):
    head: BatchShadowHead
    envelope_sha256: _Digest
    available_ns: _PositiveClock
    disposition: Literal["settled", "no_swap", "reverted"]
    settlement_delta_wei: _SignedAmount | None
    modeled_network_fee_wei: _Amount | None

    @model_validator(mode="after")
    def valid_settlement(self) -> Self:
        if self.available_ns < self.head.observed_ns:
            raise ValueError("simulation was available before its block was observed")
        if self.disposition != "settled" and self.settlement_delta_wei != 0:
            raise ValueError("a revert or a verified no-swap needs zero settlement")
        if (net := self.modeled_net_wei) is not None and net <= -(1 << 256):
            raise ValueError("modeled net is out of range")
        return self

    @property
    def modeled_net_wei(self) -> int | None:
        if self.settlement_delta_wei is None or self.modeled_network_fee_wei is None:
            return None
        return self.settlement_delta_wei - self.modeled_network_fee_wei

    @property
    def positive(self) -> bool | None:
        if self.disposition != "settled":
            return False
        if (net := self.modeled_net_wei) is None:
            return None
        return net > 0


def _direct(parent: BatchShadowHead, child: BatchShadowHead) -> bool:
    return (
        child.height == parent.height + 1
        and child.parent_hash == parent.block_hash
        and child.observed_ns > parent.observed_ns
    )


class BasePolicyCase(_Strict):
    kind: CaseKind
    envelope_sha256: _Digest
    principal_wei: Annotated[int, Field(gt=0, lt=1 << 256)]
    head: BatchShadowHead
    child: BatchShadowHead | None
    previous_quote: PreviousQuote | None
    head_simulation: BasePolicySimulation | None
    child_simulation: BasePolicySimulation | None

    @model_validator(mode="after")
    def bound_to_envelope_and_branch(self) -> Self:
        for observation in (self.previous_quote, self.head_simulation, self.child_simulation):
            if observation is not None and observation.envelope_sha256 != self.envelope_sha256:
                raise ValueError("policy observations must share the frozen envelope")
        if self.previous_quote is not None and not _direct(self.previous_quote.head, self.head):
            raise ValueError("previous quote is not from the directly observed parent")
        if self.child is not None and not _direct(self.head, self.child):
            raise ValueError("deadline witness is not the directly observed child")
        if self.head_simulation is not None and self.head_simulation.head != self.head:
            raise ValueError("same-head simulation belongs to a different observation")
        if self.child_simulation is not None and self.child_simulation.head != self.child:
            raise ValueError("child simulation belongs to a different observation")
        return self


def _decide(deadline: int | None, available: int | None, positive: bool | None) -> Decision:
    if deadline is None:
        return "deadline_missing"
    if available is None:
        return "unavailable"
    if available >= deadline:
        return "expired"
    if positive is None:
        return "unavailable"
    return "attempt" if positive else "skip"


def _forgone(positive: bool | None, decision: Decision) -> bool | None:
    if positive is None or decision == "deadline_missing":
        return None
    return positive and decision != "attempt"


def compare_base_policies(case: BasePolicyCase) -> dict[str, object]:
    """Measure a local child deadline and same-envelope counterfactuals in ETH wei.

    H is the prestate available before the opportunity in its direct child. Reading
    H after the deadline still provides an offline reference. C is a distinct
    survival check on the state after that child. Neither simulates transaction order.
    """
    previous, head, child = case.previous_quote, case.head_simulation, case.child_simulation
    deadline = case.child.observed_ns if case.child is not None else None
    prior_positive = None
    prior_ready = None
    if previous is not None:
        prior_ready = max(case.head.observed_ns, previous.available_ns)
        if not previous.meets_minimum_output:
            prior_positive = False
        elif previous.modeled_net_wei is not None:
            prior_positive = previous.modeled_net_wei > 0
    head_positive = head.positive if head is not None else None
    child_positive = child.positive if child is not None else None
    baseline = _decide(deadline, prior_ready, prior_positive)
    same_head = _decide(deadline, head.available_ns if head is not None else None, head_positive)
    incremental_skip = baseline == "attempt" and same_head != "attempt"
    avoids_failed_attempt = None
    avoidable_fee, net = None, None
    if head is not None:
        net = head.modeled_net_wei
    if deadline is not None and head is not None:
        avoids_failed_attempt = incremental_skip and head.disposition in ("reverted", "no_swap")
    if avoids_failed_attempt and head is not None:
        avoidable_fee = head.modeled_network_fee_wei
    return {
        "head_height": case.head.height,
        "head_hash": case.head.block_hash,
        "envelope_sha256": case.envelope_sha256,
        "principal_wei": case.principal_wei,
        "kind": case.kind,
        "previous_block_decision": baseline,
        "same_head_decision": same_head,
        "same_head_before_child": (
            head.available_ns < deadline if head is not None and deadline is not None else None
        ),
        "head_counterfactual_positive": head_positive,
        "head_counterfactual_disposition": head.disposition if head is not None else None,
        "head_counterfactual_net_wei": net,
        "child_counterfactual_positive": child_positive,
        "child_counterfactual_net_wei": child.modeled_net_wei if child is not None else None,
        "previous_block_forgone_positive": _forgone(head_positive, baseline),
        "same_head_forgone_positive": _forgone(head_positive, same_head),
        "newly_forgone_positive": (
            head_positive and incremental_skip
            if deadline is not None and head_positive is not None
            else None
        ),
        "newly_captured_positive": (
            head_positive and baseline != "attempt" and same_head == "attempt"
            if deadline is not None and head_positive is not None
            else None
        ),
        "newly_forgone_positive_net_wei": net if incremental_skip and head_positive else None,
        "avoids_failed_or_no_swap": avoids_failed_attempt,
        "avoided_failed_or_no_swap_fee_wei": avoidable_fee,
    }


class BasePolicyWindow(_Strict):
    schema_version: Literal["oasis.base-shadow-policy-input.v1"]
    cases: Annotated[tuple[BasePolicyCase, ...], Field(min_length=1, max_length=4096)]

    def report(self) -> dict[str, object]:
        pair_keys = {(case.head.height, case.envelope_sha256) for case in self.cases}
        if len(pair_keys) != len(self.cases):
            raise ValueError("duplicate Base policy observation")
        if len({(c.envelope_sha256, c.principal_wei) for c in self.cases}) != len(
            {c.envelope_sha256 for c in self.cases}
        ):
            raise ValueError("principal changed for a frozen envelope")
        heads = [
            state
            for case in self.cases
            for state in (
                case.head,
                case.child,
                case.previous_quote.head if case.previous_quote else None,
            )
            if state is not None
        ]
        if len({(h.height, h.block_hash, h.parent_hash, h.observed_ns) for h in heads}) != len(
            {h.height for h in heads}
        ):
            raise ValueError("conflicting head observations in the Base policy window")
        rows = [compare_base_policies(case) for case in self.cases]
        cohorts = {}
        for kind in ("natural", "fixed_route", "control"):
            selected = [row for row in rows if row["kind"] == kind]
            priced = [
                fee
                for row in selected
                if type(fee := row["avoided_failed_or_no_swap_fee_wei"]) is int
            ]
            avoidable: Counter[str] = Counter()
            for row in selected:
                fee = row["avoided_failed_or_no_swap_fee_wei"]
                if type(fee) is int:
                    avoidable[str(row["same_head_decision"])] += fee
            forgone = [
                net for row in selected if type(net := row["newly_forgone_positive_net_wei"]) is int
            ]
            cohorts[kind] = {
                "cases": len(selected),
                "previous_block_decisions": dict(
                    Counter(row["previous_block_decision"] for row in selected)
                ),
                "same_head_decisions": dict(Counter(row["same_head_decision"] for row in selected)),
                "paired_positive_reference_known": sum(
                    row["same_head_forgone_positive"] is not None for row in selected
                ),
                "head_positive": sum(
                    row["head_counterfactual_positive"] is True for row in selected
                ),
                "observed_newly_forgone_positive": sum(
                    row["newly_forgone_positive"] is True for row in selected
                ),
                "observed_newly_captured_positive": sum(
                    row["newly_captured_positive"] is True for row in selected
                ),
                "observed_newly_forgone_positive_net_wei": sum(forgone),
                "observed_avoided_failed_or_no_swap": sum(
                    row["avoids_failed_or_no_swap"] is True for row in selected
                ),
                "priced_avoided_failed_or_no_swap": len(priced),
                "observed_avoided_failed_or_no_swap_fee_wei": sum(priced),
                "observed_avoided_fee_by_same_head_decision_wei": dict(avoidable),
            }
        return {
            "schema": "oasis.base-shadow-policy-report.v1",
            "cohorts": cohorts,
            "rows": rows,
            "actual_paid_wei": None,
            "execution_eligible": False,
        }
