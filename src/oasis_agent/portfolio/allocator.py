"""Stage 7 cross-strategy portfolio capital allocator.

Allocates deployable capital across independent strategy tracks using exact integer
arithmetic (USD micros and basis points):
1. Segregates LP unwind gas, perp margin, incident cash, and ops reserves first.
2. Immediately scales disqualified tracks to zero (reserve deficit, halted error/revert
   budget, non-zero reconciliation residual, stale evidence, negative/insufficient LCB
   net edge, or tail loss above ceiling).
3. Ranks eligible tracks by integer risk-adjusted marginal score and allocates greedily
   within deployable NAV, strategy capacity, single-strategy concentration share, slow
   step-up bounds, and shared-correlation domain exposure caps.
"""

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Final

from ..models.common import canonical_json, sha256_hex
from .models import (
    AllocationAction,
    ExposureDomainCap,
    PortfolioAllocationReport,
    PortfolioReserves,
    StrategyAllocationDecision,
    StrategyTrackCandidate,
    require_non_negative_int,
)

BPS_DENOMINATOR: Final[int] = 10_000
MIN_RISK_DENOM_BPS: Final[int] = 100
REPORT_SCHEMA: Final[str] = "oasis.portfolio-allocation.v1"


@dataclass(frozen=True, slots=True)
class PortfolioAllocatorConfig:
    """Owner-configured constraints for Stage 7 cross-strategy capital allocation.

    ``max_single_strategy_share_bps`` defaults to ``5_000`` (50% of deployable budget)
    to enforce cross-strategy diversification alongside track ``capacity_usd_micros``
    and shared correlation ``ExposureDomainCap`` bounds.
    """

    min_lcb_edge_bps: int = 1
    max_tail_loss_99_bps: int = 2_000
    max_single_strategy_share_bps: int = 5_000

    def __post_init__(self) -> None:
        require_non_negative_int(self.min_lcb_edge_bps, "min_lcb_edge_bps")
        tail = require_non_negative_int(self.max_tail_loss_99_bps, "max_tail_loss_99_bps")
        if tail > BPS_DENOMINATOR:
            raise ValueError("max_tail_loss_99_bps must not exceed 10000")
        share = require_non_negative_int(
            self.max_single_strategy_share_bps,
            "max_single_strategy_share_bps",
        )
        if share > BPS_DENOMINATOR:
            raise ValueError("max_single_strategy_share_bps must not exceed 10000")


def _disqualifiers(
    track: StrategyTrackCandidate,
    *,
    reserve_deficit: int,
    config: PortfolioAllocatorConfig,
) -> tuple[str, ...]:
    reasons: list[str] = []
    if reserve_deficit > 0:
        reasons.append("portfolio_reserve_deficit")
    if track.error_budget_halted:
        reasons.append("error_or_revert_budget_halted")
    if track.reconciliation_residual_raw != 0:
        reasons.append("reconciliation_residual_nonzero")
    if track.evidence_age_seconds > track.max_evidence_age_seconds:
        reasons.append("stale_evidence")
    if track.lcb_net_edge_bps < config.min_lcb_edge_bps:
        reasons.append("negative_or_insufficient_lcb_edge")
    if track.tail_loss_99_bps > config.max_tail_loss_99_bps:
        reasons.append("tail_loss_exceeds_ceiling")
    return tuple(reasons)


def _risk_adjusted_score(track: StrategyTrackCandidate) -> int:
    risk_denom = max(
        MIN_RISK_DENOM_BPS,
        track.expected_drawdown_bps + 2 * track.tail_loss_99_bps,
    )
    return (track.lcb_net_edge_bps * BPS_DENOMINATOR) // risk_denom


