from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .common import BlockRef, normalize_address, normalize_hash32

FEATURE_SCALE = 10**18
MAX_FEATURE = FEATURE_SCALE


def _finite_decimal(value: Decimal, name: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{name} must be finite")
    return value


def _nonnegative_decimal(value: Decimal, name: str) -> Decimal:
    parsed = _finite_decimal(value, name)
    if parsed < 0:
        raise ValueError(f"{name} must be nonnegative")
    return parsed


@dataclass(frozen=True, slots=True)
class CostModel:
    """All costs are required, nonnegative, and valued in one report currency.

    Missing costs must never silently default to zero: fail-closed accounting
    means the caller supplies every term explicitly.
    """

    gas_cost: Decimal
    rebalancing_cost: Decimal
    adverse_selection_cost: Decimal
    inventory_risk_cost: Decimal
    failed_tx_cost: Decimal
    opportunity_cost: Decimal

    def __post_init__(self) -> None:
        for name, value in (
            ("gas_cost", self.gas_cost),
            ("rebalancing_cost", self.rebalancing_cost),
            ("adverse_selection_cost", self.adverse_selection_cost),
            ("inventory_risk_cost", self.inventory_risk_cost),
            ("failed_tx_cost", self.failed_tx_cost),
            ("opportunity_cost", self.opportunity_cost),
        ):
            _nonnegative_decimal(value, name)


@dataclass(frozen=True, slots=True)
class LpPerformance:
    capital: Decimal
    gross_fees: Decimal
    impermanent_loss: Decimal
    costs: Decimal
    net_pnl: Decimal
    net_yield: Decimal

    def __post_init__(self) -> None:
        for name, value in (
            ("capital", self.capital),
            ("gross_fees", self.gross_fees),
            ("impermanent_loss", self.impermanent_loss),
            ("costs", self.costs),
            ("net_pnl", self.net_pnl),
            ("net_yield", self.net_yield),
        ):
            _finite_decimal(value, name)
        if self.capital <= 0:
            raise ValueError("capital must be positive")
        for name, value in (
            ("gross_fees", self.gross_fees),
            ("impermanent_loss", self.impermanent_loss),
            ("costs", self.costs),
        ):
            if value < 0:
                raise ValueError(f"{name} must be nonnegative")
        expected = self.gross_fees - self.impermanent_loss - self.costs
        if self.net_pnl != expected:
            raise ValueError("net_pnl is inconsistent with income and costs")
        if self.net_yield != expected / self.capital:
            raise ValueError("net_yield is inconsistent with net_pnl and capital")


@dataclass(frozen=True, slots=True)
class TokenAmount:
    token: str
    decimals: int
    amount: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "token", normalize_address(self.token))
        if type(self.decimals) is not int or not 0 <= self.decimals <= 255:
            raise ValueError("token decimals must fit uint8")
        if isinstance(self.amount, bool) or not isinstance(self.amount, int):
            raise TypeError("token amount must be an integer base-unit amount")


@dataclass(frozen=True, slots=True)
class AssetPair:
    token0: TokenAmount
    token1: TokenAmount

    def __post_init__(self) -> None:
        if self.token0.token == self.token1.token:
            raise ValueError("asset pair requires distinct tokens")
        if self.token0.token > self.token1.token:
            raise ValueError("asset pair requires canonical token order")

    @property
    def amount0(self) -> int:
        return self.token0.amount

    @property
    def amount1(self) -> int:
        return self.token1.amount

    def __add__(self, other: AssetPair) -> AssetPair:
        self._check_compatible(other)
        return AssetPair(
            TokenAmount(self.token0.token, self.token0.decimals, self.amount0 + other.amount0),
            TokenAmount(self.token1.token, self.token1.decimals, self.amount1 + other.amount1),
        )

    def _check_compatible(self, other: AssetPair) -> None:
        if (self.token0.token, self.token1.token) != (other.token0.token, other.token1.token):
            raise ValueError("asset pairs use different token ordering")
        if (self.token0.decimals, self.token1.decimals) != (
            other.token0.decimals,
            other.token1.decimals,
        ):
            raise ValueError("asset pairs use different decimals")

    def zero(self) -> AssetPair:
        return AssetPair(
            TokenAmount(self.token0.token, self.token0.decimals, 0),
            TokenAmount(self.token1.token, self.token1.decimals, 0),
        )

    def is_zero(self) -> bool:
        return self.amount0 == 0 and self.amount1 == 0


@dataclass(frozen=True, slots=True)
class AccountingComponents:
    trader_input: AssetPair
    trader_output: AssetPair
    pool_delta: AssetPair
    protocol_fee: AssetPair
    lp_fee: AssetPair
    hook_delta: AssetPair
    token_transfer_delta: AssetPair
    unexplained: AssetPair


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    closed: bool
    residual: AssetPair


