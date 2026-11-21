"""Configurable capital risk policy, headroom accounting, and dynamic notional sizer.

Provides typed integer-micros risk limits (`CapitalRiskConfig`), reconciled account
headroom computation (`compute_deployable_headroom`), and deterministic bounded
integer optimization (`solve_optimal_notional`) for profit-engine sizing.

All decisions fail closed and remain non-executing (`execution_eligible=False`).
"""

import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

from ..models.common import canonical_json, sha256_hex

SCHEMA_VERSION: Final[str] = "oasis.capital-risk-policy.v1"
USD_MICROS_SCALE: Final[int] = 1_000_000
BPS_DENOMINATOR: Final[int] = 10_000
MAX_CONFIG_FILE_BYTES: Final[int] = 1_000_000

DEFAULT_TOTAL_CAPITAL_USD_MICROS: Final[int] = 500_000_000
DEFAULT_MAX_DAILY_LOSS_USD_MICROS: Final[int] = 200_000_000
DEFAULT_MAX_CUMULATIVE_LOSS_USD_MICROS: Final[int] = 500_000_000
DEFAULT_RESERVE_BUFFER_BPS: Final[int] = 1_000
DEFAULT_MAX_SINGLE_CLIP_SHARE_BPS: Final[int] = 5_000
DEFAULT_MIN_CLIP_USD_MICROS: Final[int] = 10_000_000
DEFAULT_ASSUMED_TAIL_STRESS_BPS: Final[int] = 250
DEFAULT_MAX_FEED_AGE_SECONDS: Final[int] = 1_800
DEFAULT_USDC_PRICE_USD_MICROS: Final[int] = 1_000_000

_ALLOWED_CONFIG_KEYS: Final[frozenset[str]] = frozenset(
    {
        "schema_version",
        "total_capital_usd_micros",
        "max_daily_loss_usd_micros",
        "max_cumulative_loss_usd_micros",
        "reserve_buffer_bps",
        "max_single_clip_share_bps",
        "min_clip_usd_micros",
        "assumed_tail_stress_bps",
        "max_feed_age_seconds",
        "usdc_price_usd_micros",
    }
)
_KNOT_PERCENTS: Final[tuple[int, ...]] = (5, 10, 20, 35, 50, 75, 100)
_MAX_REFINEMENT_ITERS: Final[int] = 12


def _require_int(value: object, name: str) -> int:
    if type(value) is not int:
        raise ValueError(f"{name} must be a strict integer (not bool)")
    return value


