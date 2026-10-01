"""Audit raw-replayed market coverage against the locked Phase 1 time design.

This is a dataset prerequisite, not a profitability test. It deliberately cannot
turn coverage into economic acceptance, a frozen trial or execution authority.
Callers must build each MarketWindow through the retained raw-RPC replayer.
"""

from dataclasses import asdict, dataclass, field
from itertools import pairwise

from ..models.common import canonical_json, sha256_hex
from ..models.v4 import SwapDirection
from ..shadow.market_window import MarketWindow, validate_market_successor

DAY = 86_400
PURGE_SECONDS = 3_600
MAX_CADENCE_SECONDS = 30
SEGMENTS = ("discovery", "calibration", "validation", "untouched_test")


@dataclass(frozen=True, slots=True)
class CoverageSegment:
    name: str
    windows: int
    start_block: int | None
    end_block: int | None
    span_seconds: int
    directions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DatasetCoverage:
    dataset_hash: str
    windows: int
    span_seconds: int
    segments: tuple[CoverageSegment, ...]
    coverage_passed: bool
    reasons: tuple[str, ...]
    out_of_sample_accepted: bool = field(default=False, init=False)
    execution_eligible: bool = field(default=False, init=False)

    @property
    def payload(self) -> bytes:
        return canonical_json(
            {
                "schema": "oasis.lp-dataset-coverage.v1",
                **asdict(self),
                "unresolved": [
                    "frozen_trial_and_untouched_test_seal",
                    "chain_finality_depth_and_any_longer_required_purge",
                    "paired_reference_prices_and_reconciled_inventory",
                    "evidence_derived_cost_and_edge_estimator",
                    "per_fold_direction_and_volatility_regime_coverage",
                    "all_baseline_net_excess_and_risk_acceptance",
                ],
            }
        )


def _timestamp(window: MarketWindow) -> int:
    timestamp = window.decision_block.timestamp
    if timestamp is None:
        raise ValueError("coverage needs timestamped decision blocks")
    return timestamp


def assess_dataset_coverage(windows: tuple[MarketWindow, ...]) -> DatasetCoverage:
    """Partition a replayed constant-cadence timeline 50/20/10/20, then purge.

    Boundaries round up to whole windows. Later segments discard windows whose
    lookback starts less than 3600 seconds after the preceding segment's final
    decision. No dropped window repairs a gap in the full acquired timeline.
    Duplicate decisions, changed providers, scopes or overlapping states refuse.
    """
    if not windows:
        raise ValueError("coverage needs at least one replayed market window")
    reasons: set[str] = set()
    providers = {ref.source for ref in windows[0].input_references}
    cadence: int | None = None
    for index, window in enumerate(windows):
        now = _timestamp(window)
        if len(providers) != 2 or {r.source for r in window.input_references} != providers:
            raise ValueError("dataset provider scope changed or is not a pair")
        if index:
            previous = windows[index - 1]
            validate_market_successor(previous, window)
            gap = now - _timestamp(previous)
            if gap <= 0:
                raise ValueError("dataset decision time must strictly increase")
            cadence = gap if cadence is None else cadence
            if gap > MAX_CADENCE_SECONDS or gap != cadence:
                reasons.add("decision_cadence_exceeds_30s_or_changed")
        if (window.horizon_seconds, window.max_lateness_seconds) != (300, 60):
            reasons.add("locked_markout_horizon_or_lateness_not_met")
        if not window.markouts_complete:
            reasons.add("incomplete_markouts")
        if len(window.prices) < 20:
            reasons.add("fewer_than_20_price_observations")
        times = tuple(price.block.timestamp for price in window.prices)
        if not times or any(value is None for value in times):
            raise ValueError("coverage needs timestamped price observations")
        checked = tuple(int(value) for value in times if value is not None)
        if checked[-1] != now or window.prices[-1].block != window.decision_block:
            raise ValueError("price window does not end at its decision block")
        if checked[-1] - checked[0] < 600:
            reasons.add("price_lookback_below_600s")
        if any(not 0 < right - left <= 30 for left, right in pairwise(checked)):
            reasons.add("price_observation_gap")

    count = len(windows)
    bounds = (
        0,
        (count * 50 + 99) // 100,
        (count * 70 + 99) // 100,
        (count * 80 + 99) // 100,
        count,
    )
    segments = []
    for index, name in enumerate(SEGMENTS):
        start, end = bounds[index], bounds[index + 1]
        segment = windows[start:end]
        if index and start:
            cutoff = _timestamp(windows[start - 1]) + PURGE_SECONDS
            segment = tuple(
                window
                for window in segment
                if window.prices[0].block.timestamp is not None
                and window.prices[0].block.timestamp >= cutoff
            )
        directions = tuple(
            sorted({item.value.direction.value for window in segment for item in window.matured})
        )
        span = _timestamp(segment[-1]) - _timestamp(segment[0]) if segment else 0
        segments.append(
            CoverageSegment(
                name,
                len(segment),
                segment[0].decision_block.number if segment else None,
                segment[-1].decision_block.number if segment else None,
                span,
                directions,
            )
        )
        if not segment:
            reasons.add(f"{name}_empty_after_purge")
        if set(directions) != {direction.value for direction in SwapDirection}:
            reasons.add(f"{name}_missing_swap_direction")
    if sum(segment.span_seconds for segment in segments[:3]) < 21 * DAY:
        reasons.add("development_coverage_below_21_days")
    if segments[3].span_seconds < 7 * DAY:
        reasons.add("untouched_coverage_below_7_days")
    digest = sha256_hex(
        canonical_json(
            {
                "schema": "oasis.lp-coverage-inputs.v1",
                "windows": [sha256_hex(window.payload) for window in windows],
            }
        )
    )
    return DatasetCoverage(
        digest,
        count,
        _timestamp(windows[-1]) - _timestamp(windows[0]),
        tuple(segments),
        not reasons,
        tuple(sorted(reasons)),
    )
