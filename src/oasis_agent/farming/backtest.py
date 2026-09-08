"""Evidence-bound factorial LP replay and walk-forward evaluation.

The engine computes R00/R01/R10/R11 from one ordered stream. Observations are
increments reconstructed by an upstream ledger; this module does not infer
missing cash flows, prices, routing, or costs. Incomplete evidence and any
unexplained residual fail closed. Results never authorize execution.
"""

from dataclasses import asdict, dataclass, field

from ..models.common import BlockRef, EvidenceRef, canonical_json, sha256_hex
from .counterfactual import (
    CounterfactualRun,
    FactorialLpComparison,
    FeePolicyKind,
    RangePolicyKind,
    compare_lp_factorial,
)

PPM = 1_000_000
ENGINE_VERSION = "lp-factorial-replay-v1"


def _integer(value: int, name: str, *, nonnegative: bool = False) -> None:
    if type(value) is not int:
        raise TypeError(f"{name} must be an integer quote base-unit amount")
    if nonnegative and value < 0:
        raise ValueError(f"{name} must be nonnegative")


@dataclass(frozen=True, slots=True)
class LpReplayObservation:
    """One event-time ordered, already-valued ledger increment."""

    block: BlockRef
    transaction_index: int
    log_index: int
    gross_routable_volume1: int
    hold_pnl1: int
    fixed_inventory_pnl1: int
    active_inventory_pnl1: int
    static_fee_pips: int
    dynamic_fee_pips: int
    fixed_liquidity_share_ppm: int
    active_liquidity_share_ppm: int
    elasticity_ppm_per_fee_pip: int
    gas_cost_active1: int
    rebalance_cost_active1: int
    adverse_selection_fixed1: int
    adverse_selection_active1: int
    other_cost1: int
    failed_tx_cost1: int
    opportunity_cost1: int
    unexplained_residual1: int
    evidence: tuple[EvidenceRef, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.block, BlockRef):
            raise TypeError("block must be a BlockRef")
        for name in ("transaction_index", "log_index"):
            _integer(getattr(self, name), name, nonnegative=True)
        for name in ("hold_pnl1", "fixed_inventory_pnl1", "active_inventory_pnl1"):
            _integer(getattr(self, name), name)
        for name in (
            "gross_routable_volume1",
            "static_fee_pips",
            "dynamic_fee_pips",
            "fixed_liquidity_share_ppm",
            "active_liquidity_share_ppm",
            "elasticity_ppm_per_fee_pip",
            "gas_cost_active1",
            "rebalance_cost_active1",
            "adverse_selection_fixed1",
            "adverse_selection_active1",
            "other_cost1",
            "failed_tx_cost1",
            "opportunity_cost1",
        ):
            _integer(getattr(self, name), name, nonnegative=True)
        _integer(self.unexplained_residual1, "unexplained_residual1")
        for name in ("static_fee_pips", "dynamic_fee_pips"):
            if getattr(self, name) > PPM:
                raise ValueError(f"{name} exceeds the v4 fee maximum")
        for name in (
            "fixed_liquidity_share_ppm",
            "active_liquidity_share_ppm",
            "elasticity_ppm_per_fee_pip",
        ):
            if getattr(self, name) > PPM:
                raise ValueError(f"{name} exceeds one million ppm")
        if not self.evidence:
            raise ValueError("observation requires retained evidence")
        sources: set[str] = set()
        identities: set[tuple[str, str]] = set()
        for reference in self.evidence:
            if (
                reference.block != self.block
                or not reference.request_hash
                or not reference.raw_response_hash
                or not reference.interpretation_hash
            ):
                raise ValueError("observation evidence must be complete and pinned to its block")
            sources.add(reference.source)
            identities.add((reference.content_hash, reference.interpretation_hash))
        if len(sources) < 2 or len(identities) != 1:
            raise ValueError("observation requires agreeing evidence from two distinct providers")

    @property
    def order_key(self) -> tuple[int, int, int]:
        return self.block.number, self.transaction_index, self.log_index


@dataclass(frozen=True, slots=True)
class WalkForwardSpec:
    calibration_size: int
    evaluation_size: int
    purge_size: int
    minimum_folds: int

    def __post_init__(self) -> None:
        for name in ("calibration_size", "evaluation_size", "purge_size", "minimum_folds"):
            _integer(getattr(self, name), name, nonnegative=True)
        if self.calibration_size == 0 or self.evaluation_size == 0 or self.minimum_folds == 0:
            raise ValueError("calibration, evaluation and minimum folds must be positive")


