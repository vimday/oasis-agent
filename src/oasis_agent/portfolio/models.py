"""Exact-integer domain models for Stage 7 cross-strategy portfolio allocation.

All monetary values are integer USD micros and all rates/thresholds are integer basis
points. Decisions and reports are advisory allocation proposals only and always carry
``execution_eligible = False``.
"""

from dataclasses import dataclass
from typing import Final, Literal

DomainKind = Literal[
    "chain",
    "sequencer",
    "token",
    "stablecoin",
    "venue",
    "oracle",
    "custodian",
]
VALID_DOMAIN_KINDS: Final[frozenset[str]] = frozenset(
    {
        "chain",
        "sequencer",
        "token",
        "stablecoin",
        "venue",
        "oracle",
        "custodian",
    }
)
AllocationAction = Literal[
    "scale_to_zero",
    "scale_down",
    "hold",
    "scale_up",
    "blocked_zero",
]
VALID_ALLOCATION_ACTIONS: Final[frozenset[str]] = frozenset(
    {
        "scale_to_zero",
        "scale_down",
        "hold",
        "scale_up",
        "blocked_zero",
    }
)
MIN_STAGE: Final[int] = 1
MAX_STAGE: Final[int] = 7


def require_int(value: object, name: str) -> int:
    """Require a plain ``int`` (explicitly rejecting ``bool`` and floats)."""
    if type(value) is not int:
        raise TypeError(f"{name} must be an int, got {type(value).__name__}")
    return value


def require_non_negative_int(value: object, name: str) -> int:
    """Require a non-negative plain ``int``."""
    val = require_int(value, name)
    if val < 0:
        raise ValueError(f"{name} must be non-negative")
    return val


def require_bool(value: object, name: str) -> bool:
    """Require a plain ``bool``."""
    if type(value) is not bool:
        raise TypeError(f"{name} must be a bool, got {type(value).__name__}")
    return value


