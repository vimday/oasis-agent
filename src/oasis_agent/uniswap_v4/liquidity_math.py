from __future__ import annotations

MIN_TICK = -887_272
MAX_TICK = 887_272
MIN_SQRT_PRICE = 4_295_128_739
MAX_SQRT_PRICE = 1_461_446_703_485_210_103_287_273_052_203_988_822_378_723_970_342
Q96 = 1 << 96
Q128 = 1 << 128
UINT160_MAX = (1 << 160) - 1
UINT256_MAX = (1 << 256) - 1

_TICK_RATIOS = (
    0xFFFCB933BD6FAD37AA2D162D1A594001,
    0xFFF97272373D413259A46990580E213A,
    0xFFF2E50F5F656932EF12357CF3C7FDCC,
    0xFFE5CACA7E10E4E61C3624EAA0941CD0,
    0xFFCB9843D60F6159C9DB58835C926644,
    0xFF973B41FA98C081472E6896DFB254C0,
    0xFF2EA16466C96A3843EC78B326B52861,
    0xFE5DEE046A99A2A811C461F1969C3053,
    0xFCBE86C7900A88AEDCFFC83B479AA3A4,
    0xF987A7253AC413176F2B074CF7815E54,
    0xF3392B0822B70005940C7A398E4B70F3,
    0xE7159475A2C29B7443B29C7FA6E889D9,
    0xD097F3BDFD2022B8845AD8F792AA5825,
    0xA9F746462D870FDF8A65DC1F90E061E5,
    0x70D869A156D2A1B890BB3DF62BAF32F7,
    0x31BE135F97D08FD981231505542FCFA6,
    0x9AA508B5B7A84E1C677DE54F3E99BC9,
    0x5D6AF8DEDB81196699C329225EE604,
    0x2216E584F5FA1EA926041BEDFE98,
    0x48A170391F7DC42444E8FA2,
)


def _validate_uint(value: int, bits: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if not 0 <= value < 1 << bits:
        raise ValueError(f"{name} must fit uint{bits}")


def _ceil_div(numerator: int, denominator: int) -> int:
    return (numerator + denominator - 1) // denominator


def sqrt_price_at_tick(tick: int) -> int:
    if isinstance(tick, bool) or not isinstance(tick, int):
        raise TypeError("tick must be an integer")
    if not MIN_TICK <= tick <= MAX_TICK:
        raise ValueError("tick is outside canonical TickMath bounds")
    absolute = abs(tick)
    ratio = _TICK_RATIOS[0] if absolute & 1 else Q128
    for bit, multiplier in enumerate(_TICK_RATIOS[1:], start=1):
        if absolute & (1 << bit):
            ratio = ratio * multiplier >> 128
    if tick > 0:
        ratio = UINT256_MAX // ratio
    return (ratio + (1 << 32) - 1) >> 32


def amount0_delta(
    sqrt_price_a_x96: int,
    sqrt_price_b_x96: int,
    liquidity: int,
    *,
    round_up: bool,
) -> int:
    _validate_uint(sqrt_price_a_x96, 160, "sqrt_price_a_x96")
    _validate_uint(sqrt_price_b_x96, 160, "sqrt_price_b_x96")
    _validate_uint(liquidity, 128, "liquidity")
    lower, upper = sorted((sqrt_price_a_x96, sqrt_price_b_x96))
    if lower == 0:
        raise ValueError("lower sqrt price cannot be zero")
    numerator1 = liquidity << 96
    numerator2 = upper - lower
    if round_up:
        intermediate = _ceil_div(numerator1 * numerator2, upper)
        return _ceil_div(intermediate, lower)
    return (numerator1 * numerator2 // upper) // lower


def amount1_delta(
    sqrt_price_a_x96: int,
    sqrt_price_b_x96: int,
    liquidity: int,
    *,
    round_up: bool,
) -> int:
    _validate_uint(sqrt_price_a_x96, 160, "sqrt_price_a_x96")
    _validate_uint(sqrt_price_b_x96, 160, "sqrt_price_b_x96")
    _validate_uint(liquidity, 128, "liquidity")
    numerator = liquidity * abs(sqrt_price_b_x96 - sqrt_price_a_x96)
    return _ceil_div(numerator, Q96) if round_up else numerator // Q96


def amounts_for_liquidity(
    sqrt_price_x96: int,
    sqrt_price_lower_x96: int,
    sqrt_price_upper_x96: int,
    liquidity: int,
    *,
    round_up: bool = False,
) -> tuple[int, int]:
    _validate_uint(sqrt_price_x96, 160, "sqrt_price_x96")
    _validate_uint(sqrt_price_lower_x96, 160, "sqrt_price_lower_x96")
    _validate_uint(sqrt_price_upper_x96, 160, "sqrt_price_upper_x96")
    _validate_uint(liquidity, 128, "liquidity")
    if not sqrt_price_lower_x96 < sqrt_price_upper_x96:
        raise ValueError("sqrt price range must be ordered")
    if sqrt_price_x96 <= sqrt_price_lower_x96:
        return (
            amount0_delta(
                sqrt_price_lower_x96,
                sqrt_price_upper_x96,
                liquidity,
                round_up=round_up,
            ),
            0,
        )
    if sqrt_price_x96 < sqrt_price_upper_x96:
        return (
            amount0_delta(
                sqrt_price_x96,
                sqrt_price_upper_x96,
                liquidity,
                round_up=round_up,
            ),
            amount1_delta(
                sqrt_price_lower_x96,
                sqrt_price_x96,
                liquidity,
                round_up=round_up,
            ),
        )
    return (
        0,
        amount1_delta(
            sqrt_price_lower_x96,
            sqrt_price_upper_x96,
            liquidity,
            round_up=round_up,
        ),
    )


__all__ = [
    "MAX_SQRT_PRICE",
    "MAX_TICK",
    "MIN_SQRT_PRICE",
    "MIN_TICK",
    "amount0_delta",
    "amount1_delta",
    "amounts_for_liquidity",
    "sqrt_price_at_tick",
]