@dataclass(frozen=True, slots=True)
class ExperimentLock:
    experiment_id: str
    dataset_hash: str
    policy_version: str
    flow_response_model: str
    initial_capital1: int
    trial_number: int
    maximum_trials: int
    minimum_evaluation_events: int
    minimum_r11_excess1: int
    maximum_drawdown1: int
    maximum_expected_shortfall1: int
    tail_fraction_ppm: int
    locked_before_block: int
    evidence: tuple[EvidenceRef, ...]

    def __post_init__(self) -> None:
        if not self.experiment_id or not self.policy_version or not self.flow_response_model:
            raise ValueError("experiment identity and versioned assumptions are required")
        if len(self.dataset_hash) != 64:
            raise ValueError("dataset hash must be a SHA-256 hex digest")
        try:
            int(self.dataset_hash, 16)
        except ValueError as exc:
            raise ValueError("dataset hash must be a SHA-256 hex digest") from exc
        for name in (
            "initial_capital1",
            "trial_number",
            "maximum_trials",
            "minimum_evaluation_events",
            "maximum_drawdown1",
            "maximum_expected_shortfall1",
            "tail_fraction_ppm",
            "locked_before_block",
        ):
            _integer(getattr(self, name), name, nonnegative=True)
        _integer(self.minimum_r11_excess1, "minimum_r11_excess1")
        if self.initial_capital1 == 0 or self.trial_number == 0 or self.maximum_trials == 0:
            raise ValueError("capital and trial counters must be positive")
        if self.minimum_evaluation_events == 0:
            raise ValueError("minimum evaluation event count must be positive")
        if (
            self.maximum_drawdown1 > self.initial_capital1
            or self.maximum_expected_shortfall1 > self.initial_capital1
        ):
            raise ValueError("loss limits cannot exceed initial capital")
        if self.trial_number > self.maximum_trials:
            raise ValueError("trial ledger exceeds its precommitted maximum")
        if not 0 < self.tail_fraction_ppm <= PPM:
            raise ValueError("tail fraction must be in (0, 1]")
        if not self.evidence:
            raise ValueError("experiment lock requires retained evidence")
        for reference in self.evidence:
            if (
                reference.kind != "experiment-lock"
                or reference.block is None
                or reference.block.number > self.locked_before_block
                or not reference.request_hash
                or not reference.raw_response_hash
                or not reference.interpretation_hash
            ):
                raise ValueError("experiment lock evidence is incomplete or not precommitted")


@dataclass(frozen=True, slots=True)
class EvaluationMetrics:
    sample_count: int
    fold_count: int
    r11_excess_lower_bound1: int
    r11_max_drawdown1: int
    r11_expected_shortfall1: int


@dataclass(frozen=True, slots=True)
class BacktestResult:
    comparison: FactorialLpComparison
    metrics: EvaluationMetrics
    accepted: bool
    reasons: tuple[str, ...]
    evaluation_order_keys: tuple[tuple[int, int, int], ...]
    execution_eligible: bool = field(default=False, init=False)


def observation_dataset_hash(observations: tuple[LpReplayObservation, ...]) -> str:
    """Bind an experiment lock to the exact ordered observation payload."""
    if not observations:
        raise ValueError("dataset hash requires observations")
    return sha256_hex(
        canonical_json(
            {
                "schema": "oasis.lp-replay-observations.v1",
                "observations": [asdict(item) for item in observations],
            }
        )
    )


