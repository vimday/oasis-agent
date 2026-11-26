"""Offline integer sizing for fixed-state, distinct-pool constant-product cycles.

Standard V2 swap math assumes each hop receives the full previous output.
Inputs use each token's integer base units. Bounds and costs use anchor base
units. Callers supply pool identity, one coherent state, and a fixed nonnegative
cost. Token transfer behavior, EVM admission, gas estimation, and inclusion
are outside this model.
"""

from dataclasses import dataclass, field
from heapq import heappop, heappush
from itertools import pairwise
from math import gcd, isqrt
from re import fullmatch

MAX_V2_LEGS = 400
MAX_V2_SEARCH_WORK = 100_000
_MAX_UINT112 = (1 << 112) - 1
_MAX_UINT256 = (1 << 256) - 1
type _Curve = tuple[int, int, int]


def _uint(value: int, minimum: int, maximum: int, label: str) -> None:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"invalid {label}")


def _identity(value: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("token and pool identity must be a nonempty, unpadded string")
    if fullmatch(r"0[xX][0-9A-Fa-f]{40}", value) is not None:
        return value.lower()
    return value


def _compose_curve(coefficients: tuple[_Curve, ...], start: int, end: int) -> _Curve:
    """Compose adjacent unrounded swaps, merging similarly sized integer products."""
    if end - start == 1:
        return coefficients[start]
    mid = (start + end) // 2
    a, b, c = _compose_curve(coefficients, start, mid)
    d, e, f = _compose_curve(coefficients, mid, end)
    # Applying d*x/(e+f*x) after a*x/(b+c*x) preserves route order.
    numerator, constant, slope = d * a, e * b, e * c + f * a
    divisor = gcd(numerator, constant, slope)
    return numerator // divisor, constant // divisor, slope // divisor


@dataclass(frozen=True, slots=True)
class V2Pool:
    pool_id: str
    token0: str
    token1: str
    reserve0: int
    reserve1: int
    fee_multiplier: int
    fee_denominator: int

    def __post_init__(self) -> None:
        for name, value in (
            ("pool_id", self.pool_id),
            ("token0", self.token0),
            ("token1", self.token1),
        ):
            object.__setattr__(self, name, _identity(value))
        if self.token0 == self.token1:
            raise ValueError("a pool must bind two distinct tokens")
        _uint(self.reserve0, 1, _MAX_UINT112, "reserve0")
        _uint(self.reserve1, 1, _MAX_UINT112, "reserve1")
        _uint(self.fee_denominator, 1, _MAX_UINT256, "fee denominator")
        _uint(self.fee_multiplier, 1, self.fee_denominator, "fee multiplier")


@dataclass(frozen=True, slots=True)
class V2Leg:
    pool: V2Pool
    token_in: str

    def __post_init__(self) -> None:
        token = _identity(self.token_in)
        if token not in (self.pool.token0, self.pool.token1):
            raise ValueError("leg input token is outside the pool")
        object.__setattr__(self, "token_in", token)

    @property
    def token_out(self) -> str:
        return self.pool.token1 if self.token_in == self.pool.token0 else self.pool.token0

    @property
    def _coefficients(self) -> _Curve:
        pool = self.pool
        reserves = (pool.reserve0, pool.reserve1)
        reserve_in, reserve_out = reserves if self.token_in == pool.token0 else reserves[::-1]
        return (
            reserve_out * pool.fee_multiplier,
            reserve_in * pool.fee_denominator,
            pool.fee_multiplier,
        )

    def quote_exact_input(self, amount_in: int) -> int:
        """One hop of the same standard integer model used for fixed V2 routes."""
        _uint(amount_in, 0, _MAX_UINT256, "amount in")
        return _quote_step(self._coefficients, amount_in)


def _quote_step(coefficients: _Curve, amount_in: int) -> int:
    a, b, c = coefficients
    return a * amount_in // (b + c * amount_in)


@dataclass(frozen=True, slots=True)
class V2RouteQuote:
    """Integer values in route order. The first and last are in anchor units."""

    amounts: tuple[int, ...]

    @property
    def amount_in(self) -> int:
        return self.amounts[0]

    @property
    def amount_out(self) -> int:
        return self.amounts[-1]

    @property
    def gross_profit(self) -> int:
        return self.amount_out - self.amount_in


@dataclass(frozen=True, slots=True)
class V2ProfitBounds:
    gross_upper: int
    net_upper: int


@dataclass(frozen=True, slots=True)
class V2SizingResidual:
    min_amount_in: int
    max_amount_in: int
    net_upper: int


@dataclass(frozen=True, slots=True)
class V2SizingResult:
    best: V2RouteQuote | None
    best_net: int | None
    gross_upper: int | None
    net_upper: int | None
    quotes_evaluated: int
    steps: int
    residuals: tuple[V2SizingResidual, ...]

    @property
    def complete(self) -> bool:
        """The greatest attainable modeled net value is known, or no input returns tokens."""
        return not self.residuals


@dataclass(frozen=True, slots=True)
class V2Route:
    legs: tuple[V2Leg, ...]
    _coefficients: tuple[_Curve, ...] = field(init=False, repr=False)
    _curve: _Curve = field(init=False, repr=False)
    _peak_floor: int = field(init=False, repr=False)

    def __post_init__(self) -> None:
        legs = tuple(self.legs)
        if not 2 <= len(legs) <= MAX_V2_LEGS:
            raise ValueError("a route must contain 2 to 400 legs")
        if len({leg.pool.pool_id for leg in legs}) != len(legs):
            raise ValueError("a route cannot reuse a pool")
        if any(leg.token_out != successor.token_in for leg, successor in pairwise(legs)):
            raise ValueError("route legs are not contiguous")
        if legs[-1].token_out != legs[0].token_in:
            raise ValueError("route does not return to its anchor")
        coefficients = tuple(leg._coefficients for leg in legs)
        a, b, c = _compose_curve(coefficients, 0, len(coefficients))
        object.__setattr__(self, "legs", legs)
        object.__setattr__(self, "_coefficients", coefficients)
        object.__setattr__(self, "_curve", (a, b, c))
        object.__setattr__(self, "_peak_floor", (isqrt(a * b) - b) // c)

    @property
    def anchor(self) -> str:
        return self.legs[0].token_in

    def quote(self, amount_in: int) -> V2RouteQuote:
        """Quote each leg with the protocol's integer floor, including a zero-output diagnostic."""
        _uint(amount_in, 0, _MAX_UINT256, "amount in")
        amounts = [amount_in]
        for coefficients in self._coefficients:
            amount_in = _quote_step(coefficients, amount_in)
            amounts.append(amount_in)
        return V2RouteQuote(tuple(amounts))

    def _continuous_integer_upper(self, amount_in: int) -> int:
        a, b, c = self._curve
        return a * amount_in // (b + c * amount_in) - amount_in

    def _peak_and_bound(self, low: int, high: int) -> tuple[int, int]:
        # The floor of the real maximizer is exact because b and c are integers.
        # Each monotone side of the continuous curve attains its best integer
        # value at one of the two integers neighboring that maximizer.
        left = max(low, min(high, self._peak_floor))
        right = max(low, min(high, self._peak_floor + 1))
        left_value = self._continuous_integer_upper(left)
        right_value = self._continuous_integer_upper(right)
        return (right, right_value) if right_value > left_value else (left, left_value)

    def all_input_bounds(self, *, total_cost_in_anchor: int) -> V2ProfitBounds:
        """Bound modeled gross and net for all positive inputs, regardless of sizing limits."""
        _uint(total_cost_in_anchor, 0, _MAX_UINT256, "anchor cost")
        _, gross = self._peak_and_bound(1, max(1, self._peak_floor + 1))
        return V2ProfitBounds(gross, gross - total_cost_in_anchor)

    def _first_input_at_least(self, out: int) -> int | None:
        """Invert every floored step at the least integer that covers its output requirement."""
        for a, b, c in reversed(self._coefficients):
            denominator = a - c * out
            if denominator <= 0:
                return None
            out = (b * out + denominator - 1) // denominator
            if out > _MAX_UINT256:
                return None
        return out

    def size(
        self,
        min_amount_in: int,
        max_amount_in: int,
        *,
        total_cost_in_anchor: int,
        max_quotes: int = 4096,
        max_steps: int = 8192,
    ) -> V2SizingResult:
        """Maximize integer net over inputs with positive final output.

        The route's continuous composition upper-bounds all integer output
        because each hop is increasing and integer rounding only removes
        output. Exact monotone inversion skips constant-output intervals.
        Positive-output allocations can still be infeasible in the EVM.
        """
        _uint(min_amount_in, 1, _MAX_UINT256, "minimum input")
        _uint(max_amount_in, min_amount_in, _MAX_UINT256, "maximum input")
        _uint(total_cost_in_anchor, 0, _MAX_UINT256, "anchor cost")
        _uint(max_quotes, 1, MAX_V2_SEARCH_WORK, "quote budget")
        _uint(max_steps, 1, MAX_V2_SEARCH_WORK, "interval budget")

        feasible = self._first_input_at_least(1)
        if feasible is None or feasible > max_amount_in:
            return V2SizingResult(None, None, None, None, 0, 0, ())
        lower = max(min_amount_in, feasible)
        pivot, upper = self._peak_and_bound(lower, max_amount_in)
        pending = [(-upper, lower, max_amount_in)]
        best: V2RouteQuote | None = None
        quotes = steps = 0

        while pending and quotes < max_quotes and steps < max_steps:
            neg_upper, low, high = heappop(pending)
            steps += 1
            if best is not None and -neg_upper <= best.gross_profit:
                # The heap is ordered by descending bound, so every later interval is dominated.
                pending.clear()
                break
            point = pivot if steps == 1 else (low + high) // 2
            current = self.quote(point)
            quotes += 1

            first = self._first_input_at_least(current.amount_out)
            if first is None or first > point or current.amount_out == 0:
                raise RuntimeError("inconsistent inverse of positive V2 output")
            first = max(first, low)
            if first < point and quotes < max_quotes:
                # Same output at the earliest input is strictly better on this plateau.
                current = self.quote(first)
                quotes += 1
            if best is None or current.gross_profit > best.gross_profit:
                best = current
            left_end = first - 1 if current.amount_out - first <= best.gross_profit else point - 1
            right_start = self._first_input_at_least(current.amount_out + 1)
            # Inversion collapses arbitrarily wide plateaus without expanding their entries.
            intervals = [(low, left_end)]
            if right_start is not None:
                intervals.append((right_start, high))
            for start, end in intervals:
                if start <= end:
                    _, gross_bound = self._peak_and_bound(start, end)
                    if gross_bound > best.gross_profit:
                        heappush(pending, (-gross_bound, start, end))

        assert best is not None
        residuals = tuple(
            V2SizingResidual(low, high, -neg_upper - total_cost_in_anchor)
            for neg_upper, low, high in sorted(pending)
            if -neg_upper > best.gross_profit
        )
        best_net = best.gross_profit - total_cost_in_anchor
        net_upper = max((item.net_upper for item in residuals), default=best_net)
        return V2SizingResult(
            best, best_net, net_upper + total_cost_in_anchor, net_upper, quotes, steps, residuals
        )
