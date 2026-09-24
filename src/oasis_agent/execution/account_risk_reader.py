"""Evidence-bound account reader for the signer risk boundary.

Replays retained, hash-pinned raw proofs from the evidence store into one
complete reconciled portfolio frame and prices the maximum incremental
principal plus gas for a signing request. It holds no key and exposes no
signing or broadcast interface. Nothing loads it unless an operator wires it
explicitly, so the default signer guard still denies every attempt.

The reader is only as trustworthy as the evidence an operator retains: it does
not fetch, value or attest anything on its own, and it never weakens a ledger
check. A missing, stale, disagreeing or unexplained input raises instead of
becoming a permissive default.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from eth_abi.abi import decode, encode
from eth_abi.exceptions import DecodingError

from ..evidence.store import EvidenceStore
from ..farming.account_flows import (
    FlowScope,
    TokenFlow,
    reconstruct_movements,
    replay_token_flow_evidence,
)
from ..farming.account_inventory import (
    AccountInventoryQuorum,
    FeedAnswer,
    replay_account_inventory_evidence,
)
from ..farming.price_evidence import value_usd_micros
from ..models.common import EvidenceRef
from ..models.v4 import PoolKey
from ..risk.account_risk import AccountFrame, AssetBalance, PortfolioFrame
from ..risk.portfolio_manifest import ChainInventory, ManifestAsset, PortfolioManifest
from ..uniswap_v4.pool_id import pool_id_from_key
from .lp_signer_policy import EXECUTE_SELECTOR, EXECUTE_TYPE
from .lp_signer_sidecar import LpSidecarRequest


@dataclass(frozen=True, slots=True)
class AssetFlowEvidence:
    """Retained two-provider ERC-20 movement attribution for one asset."""

    asset_id: str
    references: tuple[EvidenceRef, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "references", tuple(self.references))
        if not self.asset_id:
            raise ValueError("asset flow evidence requires an asset identifier")
        if len(self.references) < 2 or len({r.source for r in self.references}) != len(
            self.references
        ):
            raise ValueError("asset flow evidence requires two distinct providers")
        if any(reference.kind != "account_token_flow" for reference in self.references):
            raise ValueError("asset flow evidence must retain token flow proofs")


@dataclass(frozen=True, slots=True)
class AccountObservation:
    """Latest retained reconciliation evidence for one manifest account."""

    inventory: tuple[EvidenceRef, EvidenceRef]
    flows: tuple[AssetFlowEvidence, ...] = ()
    native_gas_units: int = 0
    non_trading_native_units: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "inventory", tuple(self.inventory))
        object.__setattr__(self, "flows", tuple(self.flows))
        if len(self.inventory) != 2 or len({r.source for r in self.inventory}) != 2:
            raise ValueError("account observation requires two distinct inventory providers")
        if any(reference.kind != "account_inventory" for reference in self.inventory):
            raise ValueError("account observation must retain inventory proofs")
        if len({flow.asset_id for flow in self.flows}) != len(self.flows):
            raise ValueError("one flow attribution per asset required")
        for name in ("native_gas_units", "non_trading_native_units"):
            value = getattr(self, name)
            if type(value) is not int:
                raise TypeError(f"{name} must be an integer")
        if self.native_gas_units < 0:
            raise ValueError("native gas units must be nonnegative")


def _pool_currencies(calldata: bytes, *, pool_id: str) -> tuple[str, str]:
    """Recover the signed pool currencies from the bounded executor calldata."""
    if not isinstance(calldata, bytes) or calldata[:4] != EXECUTE_SELECTOR:
        raise ValueError("signing calldata is not a bounded executor call")
    try:
        decoded = decode([EXECUTE_TYPE], calldata[4:])[0]
        if EXECUTE_SELECTOR + encode([EXECUTE_TYPE], [decoded]) != calldata:
            raise ValueError("signing calldata is not canonical")
        pool_values = decoded[3]
    except (DecodingError, IndexError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError("signing calldata is malformed or noncanonical") from exc
    pool_key = PoolKey(*pool_values)
    if pool_id_from_key(pool_key) != pool_id:
        raise ValueError("signing calldata pool does not match the declared intent")
    return pool_key.currency0, pool_key.currency1


def _unit_price_ratio(asset: ManifestAsset, feed: FeedAnswer) -> tuple[int, int]:
    return feed.usd_micros_numerator, feed.price_denominator * 10**asset.decimals


def _pricier(
    left: ManifestAsset, right: ManifestAsset, feeds: Mapping[str, FeedAnswer]
) -> ManifestAsset:
    """The more valuable pool currency: a conservative denomination for a swap cap."""
    left_num, left_den = _unit_price_ratio(left, feeds[left.asset_id])
    right_num, right_den = _unit_price_ratio(right, feeds[right.asset_id])
    return left if left_num * right_den >= right_num * left_den else right


class EvidenceReplayAccountRiskReader:
    """Replays retained proofs into a complete frame and a worst-case commitment."""

    def __init__(
        self,
        store: EvidenceStore,
        manifest: PortfolioManifest,
        observations: Mapping[str, AccountObservation],
        *,
        previous: PortfolioFrame | None = None,
    ) -> None:
        if set(observations) != set(manifest.keys):
            raise ValueError("observations must exactly cover the manifest accounts")
        if previous is not None and {a.key for a in previous.accounts} != set(manifest.keys):
            raise ValueError("previous frame must cover exactly the manifest accounts")
        self.store = store
        self.manifest = manifest
        self.observations = dict(observations)
        self.previous = previous

    def reconcile(self) -> PortfolioFrame:
        """Rebuild the complete frame; raises on any missing or contradictory proof."""
        return PortfolioFrame(accounts=self._build()[0])

    def reconcile_and_price(self, request: LpSidecarRequest) -> tuple[PortfolioFrame, int]:
        frames, valuations = self._build()
        signing = request.signing
        key = f"{signing.intent.chain_id}:{signing.account}"
        signed = next((frame for frame in frames if frame.key == key), None)
        if signed is None:
            # A bounded refusal, never an escaping StopIteration: the signer's
            # admission boundary must be able to name an unreconciled account.
            raise ValueError("signing account is not a reconciled manifest account")
        if (
            signed.block_number != signing.current_block.number
            or signed.block_hash != signing.current_block.block_hash
        ):
            raise ValueError("reader account block differs from the signing request")
        return (
            PortfolioFrame(accounts=frames),
            self._worst_case(request, self.manifest.chain(key), valuations[key]),
        )

    def _build(self) -> tuple[tuple[AccountFrame, ...], dict[str, dict[str, FeedAnswer]]]:
        frames: list[AccountFrame] = []
        valuations: dict[str, dict[str, FeedAnswer]] = {}
        for chain in self.manifest.chains:
            frame, feeds = self._account_frame(chain)
            frames.append(frame)
            valuations[chain.key] = feeds
        return tuple(frames), valuations

    def _account_frame(self, entry: ChainInventory) -> tuple[AccountFrame, dict[str, FeedAnswer]]:
        observation = self.observations[entry.key]
        self._require_declared_sources(entry, observation.inventory)
        left = replay_account_inventory_evidence(self.store, observation.inventory[0], entry=entry)
        right = replay_account_inventory_evidence(self.store, observation.inventory[1], entry=entry)
        quorum = AccountInventoryQuorum(left, right)
        ref = quorum.header.ref
        if ref.timestamp is None or ref.chain_id != entry.chain_id:
            raise ValueError("inventory quorum is not bound to the manifest chain")
        units = {unit.asset_id: unit.units for unit in quorum.units}
        feeds = {feed.asset_id: feed for feed in quorum.feeds}
        declared = {asset.asset_id for asset in entry.assets}
        if set(units) != declared or set(feeds) != declared:
            raise ValueError("inventory quorum does not cover the declared assets")
        prices = {
            asset.asset_id: (
                feeds[asset.asset_id].usd_micros_numerator,
                feeds[asset.asset_id].price_denominator,
                asset.decimals,
            )
            for asset in entry.assets
        }
        prior = self._prior(entry.key)
        if prior is None:
            if (
                observation.flows
                or observation.native_gas_units
                or observation.non_trading_native_units
            ):
                raise ValueError("inception baseline must not carry flow attribution")
            movements = dict.fromkeys(declared, 0)
            flow, unresolved = 0, 0
        else:
            previous_units = {asset.asset_id: asset.units for asset in prior.assets}
            if set(previous_units) != declared:
                raise ValueError("account asset inventory changed between frames")
            flows = self._flows(entry, prior, ref.number, observation)
            reconstruction = reconstruct_movements(
                previous_units=previous_units,
                current_units=units,
                prices=prices,
                flows=flows,
                native_asset_id=entry.native.asset_id,
                native_gas_units=observation.native_gas_units,
                non_trading_native_units=observation.non_trading_native_units,
            )
            movements = reconstruction.movements
            flow = reconstruction.non_trading_flow_usd_micros
            unresolved = reconstruction.unresolved_operations
        assets = tuple(
            AssetBalance(
                asset_id=asset.asset_id,
                units=units[asset.asset_id],
                decimals=asset.decimals,
                price_usd_micros_numerator=feeds[asset.asset_id].usd_micros_numerator,
                price_denominator=feeds[asset.asset_id].price_denominator,
                price_timestamp=ref.timestamp,
                role=asset.role,
                evidence_hash=observation.inventory[0].content_hash,
            )
            for asset in entry.assets
        )
        return (
            AccountFrame(
                chain_id=entry.chain_id,
                account=entry.account,
                block_number=ref.number,
                block_hash=ref.block_hash,
                timestamp=ref.timestamp,
                assets=assets,
                movements=movements,
                non_trading_flow_usd_micros=flow,
                inventory_evidence_hash=observation.inventory[0].content_hash,
                unresolved_operations=unresolved,
            ),
            feeds,
        )

    def _flows(
        self,
        entry: ChainInventory,
        prior: AccountFrame,
        end_block: int,
        observation: AccountObservation,
    ) -> dict[str, TokenFlow]:
        declared = {asset.asset_id for asset in entry.erc20_assets}
        unknown = {flow.asset_id for flow in observation.flows} - declared
        if unknown:
            raise ValueError("flow evidence for an undeclared asset")
        tracked = self.manifest.accounts_on(entry.chain_id)
        flows: dict[str, TokenFlow] = {}
        for attribution in observation.flows:
            self._require_declared_sources(entry, attribution.references)
            asset = entry.asset(attribution.asset_id)
            scope = FlowScope(
                entry.chain_id,
                entry.account,
                asset.asset_id,
                asset.address,
                asset.decimals,
                prior.block_number,
                end_block,
            )
            providers = [
                replay_token_flow_evidence(
                    self.store, reference, scope=scope, tracked_accounts=tracked
                )
                for reference in attribution.references
            ]
            if any(provider != providers[0] for provider in providers[1:]):
                raise ValueError("flow providers disagree on the transfer set")
            flows[asset.asset_id] = providers[0]
        return flows

    def _prior(self, key: str) -> AccountFrame | None:
        if self.previous is None:
            return None
        return next(account for account in self.previous.accounts if account.key == key)

    @staticmethod
    def _require_declared_sources(
        entry: ChainInventory, references: tuple[EvidenceRef, ...]
    ) -> None:
        """Only the manifest's declared, independent providers may support a frame.

        Two arbitrary distinct sources are not enough: the manifest names who the
        reconciliation trusts, so retained proofs from anyone else are refused
        instead of silently satisfying a different trust assumption.
        """
        declared = {entry.primary_source, entry.secondary_source}
        if {reference.source for reference in references} != declared:
            raise ValueError("evidence must come from the declared manifest providers")

    def _worst_case(
        self, request: LpSidecarRequest, entry: ChainInventory, feeds: Mapping[str, FeedAnswer]
    ) -> int:
        signing = request.signing
        intent = signing.intent
        if type(request.max_fee_per_gas) is not int or request.max_fee_per_gas < 0:
            raise ValueError("signing request requires a nonnegative gas price cap")
        currency0, currency1 = _pool_currencies(signing.calldata, pool_id=intent.pool_id)
        try:
            asset0 = entry.asset_by_address(currency0)
            asset1 = entry.asset_by_address(currency1)
        except KeyError as exc:
            raise ValueError("pool currency is not a declared inventory asset") from exc
        total = self._value(asset0, feeds, intent.amount0_max)
        total += self._value(asset1, feeds, intent.amount1_max)
        swap_asset = _pricier(asset0, asset1, feeds)
        total += self._value(swap_asset, feeds, intent.max_swap_input)
        gas_units = signing.gas_limit * request.max_fee_per_gas
        total += self._value(entry.native, feeds, gas_units + signing.value)
        if total <= 0:
            raise ValueError("worst-case commitment must be positive")
        return total

    @staticmethod
    def _value(asset: ManifestAsset, feeds: Mapping[str, FeedAnswer], units: int) -> int:
        feed = feeds[asset.asset_id]
        return value_usd_micros(
            units,
            numerator=feed.usd_micros_numerator,
            denominator=feed.price_denominator,
            decimals=asset.decimals,
            round_up=True,
        )


__all__ = [
    "AccountObservation",
    "AssetFlowEvidence",
    "EvidenceReplayAccountRiskReader",
]
