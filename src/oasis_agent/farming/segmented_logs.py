"""Bounded historical log acquisition using retained contiguous header proofs.

Each wire query stays separate. These proofs are not the single-query block-range
evidence format consumed by cash_window; no synthetic merged RPC reply is created.
"""

from dataclasses import asdict, dataclass

from pydantic import TypeAdapter

from ..evidence.exact_capture import (
    RecordedSession,
    decode_header,
    successful_results,
    wire_envelope,
)
from ..evidence.ingestion import replay_block_range_evidence
from ..evidence.rpc_validation import (
    restore_exchanges,
    rpc_quantity,
    strict_boolean,
    strict_json_object,
    strict_list,
    strict_object,
    strict_string,
)
from ..evidence.store import EvidenceStore
from ..models.chain import BlockSnapshot, LogRecord
from ..models.common import EvidenceRef, canonical_json, normalize_address, normalize_hash32
from ..rpc.collector import BlockRangeDataset
from ..rpc.transport import RpcExchange

KIND = "segmented-log-range"


@dataclass(frozen=True, slots=True)
class SegmentedLogPlan:
    header_evidence: tuple[EvidenceRef, ...]
    address: str
    topics: tuple[str, ...]
    segment_blocks: int

    def __post_init__(self) -> None:
        if not 1 <= len(self.header_evidence) <= 1000:
            raise ValueError("bounded retained header proof sequence required")
        if type(self.segment_blocks) is not int or not 1 <= self.segment_blocks <= 100:
            raise ValueError("segment size must be 1..100 blocks")
        if not 1 <= len(self.topics) <= 4:
            raise ValueError("explicit event topic filter required")
        object.__setattr__(self, "address", normalize_address(self.address))
        object.__setattr__(self, "topics", tuple(normalize_hash32(t) for t in self.topics))


def _headers(
    store: EvidenceStore, plan: SegmentedLogPlan, source: str
) -> tuple[BlockSnapshot, ...]:
    if any(ref.source != source for ref in plan.header_evidence):
        raise ValueError("header proofs must belong to the querying provider")
    blocks = tuple(
        b for ref in plan.header_evidence for b in replay_block_range_evidence(store, ref).blocks
    )
    if len(blocks) > 1000:
        raise ValueError("segmented capture exceeds bounded block range")
    # Validates gaps, overlap, ancestry, chain and timestamp order before any I/O.
    return BlockRangeDataset(source, blocks, ()).blocks


def _requests(
    plan: SegmentedLogPlan, blocks: tuple[BlockSnapshot, ...]
) -> list[tuple[str, list[object]]]:
    first, last = blocks[0].ref.number, blocks[-1].ref.number
    queries: list[tuple[str, list[object]]] = [
        ("eth_chainId", []),
        ("eth_getBlockByNumber", [hex(first), False]),
    ]
    queries.extend(
        (
            "eth_getLogs",
            [
                {
                    "fromBlock": hex(start),
                    "toBlock": hex(min(last, start + plan.segment_blocks - 1)),
                    "address": plan.address,
                    "topics": list(plan.topics),
                }
            ],
        )
        for start in range(first, last + 1, plan.segment_blocks)
    )
    queries.append(("eth_getBlockByNumber", [hex(last), False]))
    return queries


def minimum_requests(store: EvidenceStore, plan: SegmentedLogPlan, source: str) -> int:
    return len(_requests(plan, _headers(store, plan, source)))


def _log(value: object) -> LogRecord:
    v = strict_object(value, "log")
    return LogRecord(
        strict_string(v.get("address"), "address"),
        tuple(strict_string(t, "topic") for t in strict_list(v.get("topics"), "topics")),
        strict_string(v.get("data"), "data"),
        rpc_quantity(v.get("blockNumber")),
        strict_string(v.get("transactionHash"), "transaction hash"),
        rpc_quantity(v.get("logIndex")),
        strict_string(v.get("blockHash"), "block hash"),
        rpc_quantity(v.get("transactionIndex")),
        strict_boolean(v.get("removed"), "removed"),
    )


