"""Two-provider acquisition of one complete reconciled account observation.

This turns the observe-only reconciliation CLI's acquisition path into a library so
a signing admission boundary can rebuild a block-pinned observation right before it
reserves capital, instead of consuming an observation captured at some other block.
It acquires and retains raw proofs only: it never values an account, never admits a
commitment, never signs and never broadcasts, and it holds no key material.

Every capture is two-provider and one-shot. A session that already produced
exchanges cannot be relabelled, so a partial or reused capture is refused rather
than silently attributed to a second source. Provider names must be exactly the
manifest's declared pair, so retained proofs always satisfy the manifest's own
trust assumption.

A capture that fails is not an empty market or a zero balance: every helper raises,
and no caller in this module substitutes a default.
"""

from collections.abc import Callable, Mapping
from pathlib import Path

from ..evidence.exact_capture import RecordedSession, validate_session_pair
from ..evidence.rpc_capture import BoundedExchangeRecorder
from ..evidence.store import EvidenceStore
from ..farming.account_flows import (
    FlowScope,
    collect_token_flow_quorum,
    persist_token_flow_evidence,
)
from ..farming.account_inventory import (
    AccountInventoryQuorum,
    collect_account_inventory_quorum,
    persist_account_inventory_evidence,
)
from ..farming.position_quorum import collect_position_quorum
from ..risk.account_risk import AccountFrame
from ..risk.portfolio_manifest import ChainInventory
from ..rpc.budget import BudgetedPoster, BudgetPolicy
from ..rpc.transport import JsonRpcTransport, _UrlLibPoster
from ..shadow.pool_state_capture import (
    collect_slot0_window_quorum,
    persist_slot0_window_evidence,
)
from .account_risk_reader import AccountObservation, AssetFlowEvidence, PositionEvidence

type SessionPairFactory = Callable[[tuple[str, str]], tuple[RecordedSession, RecordedSession]]
"""Builds one fresh one-shot session per provider alias, in the given order."""

type SessionFactory = Callable[[str], RecordedSession]
"""Builds one fresh one-shot session for one provider alias."""


def one_shot_session(
    source: str,
    *,
    urls: Mapping[str, str],
    policies: tuple[BudgetPolicy, ...],
    budget: Path,
    require_existing_budget: bool = False,
) -> RecordedSession:
    """One fresh session for one declared provider, sharing the caller's budget ledger.

    An exact capture may not reuse a recorder, so a new transport and a new recorder
    are built for every call: a caller that needs a lookup before the capture itself
    (locating a receipt's block, for example) must not spend the capture's own session.
    """
    recorder = BoundedExchangeRecorder()
    http = _UrlLibPoster(urls[source], 15)
    poster = (
        BudgetedPoster(http, budget, policies, source, require_existing=True)
        if require_existing_budget
        else BudgetedPoster(http, budget, policies, source)
    )
    return (
        JsonRpcTransport(
            "https://unused.invalid",
            http=poster,
            retries=0,
            recorder=recorder,
        ),
        recorder,
        source,
    )


def one_shot_sessions(
    sources: tuple[str, str],
    *,
    urls: Mapping[str, str],
    policies: tuple[BudgetPolicy, ...],
    budget: Path,
) -> tuple[RecordedSession, RecordedSession]:
    """One fresh session per declared provider, validated as a distinct pair."""
    pair = (
        one_shot_session(sources[0], urls=urls, policies=policies, budget=budget),
        one_shot_session(sources[1], urls=urls, policies=policies, budget=budget),
    )
    validate_session_pair(*pair)
    return pair


def capture_inventory(
    entry: ChainInventory, *, block: int, sessions: SessionPairFactory
) -> AccountInventoryQuorum:
    """Both providers' balances, ownership and feed answers at one pinned block."""
    pair = sessions((entry.primary_source, entry.secondary_source))
    return collect_account_inventory_quorum(pair[0], pair[1], entry=entry, block_number=block)


