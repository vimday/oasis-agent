"""Produce deterministic LP shadow features from agreed block-pinned inputs."""

from dataclasses import asdict, dataclass
from decimal import Decimal, localcontext
from itertools import pairwise

from ..models.common import BlockRef, EvidenceRef, canonical_json, sha256_hex
from ..models.finance import PoolState
from ..models.v4 import SwapDirection

Q192 = 1 << 192

AGREED_INTEGER_SCHEMA = "oasis.agreed-integer.v1"


def agreed_value_content_hash(name: str, value: int, block: BlockRef) -> str:
    """Canonical digest that cryptographically binds an agreed value to evidence.

    A producer must persist agreed-value evidence whose payload hashes to this
    digest; the constructor rejects any agreed value whose ``value`` is not
    bound to every supplied evidence reference. This moves the trust boundary
    from the constructor caller to the stored evidence, matching the explicit
    content-hash binding used by ``lp_signer_policy``.
    """
    return sha256_hex(
        canonical_json(
            {
                "schema": AGREED_INTEGER_SCHEMA,
                "name": name,
                "value": value,
                "block": asdict(block),
            }
        )
    )


@dataclass(frozen=True, slots=True)
class AgreedInteger:
    name: str
    value: int
    block: BlockRef
    evidence: tuple[EvidenceRef, ...]

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("agreed value name is required")
        if type(self.value) is not int or self.value < 0:
            raise ValueError("agreed value must be a nonnegative integer")
        expected = agreed_value_content_hash(self.name, self.value, self.block)
        sources: set[str] = set()
        interpretations: set[tuple[str, str]] = set()
        for reference in self.evidence:
            if (
                reference.block != self.block
                or not reference.request_hash
                or not reference.raw_response_hash
                or not reference.interpretation_hash
            ):
                raise ValueError("agreed value evidence must be complete and block-pinned")
            if reference.content_hash != expected:
                raise ValueError("agreed value is not bound to its evidence content")
            sources.add(reference.source)
            interpretations.add((reference.content_hash, reference.interpretation_hash))
        if len(sources) < 2 or len(interpretations) != 1:
            raise ValueError("agreed value requires two distinct providers with one interpretation")


@dataclass(frozen=True, slots=True)
class SwapMarkout:
    direction: SwapDirection
    amount0: int
    amount1: int
    future_price_x192: AgreedInteger

    def __post_init__(self) -> None:
        if not isinstance(self.direction, SwapDirection):
            raise TypeError("typed swap direction required")
        for name in ("amount0", "amount1"):
            if type(getattr(self, name)) is not int:
                raise TypeError(f"{name} must be an integer base-unit delta")
        if self.direction is SwapDirection.ZERO_FOR_ONE:
            if self.amount0 <= 0 or self.amount1 >= 0:
                raise ValueError("zero-for-one markout has invalid pool delta signs")
        elif self.amount0 >= 0 or self.amount1 <= 0:
            raise ValueError("one-for-zero markout has invalid pool delta signs")
        if self.future_price_x192.value == 0:
            raise ValueError("future markout price must be positive")


@dataclass(frozen=True, slots=True)
class LpFeatureWindow:
    decision_block: BlockRef
    sqrt_prices_x96: tuple[AgreedInteger, ...]
    inventory0: AgreedInteger
    inventory1: AgreedInteger
    reference_price_x192: AgreedInteger
    markouts: tuple[SwapMarkout, ...]
    previous_fee: int | None = None
    previous_fee_timestamp: int | None = None

    def __post_init__(self) -> None:
        if self.decision_block.timestamp is None:
            raise ValueError("decision block timestamp is required")
        if len(self.sqrt_prices_x96) < 2:
            raise ValueError("volatility window requires at least two prices")
        blocks = tuple(item.block.number for item in self.sqrt_prices_x96)
        if blocks != tuple(sorted(blocks)) or len(set(blocks)) != len(blocks):
            raise ValueError("price window must be strictly block ordered")
        all_values = (
            *self.sqrt_prices_x96,
            self.inventory0,
            self.inventory1,
            self.reference_price_x192,
            *(item.future_price_x192 for item in self.markouts),
        )
        if any(
            item.block.chain_id != self.decision_block.chain_id
            or item.block.number > self.decision_block.number
            for item in all_values
        ):
            raise ValueError("feature input is cross-chain or from the future")
        if self.sqrt_prices_x96[-1].block != self.decision_block:
            raise ValueError("last pool price must use the decision block")
        if (
            self.inventory0.block != self.decision_block
            or self.inventory1.block != self.decision_block
        ):
            raise ValueError("inventory must use the decision block")
        if self.reference_price_x192.block != self.decision_block:
            raise ValueError("reference price must use the decision block")
        if self.reference_price_x192.value == 0 or any(
            item.value == 0 or item.value >= 1 << 160 for item in self.sqrt_prices_x96
        ):
            raise ValueError("pool and reference prices must be positive and bounded")
        if (self.previous_fee is None) != (self.previous_fee_timestamp is None):
            raise ValueError("previous fee and timestamp must be supplied together")


def produce_pool_state(window: LpFeatureWindow) -> PoolState:
    """Compute lagged volatility, inventory skew, toxicity, and oracle deviation."""
    with localcontext() as context:
        context.prec = 80
        prices = tuple(Decimal(item.value) ** 2 for item in window.sqrt_prices_x96)
        volatility = sum(
            abs(current - previous) / previous for previous, current in pairwise(prices)
        ) / Decimal(len(prices) - 1)
        reference = Decimal(window.reference_price_x192.value)
        final_pool = prices[-1]
        oracle_deviation = abs(final_pool - reference) / reference
        value0 = Decimal(window.inventory0.value) * reference / Decimal(Q192)
        value1 = Decimal(window.inventory1.value)
        total = value0 + value1
        inventory_skew = Decimal(0) if total == 0 else (value0 - value1) / total
        adverse_terms: list[Decimal] = []
        for swap in window.markouts:
            execution = Decimal(abs(swap.amount1)) * Decimal(Q192) / Decimal(abs(swap.amount0))
            future = Decimal(swap.future_price_x192.value)
            if swap.direction is SwapDirection.ZERO_FOR_ONE:
                adverse_terms.append(max(Decimal(0), (execution - future) / future))
            else:
                adverse_terms.append(max(Decimal(0), (future - execution) / future))
        adverse_selection = (
            sum(adverse_terms, Decimal(0)) / Decimal(len(adverse_terms))
            if adverse_terms
            else Decimal(0)
        )
        oracle_timestamp = window.reference_price_x192.block.timestamp
        if oracle_timestamp is None:  # Block equality above makes this defensive-only.
            raise ValueError("reference price block timestamp is required")
        observed_at_timestamp = window.decision_block.timestamp
        if observed_at_timestamp is None:  # Constructor validation narrows this at runtime.
            raise ValueError("decision block timestamp is required")
        return PoolState(
            volatility=volatility,
            inventory_imbalance=inventory_skew,
            adverse_selection=adverse_selection,
            rebalance_incentive=abs(inventory_skew),
            observed_at_timestamp=observed_at_timestamp,
            oracle_timestamp=oracle_timestamp,
            oracle_deviation=oracle_deviation,
            previous_fee=window.previous_fee,
            previous_fee_timestamp=window.previous_fee_timestamp,
            block=window.decision_block,
        )


__all__ = [
    "AGREED_INTEGER_SCHEMA",
    "AgreedInteger",
    "LpFeatureWindow",
    "SwapMarkout",
    "agreed_value_content_hash",
    "produce_pool_state",
]
