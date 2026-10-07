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
from ..farming.account_gas_coverage import AccountGasRequest, replay_account_gas
from ..farming.account_inventory import (
    AccountInventoryQuorum,
    FeedAnswer,
    replay_account_inventory_evidence,
)
from ..farming.position_valuation import CurrencyPrice, PositionValuation
from ..farming.price_evidence import replay_price_observation_evidence, value_usd_micros
from ..models.common import BlockRef, EvidenceRef, normalize_address
from ..models.v4 import PoolKey
from ..risk.account_risk import AccountFrame, AssetBalance, PortfolioFrame
from ..risk.portfolio_manifest import ChainInventory, ManifestAsset, PortfolioManifest
from ..uniswap_v4.pool_id import pool_id_from_key
from .lp_signer_policy import EXECUTE_SELECTOR, EXECUTE_TYPE
from .lp_signer_sidecar import LpSidecarRequest
from .retained_position import RetainedPosition, value_retained_position


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
class PositionEvidence:
    """Retained two-provider ownership and pool-state proof for one position.

    The snapshot pair proves the position's own `ownerOf`, ticks, liquidity and
    uncollected fees at one block; the slot0 window proves the pool state that
    block. Both pairs must come from exactly the manifest's declared providers.
    """

    asset_id: str
    snapshots: tuple[EvidenceRef, EvidenceRef]
    pool_state: tuple[EvidenceRef, EvidenceRef]

    def __post_init__(self) -> None:
        object.__setattr__(self, "snapshots", tuple(self.snapshots))
        object.__setattr__(self, "pool_state", tuple(self.pool_state))
        if not self.asset_id:
            raise ValueError("position evidence requires an asset identifier")
        for name, kind in (("snapshots", "position_snapshot"), ("pool_state", "slot0_window")):
            pair: tuple[EvidenceRef, ...] = getattr(self, name)
            if len(pair) != 2 or len({reference.source for reference in pair}) != 2:
                raise ValueError(f"position evidence requires two distinct {name} providers")
            if any(reference.kind != kind for reference in pair):
                raise ValueError(f"position evidence must retain {kind} proofs")


@dataclass(frozen=True, slots=True)
class AccountObservation:
    """Latest retained reconciliation evidence for one manifest account.

    Complete native_gas_evidence, when supplied, is replayed internally instead
    of accepting native_gas_units. The separate legacy scalar declaration path
    remains available and is not authenticated outer-transaction coverage.
    """

    inventory: tuple[EvidenceRef, EvidenceRef]
    flows: tuple[AssetFlowEvidence, ...] = ()
    positions: tuple[PositionEvidence, ...] = ()
    native_gas_units: int = 0
    non_trading_native_units: int = 0
    # Optional complete raw coverage. With it, no scalar gas override is allowed.
    native_gas_evidence: AccountGasRequest | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "inventory", tuple(self.inventory))
        object.__setattr__(self, "flows", tuple(self.flows))
        object.__setattr__(self, "positions", tuple(self.positions))
        if len(self.inventory) != 2 or len({r.source for r in self.inventory}) != 2:
            raise ValueError("account observation requires two distinct inventory providers")
        if any(reference.kind != "account_inventory" for reference in self.inventory):
            raise ValueError("account observation must retain inventory proofs")
        if len({flow.asset_id for flow in self.flows}) != len(self.flows):
            raise ValueError("one flow attribution per asset required")
        if len({evidence.asset_id for evidence in self.positions}) != len(self.positions):
            raise ValueError("one position proof per position required")
        for name in ("native_gas_units", "non_trading_native_units"):
            value = getattr(self, name)
            if type(value) is not int:
                raise TypeError(f"{name} must be an integer")
        if self.native_gas_units < 0:
            raise ValueError("native gas units must be nonnegative")
        if self.native_gas_evidence is not None:
            if not isinstance(self.native_gas_evidence, AccountGasRequest):
                raise TypeError("native gas evidence requires a typed complete account request")
            if self.native_gas_units:
                raise ValueError("native gas evidence cannot also carry declared gas units")


