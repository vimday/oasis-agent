from __future__ import annotations

from dataclasses import dataclass

from .common import EvidenceLevel, normalize_address


@dataclass(frozen=True, slots=True)
class SelectorObservation:
    selector: str
    offset: int
    destination: int | None


@dataclass(frozen=True, slots=True)
class HookPermissions:
    before_initialize: bool
    after_initialize: bool
    before_add_liquidity: bool
    after_add_liquidity: bool
    before_remove_liquidity: bool
    after_remove_liquidity: bool
    before_swap: bool
    after_swap: bool
    before_donate: bool
    after_donate: bool
    before_swap_returns_delta: bool
    after_swap_returns_delta: bool
    after_add_liquidity_returns_delta: bool
    after_remove_liquidity_returns_delta: bool


@dataclass(frozen=True, slots=True)
class FeatureObservation:
    name: str
    level: EvidenceLevel
    detail: str


@dataclass(frozen=True, slots=True)
class FeatureSummary:
    opcode_counts: dict[str, int]
    hardcoded_addresses: tuple[str, ...]
    selectors: tuple[SelectorObservation, ...]
    observations: tuple[FeatureObservation, ...]
    metadata: dict[str, str]
    bytecode_hash: str
    byte_length: int


@dataclass(frozen=True, slots=True)
class HookState:
    address: str
    owner: str | None
    registrar: str | None
    pool_manager: str | None
    default_fee_config: tuple[int, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "address", normalize_address(self.address))
