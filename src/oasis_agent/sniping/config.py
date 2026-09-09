"""Strict execution configuration for the Base launch sniper.

Secrets (RPC URLs with credentials, private keys) are never stored here; the
monitor reads them from the process environment at startup. All monetary and
threshold parameters are frozen integers; unknown keys are rejected.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..launch.models import Address, Positive, StrictModel

GWEI = 10**9
WEI_PER_ETH = 10**18


class ExecutionConfig(StrictModel):
    """Frozen, validated parameters for one launch execution campaign."""

    schema_: Literal["oasis.launch-execution-config.v1"] = Field(
        "oasis.launch-execution-config.v1",
        alias="schema",
        validation_alias="schema",
        serialization_alias="schema",
    )
    chain_id: Literal[8453] = 8453
    # LAPTOP official Base CA (verified against official site + deployed code).
    target_ca: Address = "0xb095274743941e953c746f9c228da9c18bb6ec29"
    weth: Address = "0x4200000000000000000000000000000000000006"
    v3_factory: Address = "0x33128a8fc17869897dce68ed026d694621f6fdfd"
    quoter_v2: Address = "0x3d4e44eb1374240ce5f1b871ab261cd16335b76a"
    universal_router: Address = "0x3fc91a3afd70395cd496c647d5a6cc9d4b2b7fad"
    permit2: Address = "0x000000000022d473030f116ddee9f6b43ac78ba3"
    # Pre-authorized for the sell side; reserved for a future verified path.
    aerodrome_router: Address = "0xcf77a3ba9a5ca399b7c97c74d54e5b1beb874e43"
    eth_usd_feed: Address = "0x71041dddad3595f9ced3dccfbe3d1f4b0a16bb70"
    # Uniswap v3 fee tiers watched for a live LAPTOP/WETH pool.
    v3_fees: tuple[int, ...] = (100, 500, 3000, 10000)
    # Cumulative native buy schedule in wei: 0.1 ETH then 0.2 ETH total.
    buy_tranches_wei: tuple[Positive, ...] = (WEI_PER_ETH // 10, WEI_PER_ETH // 5)
    max_fdv_usd_micros: Positive = 200_000_000_000_000  # strict below $200M
    reference_supply: Literal[1_000_000_000] = 1_000_000_000
    slippage_bps: int = Field(default=1000, ge=0, lt=10_000)  # 10%
    # Sell-side triggers (frozen; owner-confirmed defaults).
    stop_loss_value_pct: int = Field(default=30, ge=1, le=99)  # exit all at -30%
    take_profit_multiple: Positive = 10  # sell half at 10x entry value
    post_half_sell_floor_multiple: Positive = 2  # exit rest if back to 2x after half-sell
    time_stop_seconds: Positive = 30 * 24 * 3600
    poll_interval_ms: Positive = 2000
    receipt_timeout_ms: Positive = 120_000
    rpc_timeout_ms: Positive = 25_000
    max_age_blocks: Positive = 10  # snapshot freshness window
    max_age_seconds: Positive = 120
    feed_max_age_seconds: Positive = 3600
    gas_buffer_bps: int = Field(default=2000, ge=0, lt=10_000)  # +20% over estimate
    max_gas: int = 800_000
    gas_price_multiplier: Positive = 2  # 2x headroom; Base fees are ~0.006 gwei
    broadcast_tries: int = Field(default=3, ge=1, le=5)
    rpc_retries: int = Field(default=3, ge=0, le=8)

    @model_validator(mode="after")
    def _ordered_tranches(self) -> "ExecutionConfig":
        if tuple(self.buy_tranches_wei) != tuple(sorted(self.buy_tranches_wei)):
            raise ValueError("buy_tranches_wei must be strictly ascending cumulative amounts")
        if len(set(self.buy_tranches_wei)) != len(self.buy_tranches_wei):
            raise ValueError("duplicate buy tranches")
        return self

    @classmethod
    def from_json(cls, raw: str | bytes) -> "ExecutionConfig":
        return cls.model_validate_json(raw)


class FeedState(BaseModel):
    """Validated ETH/USD Chainlink feed reading."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    price_usd_micros: Positive
    decimals: int = 8
    updated_at: Positive
    block: int
    provider: str

    @property
    def price_usd(self) -> float:
        return self.price_usd_micros / 1_000_000

    def is_fresh(self, now: int, max_age_seconds: int) -> bool:
        return 0 <= now - self.updated_at <= max_age_seconds
