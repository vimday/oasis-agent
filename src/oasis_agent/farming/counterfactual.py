from dataclasses import dataclass
from enum import StrEnum

from ..models.common import BlockRef, EvidenceRef


class FeePolicyKind(StrEnum):
    STATIC = "static"
    DYNAMIC = "dynamic"


class RangePolicyKind(StrEnum):
    FIXED = "fixed"
    ACTIVE = "active"


@dataclass(frozen=True, slots=True)
class CounterfactualRun:
    fee_policy: FeePolicyKind
    range_policy: RangePolicyKind
    start_block: BlockRef
    end_block: BlockRef
    dataset_hash: str
    engine_version: str
    policy_version: str
    initial_capital1: int
    fee_income1: int
    incentive_income1: int
    inventory_pnl1: int
    gas_cost1: int
    rebalance_cost1: int
    adverse_selection_cost1: int
    other_cost1: int
    failed_tx_cost1: int
    opportunity_cost1: int
    hold_net_pnl1: int
    flow_response_model: str
    evidence: tuple[EvidenceRef, ...]

    def __post_init__(self) -> None:
        if self.start_block.chain_id != self.end_block.chain_id:
            raise ValueError("counterfactual run cannot cross chains")
        if self.start_block.number >= self.end_block.number:
            raise ValueError("counterfactual run requires an increasing block window")
        if len(self.dataset_hash) != 64:
            raise ValueError("dataset hash must be a SHA-256 hex digest")
        try:
            int(self.dataset_hash, 16)
        except ValueError as exc:
            raise ValueError("dataset hash must be a SHA-256 hex digest") from exc
        if not self.engine_version or not self.policy_version or not self.flow_response_model:
            raise ValueError("engine, policy, and flow-response versions are required")
        for name in (
            "initial_capital1",
            "fee_income1",
            "incentive_income1",
            "inventory_pnl1",
            "gas_cost1",
            "rebalance_cost1",
            "adverse_selection_cost1",
            "other_cost1",
            "failed_tx_cost1",
            "opportunity_cost1",
            "hold_net_pnl1",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer currency1 base-unit amount")
        if self.initial_capital1 <= 0:
            raise ValueError("initial capital must be positive")
        for name in (
            "fee_income1",
            "incentive_income1",
            "gas_cost1",
            "rebalance_cost1",
            "adverse_selection_cost1",
            "other_cost1",
            "failed_tx_cost1",
            "opportunity_cost1",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be nonnegative")
        if not self.evidence:
            raise ValueError("counterfactual run requires retained evidence")
        for reference in self.evidence:
            if (
                reference.block is not None
                and reference.block.chain_id != self.start_block.chain_id
            ):
                raise ValueError("counterfactual evidence belongs to a different chain")

    @property
    def net_pnl1(self) -> int:
        return (
            self.fee_income1
            + self.incentive_income1
            + self.inventory_pnl1
            - self.gas_cost1
            - self.rebalance_cost1
            - self.adverse_selection_cost1
            - self.other_cost1
            - self.failed_tx_cost1
            - self.opportunity_cost1
        )

    @property
    def excess_vs_hold1(self) -> int:
        return self.net_pnl1 - self.hold_net_pnl1


@dataclass(frozen=True, slots=True)
class FactorialLpComparison:
    r00_static_fixed: CounterfactualRun
    r01_static_active: CounterfactualRun
    r10_dynamic_fixed: CounterfactualRun
    r11_dynamic_active: CounterfactualRun
    fee_contribution_fixed1: int
    range_contribution_static1: int
    combined_excess1: int
    interaction1: int


def compare_lp_factorial(runs: tuple[CounterfactualRun, ...]) -> FactorialLpComparison:
    by_cell = {(run.fee_policy, run.range_policy): run for run in runs}
    required = {
        (FeePolicyKind.STATIC, RangePolicyKind.FIXED),
        (FeePolicyKind.STATIC, RangePolicyKind.ACTIVE),
        (FeePolicyKind.DYNAMIC, RangePolicyKind.FIXED),
        (FeePolicyKind.DYNAMIC, RangePolicyKind.ACTIVE),
    }
    if len(runs) != 4 or set(by_cell) != required:
        raise ValueError("factorial comparison requires exactly one run in each of four cells")
    first = runs[0]
    for run in runs[1:]:
        for name in (
            "start_block",
            "end_block",
            "dataset_hash",
            "engine_version",
            "initial_capital1",
            "hold_net_pnl1",
            "flow_response_model",
        ):
            if getattr(run, name) != getattr(first, name):
                raise ValueError(f"counterfactual cells are not comparable: {name}")

    r00 = by_cell[FeePolicyKind.STATIC, RangePolicyKind.FIXED]
    r01 = by_cell[FeePolicyKind.STATIC, RangePolicyKind.ACTIVE]
    r10 = by_cell[FeePolicyKind.DYNAMIC, RangePolicyKind.FIXED]
    r11 = by_cell[FeePolicyKind.DYNAMIC, RangePolicyKind.ACTIVE]
    return FactorialLpComparison(
        r00,
        r01,
        r10,
        r11,
        r10.net_pnl1 - r00.net_pnl1,
        r01.net_pnl1 - r00.net_pnl1,
        r11.net_pnl1 - r00.net_pnl1,
        r11.net_pnl1 - r10.net_pnl1 - r01.net_pnl1 + r00.net_pnl1,
    )


__all__ = [
    "CounterfactualRun",
    "FactorialLpComparison",
    "FeePolicyKind",
    "RangePolicyKind",
    "compare_lp_factorial",
]
