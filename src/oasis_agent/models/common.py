from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Final

ZERO_ADDRESS: Final[str] = "0x" + "0" * 40


class EvidenceLevel(str, Enum):
    CONFIRMED = "confirmed"
    INFERRED = "inferred"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True, slots=True)
class BlockRef:
    chain_id: int
    number: int
    block_hash: str
    timestamp: int | None = None


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    kind: str
    content_hash: str
    block: BlockRef | None
    source: str


def normalize_address(value: str) -> str:
    if not isinstance(value, str) or len(value) != 42 or not value.startswith("0x"):
        raise ValueError(f"invalid address: {value!r}")
    try:
        int(value[2:], 16)
    except ValueError as exc:
        raise ValueError(f"invalid address: {value!r}") from exc
    return value.lower()


def normalize_hex(value: str) -> str:
    if not isinstance(value, str) or not value.startswith("0x"):
        raise ValueError("hex data must start with 0x")
    int(value[2:] or "0", 16)
    if len(value[2:]) % 2:
        raise ValueError("hex data must contain complete bytes")
    return value.lower()
