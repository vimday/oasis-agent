"""Typed, closed configuration for the live LP loop."""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ..models.common import normalize_address, normalize_hash32
from ..models.v4 import PoolKey as CanonicalPoolKey
from ..uniswap_v4.pool_id import pool_id_from_key
from .v4lp import PoolKey

Mode = Literal["shadow", "live"]
BASE_USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate live configuration key")
        result[key] = value
    return result


@dataclass(frozen=True, slots=True)
class LiveConfig:
    chain_id: int
    account: str
    pool_id: str
    pool: PoolKey
    decimals0: int
    decimals1: int
    state_view: str
    position_manager: str
    universal_router: str
    permit2: str
    eth_usd_feed: str
    half_width_ticks: int
    poll_seconds: int
    max_capital_usd: int  # whole USD
    max_daily_loss_usd: int  # whole USD
    max_cumulative_loss_usd: int  # whole USD
    gas_reserve_wei: int
    max_oracle_deviation_bps: int
    swap_slippage_bps: int
    max_actions_per_day: int
    recenter_cooldown_seconds: int
    max_fee_per_gas_wei: int
    priority_fee_wei: int
    approval_usdc: int
    approval_days: int
    expected_pins: dict[str, str]
    oracle_max_age_seconds: int
    # USDC/USD updates on a 24h heartbeat or price deviation, so it gets its own ceiling.
    # A stale answer is still bounded by the stable band and the pool/oracle deviation check.
    usdc_oracle_max_age_seconds: int
    secondary_rpc_env: str

    @staticmethod
    def load(path: Path) -> "LiveConfig":
        return LiveConfig.from_json(path.read_bytes())

    @staticmethod
    def from_json(data: bytes) -> "LiveConfig":
        raw: dict[str, Any] = json.loads(data, object_pairs_hook=_unique)
        pool = raw.pop("pool")
        if not isinstance(pool, dict) or set(pool) != {
            "currency0",
            "currency1",
            "fee",
            "tick_spacing",
            "hooks",
        }:
            raise ValueError("pool fields mismatch")
        fields = set(LiveConfig.__dataclass_fields__) - {"pool"}
        if set(raw) != fields:
            missing, extra = fields - set(raw), set(raw) - fields
            raise ValueError(f"live config fields mismatch: missing={missing} extra={extra}")
        config = LiveConfig(
            pool=PoolKey(
                normalize_address(pool["currency0"]),
                normalize_address(pool["currency1"]),
                pool["fee"],
                pool["tick_spacing"],
                normalize_address(pool["hooks"]),
            ),
            **raw,
        )
        config.validate()
        return config

    def _typed_ints(self) -> list[tuple[str, object]]:
        skip = {
            "account",
            "pool_id",
            "pool",
            "state_view",
            "position_manager",
            "universal_router",
            "permit2",
            "eth_usd_feed",
            "expected_pins",
            "secondary_rpc_env",
        }
        return [(n, getattr(self, n)) for n in self.__dataclass_fields__ if n not in skip]

    def validate(self) -> None:
        for name in (
            "account",
            "state_view",
            "position_manager",
            "universal_router",
            "permit2",
            "eth_usd_feed",
        ):
            object.__setattr__(self, name, normalize_address(getattr(self, name)))
        object.__setattr__(self, "pool_id", normalize_hash32(self.pool_id))
        for name, value in self._typed_ints():
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        pin_names = {
            "state_view",
            "position_manager",
            "universal_router",
            "permit2",
            "usdc_implementation",
        }
        if set(self.expected_pins) != pin_names:
            raise ValueError(f"expected_pins must name exactly {sorted(pin_names)}")
        object.__setattr__(
            self, "expected_pins", {k: normalize_hash32(v) for k, v in self.expected_pins.items()}
        )
        if self.chain_id != 8453:
            raise ValueError("live loop is pinned to Base (8453)")
        if (self.decimals0, self.decimals1) != (18, 6) or self.pool.currency1 != BASE_USDC:
            raise ValueError("live adapter requires Base native ETH and USDC with 18/6 decimals")
        if pool_id_from_key(CanonicalPoolKey(*self.pool.tuple())) != self.pool_id:
            raise ValueError("pool id differs from the encoded pool key")
        if self.max_daily_loss_usd > 200 or self.max_cumulative_loss_usd > 500:
            raise ValueError("owner loss caps are USD 200 daily and USD 500 cumulative")
        if not 0 < self.max_daily_loss_usd <= self.max_cumulative_loss_usd:
            raise ValueError("daily loss cap must be positive and within the cumulative cap")
        if not 0 < self.max_capital_usd <= 500:
            raise ValueError("owner capital cap is USD 500")
        if self.half_width_ticks < self.pool.tick_spacing:
            raise ValueError("range half width must be at least one tick spacing")
        if not 1 <= self.swap_slippage_bps <= 200:
            raise ValueError("swap slippage must be 1..200 bps")
        if self.priority_fee_wei > self.max_fee_per_gas_wei:
            raise ValueError("priority fee exceeds max fee")

        # These bounds are the existing trial-1 authorization, not new operator defaults.
        bounded = {
            "approval_usdc": (1, 700_000_000),
            "approval_days": (1, 30),
            "max_actions_per_day": (1, 12),
            "max_oracle_deviation_bps": (1, 40),
            "oracle_max_age_seconds": (1, 1800),
            "usdc_oracle_max_age_seconds": (1, 90_000),
            "max_fee_per_gas_wei": (1, 1_000_000_000),
            "priority_fee_wei": (0, 1_000_000),
            "poll_seconds": (1, 3600),
        }
        for name, (lower, upper) in bounded.items():
            if not lower <= getattr(self, name) <= upper:
                raise ValueError(f"{name} is outside the bounded canary policy")
        if self.swap_slippage_bps > 50 or self.recenter_cooldown_seconds < 600:
            raise ValueError("slippage/cooldown exceeds trial-1 authority")
        if self.gas_reserve_wei < 4_000_000_000_000_000:
            raise ValueError("gas reserve is below trial-1 minimum")
        if self.half_width_ticks != 490 or (self.pool.fee, self.pool.tick_spacing) != (500, 10):
            raise ValueError("range/pool differs from trial-1 strategy")
        if not isinstance(self.secondary_rpc_env, str) or not re.fullmatch(
            r"[A-Z][A-Z0-9_]{0,127}", self.secondary_rpc_env
        ):
            raise ValueError("secondary RPC must name an environment variable")
