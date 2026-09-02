from __future__ import annotations

from dataclasses import dataclass

from .common import BlockRef, normalize_address, normalize_hex


@dataclass(frozen=True, slots=True)
class ChainConfig:
    chain_id: int
    rpc_url: str
    pool_manager: str

    def __post_init__(self) -> None:
        if self.chain_id <= 0:
            raise ValueError("chain_id must be positive")
        if not self.rpc_url.startswith(("http://", "https://")):
            raise ValueError("rpc_url must use http or https")
        object.__setattr__(self, "pool_manager", normalize_address(self.pool_manager))


@dataclass(frozen=True, slots=True)
class LogRecord:
    address: str
    topics: tuple[str, ...]
    data: str
    block_number: int
    transaction_hash: str
    log_index: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "address", normalize_address(self.address))
        object.__setattr__(self, "topics", tuple(normalize_hex(x) for x in self.topics))
        object.__setattr__(self, "data", normalize_hex(self.data))
        if self.block_number < 0 or self.log_index < 0:
            raise ValueError("block_number and log_index must be nonnegative")


@dataclass(frozen=True, slots=True)
class Receipt:
    transaction_hash: str
    status: int
    block_number: int
    logs: tuple[LogRecord, ...]

    def __post_init__(self) -> None:
        if self.status not in (0, 1):
            raise ValueError("receipt status must be 0 or 1")
