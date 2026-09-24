"""Pinned USD valuation of one live v4 LP position; it never over-states the asset.

Every input is an already-retained, block-pinned fact: one position snapshot
(ticks, liquidity, uncollected fees), the pool's slot0 sqrt price and tick, and
one complete USD feed round per pool currency. This module only combines them.
It fetches nothing, prices nothing on its own, pins no runtime identity and can
neither sign nor broadcast, so a valuation is only as trustworthy as the
evidence the caller replayed into these inputs.

The result is floored, leg by leg, before the legs are added, so it can never
exceed the true claimable value. That direction is deliberate: the risk ledger
derives a loss as ``previous_nav + non_trading_flow - nav``, so an over-stated
position would hide a real loss, while an under-stated one can only make the
ledger refuse. A worst-case commitment estimate is the opposite question and
rounds up in the signer reader.
"""

from dataclasses import dataclass

from ..models.common import BlockRef, normalize_address, normalize_hash32
from ..uniswap_v4.liquidity_math import MAX_SQRT_PRICE, MAX_TICK, MIN_SQRT_PRICE, sqrt_price_at_tick
from ..uniswap_v4.state_rpc import Slot0State
from .account_inventory import FeedAnswer
from .inventory import PositionInventory, position_inventory
from .positions import PositionSnapshot
from .price_evidence import value_usd_micros


def _uint(value: object, bits: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if not 0 <= value < 1 << bits:
        raise ValueError(f"{name} must fit uint{bits}")
    return value


@dataclass(frozen=True, slots=True)
class CurrencyPrice:
    """One pool currency: its address, token scale and one complete USD feed round.

    ``decimals`` is the token's scale, not the feed's; ``max_age_seconds`` is the
    manifest's freshness bound for that asset, so a caller cannot value a stale
    round by simply passing an older block.
    """

    address: str
    asset_id: str
    decimals: int
    feed: FeedAnswer
    max_age_seconds: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "address", normalize_address(self.address))
        if not isinstance(self.asset_id, str) or not self.asset_id:
            raise ValueError("currency valuation requires an asset identifier")
        decimals = _uint(self.decimals, 8, "token decimals")
        if decimals > 36:
            raise ValueError("token decimals must be bounded")
        if not isinstance(self.feed, FeedAnswer) or self.feed.asset_id != self.asset_id:
            raise ValueError("currency valuation requires the feed round of that asset")
        if type(self.max_age_seconds) is not int or self.max_age_seconds <= 0:
            raise ValueError("currency valuation requires a positive feed age bound")


@dataclass(frozen=True, slots=True)
class PositionValuation:
    """Floored USD micros claimable by one position at one pinned block."""

    pool_id: str
    token_id: int
    block: BlockRef
    sqrt_price_x96: int
    in_range: bool
    principal0: int
    principal1: int
    uncollected_fee0: int
    uncollected_fee1: int
    total0: int
    total1: int
    usd_micros: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "pool_id", normalize_hash32(self.pool_id, "pool id"))
        _uint(self.token_id, 256, "token id")
        if self.token_id == 0:
            raise ValueError("position valuation requires a live NFT identity")
        if self.block.timestamp is None:
            raise ValueError("position valuation requires a timestamped block")
        _uint(self.sqrt_price_x96, 160, "sqrt price")
        if not MIN_SQRT_PRICE <= self.sqrt_price_x96 < MAX_SQRT_PRICE:
            raise ValueError("position valuation price is outside initialized pool bounds")
        if type(self.in_range) is not bool:
            raise TypeError("position valuation requires an explicit range flag")
        for name in ("principal0", "principal1", "uncollected_fee0", "uncollected_fee1"):
            _uint(getattr(self, name), 256, name)
        for name in ("total0", "total1"):
            _uint(getattr(self, name), 256, name)
        if (self.total0, self.total1) != (
            self.principal0 + self.uncollected_fee0,
            self.principal1 + self.uncollected_fee1,
        ):
            raise ValueError("position valuation totals must be principal plus uncollected fees")
        _uint(self.usd_micros, 256, "position value")


def _require_tick_consistency(slot0: Slot0State) -> None:
    """A retained slot0 must place its own tick around its own sqrt price."""
    if slot0.tick >= MAX_TICK:
        raise ValueError("slot0 tick has no representable upper bound")
    lower = sqrt_price_at_tick(slot0.tick)
    if not lower <= slot0.sqrt_price_x96 < sqrt_price_at_tick(slot0.tick + 1):
        raise ValueError("slot0 sqrt price is inconsistent with its tick")


def _require_fresh(price: CurrencyPrice, block_timestamp: int) -> None:
    age = block_timestamp - price.feed.updated_at
    if not 0 <= age <= price.max_age_seconds:
        raise ValueError("stale or future currency valuation")


def _leg_usd_micros(price: CurrencyPrice, units: int) -> int:
    """Floor one currency leg; a floor never inflates the position."""
    return value_usd_micros(
        units,
        numerator=price.feed.usd_micros_numerator,
        denominator=price.feed.price_denominator,
        decimals=price.decimals,
        round_up=False,
    )


def value_position(
    snapshot: PositionSnapshot,
    slot0: Slot0State,
    *,
    currency0: CurrencyPrice,
    currency1: CurrencyPrice,
) -> PositionValuation:
    """Value one position from pinned inputs; raise instead of guessing.

    The pool's two currencies must be named by address, the freshness bound is
    the caller's declared policy, and the price must be internal to its own
    tick. The caller still owns the bindings this function cannot see: that the
    slot0 belongs to this pool, that the feed is the declared one for that
    asset, and that both were replayed from retained proof.
    """
    block = snapshot.block
    if block.timestamp is None:
        raise ValueError("position valuation requires a timestamped block")
    if currency0.asset_id == currency1.asset_id:
        raise ValueError("pool currencies must be distinct assets")
    for index, (price, address) in enumerate(
        (
            (currency0, normalize_address(snapshot.pool_key.currency0)),
            (currency1, normalize_address(snapshot.pool_key.currency1)),
        )
    ):
        if price.address != address:
            raise ValueError(f"currency{index} valuation is not the position pool currency")
        _require_fresh(price, block.timestamp)
    _require_tick_consistency(slot0)
    inventory: PositionInventory = position_inventory(snapshot, slot0.sqrt_price_x96)
    return PositionValuation(
        pool_id=snapshot.pool_id,
        token_id=snapshot.token_id,
        block=block,
        sqrt_price_x96=slot0.sqrt_price_x96,
        in_range=inventory.in_range,
        principal0=inventory.principal0,
        principal1=inventory.principal1,
        uncollected_fee0=inventory.uncollected_fee0,
        uncollected_fee1=inventory.uncollected_fee1,
        total0=inventory.total0,
        total1=inventory.total1,
        usd_micros=_leg_usd_micros(currency0, inventory.total0)
        + _leg_usd_micros(currency1, inventory.total1),
    )


__all__ = [
    "CurrencyPrice",
    "PositionValuation",
    "value_position",
]
