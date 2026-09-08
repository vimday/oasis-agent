from dataclasses import dataclass
from typing import Protocol

from ..models.chain import BlockSnapshot, LogRecord
from ..models.common import canonical_json, normalize_address, normalize_hash32, sha256_hex


class RangeRpc(Protocol):
    def chain_id(self) -> int: ...

    def get_block_snapshot(self, block: int | str = "latest") -> BlockSnapshot: ...

    def get_logs(self, filter_params: dict[str, object]) -> list[LogRecord]: ...


def _validate_blocks(blocks: tuple[BlockSnapshot, ...]) -> None:
    if not blocks:
        raise ValueError("block range dataset cannot be empty")
    previous = blocks[0]
    for current in blocks[1:]:
        if current.ref.chain_id != previous.ref.chain_id:
            raise ValueError("block range cannot cross chains")
        if current.ref.number != previous.ref.number + 1:
            raise ValueError("block range must be contiguous")
        if current.parent_hash != previous.ref.block_hash:
            raise ValueError("block range parent hash mismatch")
        assert current.ref.timestamp is not None
        assert previous.ref.timestamp is not None
        if current.ref.timestamp < previous.ref.timestamp:
            raise ValueError("block range timestamps must be nondecreasing")
        previous = current


def _log_position(log: LogRecord) -> tuple[int, int, int]:
    if log.transaction_index is None:
        raise ValueError("replay log is missing transaction index")
    return log.block_number, log.transaction_index, log.log_index


def _dataset_payload(
    blocks: tuple[BlockSnapshot, ...], logs: tuple[LogRecord, ...], log_filter: dict[str, object]
) -> bytes:
    return canonical_json(
        {
            "schema": "oasis.block-range.v2",
            "log_filter": log_filter,
            "chain_id": blocks[0].ref.chain_id,
            "start_block": blocks[0].ref.number,
            "end_block": blocks[-1].ref.number,
            "blocks": [
                {
                    "number": item.ref.number,
                    "hash": item.ref.block_hash,
                    "parent_hash": item.parent_hash,
                    "timestamp": item.ref.timestamp,
                }
                for item in blocks
            ],
            "logs": [
                {
                    "address": item.address,
                    "topics": item.topics,
                    "data": item.data,
                    "block_number": item.block_number,
                    "block_hash": item.block_hash,
                    "transaction_hash": item.transaction_hash,
                    "transaction_index": item.transaction_index,
                    "log_index": item.log_index,
                    "removed": item.removed,
                }
                for item in logs
            ],
        }
    )


@dataclass(frozen=True, slots=True)
class BlockRangeDataset:
    source: str
    blocks: tuple[BlockSnapshot, ...]
    logs: tuple[LogRecord, ...]
    address: str | None = None
    topics: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if not self.source:
            raise ValueError("dataset source is required")
        object.__setattr__(self, "blocks", tuple(self.blocks))
        object.__setattr__(self, "logs", tuple(self.logs))
        if self.address is not None:
            object.__setattr__(self, "address", normalize_address(self.address))
        if self.topics is not None:
            object.__setattr__(
                self,
                "topics",
                tuple(normalize_hash32(topic, "event topic") for topic in self.topics),
            )
            if len(self.topics) > 4:
                raise ValueError("event topic filter cannot exceed four positions")
        _validate_blocks(self.blocks)
        block_by_number = {item.ref.number: item.ref for item in self.blocks}
        positions: list[tuple[int, int, int]] = []
        for log in self.logs:
            if self.address is not None and log.address != self.address:
                raise ValueError("dataset log is outside the address filter")
            if self.topics is not None and log.topics[: len(self.topics)] != self.topics:
                raise ValueError("dataset log is outside the topic filter")
            if log.removed:
                raise ValueError("removed logs cannot enter a canonical dataset")
            if log.block_hash is None:
                raise ValueError("replay log is missing block hash")
            pinned = block_by_number.get(log.block_number)
            if pinned is None or pinned.block_hash != log.block_hash:
                raise ValueError("replay log does not match a pinned block hash")
            positions.append(_log_position(log))
        if positions != sorted(positions) or len(set(positions)) != len(positions):
            raise ValueError("replay logs must be in strict order without duplicates")

    @property
    def chain_id(self) -> int:
        return self.blocks[0].ref.chain_id

    @property
    def start_block(self) -> int:
        return self.blocks[0].ref.number

    @property
    def end_block(self) -> int:
        return self.blocks[-1].ref.number

    @property
    def payload(self) -> bytes:
        return _dataset_payload(self.blocks, self.logs, self.log_filter)

    @property
    def log_filter(self) -> dict[str, object]:
        result: dict[str, object] = {
            "fromBlock": hex(self.start_block),
            "toBlock": hex(self.end_block),
        }
        if self.address is not None:
            result["address"] = self.address
        if self.topics is not None:
            result["topics"] = list(self.topics)
        return result

    @property
    def content_hash(self) -> str:
        return sha256_hex(self.payload)


