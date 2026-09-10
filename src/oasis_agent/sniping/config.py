"""Strict execution configuration for the Base launch sniper.

Secrets (RPC URLs with credentials, private keys) are never stored here; the
monitor reads them from the process environment at startup. All monetary and
threshold parameters are frozen integers; unknown keys are rejected.
"""

from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    model_serializer,
    model_validator,
)

from ..launch.models import Address, Positive, StrictModel

GWEI = 10**9
WEI_PER_ETH = 10**18

_WETH = "0x4200000000000000000000000000000000000006"
_USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"


class ExecutionConfig(StrictModel):
    """Frozen, validated parameters for one launch execution campaign."""

    schema_: Literal["oasis.launch-execution-config.v1", "oasis.launch-execution-config.v2"] = (
        Field(
            "oasis.launch-execution-config.v1",
            alias="schema",
            validation_alias="schema",
            serialization_alias="schema",
        )
    )
    chain_id: Literal[8453] = 8453
    # LAPTOP official Base CA (verified against official site + deployed code).
    target_ca: Address = "0xb095274743941e953c746f9c228da9c18bb6ec29"
    weth: Address = _WETH
    # Routing-only fields below carry a fixed default and are excluded from the
    # serialized execution policy so an in-flight campaign's immutable policy
    # binding is preserved when monitoring coverage expands. They remain frozen
    # in-memory values and are never read from the JSON config.
    usdc: Address = Field(default=_USDC, exclude=True)
    # Quote assets the target can be bought against over the v3 path. WETH is
    # native ETH; USDC routes through the canonical WETH/USDC hop. Venue
    # (Aerodrome/Slipstream/v4) USDC pairs remain discovery-only for now.
    base_tokens: tuple[Address, ...] = Field(default=(_WETH, _USDC), exclude=True)
    # Fee tier of the WETH/USDC hop used when routing a USDC-denominated buy.
    usdc_weth_fee: int = Field(default=500, ge=1, lt=2**24, exclude=True)
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
    reference_supply: Positive = 1_000_000_000
    token_decimals: int = Field(default=18, ge=0, le=77, exclude=True)
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
    max_gas: Positive = 800_000
    gas_price_multiplier: Positive = 2  # 2x headroom; Base fees are ~0.006 gwei
    broadcast_tries: int = Field(default=3, ge=1, le=5)
    rpc_retries: int = Field(default=3, ge=0, le=8)

    @model_validator(mode="after")
    def _ordered_tranches(self) -> "ExecutionConfig":
        if self.schema_ == "oasis.launch-execution-config.v1" and (
            self.token_decimals != 18 or self.reference_supply != 1_000_000_000
        ):
            raise ValueError("custom token metadata requires version 2 policy binding")
        if not self.buy_tranches_wei or not self.v3_fees:
            raise ValueError("non-empty buy schedule and fee tiers required")
        if len(set(self.v3_fees)) != len(self.v3_fees) or any(
            type(fee) is not int or not 0 < fee < 1_000_000 for fee in self.v3_fees
        ):
            raise ValueError("unique protocol-valid v3 fee tiers required")
        if (self.usdc, self.base_tokens, self.usdc_weth_fee) != (_USDC, (_WETH, _USDC), 500):
            raise ValueError("routing constants excluded from policy cannot be overridden")
        if tuple(self.buy_tranches_wei) != tuple(sorted(self.buy_tranches_wei)):
            raise ValueError("buy_tranches_wei must be strictly ascending cumulative amounts")
        if len(set(self.buy_tranches_wei)) != len(self.buy_tranches_wei):
            raise ValueError("duplicate buy tranches")
        return self

    @model_serializer(mode="wrap")
    def serialize_policy(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        result: dict[str, Any] = handler(self)
        if self.schema_ == "oasis.launch-execution-config.v2":
            result["token_decimals"] = self.token_decimals
        return result


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