def require_non_empty_str(value: object, name: str) -> str:
    """Require a non-empty string after stripping outer whitespace."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _validate_sha256(digest: object, name: str = "report_sha256") -> str:
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(ch not in "0123456789abcdef" for ch in digest)
    ):
        raise ValueError(f"{name} must be a lowercase 64-char SHA-256 hex digest")
    return digest


@dataclass(frozen=True, slots=True)
class PortfolioReserves:
    """Segregated operational and safety cash buffers carved out before allocation."""

    lp_unwind_gas_usd_micros: int
    perp_margin_buffer_usd_micros: int
    incident_cash_usd_micros: int
    ops_reserve_usd_micros: int = 0

    def __post_init__(self) -> None:
        require_non_negative_int(
            self.lp_unwind_gas_usd_micros,
            "lp_unwind_gas_usd_micros",
        )
        require_non_negative_int(
            self.perp_margin_buffer_usd_micros,
            "perp_margin_buffer_usd_micros",
        )
        require_non_negative_int(
            self.incident_cash_usd_micros,
            "incident_cash_usd_micros",
        )
        require_non_negative_int(
            self.ops_reserve_usd_micros,
            "ops_reserve_usd_micros",
        )

    @property
    def total_usd_micros(self) -> int:
        return (
            self.lp_unwind_gas_usd_micros
            + self.perp_margin_buffer_usd_micros
            + self.incident_cash_usd_micros
            + self.ops_reserve_usd_micros
        )


@dataclass(frozen=True, slots=True)
class ExposureDomainCap:
    """Hard shared-correlation exposure ceiling on a single risk domain."""

    kind: DomainKind
    domain_id: str
    max_usd_micros: int

    def __post_init__(self) -> None:
        if self.kind not in VALID_DOMAIN_KINDS:
            raise ValueError(f"unsupported domain kind: {self.kind!r}")
        cleaned_id = require_non_empty_str(self.domain_id, "domain_id")
        object.__setattr__(self, "domain_id", cleaned_id)
        require_non_negative_int(self.max_usd_micros, "max_usd_micros")

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.domain_id}"


@dataclass(frozen=True, slots=True)
class StrategyTrackCandidate:
    """Point-in-time metrics and risk inputs for one candidate strategy track."""

    strategy_id: str
    stage: int
    current_allocation_usd_micros: int
    capacity_usd_micros: int
    max_step_up_usd_micros: int
    lcb_net_edge_bps: int
    expected_drawdown_bps: int
    tail_loss_99_bps: int
    evidence_age_seconds: int
    max_evidence_age_seconds: int
    reconciliation_residual_raw: int = 0
    error_budget_halted: bool = False
    exposures: tuple[tuple[DomainKind, str], ...] = ()
    live_authorized: bool = False

    def __post_init__(self) -> None:
        cleaned_id = require_non_empty_str(self.strategy_id, "strategy_id")
        object.__setattr__(self, "strategy_id", cleaned_id)
        stage = require_int(self.stage, "stage")
        if not MIN_STAGE <= stage <= MAX_STAGE:
            raise ValueError(f"stage must be in [{MIN_STAGE}, {MAX_STAGE}]")
        require_non_negative_int(
            self.current_allocation_usd_micros,
            "current_allocation_usd_micros",
        )
        require_non_negative_int(self.capacity_usd_micros, "capacity_usd_micros")
        require_non_negative_int(self.max_step_up_usd_micros, "max_step_up_usd_micros")
        require_int(self.lcb_net_edge_bps, "lcb_net_edge_bps")
        require_non_negative_int(self.expected_drawdown_bps, "expected_drawdown_bps")
        require_non_negative_int(self.tail_loss_99_bps, "tail_loss_99_bps")
        require_non_negative_int(self.evidence_age_seconds, "evidence_age_seconds")
        require_non_negative_int(
            self.max_evidence_age_seconds,
            "max_evidence_age_seconds",
        )
        require_int(self.reconciliation_residual_raw, "reconciliation_residual_raw")
        require_bool(self.error_budget_halted, "error_budget_halted")
        require_bool(self.live_authorized, "live_authorized")
        if not isinstance(self.exposures, tuple):
            raise TypeError("exposures must be a tuple of (DomainKind, domain_id) pairs")
        normalized_exposures: list[tuple[DomainKind, str]] = []
        for idx, item in enumerate(self.exposures):
            if not isinstance(item, tuple) or len(item) != 2:
                raise TypeError(f"exposures[{idx}] must be a (DomainKind, domain_id) tuple")
            kind, domain_id = item
            if kind not in VALID_DOMAIN_KINDS:
                raise ValueError(f"exposures[{idx}] unsupported domain kind: {kind!r}")
            cleaned_domain_id = require_non_empty_str(
                domain_id,
                f"exposures[{idx}][1]",
            )
            normalized_exposures.append((kind, cleaned_domain_id))
        object.__setattr__(self, "exposures", tuple(normalized_exposures))


@dataclass(frozen=True, slots=True)
class StrategyAllocationDecision:
    """Target allocation decision for one strategy track (never execution-eligible)."""

    strategy_id: str
    stage: int
    previous_usd_micros: int
    target_usd_micros: int
    delta_usd_micros: int
    risk_adjusted_score: int
    action: AllocationAction
    reasons: tuple[str, ...]
    binding_constraints: tuple[str, ...]
    live_authorized: bool
    execution_eligible: Literal[False] = False

    def __post_init__(self) -> None:
        cleaned_id = require_non_empty_str(self.strategy_id, "strategy_id")
        object.__setattr__(self, "strategy_id", cleaned_id)
        stage = require_int(self.stage, "stage")
        if not MIN_STAGE <= stage <= MAX_STAGE:
            raise ValueError(f"stage must be in [{MIN_STAGE}, {MAX_STAGE}]")
        prev = require_non_negative_int(self.previous_usd_micros, "previous_usd_micros")
        target = require_non_negative_int(self.target_usd_micros, "target_usd_micros")
        delta = require_int(self.delta_usd_micros, "delta_usd_micros")
        if delta != target - prev:
            raise ValueError("delta_usd_micros must equal target_usd_micros - previous_usd_micros")
        require_int(self.risk_adjusted_score, "risk_adjusted_score")
        if self.action not in VALID_ALLOCATION_ACTIONS:
            raise ValueError(f"unsupported allocation action: {self.action!r}")
        if not isinstance(self.reasons, tuple) or not all(isinstance(r, str) for r in self.reasons):
            raise TypeError("reasons must be a tuple of strings")
        if not isinstance(self.binding_constraints, tuple) or not all(
            isinstance(c, str) for c in self.binding_constraints
        ):
            raise TypeError("binding_constraints must be a tuple of strings")
        require_bool(self.live_authorized, "live_authorized")
        if self.execution_eligible is not False:
            raise ValueError("portfolio allocation decision cannot claim execution_eligible=True")


@dataclass(frozen=True, slots=True)
class PortfolioAllocationReport:
    """Deterministic portfolio-level allocation proposal with SHA-256 commitment."""

    total_nav_usd_micros: int
    segregated_reserves_usd_micros: int
    deployable_budget_usd_micros: int
    allocated_total_usd_micros: int
    unallocated_cash_usd_micros: int
    reserve_deficit_usd_micros: int
    domain_utilization_usd_micros: dict[str, int]
    decisions: tuple[StrategyAllocationDecision, ...]
    report_sha256: str
    execution_eligible: Literal[False] = False

    def __post_init__(self) -> None:
        require_non_negative_int(self.total_nav_usd_micros, "total_nav_usd_micros")
        require_non_negative_int(
            self.segregated_reserves_usd_micros,
            "segregated_reserves_usd_micros",
        )
        require_non_negative_int(
            self.deployable_budget_usd_micros,
            "deployable_budget_usd_micros",
        )
        require_non_negative_int(
            self.allocated_total_usd_micros,
            "allocated_total_usd_micros",
        )
        require_non_negative_int(
            self.unallocated_cash_usd_micros,
            "unallocated_cash_usd_micros",
        )
        require_non_negative_int(
            self.reserve_deficit_usd_micros,
            "reserve_deficit_usd_micros",
        )
        if not isinstance(self.domain_utilization_usd_micros, dict):
            raise TypeError("domain_utilization_usd_micros must be a dict")
        validated_util: dict[str, int] = {}
        for key, val in self.domain_utilization_usd_micros.items():
            cleaned_key = require_non_empty_str(key, "domain_utilization key")
            validated_util[cleaned_key] = require_non_negative_int(
                val,
                f"domain_utilization_usd_micros[{cleaned_key!r}]",
            )
        object.__setattr__(self, "domain_utilization_usd_micros", validated_util)
        if not isinstance(self.decisions, tuple) or not all(
            isinstance(d, StrategyAllocationDecision) for d in self.decisions
        ):
            raise TypeError("decisions must be a tuple of StrategyAllocationDecision")
        _validate_sha256(self.report_sha256)
        if self.execution_eligible is not False:
            raise ValueError("portfolio allocation report cannot claim execution_eligible=True")
