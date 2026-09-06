from __future__ import annotations

from dataclasses import dataclass

from .common import BlockRef, normalize_address, normalize_hash32, normalize_hex


@dataclass(frozen=True, slots=True)
class ChainConfig:
    chain_id: int
    rpc_url: str
    pool_manager: str

    def __post_init__(self) -> None:
        if type(self.chain_id) is not int or self.chain_id <= 0:
            raise ValueError("chain_id must be a positive integer")
        if not self.rpc_url.startswith(("http://", "https://")):
            raise ValueError("rpc_url must use http or https")
        object.__setattr__(self, "pool_manager", normalize_address(self.pool_manager))


@dataclass(frozen=True, slots=True)
class BlockSnapshot:
    ref: BlockRef
    parent_hash: str

    def __post_init__(self) -> None:
        if self.ref.timestamp is None:
            raise ValueError("block snapshot requires a timestamp")
        object.__setattr__(self, "parent_hash", normalize_hash32(self.parent_hash, "parent hash"))


@dataclass(frozen=True, slots=True)
class LogRecord:
    address: str
    topics: tuple[str, ...]
    data: str
    block_number: int
    transaction_hash: str
    log_index: int
    block_hash: str | None = None
    transaction_index: int | None = None
    removed: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "address", normalize_address(self.address))
        object.__setattr__(self, "topics", tuple(normalize_hex(x) for x in self.topics))
        object.__setattr__(self, "data", normalize_hex(self.data))
        object.__setattr__(
            self, "transaction_hash", normalize_hash32(self.transaction_hash, "transaction hash")
        )
        if self.block_number < 0 or self.log_index < 0:
            raise ValueError("block_number and log_index must be nonnegative")
        if self.block_hash is not None:
            object.__setattr__(self, "block_hash", normalize_hash32(self.block_hash, "block hash"))
        if self.transaction_index is not None:
            if isinstance(self.transaction_index, bool) or not isinstance(
                self.transaction_index, int
            ):
                raise TypeError("transaction_index must be an integer")
            if self.transaction_index < 0:
                raise ValueError("transaction_index must be nonnegative")
        if not isinstance(self.removed, bool):
            raise TypeError("removed must be a boolean")


@dataclass(frozen=True, slots=True)
class Receipt:
    transaction_hash: str
    status: int
    block_number: int
    logs: tuple[LogRecord, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "transaction_hash", normalize_hash32(self.transaction_hash, "transaction hash")
        )
        if self.status not in (0, 1):
            raise ValueError("receipt status must be 0 or 1")
        if self.block_number < 0:
            raise ValueError("receipt block_number must be nonnegative")
        for log in self.logs:
            if (
                log.transaction_hash != self.transaction_hash
                or log.block_number != self.block_number
            ):
                raise ValueError("receipt log identity does not match its transaction and block")
