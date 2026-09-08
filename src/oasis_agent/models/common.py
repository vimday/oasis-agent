import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final

ZERO_ADDRESS: Final[str] = "0x" + "0" * 40


class EvidenceLevel(StrEnum):
    CONFIRMED = "confirmed"
    INFERRED = "inferred"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True, slots=True)
class BlockRef:
    chain_id: int
    number: int
    block_hash: str
    timestamp: int | None = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.chain_id, bool)
            or not isinstance(self.chain_id, int)
            or self.chain_id <= 0
        ):
            raise ValueError("chain_id must be a positive integer")
        if isinstance(self.number, bool) or not isinstance(self.number, int) or self.number < 0:
            raise ValueError("block number must be a nonnegative integer")
        object.__setattr__(self, "block_hash", normalize_hash32(self.block_hash, "block hash"))
        if self.timestamp is not None and (
            isinstance(self.timestamp, bool)
            or not isinstance(self.timestamp, int)
            or self.timestamp < 0
        ):
            raise ValueError("block timestamp must be a nonnegative integer")


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    kind: str
    content_hash: str
    block: BlockRef | None
    source: str
    retrieved_at: str = ""
    request_hash: str = ""
    raw_response_hash: str = ""
    interpretation_hash: str = ""
    schema_version: int = 1

    def __post_init__(self) -> None:
        if not self.kind or not self.source:
            raise ValueError("evidence kind and source are required")
        if self.schema_version <= 0:
            raise ValueError("evidence schema version must be positive")
        if not self.retrieved_at:
            object.__setattr__(self, "retrieved_at", datetime.now(UTC).isoformat())
        for name, value, optional in (
            ("content_hash", self.content_hash, False),
            ("request_hash", self.request_hash, True),
            ("raw_response_hash", self.raw_response_hash, True),
            ("interpretation_hash", self.interpretation_hash, True),
        ):
            if optional and not value:
                continue
            if len(value) != 64:
                raise ValueError(f"{name} must be a SHA-256 hex digest")
            try:
                int(value, 16)
            except ValueError as exc:
                raise ValueError(f"{name} must be a SHA-256 hex digest") from exc
        try:
            retrieved = datetime.fromisoformat(self.retrieved_at)
        except ValueError as exc:
            raise ValueError("retrieved_at must be an ISO-8601 timestamp") from exc
        if retrieved.tzinfo is None or retrieved.utcoffset() is None:
            raise ValueError("retrieved_at must include a timezone")


def canonical_json(value: object) -> bytes:
    """Return stable JSON bytes for hashing structured request/response data."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sha256_hex(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def normalize_address(value: str) -> str:
    if not isinstance(value, str) or len(value) != 42 or not value.startswith("0x"):
        raise ValueError(f"invalid address: {value!r}")
    try:
        int(value[2:], 16)  # noqa: FURB166
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


def normalize_hash32(value: str, name: str = "hash") -> str:
    normalized = normalize_hex(value)
    if len(normalized) != 66:
        raise ValueError(f"{name} must contain exactly 32 bytes")
    return normalized
