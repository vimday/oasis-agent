"""Price a whole transaction from retained two-provider receipts and USD feeds.

Quote decimals are an explicit input, not an on-chain metadata attestation here.
This produces a transaction cost, not allocation to an LP or economic acceptance.
"""

from dataclasses import dataclass, field

from ..evidence.store import EvidenceStore
from ..models.chain import BlockSnapshot
from ..models.common import EvidenceRef, normalize_address
from .price_evidence import (
    UsdFeedPolicy,
    replay_price_observation_evidence,
    value_native_cost,
)
from .transaction_costs import replay_transaction_cost


@dataclass(frozen=True, slots=True)
class PricedReceiptCost:
    transaction_hash: str
    payer: str
    status: int
    total_native: int
    quote_token: str
    quote_decimals: int
    quote_units: int
    evidence: tuple[EvidenceRef, ...]
    position_allocation: str = field(default="unresolved", init=False)
    execution_eligible: bool = field(default=False, init=False)


def price_receipt_cost(
    store: EvidenceStore,
    *,
    header: BlockSnapshot,
    transaction_hash: str,
    payer: str,
    providers: tuple[str, str],
    costs: tuple[EvidenceRef, EvidenceRef],
    native_prices: tuple[EvidenceRef, EvidenceRef],
    quote_prices: tuple[EvidenceRef, EvidenceRef],
    native_policy: UsdFeedPolicy,
    quote_policy: UsdFeedPolicy,
    quote_decimals: int,
) -> PricedReceiptCost:
    """Reconstruct all six proofs before accepting a conservatively rounded cost.

    Order each pair by the declared provider order. No fallback to a peg, operator
    amount, missing receipt or missing price is permitted. Reverted receipts remain
    chargeable. Prices must agree in full, including feed round and update time.
    """
    if len(providers) != 2 or len(set(providers)) != 2 or any(not p.strip() for p in providers):
        raise ValueError("two distinct declared providers required")
    for pair in (costs, native_prices, quote_prices):
        if len(pair) != 2 or tuple(ref.source for ref in pair) != providers:
            raise ValueError("cost and price proofs must use the declared provider pair")
    payer = normalize_address(payer)
    captures = tuple(
        replay_transaction_cost(store, ref, header=header, transaction_hash=transaction_hash)
        for ref in costs
    )
    receipts = tuple(capture.reconstruct() for capture in captures)
    if receipts[0] != receipts[1]:
        raise ValueError("receipt providers disagree")
    receipt = receipts[0]
    if receipt.payer != payer:
        raise ValueError("receipt payer differs from the declared account")
    if receipt.native_decimals != 18:
        raise ValueError("native cost scale is unsupported")
    native = tuple(
        replay_price_observation_evidence(store, ref, header=header, policy=native_policy)
        for ref in native_prices
    )
    quote = tuple(
        replay_price_observation_evidence(store, ref, header=header, policy=quote_policy)
        for ref in quote_prices
    )
    for price_pair in (native, quote):
        if price_pair[0].reconstruct_observation() != price_pair[1].reconstruct_observation():
            raise ValueError("price providers disagree on the complete feed observation")
    value = value_native_cost(
        captures[0],
        native[0],
        quote[0],
        quote_token=quote_policy.base_token,
        quote_decimals=quote_decimals,
    )
    return PricedReceiptCost(
        receipt.transaction_hash,
        payer,
        receipt.status,
        receipt.total_native,
        quote_policy.base_token,
        quote_decimals,
        value,
        (*costs, *native_prices, *quote_prices),
    )
