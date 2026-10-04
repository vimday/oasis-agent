"""Marked passive-LP observations for windows with no liquidity modification.

An empty complete event range proves no LP action, not absence of other account
activity or failed gas. Full net PnL and release eligibility remain unresolved.
"""

from dataclasses import dataclass, field

from ..evidence.ingestion import replay_block_range_evidence
from ..evidence.store import EvidenceStore
from ..execution.retained_position import RetainedPosition, value_retained_position
from ..models.chain import BlockSnapshot
from ..models.common import EvidenceRef
from ..shadow.pool_state_capture import replay_slot0_window_evidence
from .account_inventory import FeedAnswer
from .cash_window import CashWindowRequest, EvidencePair, replay_cash_window
from .full_cost import CostCategory
from .position_return_replay import _price, _usd
from .position_valuation import CurrencyPrice, PositionValuation
from .price_evidence import UsdFeedPolicy, UsdPriceObservation
from .token_identity import TokenIdentityPin, TokenIdentityQuorum, replay_token_identity_evidence


@dataclass(frozen=True, slots=True)
class HoldEndpointProof:
    currency0_identity: EvidencePair
    currency1_identity: EvidencePair
    currency0_price: EvidencePair
    currency1_price: EvidencePair

    @property
    def evidence(self) -> tuple[EvidenceRef, ...]:
        return (
            *self.currency0_identity,
            *self.currency1_identity,
            *self.currency0_price,
            *self.currency1_price,
        )


@dataclass(frozen=True, slots=True)
class PositionHoldRequest:
    cash_window: CashWindowRequest
    retained: RetainedPosition
    opening_pool_state: EvidencePair
    closing_pool_state: EvidencePair
    opening_marks: HoldEndpointProof
    closing_marks: HoldEndpointProof
    currency0_policy: UsdFeedPolicy
    currency1_policy: UsdFeedPolicy


@dataclass(frozen=True, slots=True)
class PositionHoldReturn:
    owner: str
    opening: PositionValuation
    closing: PositionValuation
    currencies: tuple[TokenIdentityPin, TokenIdentityPin]
    opening_debit_usd_micros: int
    hold_closing_debit_usd_micros: int
    marked_lp_pnl_usd_micros: int
    marked_hold_pnl_usd_micros: int
    marked_excess_vs_hold_usd_micros: int
    principal_vs_hold_usd_micros: int
    uncollected_fee_change_at_closing_usd_micros: int
    decomposition_rounding_usd_micros: int
    evidence: tuple[EvidenceRef, ...]
    missing_full_cost: tuple[str, ...]
    scope: str = field(default="unchanged_owner_no_lp_action_marked_hold_comparison", init=False)
    observed_lp_action_count: int = field(default=0, init=False)
    observed_modification_cash_units: tuple[int, int] = field(default=(0, 0), init=False)
    net_pnl: None = field(default=None, init=False)
    out_of_sample_accepted: bool = field(default=False, init=False)
    canary_accepted: bool = field(default=False, init=False)
    execution_eligible: bool = field(default=False, init=False)


