"""LP risk budgets and automatic failure responses (blueprint sections 9.1/9.2).

Canary sizing takes the minimum of the approved absolute experiment loss, 5
basis points of liquid portfolio NAV, 1 basis point of independently verified
two-sided exit depth at the approved maximum impact, and the amount whose
complete loss leaves gas/operations/unwind reserves untouched. These are
conservative initial ceilings, not evidence that a trade is safe.

The failure matrix maps every precommitted trigger to its automatic response.
Unknown triggers are never silently defaulted: the operator must extend the
matrix consciously (fail closed). Emergency responses may reduce exposure only.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

BASIS_POINT = Decimal("0.0001")
CANARY_NAV_FRACTION = BASIS_POINT * Decimal(5)
CANARY_DEPTH_FRACTION = BASIS_POINT


class FailureResponse(str, Enum):
    NO_NEW_EXPOSURE_RECONCILE = "no_new_exposure_reconcile"
    HALT_SUBMISSIONS_PRESERVE_GAS = "halt_submissions_preserve_gas"
    CONSERVATIVE_FEE_FALLBACK = "conservative_fee_fallback"
    DISABLE_ROUTE_AND_RULE = "disable_route_and_rule"
    HALT_EXECUTOR_NO_COMPENSATING_TRADE = "halt_executor_no_compensating_trade"
    REJECT_TOKEN_PREPARE_REMOVAL = "reject_token_prepare_removal"
    FAIL_CLOSED_NEW_AUDIT = "fail_closed_new_audit"
    NO_REFLEXIVE_RECENTER_EVALUATE = "no_reflexive_recenter_evaluate"
    INDEPENDENT_KILL_SWITCH = "independent_kill_switch"
    FREEZE_PARAMETER_CHANGES = "freeze_parameter_changes"


# Verbatim blueprint 9.2 matrix: trigger, automatic response, runbook detail.
FAILURE_MATRIX: tuple[tuple[str, FailureResponse, str], ...] = (
    (
        "rpc/feed stale, gap, or disagreement",
        FailureResponse.NO_NEW_EXPOSURE_RECONCILE,
        "no new exposure or rebalance; reconcile from independent source",
    ),
    (
        "sequencer outage or abnormal finality lag",
        FailureResponse.HALT_SUBMISSIONS_PRESERVE_GAS,
        "halt submissions; preserve eth; resume only after grace period",
    ),
    (
        "reference price stale/manipulable",
        FailureResponse.CONSERVATIVE_FEE_FALLBACK,
        "conservative fee fallback; disable range growth/recenter",
    ),
    (
        "quote/settlement mismatch or hook revert",
        FailureResponse.DISABLE_ROUTE_AND_RULE,
        "disable route and fee-rule version; retain evidence",
    ),
    (
        "position/balance/allowance/nonce mismatch",
        FailureResponse.HALT_EXECUTOR_NO_COMPENSATING_TRADE,
        "halt executor; no compensating trade from uncertain state",
    ),
    (
        "token bytecode/admin/transfer behavior changes",
        FailureResponse.REJECT_TOKEN_PREPARE_REMOVAL,
        "reject token and prepare bounded removal",
    ),
    (
        "hook/poolmanager/router/quoter code hash changes",
        FailureResponse.FAIL_CLOSED_NEW_AUDIT,
        "fail closed pending new audit and fork run",
    ),
    (
        "range exit during toxic/jump regime",
        FailureResponse.NO_REFLEXIVE_RECENTER_EVALUATE,
        "do not reflexively recenter; evaluate reduce/hold/unwind",
    ),
    (
        "daily loss, drawdown, residual, or gas budget breach",
        FailureResponse.INDEPENDENT_KILL_SWITCH,
        "independent kill switch; cancel growth and unwind per runbook",
    ),
    (
        "governance/security incident",
        FailureResponse.FREEZE_PARAMETER_CHANGES,
        "freeze parameter changes and follow incident-response runbook",
    ),
)


@dataclass(frozen=True, slots=True)
class FailureDecision:
    trigger: str
    response: FailureResponse
    runbook: str
    reduces_exposure_only: bool = True

    def __post_init__(self) -> None:
        if not self.trigger:
            raise ValueError("trigger is required")
        if not self.runbook:
            raise ValueError("runbook is required")
        if not isinstance(self.response, FailureResponse):
            raise TypeError("response must be a FailureResponse")


def _normalize(trigger: str) -> str:
    return " ".join(trigger.lower().split())


def classify_failure(trigger: str) -> FailureDecision:
    """Map a normalized matrix trigger to its automatic response.

    Unknown triggers raise so the matrix must be extended consciously; there is
    no silent permissive default. Emergency responses reduce exposure only.
    """
    normalized = _normalize(trigger)
    for raw_trigger, response, runbook in FAILURE_MATRIX:
        if _normalize(raw_trigger) == normalized:
            return FailureDecision(trigger, response, runbook)
    raise ValueError(f"unknown failure trigger: {trigger!r}")


@dataclass(frozen=True, slots=True)
class CanaryRiskLimits:
    """Pre-launch absolute caps in one quote currency (blueprint section 9.1)."""

    approved_experiment_loss: Decimal
    portfolio_nav: Decimal
    exit_depth_two_sided: Decimal
    reserve_requirement: Decimal

    def __post_init__(self) -> None:
        for name, value in (
            ("approved_experiment_loss", self.approved_experiment_loss),
            ("portfolio_nav", self.portfolio_nav),
            ("exit_depth_two_sided", self.exit_depth_two_sided),
            ("reserve_requirement", self.reserve_requirement),
        ):
            if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
                raise ValueError(f"{name} must be a finite nonnegative Decimal")


@dataclass(frozen=True, slots=True)
class CanaryCap:
    approved_loss_cap: Decimal
    nav_cap: Decimal
    exit_depth_cap: Decimal
    reserve_preserving_cap: Decimal
    cap: Decimal

    def __post_init__(self) -> None:
        for name, value in (
            ("approved_loss_cap", self.approved_loss_cap),
            ("nav_cap", self.nav_cap),
            ("exit_depth_cap", self.exit_depth_cap),
            ("reserve_preserving_cap", self.reserve_preserving_cap),
            ("cap", self.cap),
        ):
            if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
                raise ValueError(f"{name} must be a finite nonnegative Decimal")
        expected = min(
            self.approved_loss_cap,
            self.nav_cap,
            self.exit_depth_cap,
            self.reserve_preserving_cap,
        )
        if self.cap != expected:
            raise ValueError("cap must equal the minimum of the four ceilings")


def canary_cap(limits: CanaryRiskLimits) -> CanaryCap:
    """Calculated canary cap is no larger than the minimum of the four ceilings."""
    approved_loss_cap = limits.approved_experiment_loss
    nav_cap = limits.portfolio_nav * CANARY_NAV_FRACTION
    exit_depth_cap = limits.exit_depth_two_sided * CANARY_DEPTH_FRACTION
    reserve_preserving_cap = max(Decimal(0), limits.portfolio_nav - limits.reserve_requirement)
    cap = min(approved_loss_cap, nav_cap, exit_depth_cap, reserve_preserving_cap)
    return CanaryCap(approved_loss_cap, nav_cap, exit_depth_cap, reserve_preserving_cap, cap)


__all__ = [
    "BASIS_POINT",
    "CANARY_DEPTH_FRACTION",
    "CANARY_NAV_FRACTION",
    "FAILURE_MATRIX",
    "CanaryCap",
    "CanaryRiskLimits",
    "FailureDecision",
    "FailureResponse",
    "canary_cap",
    "classify_failure",
]