class PortfolioAllocator:
    """Deterministic, fail-closed Stage 7 capital allocator."""

    def __init__(self, config: PortfolioAllocatorConfig | None = None) -> None:
        active_config = config if config is not None else PortfolioAllocatorConfig()
        if not isinstance(active_config, PortfolioAllocatorConfig):
            raise TypeError("config must be PortfolioAllocatorConfig or None")
        self.config = active_config

    def allocate(
        self,
        total_nav_usd_micros: int,
        reserves: PortfolioReserves,
        tracks: Sequence[StrategyTrackCandidate],
        domain_caps: Sequence[ExposureDomainCap] = (),
    ) -> PortfolioAllocationReport:
        """Compute deterministic per-track targets without granting execution authority."""
        require_non_negative_int(total_nav_usd_micros, "total_nav_usd_micros")
        if not isinstance(reserves, PortfolioReserves):
            raise TypeError("reserves must be PortfolioReserves")

        seen_strategy_ids: set[str] = set()
        validated_tracks: list[StrategyTrackCandidate] = []
        for idx, track in enumerate(tracks):
            if not isinstance(track, StrategyTrackCandidate):
                raise TypeError(f"tracks[{idx}] must be StrategyTrackCandidate")
            if track.strategy_id in seen_strategy_ids:
                raise ValueError(f"duplicate strategy_id: {track.strategy_id}")
            seen_strategy_ids.add(track.strategy_id)
            validated_tracks.append(track)

        cap_by_key: dict[str, ExposureDomainCap] = {}
        for idx, cap in enumerate(domain_caps):
            if not isinstance(cap, ExposureDomainCap):
                raise TypeError(f"domain_caps[{idx}] must be ExposureDomainCap")
            if cap.key in cap_by_key:
                raise ValueError(f"duplicate domain cap: {cap.key}")
            cap_by_key[cap.key] = cap

        segregated_reserves = reserves.total_usd_micros
        reserve_deficit = max(0, segregated_reserves - total_nav_usd_micros)
        deployable_budget = max(0, total_nav_usd_micros - segregated_reserves)

        used_by_domain: dict[str, int] = dict.fromkeys(sorted(cap_by_key), 0)
        for track in validated_tracks:
            for kind, domain_id in track.exposures:
                used_by_domain.setdefault(f"{kind}:{domain_id}", 0)

        disqualified_decisions: list[StrategyAllocationDecision] = []
        eligible: list[tuple[int, StrategyTrackCandidate]] = []

        for track in validated_tracks:
            reasons = _disqualifiers(
                track,
                reserve_deficit=reserve_deficit,
                config=self.config,
            )
            if reasons:
                prev = track.current_allocation_usd_micros
                action: AllocationAction = "scale_to_zero" if prev > 0 else "blocked_zero"
                disqualified_decisions.append(
                    StrategyAllocationDecision(
                        strategy_id=track.strategy_id,
                        stage=track.stage,
                        previous_usd_micros=prev,
                        target_usd_micros=0,
                        delta_usd_micros=-prev,
                        risk_adjusted_score=0,
                        action=action,
                        reasons=reasons,
                        binding_constraints=(),
                        live_authorized=track.live_authorized,
                        execution_eligible=False,
                    )
                )
            else:
                eligible.append((_risk_adjusted_score(track), track))

        eligible.sort(key=lambda item: (-item[0], item[1].strategy_id))

        remaining_deployable = deployable_budget
        single_strategy_cap = (
            deployable_budget * self.config.max_single_strategy_share_bps
        ) // BPS_DENOMINATOR

        eligible_decisions: list[StrategyAllocationDecision] = []
        for score, track in eligible:
            prev = track.current_allocation_usd_micros
            bounds: list[tuple[int, str]] = [
                (remaining_deployable, "deployable_nav_exhausted"),
                (track.capacity_usd_micros, "capacity_cap"),
                (single_strategy_cap, "single_strategy_share_cap"),
                (prev + track.max_step_up_usd_micros, "slow_scale_up_step_cap"),
            ]
            unique_domain_keys = tuple(
                dict.fromkeys(f"{kind}:{domain_id}" for kind, domain_id in track.exposures)
            )
            for domain_key in unique_domain_keys:
                domain_cap = cap_by_key.get(domain_key)
                if domain_cap is not None:
                    headroom = max(0, domain_cap.max_usd_micros - used_by_domain[domain_key])
                    bounds.append((headroom, f"domain_cap:{domain_key}"))

            target = min(bound for bound, _ in bounds)
            binding = tuple(name for bound, name in bounds if bound == target)

            if target < prev:
                action = "scale_down"
                decision_reasons: tuple[str, ...] = ("eligible_cap_scale_down",)
            elif target == prev:
                action = "hold"
                decision_reasons = ("eligible_risk_adjusted",)
            else:
                action = "scale_up"
                decision_reasons = ("eligible_risk_adjusted",)

            remaining_deployable -= target
            for domain_key in unique_domain_keys:
                used_by_domain[domain_key] += target

            eligible_decisions.append(
                StrategyAllocationDecision(
                    strategy_id=track.strategy_id,
                    stage=track.stage,
                    previous_usd_micros=prev,
                    target_usd_micros=target,
                    delta_usd_micros=target - prev,
                    risk_adjusted_score=score,
                    action=action,
                    reasons=decision_reasons,
                    binding_constraints=binding,
                    live_authorized=track.live_authorized,
                    execution_eligible=False,
                )
            )

        disqualified_decisions.sort(key=lambda d: d.strategy_id)
        decisions = tuple(eligible_decisions) + tuple(disqualified_decisions)
        allocated_total = sum(d.target_usd_micros for d in decisions)
        unallocated_cash = deployable_budget - allocated_total
        sorted_utilization = dict(sorted(used_by_domain.items()))

        payload = {
            "schema": REPORT_SCHEMA,
            "config": asdict(self.config),
            "reserves": asdict(reserves),
            "domain_caps": [asdict(cap_by_key[key]) for key in sorted(cap_by_key)],
            "total_nav_usd_micros": total_nav_usd_micros,
            "segregated_reserves_usd_micros": segregated_reserves,
            "deployable_budget_usd_micros": deployable_budget,
            "allocated_total_usd_micros": allocated_total,
            "unallocated_cash_usd_micros": unallocated_cash,
            "reserve_deficit_usd_micros": reserve_deficit,
            "domain_utilization_usd_micros": sorted_utilization,
            "decisions": [asdict(d) for d in decisions],
            "execution_eligible": False,
        }
        report_sha256 = sha256_hex(canonical_json(payload))

        return PortfolioAllocationReport(
            total_nav_usd_micros=total_nav_usd_micros,
            segregated_reserves_usd_micros=segregated_reserves,
            deployable_budget_usd_micros=deployable_budget,
            allocated_total_usd_micros=allocated_total,
            unallocated_cash_usd_micros=unallocated_cash,
            reserve_deficit_usd_micros=reserve_deficit,
            domain_utilization_usd_micros=sorted_utilization,
            decisions=decisions,
            report_sha256=report_sha256,
            execution_eligible=False,
        )
