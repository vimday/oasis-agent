from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ..errors import FeeEncodingError
from ..models.v4 import DYNAMIC_FEE_FLAG, OVERRIDE_FEE_FLAG, SwapDirection

UINT24_MAX = 0xFFFFFF
REMOVE_OVERRIDE_MASK = 0xBFFFFF
MAX_LP_FEE = 1_000_000
MAX_PROTOCOL_FEE = 1_000
PROTOCOL_FEE_MASK = 0xFFF
FEE_DENOMINATOR = Decimal(1_000_000)


@dataclass(frozen=True, slots=True)
class FeeBreakdown:
    lp_fee: int
    protocol_fee: int
    swap_fee: int


def validate_pool_fee(value: int) -> int:
    if value == DYNAMIC_FEE_FLAG:
        return value
    return validate_lp_fee(value)


def validate_lp_fee(value: int) -> int:
    if not 0 <= value <= MAX_LP_FEE:
        raise FeeEncodingError("LP fee exceeds the 100% economic limit")
    return value


def validate_fee_override(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("fee override must be an integer")
    if not 0 <= value <= UINT24_MAX:
        raise FeeEncodingError("fee override is outside uint24 range")
    if value & OVERRIDE_FEE_FLAG == 0:
        raise FeeEncodingError("fee override flag is missing")
    # The dynamic-fee sentinel is a pool configuration value, never a fee override.
    decoded = value & REMOVE_OVERRIDE_MASK
    if decoded & DYNAMIC_FEE_FLAG:
        raise FeeEncodingError("dynamic fee flag cannot be used as an override")
    return validate_lp_fee(decoded)


def encode_fee_override(fee_pips: int) -> int:
    """Encode an LP fee using the canonical v4 override flag."""

    validate_lp_fee(fee_pips)
    if fee_pips & DYNAMIC_FEE_FLAG:
        raise FeeEncodingError("dynamic fee flag cannot be encoded as an override")
    return fee_pips | OVERRIDE_FEE_FLAG


def validate_protocol_fee(value: int) -> int:
    if not 0 <= value <= UINT24_MAX:
        raise FeeEncodingError("protocol fee configuration is outside uint24 range")
    if value & PROTOCOL_FEE_MASK > MAX_PROTOCOL_FEE:
        raise FeeEncodingError("zero-for-one protocol fee exceeds 0.1%")
    if value >> 12 > MAX_PROTOCOL_FEE:
        raise FeeEncodingError("one-for-zero protocol fee exceeds 0.1%")
    return value


def protocol_fee_for_direction(value: int, direction: SwapDirection) -> int:
    validate_protocol_fee(value)
    if direction is SwapDirection.ZERO_FOR_ONE:
        return value & PROTOCOL_FEE_MASK
    return value >> 12


def calculate_swap_fee(lp_fee: int, protocol_fee: int) -> int:
    validate_lp_fee(lp_fee)
    if not 0 <= protocol_fee <= MAX_PROTOCOL_FEE:
        raise FeeEncodingError("directional protocol fee exceeds 0.1%")
    return protocol_fee + lp_fee - (protocol_fee * lp_fee // int(FEE_DENOMINATOR))


def settled_fee_breakdown(
    lp_fee: int, protocol_fee_config: int, direction: SwapDirection
) -> FeeBreakdown:
    directional_protocol_fee = protocol_fee_for_direction(protocol_fee_config, direction)
    return FeeBreakdown(
        lp_fee=validate_lp_fee(lp_fee),
        protocol_fee=directional_protocol_fee,
        swap_fee=calculate_swap_fee(lp_fee, directional_protocol_fee),
    )


def fee_fraction(fee: int) -> Decimal:
    return Decimal(validate_lp_fee(fee)) / FEE_DENOMINATOR