def _fee_income(observation: LpReplayObservation, dynamic: bool, active: bool) -> int:
    fee = observation.dynamic_fee_pips if dynamic else observation.static_fee_pips
    share = (
        observation.active_liquidity_share_ppm if active else observation.fixed_liquidity_share_ppm
    )
    fee_premium = max(0, fee - observation.static_fee_pips)
    retained_ppm = max(0, PPM - fee_premium * observation.elasticity_ppm_per_fee_pip)
    routed = observation.gross_routable_volume1 * retained_ppm // PPM
    return (routed * fee // PPM) * share // PPM


def _event_net(observation: LpReplayObservation, dynamic: bool, active: bool) -> int:
    inventory = observation.active_inventory_pnl1 if active else observation.fixed_inventory_pnl1
    adverse = (
        observation.adverse_selection_active1 if active else observation.adverse_selection_fixed1
    )
    operating = observation.gas_cost_active1 + observation.rebalance_cost_active1 if active else 0
    return (
        _fee_income(observation, dynamic, active)
        + inventory
        - adverse
        - operating
        - observation.other_cost1
        - observation.failed_tx_cost1
        - observation.opportunity_cost1
    )


def _drawdown(increments: tuple[int, ...]) -> int:
    value = peak = 0
    worst = 0
    for increment in increments:
        value += increment
        peak = max(peak, value)
        worst = max(worst, peak - value)
    return worst


def _expected_shortfall(increments: tuple[int, ...], tail_fraction_ppm: int) -> int:
    losses = sorted((max(0, -value) for value in increments), reverse=True)
    count = max(1, (len(losses) * tail_fraction_ppm + PPM - 1) // PPM)
    return sum(losses[:count]) // count


def backtest_strategy(
    observations: tuple[LpReplayObservation, ...],
    experiment: ExperimentLock,
    walk_forward: WalkForwardSpec,
) -> BacktestResult:
    """Run a locked four-cell evaluation over purged walk-forward folds."""
    if not observations:
        raise ValueError("backtest requires observations")
    ordered = tuple(sorted(observations, key=lambda item: item.order_key))
    if ordered != observations or len({item.order_key for item in ordered}) != len(ordered):
        raise ValueError("observations must be uniquely event-time ordered")
    if observation_dataset_hash(ordered) != experiment.dataset_hash:
        raise ValueError("observations do not match the precommitted dataset hash")
    chain_id = ordered[0].block.chain_id
    if any(item.block.chain_id != chain_id for item in ordered):
        raise ValueError("backtest cannot cross chains")
    if ordered[0].block.number <= experiment.locked_before_block:
        raise ValueError("experiment lock must predate every observation")
    if any(
        reference.block is None or reference.block.chain_id != chain_id
        for reference in experiment.evidence
    ):
        raise ValueError("experiment lock evidence belongs to another chain")
    if any(item.unexplained_residual1 != 0 for item in ordered):
        raise ValueError("unexplained accounting residual blocks evaluation")

    evaluation: list[LpReplayObservation] = []
    fold_totals: list[int] = []
    cursor = 0
    width = walk_forward.calibration_size + walk_forward.purge_size + walk_forward.evaluation_size
    while cursor + width <= len(ordered):
        start = cursor + walk_forward.calibration_size + walk_forward.purge_size
        fold = ordered[start : start + walk_forward.evaluation_size]
        evaluation.extend(fold)
        fold_totals.append(sum(_event_net(item, True, True) - item.hold_pnl1 for item in fold))
        cursor = start + walk_forward.evaluation_size
    if len(fold_totals) < walk_forward.minimum_folds:
        raise ValueError("insufficient observations for precommitted walk-forward folds")

    evaluation_tuple = tuple(evaluation)
    start_block, end_block = evaluation_tuple[0].block, evaluation_tuple[-1].block
    if start_block.number >= end_block.number:
        raise ValueError("evaluation must span an increasing block window")
    refs = tuple(reference for item in evaluation_tuple for reference in item.evidence)
    runs: list[CounterfactualRun] = []
    for fee_policy in FeePolicyKind:
        for range_policy in RangePolicyKind:
            dynamic = fee_policy is FeePolicyKind.DYNAMIC
            active = range_policy is RangePolicyKind.ACTIVE
            runs.append(
                CounterfactualRun(
                    fee_policy=fee_policy,
                    range_policy=range_policy,
                    start_block=start_block,
                    end_block=end_block,
                    dataset_hash=experiment.dataset_hash,
                    engine_version=ENGINE_VERSION,
                    policy_version=experiment.policy_version,
                    initial_capital1=experiment.initial_capital1,
                    fee_income1=sum(
                        _fee_income(item, dynamic, active) for item in evaluation_tuple
                    ),
                    incentive_income1=0,
                    inventory_pnl1=sum(
                        item.active_inventory_pnl1 if active else item.fixed_inventory_pnl1
                        for item in evaluation_tuple
                    ),
                    gas_cost1=(
                        sum(item.gas_cost_active1 for item in evaluation_tuple) if active else 0
                    ),
                    rebalance_cost1=(
                        sum(item.rebalance_cost_active1 for item in evaluation_tuple)
                        if active
                        else 0
                    ),
                    adverse_selection_cost1=sum(
                        item.adverse_selection_active1 if active else item.adverse_selection_fixed1
                        for item in evaluation_tuple
                    ),
                    other_cost1=sum(item.other_cost1 for item in evaluation_tuple),
                    failed_tx_cost1=sum(item.failed_tx_cost1 for item in evaluation_tuple),
                    opportunity_cost1=sum(item.opportunity_cost1 for item in evaluation_tuple),
                    hold_net_pnl1=sum(item.hold_pnl1 for item in evaluation_tuple),
                    flow_response_model=experiment.flow_response_model,
                    evidence=refs + experiment.evidence,
                )
            )
    comparison = compare_lp_factorial(tuple(runs))
    r11_increments = tuple(_event_net(item, True, True) for item in evaluation_tuple)
    metrics = EvaluationMetrics(
        sample_count=len(evaluation_tuple),
        fold_count=len(fold_totals),
        r11_excess_lower_bound1=min(fold_totals),
        r11_max_drawdown1=_drawdown(r11_increments),
        r11_expected_shortfall1=_expected_shortfall(r11_increments, experiment.tail_fraction_ppm),
    )
    reasons: list[str] = []
    if metrics.sample_count < experiment.minimum_evaluation_events:
        reasons.append("evaluation sample count below precommitted minimum")
    if metrics.r11_excess_lower_bound1 < experiment.minimum_r11_excess1:
        reasons.append("R11 fold lower bound below precommitted excess threshold")
    if metrics.r11_max_drawdown1 > experiment.maximum_drawdown1:
        reasons.append("R11 drawdown exceeds precommitted limit")
    if metrics.r11_expected_shortfall1 > experiment.maximum_expected_shortfall1:
        reasons.append("R11 expected shortfall exceeds precommitted limit")
    return BacktestResult(
        comparison,
        metrics,
        not reasons,
        tuple(reasons),
        tuple(item.order_key for item in evaluation_tuple),
    )


__all__ = [
    "ENGINE_VERSION",
    "PPM",
    "BacktestResult",
    "EvaluationMetrics",
    "ExperimentLock",
    "LpReplayObservation",
    "WalkForwardSpec",
    "backtest_strategy",
    "observation_dataset_hash",
]
