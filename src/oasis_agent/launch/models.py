"""Strict retained launch inputs; money is integer USD micros or EVM base units."""

import json
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

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


class Target(StrictModel):
    chain_id: Literal[8453] = 8453
    target_ca: Address = "0xb095274743941e953c746f9c228da9c18bb6ec29"
    symbol: Literal["LAPTOP"] = "LAPTOP"
    reference_supply: Literal[1_000_000_000] = 1_000_000_000
    max_fdv_usd_micros: Literal[200_000_000_000_000] = 200_000_000_000_000
    # 0.2 ETH total spend at ETH <= 5000 USD is the bounded worst case; the
    # actual buy schedule is governed by buy_tranches_wei, never this cap.
    principal_limit_usd_micros: Literal[1_000_000_000] = 1_000_000_000
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


class Snapshot(StrictModel):
    chain_id: Literal[8453]
    number: UInt
    block_hash: Hash
    timestamp: UInt
    parent_hash: Hash


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
