from __future__ import annotations

from ..models.v4 import BalanceDelta
from ..errors import FeeEncodingError


def decode_balance_delta(value: bytes) -> BalanceDelta:
    if len(value) != 32:
        raise FeeEncodingError("BalanceDelta must be exactly 32 bytes")
    raw0 = int.from_bytes(value[:16], "big")
    raw1 = int.from_bytes(value[16:], "big")
    sign = 1 << 127
    amount0 = raw0 - (1 << 128) if raw0 & sign else raw0
    amount1 = raw1 - (1 << 128) if raw1 & sign else raw1
    return BalanceDelta(amount0, amount1)