def _derive(
    store: EvidenceStore,
    plan: SegmentedLogPlan,
    source: str,
    exchanges: tuple[RpcExchange, ...],
) -> BlockRangeDataset:
    blocks = _headers(store, plan, source)
    queries = _requests(plan, blocks)
    results = successful_results(exchanges, queries)
    chain = blocks[0].ref.chain_id
    if rpc_quantity(results[0]) != chain:
        raise ValueError("segmented capture chain differs from retained headers")
    if (
        decode_header(results[1], chain_id=chain) != blocks[0]
        or decode_header(results[-1], chain_id=chain) != blocks[-1]
    ):
        raise ValueError("segmented capture boundary moved or differs from retained headers")
    logs: list[LogRecord] = []
    for query, value in zip(queries[2:-1], results[2:-1], strict=True):
        segment = tuple(_log(v) for v in strict_list(value, "logs"))
        bounds = strict_object(query[1][0], "filter")
        start, end = rpc_quantity(bounds["fromBlock"]), rpc_quantity(bounds["toBlock"])
        # Validate each segment separately, so a correct log in the wrong response
        # cannot be accepted merely because it belongs to the overall range.
        BlockRangeDataset(
            source,
            tuple(b for b in blocks if start <= b.ref.number <= end),
            segment,
            plan.address,
            plan.topics,
        )
        logs.extend(segment)
    return BlockRangeDataset(source, blocks, tuple(logs), plan.address, plan.topics)


def _payload(plan: SegmentedLogPlan, dataset: BlockRangeDataset) -> bytes:
    return canonical_json(
        {
            "schema": "oasis.segmented-log-range.v1",
            "plan": asdict(plan),
            "dataset_hash": dataset.content_hash,
            "event_count": len(dataset.logs),
            "execution_eligible": False,
        }
    )


def collect_segmented_logs(
    store: EvidenceStore, plan: SegmentedLogPlan, session: RecordedSession
) -> EvidenceRef:
    rpc, recorder, source = session
    if recorder.exchanges:
        raise ValueError("fresh segmented capture session required")
    blocks = _headers(store, plan, source)
    queries = _requests(plan, blocks)
    if rpc_quantity(rpc.request(*queries[0])) != blocks[0].ref.chain_id:
        raise ValueError("segmented capture chain differs from retained headers")
    if decode_header(rpc.request(*queries[1]), chain_id=blocks[0].ref.chain_id) != blocks[0]:
        raise ValueError("segmented capture opening boundary differs from retained headers")
    for method, params in queries[2:-1]:
        result = rpc.request(method, params)
        bounds = strict_object(params[0], "filter")
        start, end = rpc_quantity(bounds["fromBlock"]), rpc_quantity(bounds["toBlock"])
        BlockRangeDataset(
            source,
            tuple(b for b in blocks if start <= b.ref.number <= end),
            tuple(_log(v) for v in strict_list(result, "logs")),
            plan.address,
            plan.topics,
        )
    rpc.request(*queries[-1])
    dataset = _derive(store, plan, source, recorder.exchanges)
    return store.put(
        KIND,
        _payload(plan, dataset),
        blocks[-1].ref,
        source,
        request=wire_envelope(recorder.exchanges, response=False),
        raw_response=wire_envelope(recorder.exchanges, response=True),
    )


def replay_segmented_logs(store: EvidenceStore, ref: EvidenceRef) -> BlockRangeDataset:
    if ref.kind != KIND:
        raise ValueError("segmented log evidence required")
    record = store.get_record(ref)
    if record.request_payload is None:
        raise ValueError("segmented log request bytes absent")
    payload = strict_json_object(record.payload)
    plan = TypeAdapter(SegmentedLogPlan).validate_python(payload.get("plan"))
    exchanges = restore_exchanges(record.request_payload, record.raw_response_payload)
    dataset = _derive(store, plan, ref.source, exchanges)
    if ref.block != dataset.blocks[-1].ref or record.payload != _payload(plan, dataset):
        raise ValueError("segmented log interpretation differs from its retained proofs")
    return dataset
