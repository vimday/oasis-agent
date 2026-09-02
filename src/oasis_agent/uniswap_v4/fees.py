from __future__ import annotations

from decimal import Decimal, ROUND_DOWN

from ..errors import FeeEncodingError
from ..models.v4 import DYNAMIC_FEE_FLAG, OVERRIDE_FEE_FLAG

UINT24_MAX = 0xFFFFFF
OVERRIDE_PAYLOAD_MAX = 0x3FFFFF
FEE_DENOMINATOR = Decimal(1_000_000)


def validate_fee_override(value: int) -> int:
    if value & OVERRIDE_FEE_FLAG == 0:
        raise FeeEncodingError("fee override flag is missing")
    fee = value & OVERRIDE_PAYLOAD_MAX
    if fee == 0 or fee > OVERRIDE_PAYLOAD_MAX:
        raise FeeEncodingError("invalid fee override payload")
    return fee


def effective_fee(input_fee: int, protocol_fee: int) -> Decimal:
    if not 0 <= input_fee <= UINT24_MAX:
        raise FeeEncodingError("input fee is outside uint24 range")
    if not 0 <= protocol_fee <= UINT24_MAX:
        raise FeeEncodingError("protocol fee is outside uint24 range")
    return (Decimal(input_fee) / FEE_DENOMINATOR) + (Decimal(protocol_fee) / FEE_DENOMINATOR)