def _position_snapshot_hash(observation: AccountObservation, asset_id: str) -> str:
    for evidence in observation.positions:
        if evidence.asset_id == asset_id:
            return evidence.snapshots[0].content_hash
    raise ValueError("position evidence is missing for a declared position")


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
        fungible = {asset.asset_id for asset in entry.assets}
        declared = set(entry.asset_ids)
        if set(units) != declared:
            raise ValueError("inventory quorum does not cover the declared assets")
        if set(feeds) != fungible:
            raise ValueError("inventory quorum does not cover the declared feeds")
        position_values = self._position_values(entry, ref, feeds, units, observation)
        prices = {
            asset.asset_id: (
                feeds[asset.asset_id].usd_micros_numerator,
                feeds[asset.asset_id].price_denominator,
                asset.decimals,
            )
            for asset in entry.assets
        }
        prices.update(
            {
                position.asset_id: (position_values[position.asset_id].usd_micros, 1, 0)
                for position in entry.positions
            }
        )
        prior = self._prior(entry.key)
        if prior is None:
            if (
                observation.flows
                or observation.native_gas_units
                or observation.non_trading_native_units
                or observation.native_gas_evidence is not None
            ):
                raise ValueError("inception baseline must not carry flow attribution")
            movements = dict.fromkeys(declared, 0)
            flow, unresolved = 0, 0
        else:
            previous_units = {asset.asset_id: asset.units for asset in prior.assets}
            if set(previous_units) != declared:
                raise ValueError("account asset inventory changed between frames")
            flows = self._flows(entry, prior, ref, observation)
            reconstruction = reconstruct_movements(
                previous_units=previous_units,
                current_units=units,
                prices=prices,
                flows=flows,
                native_asset_id=entry.native.asset_id,
                native_gas_units=self._native_gas(entry, prior, quorum, observation),
                non_trading_native_units=observation.non_trading_native_units,
                # Cash movements in these currencies can exchange wallet value
                # for the LP claim already included in NAV. Transfer evidence
                # alone cannot classify them as external funding/withdrawals;
                # neither an operator declaration nor an arbitrary counterparty
                # may subtract that activity from the account's observed loss.
                unclassified_assets=frozenset(
                    currency
                    for position in entry.positions
                    for currency in (position.currency0_id, position.currency1_id)
                ),
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
        ) + tuple(
            AssetBalance(
                asset_id=position.asset_id,
                units=units[position.asset_id],
                decimals=0,
                price_usd_micros_numerator=position_values[position.asset_id].usd_micros,
                price_denominator=1,
                price_timestamp=ref.timestamp,
                role="investment",
                # The position's value rides on its own snapshot proof; the frame's
                # inventory hash still binds the ownership and unit fact.
                evidence_hash=_position_snapshot_hash(observation, position.asset_id),
            )
            for position in entry.positions
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

    def _native_gas(
        self,
        entry: ChainInventory,
        prior: AccountFrame,
        current: AccountInventoryQuorum,
        observation: AccountObservation,
    ) -> int:
        """Rebuild all outer-account costs at this exact reconciliation interval.

        Expenses already reduce observed wallet units; this explains that debit,
        never subtracts them again from NAV or labels them LP costs. Unexplained
        reimbursements/native movement still refuse risk reconciliation.
        """
        request = observation.native_gas_evidence
        if request is None:
            return observation.native_gas_units
        opening = request.opening.ref
        if (
            normalize_address(request.account) != entry.account
            or request.providers != (entry.primary_source, entry.secondary_source)
            or request.closing != current.header
            or opening
            != BlockRef(entry.chain_id, prior.block_number, prior.block_hash, prior.timestamp)
            or request.native_policy != entry.native.usd_feed(entry.chain_id)
        ):
            raise ValueError(
                "complete gas proof differs from account/interval/providers/native feed"
            )
        gas = replay_account_gas(self.store, request)
        feed = next(f for f in current.feeds if f.asset_id == entry.native.asset_id)
        for reference in request.blocks[-1].native_prices:
            price = replay_price_observation_evidence(
                self.store, reference, header=current.header, policy=request.native_policy
            ).reconstruct_observation()
            if (price.answer, price.decimals, price.round_id, price.updated_at) != (
                feed.answer,
                feed.decimals,
                feed.round_id,
                feed.updated_at,
            ):
                raise ValueError("closing gas and inventory native USD rounds differ")
        return gas.total_native

    def _position_values(
        self,
        entry: ChainInventory,
        ref: BlockRef,
        feeds: Mapping[str, FeedAnswer],
        units: Mapping[str, int],
        observation: AccountObservation,
    ) -> dict[str, PositionValuation]:
        """Value every declared position from its retained proof at the frame block."""
        declared = {position.asset_id: position for position in entry.positions}
        if {evidence.asset_id for evidence in observation.positions} != set(declared):
            raise ValueError("position evidence must cover exactly the declared positions")
        providers = frozenset({entry.primary_source, entry.secondary_source})
        values: dict[str, PositionValuation] = {}
        for evidence in observation.positions:
            position = declared[evidence.asset_id]
            self._require_declared_sources(entry, (*evidence.snapshots, *evidence.pool_state))
            if units[position.asset_id] != 1:
                # Two independent capture paths must agree that the account owns it:
                # the inventory's `ownerOf` here and the snapshot's own answer below.
                raise ValueError("a declared position must be owned at the reconciled block")
            valuation = value_retained_position(
                self.store,
                retained=RetainedPosition(
                    chain_id=entry.chain_id,
                    position_manager=position.position_manager,
                    state_view=position.state_view,
                    pool_id=position.pool_id,
                    token_id=position.token_id,
                    state_view_runtime_keccak=position.state_view_runtime_keccak,
                    pool_manager_runtime_keccak=position.pool_manager_runtime_keccak,
                ),
                snapshots=evidence.snapshots,
                pool_state=evidence.pool_state,
                currency0=self._currency(entry, feeds, position.currency0_id),
                currency1=self._currency(entry, feeds, position.currency1_id),
                providers=providers,
                owner=entry.account,
            )
            if valuation.block != ref:
                raise ValueError("position valuation is not at the reconciled block")
            if valuation.usd_micros <= 0:
                # A zero numerator cannot be carried by the ledger, and dropping the
                # asset would silently redefine the inventory: refuse instead.
                raise ValueError("a declared position must have a positive retained value")
            values[evidence.asset_id] = valuation
        return values

    @staticmethod
    def _currency(
        entry: ChainInventory, feeds: Mapping[str, FeedAnswer], asset_id: str
    ) -> CurrencyPrice:
        asset = entry.asset(asset_id)
        return CurrencyPrice(
            address=asset.address,
            asset_id=asset.asset_id,
            decimals=asset.decimals,
            feed=feeds[asset.asset_id],
            max_age_seconds=asset.feed.max_age_seconds,
        )

    def _flows(
        self,
        entry: ChainInventory,
        prior: AccountFrame,
        end_block: BlockRef,
        observation: AccountObservation,
    ) -> dict[str, TokenFlow]:
        positions = {position.asset_id: position for position in entry.positions}
        declared = {asset.asset_id for asset in entry.erc20_assets} | set(positions)
        unknown = {flow.asset_id for flow in observation.flows} - declared
        if unknown:
            raise ValueError("flow evidence for an undeclared asset")
        tracked = self.manifest.accounts_on(entry.chain_id)
        flows: dict[str, TokenFlow] = {}
        for attribution in observation.flows:
            self._require_declared_sources(entry, attribution.references)
            position = positions.get(attribution.asset_id)
            if position is None:
                asset = entry.asset(attribution.asset_id)
                scope = FlowScope(
                    entry.chain_id,
                    entry.account,
                    asset.asset_id,
                    asset.address,
                    asset.decimals,
                    prior.block_number,
                    end_block.number,
                    start_block_hash=prior.block_hash,
                    end_block_hash=end_block.block_hash,
                )
            else:
                scope = FlowScope(
                    entry.chain_id,
                    entry.account,
                    position.asset_id,
                    position.position_manager,
                    0,
                    prior.block_number,
                    end_block.number,
                    position.token_id,
                    start_block_hash=prior.block_hash,
                    end_block_hash=end_block.block_hash,
                )
            providers = [
                replay_token_flow_evidence(
                    self.store, reference, scope=scope, tracked_accounts=tracked
                )
                for reference in attribution.references
            ]
            if any(provider != providers[0] for provider in providers[1:]):
                raise ValueError("flow providers disagree on the transfer set")
            flows[attribution.asset_id] = providers[0]
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
    "PositionEvidence",
]
