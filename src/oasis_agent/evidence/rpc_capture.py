from dataclasses import dataclass, field
from datetime import UTC, datetime

from ..models.common import EvidenceRef
from ..rpc.collector import BlockRangeDataset
from ..rpc.transport import RpcExchange
from .range_validation import validate_block_range_exchanges
from .store import EvidenceStore


@dataclass(slots=True)
class BoundedExchangeRecorder:
    max_exchanges: int = 5_000
    max_bytes: int = 50_000_000
    _items: list[RpcExchange] = field(default_factory=list, init=False, repr=False)
    _bytes: int = field(default=0, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.max_exchanges <= 0 or self.max_bytes <= 0:
            raise ValueError("exchange recorder bounds must be positive")

    def record(self, exchange: RpcExchange) -> None:
        next_count = len(self._items) + 1
        next_bytes = self._bytes + len(exchange.request_payload) + len(exchange.response_payload)
        if next_count > self.max_exchanges or next_bytes > self.max_bytes:
            raise ValueError("RPC evidence recorder capacity exceeded")
        self._items.append(exchange)
        self._bytes = next_bytes

    @property
    def exchanges(self) -> tuple[RpcExchange, ...]:
        return tuple(self._items)

    @property
    def retained_bytes(self) -> int:
        return self._bytes


def persist_block_range_evidence(
    store: EvidenceStore,
    dataset: BlockRangeDataset,
    exchanges: tuple[RpcExchange, ...],
    *,
    retrieved_at: str | None = None,
) -> EvidenceRef:
    if not exchanges:
        raise ValueError("block range evidence requires raw RPC exchanges")
    methods = tuple(exchange.method for exchange in exchanges)
    if "eth_getLogs" not in methods or "eth_getBlockByNumber" not in methods:
        raise ValueError("RPC exchanges do not cover block headers and logs")
    validate_block_range_exchanges(dataset, exchanges)
    timestamp = retrieved_at or datetime.now(UTC).isoformat()
    request_envelope = {
        "encoding": "hex_exact_bytes",
        "exchanges": [
            {
                "method": exchange.method,
                "request_id": exchange.request_id,
                "attempt": exchange.attempt,
                "payload": exchange.request_payload.hex(),
            }
            for exchange in exchanges
        ],
    }
    response_envelope = {
        "encoding": "hex_exact_bytes",
        "exchanges": [
            {
                "method": exchange.method,
                "request_id": exchange.request_id,
                "attempt": exchange.attempt,
                "payload": exchange.response_payload.hex(),
            }
            for exchange in exchanges
        ],
    }
    interpretation = {
        "schema": "oasis.block-range.v2",
        "log_filter": dataset.log_filter,
        "dataset_content_hash": dataset.content_hash,
        "chain_id": dataset.chain_id,
        "start_block": dataset.start_block,
        "end_block": dataset.end_block,
        "block_count": len(dataset.blocks),
        "log_count": len(dataset.logs),
        "rpc_exchange_count": len(exchanges),
    }
    return store.put(
        "canonical_block_range",
        dataset.payload,
        dataset.blocks[-1].ref,
        dataset.source,
        request=request_envelope,
        raw_response=response_envelope,
        interpretation=interpretation,
        retrieved_at=timestamp,
        schema_version=2,
    )


__all__ = ["BoundedExchangeRecorder", "persist_block_range_evidence"]