def replay_position_hold_return(
    store: EvidenceStore, request: PositionHoldRequest
) -> PositionHoldReturn:
    """Observe HOLD rather than requiring an LP transaction to materialize a mark.

    Token decimals and runtime are replayed at both endpoints, never borrowed
    from a past payment or provided as an override. The baseline holds the
    exact opening integer inventory, including its existing fee claims. The
    comparator is a marked baseline, not an executable purchase/exit strategy.
    """
    window = replay_cash_window(store, request.cash_window)
    ledger = window.ledger
    if ledger.entries or window.payments:
        raise ValueError("hold return requires complete event range with no LP modification")
    ownership = ledger.ownership_replay
    if (
        ownership is None
        or ledger.opening.nft_owner != ledger.closing.nft_owner
        or any(t.from_owner != t.to_owner for t in ownership.transfers)
    ):
        raise ValueError("hold return requires unchanged owner over the complete NFT range")
    providers = (request.cash_window.opening[0].source, request.cash_window.opening[1].source)
    core = replay_block_range_evidence(store, request.cash_window.core[0])
    headers = {h.ref: h for h in core.blocks}
    policies = (request.currency0_policy, request.currency1_policy)
    key = ledger.opening.pool_key
    if tuple(p.base_token for p in policies) != (key.currency0, key.currency1):
        raise ValueError("hold USD feed denominations must equal the ordered pool currencies")

    def endpoint(
        snapshots: EvidencePair, state_refs: EvidencePair, proof: HoldEndpointProof
    ) -> tuple[PositionValuation, tuple[TokenIdentityPin, ...], tuple[UsdPriceObservation, ...]]:
        block = snapshots[0].block
        if block is None:
            raise ValueError("hold snapshot must be block pinned")
        header: BlockSnapshot = headers[block]
        pins = []
        rounds = []
        currencies = []
        pairs = (
            (proof.currency0_identity, proof.currency0_price),
            (proof.currency1_identity, proof.currency1_price),
        )
        for i, ((identity_refs, price_refs), policy) in enumerate(
            zip(pairs, policies, strict=True)
        ):
            if len(identity_refs) != 2 or tuple(r.source for r in identity_refs) != providers:
                raise ValueError("hold token identity must use the window's ordered provider pair")
            quorum = TokenIdentityQuorum(
                *(replay_token_identity_evidence(store, ref) for ref in identity_refs)
            )
            pin = quorum.left.pin
            if quorum.left.header != header or pin.address != policy.base_token:
                raise ValueError("hold token identity header or denomination differs")
            if pin.decimals > 36:
                raise ValueError("hold token scale must be bounded")
            observed = _price(store, price_refs, header, policy, providers)
            pins.append(pin)
            rounds.append(observed)
            currencies.append(
                CurrencyPrice(
                    pin.address,
                    f"currency{i}",
                    pin.decimals,
                    FeedAnswer(
                        f"currency{i}",
                        observed.answer,
                        observed.decimals,
                        observed.round_id,
                        observed.updated_at,
                    ),
                    policy.max_age_seconds,
                )
            )
        for ref in state_refs:
            if (
                replay_slot0_window_evidence(store, ref).pool_manager
                != request.cash_window.pool_manager
            ):
                raise ValueError("hold endpoint PoolManager differs from cash event range")
        value = value_retained_position(
            store,
            retained=request.retained,
            snapshots=snapshots,
            pool_state=state_refs,
            currency0=currencies[0],
            currency1=currencies[1],
            providers=frozenset(providers),
            owner=ledger.opening.nft_owner,
        )
        return value, tuple(pins), tuple(rounds)

    opening, pins, opening_prices = endpoint(
        request.cash_window.opening, request.opening_pool_state, request.opening_marks
    )
    closing, closing_pins, closing_prices = endpoint(
        request.cash_window.closing, request.closing_pool_state, request.closing_marks
    )
    if pins != closing_pins:
        raise ValueError("hold token identity changed across endpoints")

    def mark(units: tuple[int, int], prices: tuple[UsdPriceObservation, ...], *, up: bool) -> int:
        return sum(
            _usd(amount, price, pin.decimals, up=up)
            for amount, price, pin in zip(units, prices, pins, strict=True)
        )

    initial_units = (opening.total0, opening.total1)
    opening_debit = mark(initial_units, opening_prices, up=True)
    hold_closing = mark(initial_units, closing_prices, up=True)
    principal_vs_hold = mark(
        (closing.principal0, closing.principal1), closing_prices, up=False
    ) - mark((opening.principal0, opening.principal1), closing_prices, up=True)
    fees_change = mark(
        (closing.uncollected_fee0, closing.uncollected_fee1), closing_prices, up=False
    ) - mark((opening.uncollected_fee0, opening.uncollected_fee1), closing_prices, up=True)
    lp_pnl = closing.usd_micros - opening_debit
    hold_pnl = hold_closing - opening_debit
    refs = (
        *request.cash_window.opening,
        *request.cash_window.closing,
        *request.cash_window.core,
        *request.cash_window.ownership,
        *request.opening_pool_state,
        *request.closing_pool_state,
        *request.opening_marks.evidence,
        *request.closing_marks.evidence,
    )
    return PositionHoldReturn(
        ledger.opening.nft_owner,
        opening,
        closing,
        (pins[0], pins[1]),
        opening_debit,
        hold_closing,
        lp_pnl,
        hold_pnl,
        lp_pnl - hold_pnl,
        principal_vs_hold,
        fees_change,
        lp_pnl - hold_pnl - principal_vs_hold - fees_change,
        tuple(dict.fromkeys(refs)),
        (
            *(c.value for c in CostCategory),
            "complete_account_activity_and_failed_attempts",
            "executable_baselines_and_fee_lvr_attribution",
        ),
    )
