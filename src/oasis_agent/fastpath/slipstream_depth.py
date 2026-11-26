"""Integer exact-input replay on an explicitly covered Slipstream bitmap window."""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from ..uniswap_v4.liquidity_math import (
    MAX_TICK,
    MIN_TICK,
    Q96,
    UINT256_MAX,
    amount0_delta,
    amount1_delta,
    sqrt_price_at_tick,
)

FEE_DENOMINATOR = 1_000_000


class DepthCoverageError(ValueError):
    """The swap leaves the observed contiguous bitmap window."""


@dataclass(frozen=True, slots=True)
class DepthQuote:
    amount_out: int
    sqrt_price_x96: int
    initialized_ticks_crossed: int


def initialized_ticks(words: Mapping[int, int], spacing: int) -> set[int]:
    if type(spacing) is not int or not 0 < spacing < 1 << 23 or not words:
        raise ValueError("invalid tick spacing or empty bitmap")
    for page, bitmap in words.items():
        if (
            type(page) is not int
            or not -(1 << 15) <= page < 1 << 15
            or type(bitmap) is not int
            or not 0 <= bitmap <= UINT256_MAX
        ):
            raise ValueError("invalid Slipstream bitmap word")
    if len(words) != max(words) - min(words) + 1:
        raise ValueError("noncontiguous Slipstream bitmap words")
    ticks = set()
    for page, bitmap in words.items():
        remaining = bitmap
        while remaining:
            bit = remaining & -remaining
            tick = (256 * page + bit.bit_length() - 1) * spacing
            if not MIN_TICK <= tick <= MAX_TICK:
                raise ValueError("initialized bitmap tick outside TickMath bounds")
            ticks.add(tick)
            remaining ^= bit
    return ticks


def _ceil_div(numerator: int, denominator: int) -> int:
    return (numerator + denominator - 1) // denominator


def _partial_price(price: int, liquidity: int, usable: int, zero_for_one: bool) -> int:
    if not zero_for_one:
        return price + usable * Q96 // liquidity
    numerator = liquidity * Q96
    denominator = numerator + usable * price
    if denominator <= UINT256_MAX:
        return _ceil_div(numerator * price, denominator)
    # Slipstream's SqrtPriceMath uses this division order when the addition or product wraps.
    denominator = numerator // price + usable
    if denominator > UINT256_MAX:
        raise ValueError("Slipstream sqrt price denominator overflow")
    return _ceil_div(numerator, denominator)


def _tick_at_price(price: int, lower: int, upper: int) -> int:
    while lower < upper:
        middle = (lower + upper + 1) // 2
        if sqrt_price_at_tick(middle) <= price:
            lower = middle
        else:
            upper = middle - 1
    return lower


@dataclass(frozen=True, slots=True)
class SlipstreamDepth:
    sqrt_price_x96: int
    tick: int
    liquidity: int
    fee_pips: int
    spacing: int
    words: Mapping[int, int]
    liquidity_nets: Mapping[int, int]

    def __post_init__(self) -> None:
        object.__setattr__(self, "words", MappingProxyType(dict(self.words)))
        object.__setattr__(self, "liquidity_nets", MappingProxyType(dict(self.liquidity_nets)))
        if any(
            type(v) is not int
            for v in (self.sqrt_price_x96, self.tick, self.liquidity, self.fee_pips)
        ):
            raise ValueError("Slipstream state must use integers")
        if initialized_ticks(self.words, self.spacing) != self.liquidity_nets.keys():
            raise ValueError("bitmap initialized ticks disagree with decoded liquidity nets")
        if any(
            type(tick) is not int or type(net) is not int or not -(1 << 127) < net < 1 << 127
            for tick, net in self.liquidity_nets.items()
        ):
            raise ValueError("invalid tick liquidity net")
        if not MIN_TICK <= self.tick < MAX_TICK or not (
            sqrt_price_at_tick(self.tick)
            <= self.sqrt_price_x96
            <= sqrt_price_at_tick(self.tick + 1)
        ):
            raise ValueError("slot0 price does not match the active tick")
        if (self.tick // self.spacing >> 8) not in self.words:
            raise ValueError("current tick is outside the observed bitmap")
        if not 0 <= self.liquidity < 1 << 128 or not 0 <= self.fee_pips < FEE_DENOMINATOR:
            raise ValueError("invalid Slipstream liquidity or fee")

    def _quoter_crossings(self, end_tick: int) -> int:
        # PoolTicksCounter uses Solidity truncating division, unlike the swap bitmap cursor.
        before = (1 if self.tick >= 0 else -1) * (abs(self.tick) // self.spacing)
        after = (1 if end_tick >= 0 else -1) * (abs(end_tick) // self.spacing)
        if (before >> 8) not in self.words or (after >> 8) not in self.words:
            raise DepthCoverageError("Quoter tick count needs an unobserved bitmap word")
        lower, upper = sorted((before * self.spacing, after * self.spacing))
        count = sum(lower <= tick <= upper for tick in self.liquidity_nets)
        if self.tick != end_tick:
            count -= int(min(self.tick, end_tick) in self.liquidity_nets)
        return count

    def quote(self, amount_in: int, *, zero_for_one: bool) -> DepthQuote:
        if type(amount_in) is not int or not 0 < amount_in < 1 << 255:
            raise ValueError("invalid Slipstream exact input")
        if type(zero_for_one) is not bool:
            raise ValueError("invalid Slipstream direction")
        price, tick, liquidity = self.sqrt_price_x96, self.tick, self.liquidity
        remaining, output = amount_in, 0
        net_fraction = FEE_DENOMINATOR - self.fee_pips
        delta_in, delta_out = (
            (amount0_delta, amount1_delta) if zero_for_one else (amount1_delta, amount0_delta)
        )
        while remaining:
            cursor = tick // self.spacing + (not zero_for_one)
            page, bit = divmod(cursor, 256)
            if page not in self.words:
                raise DepthCoverageError("amount exceeds the observed tick bitmap")
            word = self.words[page]
            if zero_for_one:
                masked = word & ((1 << (bit + 1)) - 1)
                next_bit = masked.bit_length() - 1 if masked else 0
            else:
                masked = word >> bit
                next_bit = bit + (masked & -masked).bit_length() - 1 if masked else 255
            next_tick = (page * 256 + next_bit) * self.spacing
            if not MIN_TICK < next_tick < MAX_TICK:
                raise DepthCoverageError("swap reaches the protocol price boundary")
            target = sqrt_price_at_tick(next_tick)
            usable = remaining * net_fraction // FEE_DENOMINATOR
            required = delta_in(price, target, liquidity, round_up=True)
            next_price = (
                target
                if usable >= required
                else _partial_price(price, liquidity, usable, zero_for_one)
            )
            output += delta_out(price, next_price, liquidity, round_up=False)
            if next_price != target:
                if next_price != price:
                    tick = _tick_at_price(next_price, min(tick, next_tick), max(tick, next_tick))
                return DepthQuote(output, next_price, self._quoter_crossings(tick))
            price = next_price
            remaining -= required + _ceil_div(required * self.fee_pips, net_fraction)
            if masked:
                net = self.liquidity_nets[next_tick]
                liquidity += -net if zero_for_one else net
                if not 0 <= liquidity < 1 << 128:
                    raise ValueError("crossing a tick yields invalid active liquidity")
            tick = next_tick - 1 if zero_for_one else next_tick
        return DepthQuote(output, price, self._quoter_crossings(tick))
