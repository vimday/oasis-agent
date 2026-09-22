"""Reconstruct a complete position cash window exclusively from retained RPC evidence."""

from dataclasses import asdict, dataclass, replace

from ..evidence.ingestion import (
    IngestionTrust,
    VerifiedBlockRange,
    replay_block_range_evidence,
)
from ..evidence.store import EvidenceStore
from ..models.common import EvidenceRef, canonical_json, normalize_address
from ..models.finance import AssetPair
from ..rpc.collector import require_provider_agreement
from .owner_quorum import OwnerCashQuorum, replay_owner_cash_quorum
from .position_capture import replay_position_snapshot_evidence
from .position_dataset import replay_position_datasets
from .position_ledger import PositionTokenLedger, SettlementAttribution, build_position_token_ledger
from .position_quorum import PositionQuorum

EvidencePair = tuple[EvidenceRef, EvidenceRef]


@dataclass(frozen=True, slots=True)
class CashWindowRequest:
    pool_manager: str
    opening: EvidencePair
    closing: EvidencePair
    core: EvidencePair
    ownership: EvidencePair
    payments: tuple[EvidencePair, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "pool_manager", normalize_address(self.pool_manager))


@dataclass(frozen=True, slots=True)
class ReconciledCashWindow:
    request: CashWindowRequest
    ledger: PositionTokenLedger
    payments: tuple[OwnerCashQuorum, ...]

    @property
    def cash_by_owner(self) -> dict[str, AssetPair]:
        totals: dict[str, AssetPair] = {}
        for payment in self.payments:
            prior = totals.get(payment.owner, payment.amounts.zero())
            totals[payment.owner] = prior + payment.amounts
        return totals

    @property
    def payload(self) -> bytes:
        return canonical_json(
            {
                "schema": "oasis.position-cash-window.v1",
                "request": asdict(self.request),
                "ledger": asdict(self.ledger),
                "payments": [asdict(payment) for payment in self.payments],
                "cash_by_owner": {
                    owner: asdict(amounts) for owner, amounts in self.cash_by_owner.items()
                },
                "ledger_totals_scope": "position window across owners, not an account balance",
                "amount_unit": "integer base units of the ordered PoolKey currencies",
                "sign_convention": "positive owner inflow, negative owner outflow",
                "execution_eligible": False,
                "net_pnl": None,
                "unresolved": [
                    "deployment_and_token_runtime_attestation",
                    "finality",
                    "valuation_and_all_transaction_costs",
                    "whole_account_inventory",
                ],
            }
        )


def _sources(refs: EvidencePair) -> frozenset[str]:
    if len(refs) != 2 or len({ref.source for ref in refs}) != 2:
        raise ValueError("each window witness requires two distinct sources")
    return frozenset(ref.source for ref in refs)


def _range(store: EvidenceStore, refs: EvidencePair) -> VerifiedBlockRange:
    datasets = tuple(replay_block_range_evidence(store, ref) for ref in refs)
    agreement = require_provider_agreement(datasets)
    return VerifiedBlockRange(
        datasets[0], refs[0], IngestionTrust.PROVIDER_QUORUM, agreement, (refs[1],)
    )


def replay_cash_window(store: EvidenceStore, request: CashWindowRequest) -> ReconciledCashWindow:
    """Bind two-source snapshots, full event lineage, action-time ownership and raw cash.

    Payment pairs must cover every replayed modification exactly once in event order.
    No caller-supplied cash amount or pre-resolved ledger entry is accepted. This is
    token cash settlement, not NAV, gas valuation, finality or release authority.
    """
    sources = _sources(request.opening)
    for refs in (request.closing, request.core, request.ownership, *request.payments):
        if _sources(refs) != sources:
            raise ValueError("window source cohort changed")
    opening = PositionQuorum(
        replay_position_snapshot_evidence(store, request.opening[0]),
        replay_position_snapshot_evidence(store, request.opening[1]),
    )
    closing = PositionQuorum(
        replay_position_snapshot_evidence(store, request.closing[0]),
        replay_position_snapshot_evidence(store, request.closing[1]),
    )
    if opening.left.state_view != closing.left.state_view:
        raise ValueError("StateView changed across cash window")
    core, ownership = _range(store, request.core), _range(store, request.ownership)
    position = replay_position_datasets(
        opening.left.snapshot,
        closing.left.snapshot,
        pool_manager=request.pool_manager,
        core=core,
        ownership=ownership,
    )
    ledger = position.ledger
    if len(request.payments) != len(ledger.entries):
        raise ValueError("cash evidence must cover every modification exactly once")
    if ledger.ownership_replay is None:
        raise ValueError("complete action-time ownership replay required")
    headers = {header.ref.number: header for header in core.dataset.blocks}
    payments: list[OwnerCashQuorum] = []
    resolved = []
    for entry, refs in zip(ledger.entries, request.payments, strict=True):
        owner = ledger.opening.nft_owner
        for transfer in ledger.ownership_replay.transfers:
            if transfer.position < entry.position:
                owner = transfer.to_owner
        payment = replay_owner_cash_quorum(
            store,
            refs,
            header=headers[entry.block.number],
            pool_manager=request.pool_manager,
            pool_key=ledger.opening.pool_key,
            entry=entry,
            owner=owner,
        )
        if payments:
            # Even across owners, token decimals/order must not drift mid-window.
            payments[0].amounts.zero() + payment.amounts.zero()
        payments.append(payment)
        resolved.append(
            replace(
                entry,
                owner_amount0=payment.amounts.amount0,
                owner_amount1=payment.amounts.amount1,
                settlement_attribution=SettlementAttribution.EXCLUSIVE_TRANSACTION,
                settlement_evidence=refs[0],
            )
        )
    reconciled = build_position_token_ledger(
        ledger.opening,
        ledger.closing,
        tuple(resolved),
        canonical_blocks=core.dataset.blocks,
        ownership_replay=ledger.ownership_replay,
    )
    return ReconciledCashWindow(request, reconciled, tuple(payments))


def require_cash_window_successor(
    previous: ReconciledCashWindow, following: ReconciledCashWindow
) -> None:
    """Require an identical boundary snapshot; never bridge an unobserved interval."""
    if previous.ledger.closing != following.ledger.opening:
        raise ValueError("cash windows have a gap, reorg or changed boundary snapshot")
    if previous.request.pool_manager != following.request.pool_manager:
        raise ValueError("cash window PoolManager changed")
    if _sources(previous.request.closing) != _sources(following.request.opening):
        raise ValueError("cash window provider cohort changed")