def _require_nonneg_int(value: object, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _require_pos_int(value: object, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _require_bool(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise TypeError(f"{name} must be a boolean")
    return value


def _require_finite_float(value: object, name: str) -> float:
    if type(value) is bool or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON constant not permitted: {value}")


def _finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("non-finite float not permitted")
    return parsed


@dataclass(frozen=True, slots=True)
class CapitalRiskConfig:
    """Configurable integer-micros capital, loss, reserve, and clip-sizing policy."""

    total_capital_usd_micros: int = DEFAULT_TOTAL_CAPITAL_USD_MICROS
    max_daily_loss_usd_micros: int = DEFAULT_MAX_DAILY_LOSS_USD_MICROS
    max_cumulative_loss_usd_micros: int = DEFAULT_MAX_CUMULATIVE_LOSS_USD_MICROS
    reserve_buffer_bps: int = DEFAULT_RESERVE_BUFFER_BPS
    max_single_clip_share_bps: int = DEFAULT_MAX_SINGLE_CLIP_SHARE_BPS
    min_clip_usd_micros: int = DEFAULT_MIN_CLIP_USD_MICROS
    assumed_tail_stress_bps: int = DEFAULT_ASSUMED_TAIL_STRESS_BPS
    max_feed_age_seconds: int = DEFAULT_MAX_FEED_AGE_SECONDS
    usdc_price_usd_micros: int = DEFAULT_USDC_PRICE_USD_MICROS

    def __post_init__(self) -> None:
        _require_pos_int(self.total_capital_usd_micros, "total_capital_usd_micros")
        _require_pos_int(self.max_daily_loss_usd_micros, "max_daily_loss_usd_micros")
        _require_pos_int(self.max_cumulative_loss_usd_micros, "max_cumulative_loss_usd_micros")
        _require_nonneg_int(self.reserve_buffer_bps, "reserve_buffer_bps")
        _require_pos_int(self.max_single_clip_share_bps, "max_single_clip_share_bps")
        _require_pos_int(self.min_clip_usd_micros, "min_clip_usd_micros")
        _require_pos_int(self.assumed_tail_stress_bps, "assumed_tail_stress_bps")
        _require_pos_int(self.max_feed_age_seconds, "max_feed_age_seconds")
        _require_pos_int(self.usdc_price_usd_micros, "usdc_price_usd_micros")

        if not (
            0
            < self.max_daily_loss_usd_micros
            <= self.max_cumulative_loss_usd_micros
            <= self.total_capital_usd_micros
        ):
            raise ValueError(
                "must satisfy 0 < max_daily_loss_usd_micros <= "
                "max_cumulative_loss_usd_micros <= total_capital_usd_micros"
            )
        if not 0 <= self.reserve_buffer_bps <= 9_500:
            raise ValueError("reserve_buffer_bps must be in [0, 9500]")
        if not 1 <= self.max_single_clip_share_bps <= BPS_DENOMINATOR:
            raise ValueError("max_single_clip_share_bps must be in [1, 10000]")
        if not 0 < self.min_clip_usd_micros <= self.total_capital_usd_micros:
            raise ValueError("min_clip_usd_micros must be in (0, total_capital_usd_micros]")
        if not 1 <= self.assumed_tail_stress_bps <= BPS_DENOMINATOR:
            raise ValueError("assumed_tail_stress_bps must be in [1, 10000]")

    def to_dict(self) -> dict[str, Any]:
        """Serialize policy configuration into canonical dictionary form."""
        return {
            "schema_version": SCHEMA_VERSION,
            "total_capital_usd_micros": self.total_capital_usd_micros,
            "max_daily_loss_usd_micros": self.max_daily_loss_usd_micros,
            "max_cumulative_loss_usd_micros": self.max_cumulative_loss_usd_micros,
            "reserve_buffer_bps": self.reserve_buffer_bps,
            "max_single_clip_share_bps": self.max_single_clip_share_bps,
            "min_clip_usd_micros": self.min_clip_usd_micros,
            "assumed_tail_stress_bps": self.assumed_tail_stress_bps,
            "max_feed_age_seconds": self.max_feed_age_seconds,
            "usdc_price_usd_micros": self.usdc_price_usd_micros,
        }

    @property
    def config_sha256(self) -> str:
        """Return canonical SHA-256 hex digest of this policy configuration."""
        return sha256_hex(canonical_json(self.to_dict()))

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> CapitalRiskConfig:
        """Build a CapitalRiskConfig from a mapping, rejecting unknown keys."""
        if not isinstance(data, Mapping):
            raise TypeError("capital risk config data must be a mapping")
        for key in data:
            if not isinstance(key, str):
                raise ValueError("capital risk config keys must be strings")
        unknown = set(data.keys()) - _ALLOWED_CONFIG_KEYS
        if unknown:
            raise ValueError(f"unknown capital risk config key(s): {', '.join(sorted(unknown))}")

        if "schema_version" in data:
            schema_version = data["schema_version"]
            if not isinstance(schema_version, str) or schema_version != SCHEMA_VERSION:
                raise ValueError(
                    f"unsupported schema_version: {schema_version!r} (expected {SCHEMA_VERSION!r})"
                )

        return cls(
            total_capital_usd_micros=_require_int(
                data.get("total_capital_usd_micros", DEFAULT_TOTAL_CAPITAL_USD_MICROS),
                "total_capital_usd_micros",
            ),
            max_daily_loss_usd_micros=_require_int(
                data.get("max_daily_loss_usd_micros", DEFAULT_MAX_DAILY_LOSS_USD_MICROS),
                "max_daily_loss_usd_micros",
            ),
            max_cumulative_loss_usd_micros=_require_int(
                data.get(
                    "max_cumulative_loss_usd_micros",
                    DEFAULT_MAX_CUMULATIVE_LOSS_USD_MICROS,
                ),
                "max_cumulative_loss_usd_micros",
            ),
            reserve_buffer_bps=_require_int(
                data.get("reserve_buffer_bps", DEFAULT_RESERVE_BUFFER_BPS),
                "reserve_buffer_bps",
            ),
            max_single_clip_share_bps=_require_int(
                data.get("max_single_clip_share_bps", DEFAULT_MAX_SINGLE_CLIP_SHARE_BPS),
                "max_single_clip_share_bps",
            ),
            min_clip_usd_micros=_require_int(
                data.get("min_clip_usd_micros", DEFAULT_MIN_CLIP_USD_MICROS),
                "min_clip_usd_micros",
            ),
            assumed_tail_stress_bps=_require_int(
                data.get("assumed_tail_stress_bps", DEFAULT_ASSUMED_TAIL_STRESS_BPS),
                "assumed_tail_stress_bps",
            ),
            max_feed_age_seconds=_require_int(
                data.get("max_feed_age_seconds", DEFAULT_MAX_FEED_AGE_SECONDS),
                "max_feed_age_seconds",
            ),
            usdc_price_usd_micros=_require_int(
                data.get("usdc_price_usd_micros", DEFAULT_USDC_PRICE_USD_MICROS),
                "usdc_price_usd_micros",
            ),
        )

    def usd_micros_to_usdc_raw(self, usd_micros: int) -> int:
        """Convert integer USD micros to floored 6-decimal USDC raw units."""
        clean_usd = _require_nonneg_int(usd_micros, "usd_micros")
        return (clean_usd * USD_MICROS_SCALE) // self.usdc_price_usd_micros

    def usdc_raw_to_usd_micros(self, usdc_raw: int) -> int:
        """Convert 6-decimal USDC raw units to floored integer USD micros."""
        clean_raw = _require_nonneg_int(usdc_raw, "usdc_raw")
        return (clean_raw * self.usdc_price_usd_micros) // USD_MICROS_SCALE


def load_capital_risk_config(path: Path) -> CapitalRiskConfig:
    """Load and validate a CapitalRiskConfig JSON file (refusing symlinks & > 1MB)."""
    if not isinstance(path, Path):
        if isinstance(path, str):
            path = Path(path)
        else:
            raise TypeError("config path must be a Path")
    if path.is_symlink():
        raise ValueError(f"refusing symlink capital risk config path: {path}")
    if not path.is_file():
        raise FileNotFoundError(f"capital risk config file not found: {path}")
    if path.stat().st_size > MAX_CONFIG_FILE_BYTES:
        raise ValueError(
            f"capital risk config exceeds maximum byte limit ({MAX_CONFIG_FILE_BYTES})"
        )
    raw = path.read_bytes()
    if len(raw) > MAX_CONFIG_FILE_BYTES:
        raise ValueError(
            f"capital risk config exceeds maximum byte limit ({MAX_CONFIG_FILE_BYTES})"
        )
    decoded = json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=_unique_object,
        parse_constant=_reject_constant,
        parse_float=_finite_float,
    )
    if not isinstance(decoded, dict):
        raise ValueError("capital risk config JSON root must be an object")
    return CapitalRiskConfig.from_dict(decoded)


@dataclass(frozen=True, slots=True)
class AccountCapitalSnapshot:
    """Reconciled account state snapshot in USD micros for headroom evaluation."""

    cash_available_usd_micros: int
    locked_inventory_usd_micros: int = 0
    pending_commitments_usd_micros: int = 0
    realized_daily_loss_usd_micros: int = 0
    unrealized_drawdown_usd_micros: int = 0
    cumulative_loss_usd_micros: int = 0
    unresolved_residuals: int = 0
    halted: bool = False

    def __post_init__(self) -> None:
        _require_nonneg_int(self.cash_available_usd_micros, "cash_available_usd_micros")
        _require_nonneg_int(self.locked_inventory_usd_micros, "locked_inventory_usd_micros")
        _require_nonneg_int(
            self.pending_commitments_usd_micros,
            "pending_commitments_usd_micros",
        )
        _require_nonneg_int(
            self.realized_daily_loss_usd_micros,
            "realized_daily_loss_usd_micros",
        )
        _require_nonneg_int(
            self.unrealized_drawdown_usd_micros,
            "unrealized_drawdown_usd_micros",
        )
        _require_nonneg_int(self.cumulative_loss_usd_micros, "cumulative_loss_usd_micros")
        _require_nonneg_int(self.unresolved_residuals, "unresolved_residuals")
        _require_bool(self.halted, "halted")


@dataclass(frozen=True, slots=True)
class HeadroomAllocation:
    """Fail-closed capital headroom and single-clip envelope derived from risk policy."""

    deployable_capital_usd_micros: int
    reserve_floor_usd_micros: int
    single_clip_cap_usd_micros: int
    remaining_daily_loss_usd_micros: int
    remaining_cumulative_loss_usd_micros: int
    loss_throttled_cap_usd_micros: int
    max_clip_usd_micros: int
    min_clip_usd_micros: int
    can_deploy: bool
    block_reason: str | None
    execution_eligible: Literal[False] = False

    def __post_init__(self) -> None:
        if self.execution_eligible is not False:
            raise ValueError("HeadroomAllocation cannot claim execution_eligible=True")
        _require_nonneg_int(
            self.deployable_capital_usd_micros,
            "deployable_capital_usd_micros",
        )
        _require_nonneg_int(self.reserve_floor_usd_micros, "reserve_floor_usd_micros")
        _require_nonneg_int(self.single_clip_cap_usd_micros, "single_clip_cap_usd_micros")
        _require_nonneg_int(
            self.remaining_daily_loss_usd_micros,
            "remaining_daily_loss_usd_micros",
        )
        _require_nonneg_int(
            self.remaining_cumulative_loss_usd_micros,
            "remaining_cumulative_loss_usd_micros",
        )
        _require_nonneg_int(self.loss_throttled_cap_usd_micros, "loss_throttled_cap_usd_micros")
        _require_nonneg_int(self.max_clip_usd_micros, "max_clip_usd_micros")
        _require_nonneg_int(self.min_clip_usd_micros, "min_clip_usd_micros")
        _require_bool(self.can_deploy, "can_deploy")
        if self.block_reason is not None and (
            not isinstance(self.block_reason, str) or not self.block_reason.strip()
        ):
            raise ValueError("block_reason must be None or a non-empty string")
        if self.can_deploy and self.block_reason is not None:
            raise ValueError("can_deploy=True requires block_reason=None")
        if not self.can_deploy and (self.block_reason is None or self.max_clip_usd_micros != 0):
            raise ValueError("blocked headroom must specify block_reason and max_clip_usd_micros=0")

    def to_dict(self) -> dict[str, Any]:
        """Serialize allocation envelope to a JSON-compatible dictionary."""
        return {
            "deployable_capital_usd_micros": self.deployable_capital_usd_micros,
            "reserve_floor_usd_micros": self.reserve_floor_usd_micros,
            "single_clip_cap_usd_micros": self.single_clip_cap_usd_micros,
            "remaining_daily_loss_usd_micros": self.remaining_daily_loss_usd_micros,
            "remaining_cumulative_loss_usd_micros": self.remaining_cumulative_loss_usd_micros,
            "loss_throttled_cap_usd_micros": self.loss_throttled_cap_usd_micros,
            "max_clip_usd_micros": self.max_clip_usd_micros,
            "min_clip_usd_micros": self.min_clip_usd_micros,
            "can_deploy": self.can_deploy,
            "block_reason": self.block_reason,
            "execution_eligible": self.execution_eligible,
        }


def compute_deployable_headroom(
    config: CapitalRiskConfig,
    snapshot: AccountCapitalSnapshot,
) -> HeadroomAllocation:
    """Compute fail-closed deployable capital and single-clip headroom."""
    if not isinstance(config, CapitalRiskConfig):
        raise TypeError("config must be a CapitalRiskConfig")
    if not isinstance(snapshot, AccountCapitalSnapshot):
        raise TypeError("snapshot must be an AccountCapitalSnapshot")

    effective_capital = min(snapshot.cash_available_usd_micros, config.total_capital_usd_micros)
    reserve_floor = (
        config.total_capital_usd_micros * config.reserve_buffer_bps + BPS_DENOMINATOR - 1
    ) // BPS_DENOMINATOR
    deployable_capital = max(
        0,
        effective_capital
        - reserve_floor
        - snapshot.locked_inventory_usd_micros
        - snapshot.pending_commitments_usd_micros,
    )
    single_clip_cap = (deployable_capital * config.max_single_clip_share_bps) // BPS_DENOMINATOR
    effective_daily_loss = (
        snapshot.realized_daily_loss_usd_micros + snapshot.unrealized_drawdown_usd_micros
    )
    remaining_daily = max(0, config.max_daily_loss_usd_micros - effective_daily_loss)
    remaining_cum = max(
        0,
        config.max_cumulative_loss_usd_micros - snapshot.cumulative_loss_usd_micros,
    )
    loss_throttled = (
        min(remaining_daily, remaining_cum) * BPS_DENOMINATOR
    ) // config.assumed_tail_stress_bps
    raw_max_clip = min(deployable_capital, single_clip_cap, loss_throttled)

    block_reason: str | None = None
    if snapshot.halted:
        block_reason = "authority_halted"
    elif snapshot.unresolved_residuals > 0:
        block_reason = "unresolved_residuals"
    elif remaining_daily <= 0:
        block_reason = "daily_loss_exhausted"
    elif remaining_cum <= 0:
        block_reason = "cumulative_loss_exhausted"
    elif raw_max_clip < config.min_clip_usd_micros:
        block_reason = "insufficient_deployable_headroom"

    if block_reason is not None:
        max_clip = 0
        can_deploy = False
    else:
        max_clip = raw_max_clip
        can_deploy = True

    return HeadroomAllocation(
        deployable_capital_usd_micros=deployable_capital,
        reserve_floor_usd_micros=reserve_floor,
        single_clip_cap_usd_micros=single_clip_cap,
        remaining_daily_loss_usd_micros=remaining_daily,
        remaining_cumulative_loss_usd_micros=remaining_cum,
        loss_throttled_cap_usd_micros=loss_throttled,
        max_clip_usd_micros=max_clip,
        min_clip_usd_micros=config.min_clip_usd_micros,
        can_deploy=can_deploy,
        block_reason=block_reason,
        execution_eligible=False,
    )


@dataclass(frozen=True, slots=True)
class CapacityPoint:
    """Single evaluated notional point on an execution profit/edge curve."""

    notional_raw: int
    net_profit_usd_micros: int
    net_edge_bps: float

    def __post_init__(self) -> None:
        _require_pos_int(self.notional_raw, "notional_raw")
        _require_int(self.net_profit_usd_micros, "net_profit_usd_micros")
        object.__setattr__(
            self,
            "net_edge_bps",
            _require_finite_float(self.net_edge_bps, "net_edge_bps"),
        )


@dataclass(frozen=True, slots=True)
class CapacityOptimizationResult:
    """Result of deterministic integer notional optimization."""

    optimal_notional_raw: int | None
    best_net_profit_usd_micros: int | None
    best_net_edge_bps: float | None
    points_evaluated: tuple[CapacityPoint, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.points_evaluated, tuple) or any(
            not isinstance(pt, CapacityPoint) for pt in self.points_evaluated
        ):
            raise TypeError("points_evaluated must be a tuple of CapacityPoint instances")
        scalars = (
            self.optimal_notional_raw,
            self.best_net_profit_usd_micros,
            self.best_net_edge_bps,
        )
        if all(val is None for val in scalars):
            return
        if any(val is None for val in scalars):
            raise ValueError("optimal_notional_raw and best_* fields must all be None or all set")
        _require_pos_int(self.optimal_notional_raw, "optimal_notional_raw")
        _require_int(self.best_net_profit_usd_micros, "best_net_profit_usd_micros")
        object.__setattr__(
            self,
            "best_net_edge_bps",
            _require_finite_float(self.best_net_edge_bps, "best_net_edge_bps"),
        )


def solve_optimal_notional(
    *,
    min_notional_raw: int,
    max_notional_raw: int,
    evaluate_fn: Callable[[int], tuple[int, float] | None],
    extra_knots: Sequence[int] = (),
) -> CapacityOptimizationResult:
    """Find net-profit-maximizing integer notional in [min_notional_raw, max_notional_raw]."""
    _require_pos_int(min_notional_raw, "min_notional_raw")
    _require_pos_int(max_notional_raw, "max_notional_raw")
    if min_notional_raw > max_notional_raw:
        raise ValueError("min_notional_raw must be <= max_notional_raw")
    if not callable(evaluate_fn):
        raise TypeError("evaluate_fn must be callable")

    raw_knots = [min_notional_raw]
    raw_knots.extend((max_notional_raw * pct) // 100 for pct in _KNOT_PERCENTS)
    for knot in extra_knots:
        _require_int(knot, "extra_knots entry")
        raw_knots.append(knot)
    knots = sorted({q for q in raw_knots if min_notional_raw <= q <= max_notional_raw})

    cache: dict[int, CapacityPoint | None] = {}
    evaluated: list[CapacityPoint] = []

    def eval_point(q: int) -> CapacityPoint | None:
        if q in cache:
            return cache[q]
        outcome = evaluate_fn(q)
        if outcome is None:
            cache[q] = None
            return None
        if not isinstance(outcome, tuple) or len(outcome) != 2:
            raise TypeError("evaluate_fn must return None or (net_profit_usd_micros, net_edge_bps)")
        net_profit_usd_micros, net_edge_bps = outcome
        point = CapacityPoint(
            notional_raw=q,
            net_profit_usd_micros=net_profit_usd_micros,
            net_edge_bps=net_edge_bps,
        )
        cache[q] = point
        evaluated.append(point)
        return point

    knot_results = [(idx, q, eval_point(q)) for idx, q in enumerate(knots)]
    feasible_knots = [(idx, pt) for idx, _, pt in knot_results if pt is not None]
    if not feasible_knots:
        return CapacityOptimizationResult(
            optimal_notional_raw=None,
            best_net_profit_usd_micros=None,
            best_net_edge_bps=None,
            points_evaluated=tuple(evaluated),
        )

    best_idx, _ = max(
        feasible_knots,
        key=lambda item: (item[1].net_profit_usd_micros, -item[1].notional_raw),
    )
    lo = knots[best_idx - 1] if best_idx > 0 else knots[0]
    hi = knots[best_idx + 1] if best_idx + 1 < len(knots) else knots[-1]

    for _ in range(_MAX_REFINEMENT_ITERS):
        if hi - lo <= 2:
            for q in range(lo, hi + 1):
                eval_point(q)
            break
        step = max(1, (hi - lo) // 3)
        m1 = lo + step
        m2 = hi - step
        p1 = eval_point(m1)
        p2 = eval_point(m2)
        if p1 is None and p2 is None:
            current_best = max(
                evaluated,
                key=lambda pt: (pt.net_profit_usd_micros, -pt.notional_raw),
            )
            if current_best.notional_raw < m1:
                hi = m1
            elif current_best.notional_raw > m2:
                lo = m2
            else:
                lo = m1
                hi = m2
        elif p1 is None:
            lo = m1
        elif p2 is None:
            hi = m2
        elif (p1.net_profit_usd_micros, -p1.notional_raw) < (
            p2.net_profit_usd_micros,
            -p2.notional_raw,
        ):
            lo = m1
        else:
            hi = m2
    else:
        if hi - lo > 2:
            eval_point((lo + hi) // 2)

    winner = max(
        evaluated,
        key=lambda pt: (pt.net_profit_usd_micros, -pt.notional_raw),
    )
    return CapacityOptimizationResult(
        optimal_notional_raw=winner.notional_raw,
        best_net_profit_usd_micros=winner.net_profit_usd_micros,
        best_net_edge_bps=winner.net_edge_bps,
        points_evaluated=tuple(evaluated),
    )


__all__ = [
    "BPS_DENOMINATOR",
    "DEFAULT_ASSUMED_TAIL_STRESS_BPS",
    "DEFAULT_MAX_CUMULATIVE_LOSS_USD_MICROS",
    "DEFAULT_MAX_DAILY_LOSS_USD_MICROS",
    "DEFAULT_MAX_FEED_AGE_SECONDS",
    "DEFAULT_MAX_SINGLE_CLIP_SHARE_BPS",
    "DEFAULT_MIN_CLIP_USD_MICROS",
    "DEFAULT_RESERVE_BUFFER_BPS",
    "DEFAULT_TOTAL_CAPITAL_USD_MICROS",
    "DEFAULT_USDC_PRICE_USD_MICROS",
    "MAX_CONFIG_FILE_BYTES",
    "SCHEMA_VERSION",
    "USD_MICROS_SCALE",
    "AccountCapitalSnapshot",
    "CapacityOptimizationResult",
    "CapacityPoint",
    "CapitalRiskConfig",
    "HeadroomAllocation",
    "compute_deployable_headroom",
    "load_capital_risk_config",
    "solve_optimal_notional",
]
