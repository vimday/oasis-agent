"""Strict retained launch inputs; money is integer USD micros or EVM base units."""

import json
from itertools import pairwise
from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    model_serializer,
    model_validator,
)

UInt = Annotated[int, Field(strict=True, ge=0, lt=2**256)]
Positive = Annotated[int, Field(strict=True, gt=0, lt=2**256)]
Address = Annotated[str, Field(pattern=r"^0x[0-9a-f]{40}$")]
Hash = Annotated[str, Field(pattern=r"^0x[0-9a-f]{64}$")]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Source = Annotated[str, Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    @model_validator(mode="before")
    @classmethod
    def reject_floats(cls, value: Any) -> Any:
        def check(item: Any) -> Any:
            if isinstance(item, float):
                raise ValueError("binary floats are not launch inputs")
            if isinstance(item, dict):
                return {key: check(child) for key, child in item.items()}
            if isinstance(item, (list, tuple)):
                return tuple(check(child) for child in item)
            return item

        return check(value)

    @classmethod
    def from_json(cls, raw: str | bytes) -> Self:
        def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate launch JSON key")
                result[key] = value
            return result

        parsed = json.loads(raw, object_pairs_hook=unique)
        return cls.model_validate_json(json.dumps(parsed))


class TargetPolicy(StrictModel):
    """Chain-local research policy; acceptance never authorizes execution."""

    chain_id: Positive
    target_ca: Address
    symbol: Annotated[str, Field(pattern=r"^[a-zA-Z0-9_-]{1,32}$")]
    reference_supply: Positive
    max_fdv_usd_micros: Positive
    principal_limit_usd_micros: Positive
    # Native buy schedule: each element is the cumulative native amount after
    # that tranche, e.g. (10**17, 2 * 10**17) = buy 0.1 ETH then 0.1 ETH more,
    # with a bound quote at every cumulative size. None means unfrozen.
    buy_tranches_wei: tuple[Positive, ...] | None = None
    # None means unverified/unfrozen, never a permissive default.
    token_decimals: Annotated[int, Field(ge=0, le=77)] | None = None
    runtime_hash: Hash | None = None
    control_baseline: Digest | None = None
    max_age_seconds: Positive | None = None
    max_age_blocks: Positive | None = None
    slippage_bps: Annotated[int, Field(ge=0, lt=10_000)] | None = None
    max_impact_bps: Annotated[int, Field(ge=0, lt=10_000)] | None = None
    min_liquidity_usd_micros: Positive | None = None
    max_stress_loss_bps: Annotated[int, Field(ge=0, le=10_000)] | None = None
    kill_price_usd_micros: Positive | None = None

    @model_validator(mode="after")
    def validate_target(self) -> Self:
        if int(self.target_ca, 16) == 0:
            raise ValueError("target token must be nonzero")
        schedule = self.buy_tranches_wei
        if schedule is not None and (
            not schedule or any(left >= right for left, right in pairwise(schedule))
        ):
            raise ValueError("nonempty strictly increasing cumulative tranches required")
        return self


class Target(TargetPolicy):
    chain_id: Literal[8453] = 8453
    target_ca: Address = "0xb095274743941e953c746f9c228da9c18bb6ec29"
    symbol: Literal["LAPTOP"] = "LAPTOP"
    reference_supply: Literal[1_000_000_000] = 1_000_000_000
    max_fdv_usd_micros: Literal[200_000_000_000_000] = 200_000_000_000_000
    # 0.2 ETH total spend at ETH <= 5000 USD is the bounded worst case; the
    # actual buy schedule is governed by buy_tranches_wei, never this cap.
    principal_limit_usd_micros: Literal[1_000_000_000] = 1_000_000_000


class MultiChainTarget(TargetPolicy):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, serialize_by_alias=True)

    schema_: Literal["oasis.launch-target.v2"] = Field(alias="schema")


class HolderLimits(StrictModel):
    min_holders: Positive
    max_single_bps: Annotated[int, Field(strict=True, ge=0, le=10_000)]
    max_top10_bps: Annotated[int, Field(strict=True, ge=0, le=10_000)]
    max_hhi_bps: Annotated[int, Field(strict=True, ge=0, le=10_000)]


class HolderTarget(TargetPolicy):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, serialize_by_alias=True)

    schema_: Literal["oasis.launch-target.v3"] = Field(alias="schema")
    holder_limits: HolderLimits


def parse_target(raw: str | bytes) -> TargetPolicy:
    """Dispatch explicitly versioned research policies; legacy defaults stay Base-only."""
    value = json.loads(raw)
    if isinstance(value, dict) and "schema" in value:
        if value["schema"] == "oasis.launch-target.v3":
            return HolderTarget.from_json(raw)
        return MultiChainTarget.from_json(raw)
    return Target.from_json(raw)


class Snapshot(StrictModel):
    chain_id: Positive
    number: UInt
    block_hash: Hash
    timestamp: UInt
    parent_hash: Hash


class HolderBalance(StrictModel):
    address: Address
    balance_base_units: Positive

    @model_validator(mode="after")
    def nonzero_address(self) -> Self:
        if int(self.address, 16) == 0:
            raise ValueError("zero address is not a holder")
        return self


class HolderObservation(StrictModel):
    snapshot: Snapshot
    source: Source
    retrieved_at: UInt
    raw_evidence_digest: Digest
    target_ca: Address
    runtime_hash: Hash
    token_decimals: Annotated[int, Field(strict=True, ge=0, le=77)]
    total_supply_base_units: Positive
    complete: bool
    balances: Annotated[tuple[HolderBalance, ...], Field(max_length=100_000)]

    @model_validator(mode="after")
    def valid_balances(self) -> Self:
        if len({item.address for item in self.balances}) != len(self.balances):
            raise ValueError("duplicate holder address")
        if sum(item.balance_base_units for item in self.balances) > self.total_supply_base_units:
            raise ValueError("holder balances exceed supply")
        return self


class Quote(StrictModel):
    # USD valuation of native_wei at the snapshot time (integer micros). The
    # quote grid is keyed by native_wei against Target.buy_tranches_wei.
    spend_usd_micros: UInt
    native_wei: Positive
    received_base_units: Positive


class Observation(StrictModel):
    """Retained adapter interpretation, not a claim of independent verification.

    Raw acquisition/protocol proofs remain required before promotion. Source is a
    public provider alias, never a URL or credential. Both providers must bind to
    the same canonical block; pending observations cannot enter this schema.
    """

    snapshot: Snapshot
    source: Source
    retrieved_at: UInt
    raw_evidence_digest: Digest
    target_ca: Address
    runtime_hash: Hash
    token_decimals: Annotated[int, Field(ge=0, le=77)]
    control_digest: Digest
    pool: Address
    token0: Address
    token1: Address
    quotes: tuple[Quote, ...]


class Cycle(StrictModel):
    observations: tuple[Observation, ...]
    evaluated_at: UInt
    current_block: UInt
    holder_observations: tuple[HolderObservation, ...] | None = None

    @model_serializer(mode="wrap")
    def serialize_cycle(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        result: dict[str, Any] = handler(self)
        if self.holder_observations is None:
            result.pop("holder_observations", None)
        return result
