"""Shared bounded market acquisition configuration; no endpoints or credentials."""

from typing import Annotated

from pydantic import Field

from ..launch.models import Address, Hash, Positive, StrictModel
from ..models.v4 import PoolKey
from ..rpc.budget import BudgetPolicy


class MarketConfig(StrictModel):
    pool: PoolKey
    blocks: Annotated[int, Field(ge=2, le=10)]
    horizon_seconds: Annotated[int, Field(ge=1, le=300)]
    max_lateness_seconds: Annotated[int, Field(ge=1, le=300)]


class Config(StrictModel):
    market: MarketConfig | None = None
    chain_id: Positive
    pool_id: Hash
    state_view: Address
    pool_manager: Address
    state_view_runtime_keccak: Hash
    pool_manager_runtime_keccak: Hash
    public_request_cap: Annotated[int, Field(ge=1, le=1000)]
    paid_request_cap: Annotated[int, Field(ge=1, le=100)]
    public_interval_ms: Annotated[int, Field(ge=1000, le=60000)]
    paid_interval_ms: Annotated[int, Field(ge=3000, le=60000)]
    max_age_seconds: Annotated[int, Field(ge=1, le=300)]

    def policies(self) -> tuple[BudgetPolicy, ...]:
        return (
            BudgetPolicy("public", self.public_request_cap, self.public_interval_ms),
            BudgetPolicy("paid", self.paid_request_cap, self.paid_interval_ms),
        )