@dataclass(frozen=True, slots=True)
class ControllerState:
    """Bounded fixed-point state consumed by the deterministic fee controller."""

    pool_id: str
    chain_id: int
    inventory_skew: int
    volatility: int
    toxicity: int
    rebalance_incentive: int
    observed_at: int
    published_at: int
    source_epoch: int
    rule_version: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "pool_id", normalize_hash32(self.pool_id, "pool_id"))
        for name, value in (
            ("chain_id", self.chain_id),
            ("inventory_skew", self.inventory_skew),
            ("volatility", self.volatility),
            ("toxicity", self.toxicity),
            ("rebalance_incentive", self.rebalance_incentive),
            ("observed_at", self.observed_at),
            ("published_at", self.published_at),
            ("source_epoch", self.source_epoch),
            ("rule_version", self.rule_version),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
        if self.chain_id <= 0:
            raise ValueError("chain_id must be positive")
        if not -FEATURE_SCALE <= self.inventory_skew <= FEATURE_SCALE:
            raise ValueError("inventory_skew is outside the fixed-point bounds")
        for name, value in (
            ("volatility", self.volatility),
            ("toxicity", self.toxicity),
            ("rebalance_incentive", self.rebalance_incentive),
        ):
            if not 0 <= value <= MAX_FEATURE:
                raise ValueError(f"{name} is outside the fixed-point bounds")
        if self.observed_at < 0 or self.published_at < 0:
            raise ValueError("state timestamps must be nonnegative")
        if self.observed_at > self.published_at:
            raise ValueError("observed_at cannot be later than published_at")
        if self.source_epoch <= 0 or self.rule_version <= 0:
            raise ValueError("source_epoch and rule_version must be positive")


@dataclass(frozen=True, slots=True)
class FeePolicyConfig:
    """Integer coefficients shared by offline replay and the Solidity hook."""

    min_fee: int
    base_fee: int
    max_fee: int
    volatility_weight: int
    inventory_weight: int
    toxicity_weight: int
    incentive_weight: int
    amount_weight: int
    medium_amount0_threshold: int
    high_amount0_threshold: int
    medium_amount1_threshold: int
    high_amount1_threshold: int
    max_state_age_seconds: int = 30
    quote_validity_seconds: int = 5
    rule_version: int = 1

    def __post_init__(self) -> None:
        for name, value in (
            ("min_fee", self.min_fee),
            ("base_fee", self.base_fee),
            ("max_fee", self.max_fee),
            ("volatility_weight", self.volatility_weight),
            ("inventory_weight", self.inventory_weight),
            ("toxicity_weight", self.toxicity_weight),
            ("incentive_weight", self.incentive_weight),
            ("amount_weight", self.amount_weight),
            ("medium_amount0_threshold", self.medium_amount0_threshold),
            ("high_amount0_threshold", self.high_amount0_threshold),
            ("medium_amount1_threshold", self.medium_amount1_threshold),
            ("high_amount1_threshold", self.high_amount1_threshold),
            ("max_state_age_seconds", self.max_state_age_seconds),
            ("quote_validity_seconds", self.quote_validity_seconds),
            ("rule_version", self.rule_version),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
        for name, value in (
            ("min_fee", self.min_fee),
            ("base_fee", self.base_fee),
            ("max_fee", self.max_fee),
        ):
            if not 0 <= value <= 1_000_000:
                raise ValueError(f"{name} must be between 0 and 1000000")
        if not self.min_fee <= self.base_fee <= self.max_fee:
            raise ValueError("fee bounds are invalid")
        for name, value in (
            ("volatility_weight", self.volatility_weight),
            ("inventory_weight", self.inventory_weight),
            ("toxicity_weight", self.toxicity_weight),
            ("incentive_weight", self.incentive_weight),
            ("amount_weight", self.amount_weight),
        ):
            if value < 0:
                raise ValueError(f"{name} must be nonnegative")
        for medium, high, token in (
            (self.medium_amount0_threshold, self.high_amount0_threshold, "0"),
            (self.medium_amount1_threshold, self.high_amount1_threshold, "1"),
        ):
            if medium <= 0 or medium >= high:
                raise ValueError(f"token{token} amount thresholds are invalid")
        if self.max_state_age_seconds < 0 or self.quote_validity_seconds < 0:
            raise ValueError("state age and quote validity must be nonnegative")
        if self.rule_version <= 0:
            raise ValueError("rule_version must be positive")


@dataclass(frozen=True, slots=True)
class PoolState:
    volatility: Decimal
    inventory_imbalance: Decimal
    adverse_selection: Decimal
    rebalance_incentive: Decimal
    observed_at_timestamp: int
    oracle_timestamp: int
    oracle_deviation: Decimal = Decimal(0)
    previous_fee: int | None = None
    previous_fee_timestamp: int | None = None
    # Offline fee fixtures may omit provenance; shadow evaluation requires a
    # complete block identity matching its independently observed header.
    block: BlockRef | None = None
