"""Frozen allowlist and resource bounds for the offline three-swap study."""

from datetime import UTC, datetime
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from ..models.common import ZERO_ADDRESS, normalize_address, normalize_hash32

Address = Annotated[str, StringConstraints(pattern=r"^0x[0-9a-f]{40}$")]
Hash = Annotated[str, StringConstraints(pattern=r"^0x[0-9a-f]{64}$")]
Label = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9_.-]{1,64}$")]
Raw = Annotated[int, Field(ge=0, lt=1 << 256)]
Size = Annotated[int, Field(gt=0, lt=1 << 127)]
BlockNumber = Annotated[int, Field(ge=0, lt=1 << 64)]
Costs = Literal["gas", "builder", "protocol", "financing"]
COSTS: tuple[Costs, ...] = ("gas", "builder", "protocol", "financing")


def utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if "T" not in value or parsed.utcoffset() is None:
        raise ValueError("evidence timestamp must include a time and offset")
    try:
        return parsed.astimezone(UTC)
    except OverflowError as exc:
        raise ValueError("evidence timestamp is outside the supported range") from exc


class Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Asset(Frozen):
    address: Address | None
    symbol: Label
    decimals: Annotated[int, Field(ge=0, le=255)]
    runtime_hash: Hash | None = None

    @property
    def currency(self) -> str:
        return self.address or ZERO_ADDRESS


class Pool(Frozen):
    name: Label
    protocol: Literal["v4", "v3", "solidly_volatile"]
    address: Address
    token0: Address
    token1: Address
    runtime_hash: Hash | None = None
    factory: Address | None = None
    factory_hash: Hash | None = None
    quoter: Address | None = None
    quoter_hash: Hash | None = None
    fee: Annotated[int, Field(ge=0, lt=1 << 24)] | None = None
    tick_spacing: Annotated[int, Field(gt=0, le=32767)] | None = None
    hooks: Address | None = None
    pool_id: Hash | None = None

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        if self.address == ZERO_ADDRESS or self.token0 >= self.token1:
            raise ValueError("pool address and canonical token order required")
        if (self.protocol == "v4") != (self.pool_id is not None):
            raise ValueError("pool ID is required exactly for v4")
        if self.protocol != "v4" and self.token0 == ZERO_ADDRESS:
            raise ValueError("v3 and Solidly pools require two ERC-20 tokens")
        if self.protocol != "v4" and (self.tick_spacing is not None or self.hooks is not None):
            raise ValueError("tick spacing and hooks belong to v4")
        if self.protocol == "solidly_volatile" and (
            self.quoter is not None or self.fee is not None
        ):
            raise ValueError("Solidly pool supplies its own fee-inclusive quote")
        return self


class Cost(Frozen):
    kind: Costs
    status: Literal["estimate", "known", "unknown"]
    raw: Raw | None
    source: Label | None

    @model_validator(mode="after")
    def validate_cost(self) -> Self:
        if self.status == "unknown":
            if self.raw is not None:
                raise ValueError("unknown costs cannot carry a value")
        elif self.raw is None or self.source is None:
            raise ValueError("cost bounds require an amount and a source label")
        return self


class Window(Frozen):
    name: Label
    block: BlockNumber
    expected_hash: Hash | None = None
    cutoff_utc: str | None = None


class Case(Frozen):
    trigger_hash: Hash
    trigger_block: BlockNumber
    trigger_index: BlockNumber
    router: Address
    router_hash: Hash | None = None
    winner_hash: Hash
    winner_block: BlockNumber
    winner_index: BlockNumber
    sender: Address
    executor: Address
    executor_hash: Hash | None = None
    route: tuple[Label, Label, Label]
    prefix_system_type: Annotated[int, Field(ge=0, le=255)] | None = None


class Plan(Frozen):
    schema_version: Literal["oasis-cycle-plan-v1"] = Field(alias="schema")
    scenario: Literal["synthetic", "historical"]
    chain_id: Annotated[int, Field(gt=0, lt=1 << 64)]
    sources: Annotated[tuple[Label, ...], Field(min_length=1, max_length=2)]
    assets: Annotated[tuple[Asset, ...], Field(min_length=4, max_length=8)]
    wrapped_native: Address
    pools: Annotated[tuple[Pool, ...], Field(min_length=3, max_length=8)]
    windows: Annotated[tuple[Window, ...], Field(min_length=1, max_length=16)]
    sizes_raw: Annotated[tuple[Size, ...], Field(min_length=1, max_length=16)]
    inventory_raw: Raw | None
    gas_reserve_raw: Raw | None
    commitments_raw: Raw | None
    costs: tuple[Cost, Cost, Cost, Cost]
    anomaly_ratio_bps: Annotated[int, Field(gt=10000, le=1_000_000_000)]
    max_points: Annotated[int, Field(ge=1, le=64)]
    max_queries_per_window: Annotated[int, Field(ge=1, le=1024)]
    case: Case | None = None

    @model_validator(mode="after")
    def validate_scope(self) -> Self:
        for values in (
            self.sources,
            tuple(a.currency for a in self.assets),
            tuple(p.name for p in self.pools),
            tuple((p.address, p.pool_id) for p in self.pools),
            tuple(w.name for w in self.windows),
            tuple(w.block for w in self.windows),
            self.sizes_raw,
        ):
            if len(values) != len(set(values)):
                raise ValueError("duplicate plan identity")
        cutoffs = [
            utc(window.cutoff_utc)
            for window in sorted(self.windows, key=lambda item: item.block)
            if window.cutoff_utc is not None
        ]
        if cutoffs and (len(cutoffs) != len(self.windows) or cutoffs != sorted(cutoffs)):
            raise ValueError("use consistent, nondecreasing retrieval cutoffs across windows")
        assets = {asset.currency: asset for asset in self.assets}
        native = assets.get(ZERO_ADDRESS)
        wrapped = assets.get(self.wrapped_native)
        if (
            native is None
            or native.address is not None
            or native.runtime_hash is not None
            or wrapped is None
            or wrapped.address is None
            or native.decimals != wrapped.decimals
        ):
            raise ValueError("native and wrapped native identity mismatch")
        for pool in self.pools:
            if not {pool.token0, pool.token1} <= assets.keys():
                raise ValueError("pool currencies must be allowlisted")
        if {cost.kind for cost in self.costs} != set(COSTS):
            raise ValueError("each incremental cost kind is required exactly once")
        if self.scenario == "historical" and any(cost.status == "known" for cost in self.costs):
            raise ValueError("historical candidate costs are estimates or unknown")
        if self.case is not None and not set(self.case.route) <= {p.name for p in self.pools}:
            raise ValueError("case route must be allowlisted")
        return self

    @property
    def native(self) -> Asset:
        return next(asset for asset in self.assets if asset.address is None)


def address(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("address must be a string")
    parsed = normalize_address(value)
    if any(c not in "0123456789abcdef" for c in parsed[2:]):
        raise ValueError("invalid address characters")
    return parsed


def hash32(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("hash must be a string")
    parsed = normalize_hash32(value)
    if any(c not in "0123456789abcdef" for c in parsed[2:]):
        raise ValueError("invalid hash characters")
    return parsed
