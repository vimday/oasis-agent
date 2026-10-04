"""Replay marked LP inventory, owner cash and action gas as one evidence chain.

The result is a marked-return component, never complete net PnL or an executable
exit quote. Unknown costs cannot become zero or open an economic release gate.
"""

from dataclasses import dataclass, field

from ..evidence.ingestion import replay_block_range_evidence
from ..evidence.store import EvidenceStore
from ..execution.retained_position import RetainedPosition, value_retained_position
from ..models.chain import BlockSnapshot
from ..models.common import ZERO_ADDRESS, BlockRef, EvidenceRef
from ..shadow.pool_state_capture import replay_slot0_window_evidence
from .account_inventory import FeedAnswer
from .cash_window import CashWindowRequest, EvidencePair, replay_cash_window
from .full_cost import CostCategory
from .position_action_costs import ActionCostProof
from .position_cost_valuation import (
    ActionPriceProof,
    QuotedPositionActionCosts,
    quote_position_action_costs,
)
from .position_valuation import CurrencyPrice, PositionValuation
from .price_evidence import UsdFeedPolicy, UsdPriceObservation, replay_price_observation_evidence


@dataclass(frozen=True, slots=True)
class ReturnPriceProof:
    block: BlockRef
    currency0: EvidencePair
    currency1: EvidencePair
    native_prices: EvidencePair | None = None


@dataclass(frozen=True, slots=True)
class PositionReturnRequest:
    cash_window: CashWindowRequest
    retained: RetainedPosition
    opening_pool_state: EvidencePair
    closing_pool_state: EvidencePair
    prices: tuple[ReturnPriceProof, ...]
    costs: tuple[ActionCostProof, ...]
    currency0_policy: UsdFeedPolicy
    currency1_policy: UsdFeedPolicy
    native_policy: UsdFeedPolicy


@dataclass(frozen=True, slots=True)
class MarkedCashFlow:
    block: BlockRef
    transaction_hash: str
    owner: str
    amount0: int
    amount1: int
    usd_micros: int
    evidence: tuple[EvidenceRef, ...]


@dataclass(frozen=True, slots=True)
class PositionReturnReplay:
    owner: str
    opening: PositionValuation
    closing: PositionValuation
    opening_debit_usd_micros: int
    cash_flows: tuple[MarkedCashFlow, ...]
    cumulative_cash_usd_micros: int
    marked_pnl_before_action_gas_usd_micros: int
    action_gas_usd_micros: int
    marked_pnl_after_action_gas_usd_micros: int
    quoted_action_costs: QuotedPositionActionCosts
    evidence: tuple[EvidenceRef, ...]
    missing_full_cost: tuple[str, ...]
    scope: str = field(default="unchanged_owner_marked_inventory_cash_action_gas", init=False)
    net_pnl: None = field(default=None, init=False)
    out_of_sample_accepted: bool = field(default=False, init=False)
    canary_accepted: bool = field(default=False, init=False)
    execution_eligible: bool = field(default=False, init=False)


def _price(
    store: EvidenceStore,
    refs: EvidencePair,
    header: BlockSnapshot,
    policy: UsdFeedPolicy,
    providers: tuple[str, str],
) -> UsdPriceObservation:
    if len(refs) != 2 or tuple(r.source for r in refs) != providers:
        raise ValueError("return prices must use the cash window provider pair in order")
    observations = tuple(
        replay_price_observation_evidence(
            store, ref, header=header, policy=policy
        ).reconstruct_observation()
        for ref in refs
    )
    if observations[0] != observations[1]:
        raise ValueError("return prices disagree on the complete feed round")
    return observations[0]


