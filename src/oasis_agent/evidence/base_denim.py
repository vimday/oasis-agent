"""Strict Base Denim timestamp checks for retained headers and BaseTime reads.

Base Denim (2026-11-05 18:00 UTC) produces native 200 ms blocks. The EVM
header timestamp stays in whole seconds, while the exact slot is published as
`timestampMs`. Mixing the two silently truncates up to 999 ms; this module
rejects malformed or inconsistent pairs instead of choosing a default.
"""

from collections.abc import Mapping
from typing import Final

from .rpc_validation import rpc_quantity

BASE_CHAIN_ID: Final[int] = 8453
DENIM_ACTIVATION_TIMESTAMP: Final[int] = 1_793_901_600
BASE_TIME: Final[str] = "0x4200000000000000000000000000000000000030"
TIMESTAMP_MS_SELECTOR: Final[str] = "0x5745a677"
TIMESTAMP_MILLIS_PART_SELECTOR: Final[str] = "0x7b2fea99"


def parse_header_timestamp_ms(header: Mapping[str, object], *, require_denim: bool) -> int | None:
    """Return the header `timestampMs` or None when absent on a pre-Denim block."""
    raw = header.get("timestampMs")
    timestamp = rpc_quantity(header.get("timestamp"))
    if raw is None:
        if require_denim:
            raise ValueError("base_denim_timestamp_ms_missing")
        return None
    value = rpc_quantity(raw)
    if value >= 1 << 64:
        raise ValueError("base_denim_timestamp_ms_overflow")
    if value // 1000 != timestamp:
        raise ValueError("base_denim_timestamp_ms_mismatch")
    return value


def validate_header_timestamp_ms(
    header: Mapping[str, object], *, chain_id: int, require_denim: bool = False
) -> int | None:
    """Validate a Base header; when required, a missing millisecond field is refused."""
    if chain_id != BASE_CHAIN_ID:
        return None
    timestamp = rpc_quantity(header.get("timestamp"))
    return parse_header_timestamp_ms(
        header,
        require_denim=require_denim or timestamp >= DENIM_ACTIVATION_TIMESTAMP,
    )


def parse_base_time(*, timestamp_ms: int, millis_part: int, header_timestamp: int) -> int:
    """Cross-check the BaseTime predeploy pair against the block's whole-second stamp."""
    if type(timestamp_ms) is not int or not 0 <= timestamp_ms < 1 << 64:
        raise ValueError("base_time_timestamp_ms_invalid")
    if type(millis_part) is not int or not 0 <= millis_part <= 999:
        raise ValueError("base_time_millis_part_invalid")
    if timestamp_ms // 1000 != header_timestamp:
        raise ValueError("base_time_second_mismatch")
    if timestamp_ms % 1000 != millis_part:
        raise ValueError("base_time_millis_part_mismatch")
    return timestamp_ms


__all__ = [
    "BASE_CHAIN_ID",
    "BASE_TIME",
    "DENIM_ACTIVATION_TIMESTAMP",
    "TIMESTAMP_MILLIS_PART_SELECTOR",
    "TIMESTAMP_MS_SELECTOR",
    "parse_base_time",
    "parse_header_timestamp_ms",
    "validate_header_timestamp_ms",
]
