"""Quote raw gas only after exclusive action-time LP owner attribution replays.

This is one input to costed LP returns, not a complete cost or return estimator.
No caller supplies a payer, gas amount, allocation weight or quote decimal scale.
"""

from dataclasses import dataclass, field

from ..evidence.exact_capture import decode_header
from ..evidence.rpc_validation import decode_exchange, restore_exchanges
from ..evidence.store import EvidenceStore
from ..models.common import BlockRef, EvidenceRef, normalize_hash32
from .cash_window import CashWindowRequest, EvidencePair, replay_cash_window
from .position_action_costs import ActionCostProof, bind_position_action_costs
from .price_evidence import UsdFeedPolicy
from .receipt_cost_valuation import price_receipt_cost


@dataclass(frozen=True, slots=True)
class ActionPriceProof:
    transaction_hash: str
    native_prices: EvidencePair
    quote_prices: EvidencePair

    def __post_init__(self) -> None:
        object.__setattr__(self, "transaction_hash", normalize_hash32(self.transaction_hash))


@dataclass(frozen=True, slots=True)
class QuotedActionCost:
    block: BlockRef
    transaction_hash: str
    owner: str
    native_units: int
    quote_units: int
    evidence: tuple[EvidenceRef, ...]


@dataclass(frozen=True, slots=True)
class QuotedPositionActionCosts:
    token_id: int
    quote_token: str
    quote_decimals: int
    actions: tuple[QuotedActionCost, ...]
    total_quote_units: int
    scope: str = field(default="successful_exclusive_owner_paid_action_gas_only", init=False)
    net_pnl: None = field(default=None, init=False)
    execution_eligible: bool = field(default=False, init=False)

    @property
    def quote_cost_by_owner(self) -> dict[str, int]:
        totals: dict[str, int] = {}
        for action in self.actions:
            totals[action.owner] = totals.get(action.owner, 0) + action.quote_units
        return totals


def quote_position_action_costs(
    store: EvidenceStore,
    cash_window: CashWindowRequest,
    costs: tuple[ActionCostProof, ...],
    prices: tuple[ActionPriceProof, ...],
    *,
    native_policy: UsdFeedPolicy,
    quote_policy: UsdFeedPolicy,
) -> QuotedPositionActionCosts:
    """Join full cash replay, matching receipt gas and six action-block proofs.

    Currency1 decimals come from the raw owner-payment witnesses, not a CLI
    number. Per-action gas is rounded upward using native/USD and currency1/USD
    observed at that action's block. A later owner or endpoint price never
    changes who paid or what price is used. Missing actions/prices refuse rather
    than becoming free gas. Failed and sponsored attempts remain outside scope.
    """
    window = replay_cash_window(store, cash_window)
    bound = bind_position_action_costs(store, cash_window, costs)
    if not bound.actions:
        raise ValueError("at least one attributed action required for gas valuation")
    token = window.ledger.opening.pool_key.currency1
    if quote_policy.base_token != token:
        raise ValueError("quote feed must price the position currency1")
    if (
        len(prices) != len(bound.actions)
        or len({p.transaction_hash for p in prices}) != len(prices)
        or {p.transaction_hash for p in prices} != {a.transaction_hash for a in bound.actions}
    ):
        raise ValueError("price proofs must cover every attributed action exactly once")
    scale = window.payments[0].amounts.token1.decimals
    if any(p.amounts.token1.decimals != scale for p in window.payments):
        raise ValueError("cash-window currency1 decimal scale changed")
    price_index = {p.transaction_hash: p for p in prices}
    cost_index = {p.transaction_hash: p for p in costs}
    actions = []
    for action in bound.actions:
        cost = cost_index[action.transaction_hash]
        price = price_index[action.transaction_hash]
        record = store.get_record(cost.evidence[0])
        if record.request_payload is None:
            raise ValueError("action cost raw requests missing")
        exchanges = restore_exchanges(record.request_payload, record.raw_response_payload)
        header = decode_header(
            decode_exchange(exchanges[1])[1]["result"], chain_id=action.block.chain_id
        )
        if header.ref != action.block:
            raise ValueError("raw action header differs from attributed action")
        sources = (cost.evidence[0].source, cost.evidence[1].source)
        valued = price_receipt_cost(
            store,
            header=header,
            transaction_hash=action.transaction_hash,
            payer=action.owner,
            providers=sources,
            costs=cost.evidence,
            native_prices=price.native_prices,
            quote_prices=price.quote_prices,
            native_policy=native_policy,
            quote_policy=quote_policy,
            quote_decimals=scale,
        )
        if valued.status != 1 or valued.total_native != action.native_units:
            raise ValueError("priced gas differs from attributed successful action")
        actions.append(
            QuotedActionCost(
                action.block,
                action.transaction_hash,
                action.owner,
                action.native_units,
                valued.quote_units,
                (*action.evidence, *price.native_prices, *price.quote_prices),
            )
        )
    return QuotedPositionActionCosts(
        bound.token_id, token, scale, tuple(actions), sum(a.quote_units for a in actions)
    )