def _usd(units: int, observation: UsdPriceObservation, decimals: int, *, up: bool) -> int:
    numerator: int = units * observation.answer * 1_000_000
    denominator: int = 10 ** (observation.decimals + decimals)
    return -((-numerator) // denominator) if up else numerator // denominator


def replay_position_return(
    store: EvidenceStore, request: PositionReturnRequest
) -> PositionReturnReplay:
    """Re-derive every numeric input; require unchanged ownership and exact coverage.

    LP claim amounts use the pool's observed slot0, while dollar valuation uses
    each currency's own USD feed. Deposits round away from zero; receipts and
    closing NAV round down; opening debit and action gas round up. No feed ratio
    is passed off as an executable pool price and no missing expense is waived.
    """
    cash = replay_cash_window(store, request.cash_window)
    ledger = cash.ledger
    ownership = ledger.ownership_replay
    if (
        ownership is None
        or ledger.opening.nft_owner != ledger.closing.nft_owner
        or any(t.from_owner != t.to_owner for t in ownership.transfers)
    ):
        raise ValueError("marked return requires unchanged owner across complete NFT event range")
    if not cash.payments:
        raise ValueError("raw payment metadata required for both currency scales")
    assets = cash.payments[0].amounts
    scales = (assets.token0.decimals, assets.token1.decimals)
    if any(d > 36 for d in scales):
        raise ValueError("return currency scales must be bounded")
    key = ledger.opening.pool_key
    if (
        request.currency0_policy.base_token != key.currency0
        or request.currency1_policy.base_token != key.currency1
        or request.native_policy.base_token != ZERO_ADDRESS
    ):
        raise ValueError("return USD feed denominations differ from currencies or native gas")
    providers = (request.cash_window.opening[0].source, request.cash_window.opening[1].source)
    core = replay_block_range_evidence(store, request.cash_window.core[0])
    headers = {h.ref: h for h in core.blocks}
    required = {ledger.opening.block, ledger.closing.block, *(e.block for e in ledger.entries)}
    if len(request.prices) != len(required) or {p.block for p in request.prices} != required:
        raise ValueError("return prices must cover exactly opening, closing and every action block")
    price_proofs = {p.block: p for p in request.prices}
    action_blocks = {e.block for e in ledger.entries}
    if key.currency0 == ZERO_ADDRESS and request.native_policy != request.currency0_policy:
        raise ValueError("native inventory and gas must use the same native/USD policy")
    rounds = {}
    for point in request.prices:
        if point.block not in action_blocks and point.native_prices is not None:
            raise ValueError("unused endpoint native gas price proof refused")
        if key.currency0 == ZERO_ADDRESS and point.native_prices not in (None, point.currency0):
            raise ValueError("native inventory and gas must use the same raw native/USD proof")
        header = headers[point.block]
        rounds[point.block] = (
            _price(store, point.currency0, header, request.currency0_policy, providers),
            _price(store, point.currency1, header, request.currency1_policy, providers),
        )
    action_prices = []
    native_rounds = {}
    for entry in ledger.entries:
        proof = price_proofs[entry.block]
        native = proof.native_prices
        if native is None:
            if key.currency0 != ZERO_ADDRESS or request.native_policy != request.currency0_policy:
                raise ValueError("each action requires its own native/USD gas price proof")
            native = proof.currency0
        native_rounds[entry.block] = _price(
            store, native, headers[entry.block], request.native_policy, providers
        )
        action_prices.append(ActionPriceProof(entry.transaction_hash, native, proof.currency1))
    gas = quote_position_action_costs(
        store,
        request.cash_window,
        request.costs,
        tuple(action_prices),
        native_policy=request.native_policy,
        quote_policy=request.currency1_policy,
    )

    def endpoint(snapshots: EvidencePair, pool_state: EvidencePair) -> PositionValuation:
        point = snapshots[0].block
        if point is None:
            raise ValueError("position snapshot needs its own block")
        for ref in pool_state:
            state = replay_slot0_window_evidence(store, ref)
            if state.pool_manager != request.cash_window.pool_manager:
                raise ValueError("endpoint state PoolManager differs from cash window")
        currencies = tuple(
            CurrencyPrice(
                policy.base_token,
                f"currency{i}",
                scales[i],
                FeedAnswer(f"currency{i}", r.answer, r.decimals, r.round_id, r.updated_at),
                policy.max_age_seconds,
            )
            for i, (policy, r) in enumerate(
                zip(
                    (request.currency0_policy, request.currency1_policy), rounds[point], strict=True
                )
            )
        )
        return value_retained_position(
            store,
            retained=request.retained,
            snapshots=snapshots,
            pool_state=pool_state,
            currency0=currencies[0],
            currency1=currencies[1],
            providers=frozenset(providers),
            owner=ledger.opening.nft_owner,
        )

    opening = endpoint(request.cash_window.opening, request.opening_pool_state)
    closing = endpoint(request.cash_window.closing, request.closing_pool_state)
    opening_debit = sum(
        _usd(units, r, decimals, up=True)
        for units, r, decimals in zip(
            (opening.total0, opening.total1), rounds[opening.block], scales, strict=True
        )
    )
    flows = []
    for payment in cash.payments:
        block = payment.entry.block
        proof = price_proofs[block]
        amounts = payment.amounts
        value = sum(
            _usd(units, r, decimals, up=False)
            for units, r, decimals in zip(
                (amounts.amount0, amounts.amount1), rounds[block], scales, strict=True
            )
        )
        flows.append(
            MarkedCashFlow(
                block,
                payment.entry.transaction_hash,
                payment.owner,
                amounts.amount0,
                amounts.amount1,
                value,
                (*payment.evidence, *proof.currency0, *proof.currency1),
            )
        )
    cash_value = sum(f.usd_micros for f in flows)
    gas_value = sum(_usd(a.native_units, native_rounds[a.block], 18, up=True) for a in gas.actions)
    gross = closing.usd_micros + cash_value - opening_debit
    refs = (
        *request.cash_window.opening,
        *request.cash_window.closing,
        *request.cash_window.core,
        *request.cash_window.ownership,
        *request.opening_pool_state,
        *request.closing_pool_state,
        *(ref for p in request.prices for ref in (*p.currency0, *p.currency1)),
        *(ref for f in flows for ref in f.evidence),
        *(ref for a in gas.actions for ref in a.evidence),
    )
    return PositionReturnReplay(
        ledger.opening.nft_owner,
        opening,
        closing,
        opening_debit,
        tuple(flows),
        cash_value,
        gross,
        gas_value,
        gross - gas_value,
        gas,
        tuple(dict.fromkeys(refs)),
        (
            *(c.value for c in CostCategory if c is not CostCategory.GAS),
            "complete_account_activity_and_failed_attempts",
            "baselines_and_economic_attribution",
        ),
    )
