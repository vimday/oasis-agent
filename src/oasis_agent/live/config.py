"""Typed, closed configuration for the live LP loop."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ..models.common import normalize_address, normalize_hash32
from .v4lp import PoolKey

Mode = Literal["shadow", "live"]


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
    secondary_rpc_env: str

    @staticmethod
    def load(path: Path) -> "LiveConfig":
        raw: dict[str, Any] = json.loads(path.read_text())
        pool = raw.pop("pool")
        fields = set(LiveConfig.__dataclass_fields__) - {"pool"}
        if set(raw) != fields:
            missing, extra = fields - set(raw), set(raw) - fields
            raise ValueError(f"live config fields mismatch: missing={missing} extra={extra}")
        config = LiveConfig(
            pool=PoolKey(
                normalize_address(pool["currency0"]),
                normalize_address(pool["currency1"]),
                int(pool["fee"]),
                int(pool["tick_spacing"]),
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