@dataclass(frozen=True, slots=True)
class ProviderAgreement:
    chain_id: int
    start_block: int
    end_block: int
    sources: tuple[str, ...]
    content_hash: str

    def __post_init__(self) -> None:
        if (
            isinstance(self.chain_id, bool)
            or not isinstance(self.chain_id, int)
            or self.chain_id <= 0
        ):
            raise ValueError("provider agreement chain_id must be positive")
        if self.start_block < 0 or self.end_block < self.start_block:
            raise ValueError("provider agreement block range is invalid")
        if len(self.sources) < 2 or len(set(self.sources)) != len(self.sources):
            raise ValueError("provider agreement requires at least two distinct sources")
        if tuple(sorted(self.sources)) != self.sources or any(
            not source for source in self.sources
        ):
            raise ValueError("provider agreement sources must be nonempty and sorted")
        normalize_hash32("0x" + self.content_hash, "dataset content hash")


def collect_block_range(
    rpc: RangeRpc,
    *,
    source: str,
    start_block: int,
    end_block: int,
    address: str | None = None,
    topics: tuple[str, ...] | None = None,
    max_blocks: int = 1_000,
) -> BlockRangeDataset:
    if isinstance(start_block, bool) or isinstance(end_block, bool):
        raise TypeError("block bounds must be integers")
    if start_block < 0 or end_block < start_block:
        raise ValueError("block range bounds are invalid")
    if max_blocks <= 0:
        raise ValueError("max_blocks must be positive")
    if end_block - start_block + 1 > max_blocks:
        raise ValueError("block range exceeds the configured maximum span")

    blocks = tuple(rpc.get_block_snapshot(number) for number in range(start_block, end_block + 1))
    if any(item.ref.chain_id != rpc.chain_id() for item in blocks):
        raise ValueError("RPC block range chain ID mismatch")

    filter_params: dict[str, object] = {
        "fromBlock": hex(start_block),
        "toBlock": hex(end_block),
    }
    normalized_address = None
    if address is not None:
        normalized_address = normalize_address(address)
        filter_params["address"] = normalized_address
    if topics is not None:
        normalized_topics = tuple(normalize_hash32(item, "event topic") for item in topics)
        filter_params["topics"] = list(normalized_topics)
    else:
        normalized_topics = None

    logs = tuple(rpc.get_logs(filter_params))
    if normalized_address is not None and any(log.address != normalized_address for log in logs):
        raise ValueError("RPC returned a log outside the address filter")
    if normalized_topics is not None and any(
        log.topics[: len(normalized_topics)] != normalized_topics for log in logs
    ):
        raise ValueError("RPC returned a log outside the topic filter")
    closing = rpc.get_block_snapshot(end_block)
    if closing != blocks[-1]:
        raise ValueError("RPC closing block changed during range collection")
    return BlockRangeDataset(source, blocks, logs, normalized_address, normalized_topics)


def require_provider_agreement(
    datasets: tuple[BlockRangeDataset, ...],
) -> ProviderAgreement:
    if len(datasets) < 2:
        raise ValueError("provider agreement requires at least two datasets")
    sources = tuple(sorted(item.source for item in datasets))
    if len(set(sources)) != len(sources):
        raise ValueError("provider agreement requires distinct providers")
    first = datasets[0]
    if any(item.content_hash != first.content_hash for item in datasets[1:]):
        raise ValueError("providers disagree on the block range dataset")
    return ProviderAgreement(
        first.chain_id,
        first.start_block,
        first.end_block,
        sources,
        first.content_hash,
    )


__all__ = [
    "BlockRangeDataset",
    "ProviderAgreement",
    "RangeRpc",
    "collect_block_range",
    "require_provider_agreement",
]
