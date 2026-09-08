"""Observation-only LP decision journaling for shadow mode.

The runner never signs or broadcasts. For each pinned block it first appends a
health observation to the journal, then evaluates the band-table fee strategy,
the range/inventory controller, and the canary risk policy, and appends a
hash-linked decision record to the same journal. A HALT health latch, stale or
unpinned state, or any evaluation failure produces a HALT decision record with
a fixed reason; no exception text or secrets are retained. Unknown failures
never become a permissive default, and ``execution_eligible`` stays False for
every record.

Decisions are chained to the health observation that preceded them through
``health_digest`` and hash-linked among themselves, so the shadow trail is
append-only and tamper-evident even across restarts.

HALT latch and reset
--------------------
A ``halt`` decision record latches the decision chain: the runner appends
``decision_halt_latched`` for every later cycle, and ``record_decision``
rejects any non-halt payload while a halt record is last. The latch lives in
the journal file, so the only recovery is operator journal rotation described
in ``shadow/journal.py``: preserve the halted file verbatim, open a fresh
journal at a new path, and restart the observation window under the same
reviewed policy fingerprint. No code path clears the latch in place.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import cast

from ..evidence.store import EvidenceStore  # noqa: F401  (typed protocol only)
from ..execution.lp_state_machine import TickRange
from ..execution.range_controller import (
    LpAction,
    LpActionType,
    MarginalValueInputs,
    RebalanceGateInput,
    rebalance_gate,
    select_action,
)
from ..farming.strategy import BandTableFeeStrategy, FeeQuote
from ..models.chain import BlockSnapshot
from ..models.common import EvidenceRef
from ..models.finance import PoolState
from ..models.v4 import SwapDirection
from ..risk.lp_policy import CanaryCap, CanaryRiskLimits, canary_cap
from .journal import ShadowJournal

DECISION_SCHEMA = "oasis.shadow-lp-decision.v1"


def _decimal_str(value: Decimal) -> str:
    """Canonical fixed-point string: strip Decimal arithmetic scale.

    Remove scale without Decimal.normalize(), which rounds using the ambient
    precision context. Audit serialization must preserve every supplied digit.
    """
    result = format(value, "f")
    if "." in result:
        result = result.rstrip("0").rstrip(".")
    return "0" if value.is_zero() else result


@dataclass(frozen=True, slots=True)
class LpShadowDecision:
    """Structured result of one shadow evaluation cycle."""

    decision: str
    health: str
    fee_quote: FeeQuote | None
    range_action: LpAction | None
    rebalance_allowed: bool | None
    rebalance_reasons: tuple[str, ...]
    canary_cap: CanaryCap | None
    reasons: tuple[str, ...]


def _fee_payload(quote: FeeQuote) -> dict[str, object]:
    return {
        "effective_fee": quote.effective_fee,
        "min_fee": quote.min_fee,
        "max_fee": quote.max_fee,
        "rule_version": quote.rule_version,
        "inputs": tuple(_decimal_str(value) for value in quote.inputs),
        "state_hash": quote.state_hash,
        "valid_until_timestamp": quote.valid_until_timestamp,
        "rate_limit_elapsed_seconds": quote.rate_limit_elapsed_seconds,
    }


def _action_payload(action: LpAction) -> dict[str, object]:
    target = None
    if action.target_range is not None:
        target = {
            "tick_lower": action.target_range.tick_lower,
            "tick_upper": action.target_range.tick_upper,
            "tick_spacing": action.target_range.tick_spacing,
        }
    return {
        "action_type": action.action_type.value,
        "marginal_value": _decimal_str(action.marginal_value),
        "lower_confidence_bound": _decimal_str(action.lower_confidence_bound),
        "target_range": target,
    }


def _canary_payload(cap: CanaryCap) -> dict[str, object]:
    return {
        "cap": _decimal_str(cap.cap),
        "approved_loss_cap": _decimal_str(cap.approved_loss_cap),
        "nav_cap": _decimal_str(cap.nav_cap),
        "exit_depth_cap": _decimal_str(cap.exit_depth_cap),
        "reserve_preserving_cap": _decimal_str(cap.reserve_preserving_cap),
    }


class LpShadowRunner:
    """Evaluate LP policies against pinned state and journal the decision."""

    def __init__(
        self,
        journal: ShadowJournal,
        strategy: BandTableFeeStrategy,
        *,
        min_benefit: Decimal = Decimal(0),
        canary_limits: CanaryRiskLimits | None = None,
    ) -> None:
        if not isinstance(journal, ShadowJournal):
            raise TypeError("journal must be a ShadowJournal")
        if not isinstance(strategy, BandTableFeeStrategy):
            raise TypeError("strategy must be a BandTableFeeStrategy")
        if not isinstance(min_benefit, Decimal) or not min_benefit.is_finite():
            raise ValueError("min_benefit must be a finite Decimal")
        if canary_limits is not None and not isinstance(canary_limits, CanaryRiskLimits):
            raise TypeError("canary_limits must be a CanaryRiskLimits")
        self.journal = journal
        self.strategy = strategy
        self.min_benefit = min_benefit
        self.canary_limits = canary_limits

    def _record(
        self,
        header: BlockSnapshot,
        evidence: EvidenceRef,
        now: int,
        decision: LpShadowDecision,
        health_digest: str,
    ) -> LpShadowDecision:
        payload: dict[str, object] = {
            "schema": DECISION_SCHEMA,
            "block": asdict(header.ref),
            "observed_at": now,
            "evidence": asdict(evidence),
            "health": decision.health,
            "health_digest": health_digest,
            "decision": decision.decision,
            "fee_quote": None if decision.fee_quote is None else _fee_payload(decision.fee_quote),
            "range_action": (
                None if decision.range_action is None else _action_payload(decision.range_action)
            ),
            "rebalance_allowed": decision.rebalance_allowed,
            "rebalance_reasons": list(decision.rebalance_reasons),
            "canary_cap": (
                None if decision.canary_cap is None else _canary_payload(decision.canary_cap)
            ),
            "reasons": list(decision.reasons),
            "execution_eligible": False,
            "strategy_qualification": "unresolved",
        }
        self.journal.record_decision(payload)
        return decision

    def _halt(
        self,
        header: BlockSnapshot,
        evidence: EvidenceRef,
        now: int,
        health_digest: str,
        reasons: tuple[str, ...],
        health: str,
    ) -> LpShadowDecision:
        return self._record(
            header,
            evidence,
            now,
            LpShadowDecision("halt", health, None, None, None, (), None, reasons),
            health_digest,
        )

    def decide(
        self,
        *,
        header: BlockSnapshot,
        evidence: EvidenceRef,
        state: PoolState,
        direction: SwapDirection,
        amount_specified: int,
        exact_input: bool,
        benign_flow: bool = False,
        now: int,
        candidates: Mapping[LpActionType, tuple[MarginalValueInputs, TickRange]] | None = None,
        gate_input: RebalanceGateInput | None = None,
    ) -> LpShadowDecision:
        """Append one health observation and one LP decision record.

        Acquisition/integrity failures of the journal itself propagate (fail
        closed: a corrupt audit trail must block evaluation). Evaluation
        failures are recorded as HALT decisions with fixed reasons and never
        retain exception text or secrets.
        """
        if evidence.block != header.ref or not evidence.raw_response_hash:
            raise ValueError("shadow decision needs raw block-pinned evidence")
        health = self.journal.observe(header, evidence, now=now)
        health_digest = str(health["digest"])
        previous = self.journal.decision_records()
        if health["decision"] != "HOLD":
            return self._halt(
                header,
                evidence,
                now,
                health_digest,
                tuple(str(x) for x in cast(Sequence[object], health["reasons"])),
                "HALT",
            )
        if previous and previous[-1]["decision"] == "halt":
            return self._halt(
                header, evidence, now, health_digest, ("decision_halt_latched",), "HOLD"
            )
        try:
            if state.block != header.ref or state.observed_at_timestamp != header.ref.timestamp:
                return self._halt(
                    header, evidence, now, health_digest, ("state_snapshot_mismatch",), "HOLD"
                )
            fee_quote = self.strategy.quote(
                state, direction, amount_specified, exact_input, benign_flow
            )
            if now > fee_quote.valid_until_timestamp:
                return self._halt(
                    header, evidence, now, health_digest, ("fee_quote_expired",), "HOLD"
                )
        except (ValueError, TypeError):
            return self._halt(
                header, evidence, now, health_digest, ("strategy_evaluation_failed",), "HOLD"
            )
        range_action = None
        if candidates is not None:
            try:
                range_action = select_action(candidates, self.min_benefit)
            except (ValueError, TypeError):
                return self._halt(
                    header, evidence, now, health_digest, ("range_evaluation_failed",), "HOLD"
                )
        rebalance_allowed: bool | None = None
        rebalance_reasons: tuple[str, ...] = ()
        if range_action is not None and range_action.action_type is not LpActionType.HOLD:
            rebalance_allowed = False
            rebalance_reasons = ("rebalance_gate_missing",)
        if gate_input is not None:
            try:
                gate = rebalance_gate(gate_input)
            except (ValueError, TypeError):
                return self._halt(
                    header, evidence, now, health_digest, ("rebalance_gate_failed",), "HOLD"
                )
            rebalance_allowed = gate.allowed
            rebalance_reasons = gate.reasons
        canary = None
        if self.canary_limits is not None:
            try:
                canary = canary_cap(self.canary_limits)
            except (ValueError, TypeError):
                return self._halt(
                    header, evidence, now, health_digest, ("canary_sizing_failed",), "HOLD"
                )
        if (
            range_action is not None
            and range_action.action_type is not LpActionType.HOLD
            and rebalance_allowed is True
        ):
            decision_value = range_action.action_type.value
        else:
            # A gate denial suppresses the proposal without erasing it from the
            # audit record; rebalance_allowed/rebalance_reasons carry the cause.
            decision_value = "hold"
        return self._record(
            header,
            evidence,
            now,
            LpShadowDecision(
                decision_value,
                "HOLD",
                fee_quote,
                range_action,
                rebalance_allowed,
                rebalance_reasons,
                canary,
                (),
            ),
            health_digest,
        )


__all__ = [
    "DECISION_SCHEMA",
    "LpShadowDecision",
    "LpShadowRunner",
]
