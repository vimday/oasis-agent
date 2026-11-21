"""Static cross-chain inventory manifest bound to one reconciled-risk policy.

The manifest is supplied research configuration, not chain-derived truth: it
declares which accounts, assets and USD feeds must be reconciled, and the global
policy that counts them. Every declared value is independently re-derived from
raw RPC before a frame is admitted, so a manifest that disagrees with the chain
fails closed instead of silently redefining the portfolio.

Declaring an asset here is not a valuation claim, a custody claim, or evidence
that any account is funded, and a declared position is only the identity that
retained position and pool-state proof must satisfy. `execution_eligible`
remains untouched.
"""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from ..farming.price_evidence import UsdFeedPolicy
from ..models.common import (
    ZERO_ADDRESS,
    canonical_json,
    normalize_hash32,
    sha256_hex,
)
from .account_risk import Address, GlobalRiskPolicy, Positive, Strict

SourceName = Annotated[str, Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")]


class ManifestFeed(Strict):
    """Pinned Chainlink-style USD feed. The base asset is implied by its asset."""

    address: Address
    description: Annotated[str, Field(min_length=5, max_length=96)]
    runtime_keccak: str
    max_age_seconds: Positive

    @model_validator(mode="after")
    def usable(self) -> ManifestFeed:
        if self.address == ZERO_ADDRESS:
            raise ValueError("USD feed must be a deployed contract")
        object.__setattr__(self, "runtime_keccak", normalize_hash32(self.runtime_keccak))
        return self

    def policy(self, chain_id: int, base_token: str) -> UsdFeedPolicy:
        """Materialize the validated feed policy, failing closed on any mismatch."""
        return UsdFeedPolicy(
            chain_id=chain_id,
            address=self.address,
            base_token=base_token,
            description=self.description,
            runtime_keccak=self.runtime_keccak,
            max_age_seconds=self.max_age_seconds,
        )


class ManifestAsset(Strict):
    """One reconciled asset. `asset_id` is the ledger's identity for it."""

    asset_id: Annotated[str, Field(min_length=1, max_length=160)]
    address: Address
    decimals: Annotated[int, Field(strict=True, ge=0, le=36)]
    runtime_keccak: str | None
    role: Literal["cash", "investment", "debt"]
    feed: ManifestFeed

    @model_validator(mode="after")
    def consistent(self) -> ManifestAsset:
        if self.address == ZERO_ADDRESS:
            if self.decimals != 18 or self.runtime_keccak is not None:
                raise ValueError("native asset requires decimals 18 and no runtime hash")
        elif self.runtime_keccak is None:
            raise ValueError("ERC-20 asset requires a runtime hash")
        if self.runtime_keccak is not None:
            object.__setattr__(self, "runtime_keccak", normalize_hash32(self.runtime_keccak))
        return self

    @property
    def is_native(self) -> bool:
        return self.address == ZERO_ADDRESS

    def usd_feed(self, chain_id: int) -> UsdFeedPolicy:
        return self.feed.policy(chain_id, self.address)


class ManifestPosition(Strict):
    """One declared v4 LP NFT whose ownership and USD value are reconciled.

    A position has no pinned oracle, so unlike a fungible asset it carries the
    identity that its retained proof must satisfy instead of a feed: two
    agreeing position snapshots at the reconciled block, the pool's slot0 window
    covering that block, and one fresh feed round per pool currency. The
    declared pool manager is proven against the state view's own `poolManager()`
    getter and its runtime hash. Both pool currencies must be declared fungible
    assets of the same chain inventory, so a position can never be valued in a
    currency the portfolio does not reconcile.
    """

    asset_id: Annotated[str, Field(min_length=1, max_length=160)]
    position_manager: Address
    position_manager_runtime_keccak: str
    token_id: Positive
    pool_id: str
    state_view: Address
    state_view_runtime_keccak: str
    pool_manager: Address
    pool_manager_runtime_keccak: str
    currency0_id: Annotated[str, Field(min_length=1, max_length=160)]
    currency1_id: Annotated[str, Field(min_length=1, max_length=160)]

    @model_validator(mode="after")
    def usable(self) -> ManifestPosition:
        if (
            self.position_manager == ZERO_ADDRESS
            or self.state_view == ZERO_ADDRESS
            or self.pool_manager == ZERO_ADDRESS
        ):
            raise ValueError("a declared position requires deployed contracts")
        for name in (
            "position_manager_runtime_keccak",
            "pool_id",
            "state_view_runtime_keccak",
            "pool_manager_runtime_keccak",
        ):
            object.__setattr__(self, name, normalize_hash32(getattr(self, name)))
        if self.currency0_id == self.currency1_id:
            raise ValueError("a declared position requires two distinct pool currencies")
        return self


class ChainInventory(Strict):
    """One reconciled account on one chain, with two independent providers."""

    chain_id: Positive
    account: Address
    primary_source: SourceName
    secondary_source: SourceName
    assets: Annotated[tuple[ManifestAsset, ...], Field(min_length=1)]
    positions: tuple[ManifestPosition, ...] = ()

    @model_validator(mode="after")
    def consistent(self) -> ChainInventory:
        if self.primary_source == self.secondary_source:
            raise ValueError("inventory requires two distinct providers")
        if int(self.account, 16) == 0:
            raise ValueError("inventory account must be nonzero")
        if len({a.asset_id for a in self.assets}) != len(self.assets):
            raise ValueError("unique asset identifiers required")
        if len({a.address for a in self.assets}) != len(self.assets):
            raise ValueError("one manifest asset per on-chain address required")
        if sum(a.is_native for a in self.assets) != 1:
            raise ValueError("exactly one native gas asset required per chain")
        for asset in self.assets:
            asset.usd_feed(self.chain_id)  # fail closed on an unusable feed pin
        if len({p.asset_id for p in self.positions}) != len(self.positions):
            raise ValueError("unique position asset identifiers required")
        if {a.asset_id for a in self.assets} & {p.asset_id for p in self.positions}:
            raise ValueError("a position cannot reuse a fungible asset identifier")
        if len({(p.position_manager, p.token_id) for p in self.positions}) != len(self.positions):
            raise ValueError("one manifest position per position manager and token id required")
        for position in self.positions:
            for currency_id in (position.currency0_id, position.currency1_id):
                if currency_id not in {a.asset_id for a in self.assets}:
                    raise ValueError("a position currency must be a declared fungible asset")
        return self

    @property
    def key(self) -> str:
        return f"{self.chain_id}:{self.account}"

    @property
    def native(self) -> ManifestAsset:
        return next(a for a in self.assets if a.is_native)

    @property
    def erc20_assets(self) -> tuple[ManifestAsset, ...]:
        return tuple(a for a in self.assets if not a.is_native)

    @property
    def asset_ids(self) -> tuple[str, ...]:
        """Every reconciled identifier on this chain, fungible assets and positions."""
        return tuple(a.asset_id for a in self.assets) + tuple(p.asset_id for p in self.positions)

    def asset(self, asset_id: str) -> ManifestAsset:
        for candidate in self.assets:
            if candidate.asset_id == asset_id:
                return candidate
        raise KeyError(asset_id)

    def position(self, asset_id: str) -> ManifestPosition:
        for candidate in self.positions:
            if candidate.asset_id == asset_id:
                return candidate
        raise KeyError(asset_id)

    def asset_by_address(self, address: str) -> ManifestAsset:
        lowered = address.lower()
        for candidate in self.assets:
            if candidate.address == lowered:
                return candidate
        raise KeyError(address)


class PortfolioManifest(Strict):
    """The manifest must exactly cover the policy it is paired with."""

    policy: GlobalRiskPolicy
    chains: Annotated[tuple[ChainInventory, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def consistent(self) -> PortfolioManifest:
        if len({c.key for c in self.chains}) != len(self.chains):
            raise ValueError("unique chain/account identities required")
        if {c.key for c in self.chains} != set(self.policy.inventory):
            raise ValueError("manifest accounts must exactly cover the policy inventory")
        for chain in self.chains:
            if set(chain.asset_ids) != set(self.policy.inventory[chain.key]):
                raise ValueError("manifest assets must exactly cover the policy inventory")
        return self

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(sorted(c.key for c in self.chains))

    @property
    def digest(self) -> str:
        return sha256_hex(canonical_json(self.model_dump(mode="json")))

    def chain(self, key: str) -> ChainInventory:
        for candidate in self.chains:
            if candidate.key == key:
                return candidate
        raise KeyError(key)

    def accounts_on(self, chain_id: int) -> frozenset[str]:
        """Every monitored account on one chain; transfers between them are internal."""
        return frozenset(c.account for c in self.chains if c.chain_id == chain_id)

    def native_asset(self, key: str) -> ManifestAsset:
        return self.chain(key).native


__all__ = [
    "ChainInventory",
    "ManifestAsset",
    "ManifestFeed",
    "ManifestPosition",
    "PortfolioManifest",
]