def capture_positions(
    store: EvidenceStore, entry: ChainInventory, *, block: int, sessions: SessionPairFactory
) -> tuple[PositionEvidence, ...]:
    """Each declared position's ownership proof and the pool state at that block."""
    collected: list[PositionEvidence] = []
    for position in entry.positions:
        pair = sessions((entry.primary_source, entry.secondary_source))
        snapshots = collect_position_quorum(
            pair[0],
            pair[1],
            chain_id=entry.chain_id,
            position_manager=position.position_manager,
            state_view=position.state_view,
            token_id=position.token_id,
            block_number=block,
        ).retain(store)
        window = sessions((entry.primary_source, entry.secondary_source))
        pool_state = persist_slot0_window_evidence(
            store,
            collect_slot0_window_quorum(
                window[0],
                window[1],
                chain_id=entry.chain_id,
                state_view=position.state_view,
                pool_manager=position.pool_manager,
                pool_id=position.pool_id,
                block_numbers=(block,),
                state_view_runtime_keccak=position.state_view_runtime_keccak,
                pool_manager_runtime_keccak=position.pool_manager_runtime_keccak,
            ),
        )
        collected.append(PositionEvidence(position.asset_id, snapshots, pool_state))
    return tuple(collected)


def capture_flows(
    store: EvidenceStore,
    entry: ChainInventory,
    *,
    prior_block: int,
    end_block: int,
    prior_block_hash: str,
    end_block_hash: str,
    sessions: SessionPairFactory,
) -> tuple[AssetFlowEvidence, ...]:
    """Attribute every declared ERC-20 and position delta from complete provider logs."""
    scopes: list[FlowScope] = [
        FlowScope(
            entry.chain_id,
            entry.account,
            asset.asset_id,
            asset.address,
            asset.decimals,
            prior_block,
            end_block,
            start_block_hash=prior_block_hash,
            end_block_hash=end_block_hash,
        )
        for asset in entry.erc20_assets
    ]
    scopes.extend(
        FlowScope(
            entry.chain_id,
            entry.account,
            position.asset_id,
            position.position_manager,
            0,
            prior_block,
            end_block,
            position.token_id,
            start_block_hash=prior_block_hash,
            end_block_hash=end_block_hash,
        )
        for position in entry.positions
    )
    collected: list[AssetFlowEvidence] = []
    for scope in scopes:
        pair = sessions((entry.primary_source, entry.secondary_source))
        quorum = collect_token_flow_quorum(pair[0], pair[1], scope=scope)
        collected.append(
            AssetFlowEvidence(scope.asset_id, persist_token_flow_evidence(store, quorum))
        )
    return tuple(collected)


def capture_observation(
    store: EvidenceStore,
    entry: ChainInventory,
    *,
    block: int,
    previous: AccountFrame | None,
    native_gas_units: int,
    non_trading_native_units: int,
    sessions: SessionPairFactory,
) -> AccountObservation:
    """One complete observation: a fresh block-pinned baseline plus what moved since.

    With no previous frame this is an inception baseline and carries no flow
    attribution at all: an inception baseline must be an explicit zero-flow
    statement, so a native declaration here is refused instead of silently
    explaining movement nobody observed.
    """
    quorum = capture_inventory(entry, block=block, sessions=sessions)
    ref = quorum.header.ref
    references = persist_account_inventory_evidence(store, quorum)
    positions = capture_positions(store, entry, block=ref.number, sessions=sessions)
    if previous is None:
        if native_gas_units or non_trading_native_units:
            raise ValueError("an inception baseline cannot declare native movement")
        return AccountObservation(inventory=references, positions=positions)
    if ref.number <= previous.block_number:
        raise ValueError("captured block does not advance the previous frame")
    return AccountObservation(
        inventory=references,
        flows=capture_flows(
            store,
            entry,
            prior_block=previous.block_number,
            end_block=ref.number,
            prior_block_hash=previous.block_hash,
            end_block_hash=ref.block_hash,
            sessions=sessions,
        ),
        positions=positions,
        native_gas_units=native_gas_units,
        non_trading_native_units=non_trading_native_units,
    )


__all__ = [
    "SessionFactory",
    "SessionPairFactory",
    "capture_flows",
    "capture_inventory",
    "capture_observation",
    "capture_positions",
    "one_shot_session",
    "one_shot_sessions",
]
