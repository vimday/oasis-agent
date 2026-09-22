from dataclasses import dataclass
from enum import StrEnum

from ..models.chain import BlockSnapshot, LogRecord
from ..models.common import BlockRef, EvidenceRef
from ..rpc.collector import BlockRangeDataset, ProviderAgreement, require_provider_agreement
from ..rpc.transport import RpcExchange
from .chain_store import CanonicalChainStore, ChainApplyResult
from .range_validation import validate_block_range_exchanges
from .rpc_capture import persist_block_range_evidence
from .rpc_validation import restore_exchanges, strict_json_object
from .store import EvidenceStore


class IngestionTrust(StrEnum):
    PROVIDER_QUORUM = "provider_quorum"
    SINGLE_PROVIDER_RESEARCH = "single_provider_research"


@dataclass(frozen=True, slots=True)
class VerifiedBlockRange:
    dataset: BlockRangeDataset
    evidence: EvidenceRef
    trust: IngestionTrust
    agreement: ProviderAgreement | None
    peer_evidence: tuple[EvidenceRef, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.trust, IngestionTrust):
            raise TypeError("ingestion trust must be an IngestionTrust")
        object.__setattr__(self, "peer_evidence", tuple(self.peer_evidence))
        if self.evidence.kind != "canonical_block_range":
            raise ValueError("block range has the wrong evidence kind")
        if self.evidence.content_hash != self.dataset.content_hash:
            raise ValueError("evidence content hash does not match the dataset")
        if self.evidence.source != self.dataset.source:
            raise ValueError("evidence source does not match the dataset")
        if self.evidence.block != self.dataset.blocks[-1].ref:
            raise ValueError("block range evidence is not pinned to the closing block")
        if self.trust is IngestionTrust.PROVIDER_QUORUM:
            if self.agreement is None:
                raise ValueError("provider-quorum ingestion requires an agreement")
            _validate_agreement(self.dataset, self.agreement)
            references = (self.evidence, *self.peer_evidence)
            sources = tuple(sorted(reference.source for reference in references))
            if sources != self.agreement.sources:
                raise ValueError("provider quorum requires evidence for every agreed provider")
            for reference in references:
                if (
                    reference.kind != "canonical_block_range"
                    or reference.schema_version != 2
                    or reference.content_hash != self.dataset.content_hash
                    or reference.block != self.dataset.blocks[-1].ref
                    or not all(
                        (
                            reference.request_hash,
                            reference.raw_response_hash,
                            reference.interpretation_hash,
                        )
                    )
                ):
                    raise ValueError("provider quorum requires complete scope-bound v2 evidence")
        elif self.agreement is not None or self.peer_evidence:
            raise ValueError("single-provider research ingestion cannot carry a quorum agreement")


@dataclass(frozen=True, slots=True)
class VerifiedIngestionResult:
    verified: VerifiedBlockRange
    chain: ChainApplyResult


def _validate_agreement(dataset: BlockRangeDataset, agreement: ProviderAgreement) -> None:
    if (
        agreement.chain_id != dataset.chain_id
        or agreement.start_block != dataset.start_block
        or agreement.end_block != dataset.end_block
        or agreement.content_hash != dataset.content_hash
        or dataset.source not in agreement.sources
    ):
        raise ValueError("provider agreement does not attest this dataset")


def persist_and_verify_block_range(
    evidence_store: EvidenceStore,
    dataset: BlockRangeDataset,
    exchanges: tuple[RpcExchange, ...],
    *,
    agreement: ProviderAgreement | None = None,
    peer_captures: tuple[tuple[BlockRangeDataset, tuple[RpcExchange, ...]], ...] = (),
    research_single_provider: bool = False,
    retrieved_at: str | None = None,
) -> VerifiedBlockRange:
    if agreement is not None and research_single_provider:
        raise ValueError("choose provider quorum or explicit single-provider research mode")
    if agreement is None and not research_single_provider:
        raise ValueError("production ingestion requires provider agreement")
    if agreement is not None:
        _validate_agreement(dataset, agreement)
        if not peer_captures:
            raise ValueError("provider quorum requires raw peer captures")
        actual_agreement = require_provider_agreement(
            (dataset, *(peer for peer, _ in peer_captures))
        )
        if actual_agreement != agreement:
            raise ValueError("raw peer captures do not match the declared provider agreement")
        # Validate the entire cohort before writing any interpreted evidence.
        validate_block_range_exchanges(dataset, exchanges)
        for peer, raw in peer_captures:
            validate_block_range_exchanges(peer, raw)
        trust = IngestionTrust.PROVIDER_QUORUM
    else:
        if peer_captures:
            raise ValueError("raw peer captures require a provider agreement")
        trust = IngestionTrust.SINGLE_PROVIDER_RESEARCH
    peers = tuple(
        persist_block_range_evidence(evidence_store, peer, raw, retrieved_at=retrieved_at)
        for peer, raw in peer_captures
    )
    evidence = persist_block_range_evidence(
        evidence_store,
        dataset,
        exchanges,
        retrieved_at=retrieved_at,
    )
    return VerifiedBlockRange(dataset, evidence, trust, agreement, peers)


def _exact_fields(value: dict[str, object], expected: set[str], name: str) -> None:
    if set(value) != expected:
        raise ValueError(f"{name} has invalid fields")


def _required_dict(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError(f"{name} must be an object")
    return value


def _required_list(value: object, name: str) -> list[object]:
    if not isinstance(value, list):
        raise TypeError(f"{name} must be an array")
    return value


def _required_int(value: object, name: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{name} must be an integer")
    return value


def _required_str(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    return value


def replay_block_range_evidence(store: EvidenceStore, ref: EvidenceRef) -> BlockRangeDataset:
    """Reconstruct one retained range from typed payload and exact RPC wire bytes."""
    if ref.kind != "canonical_block_range" or ref.schema_version != 2:
        raise ValueError("block range evidence kind or schema is unsupported")
    record = store.get_record(ref)
    if record.request_payload is None:
        raise ValueError("block range request evidence is missing")
    data = strict_json_object(record.payload)
    _exact_fields(
        data,
        {"schema", "log_filter", "chain_id", "start_block", "end_block", "blocks", "logs"},
        "block range payload",
    )
    if data["schema"] != "oasis.block-range.v2":
        raise ValueError("block range payload schema is unsupported")
    chain_id = _required_int(data["chain_id"], "chain_id")
    blocks: list[BlockSnapshot] = []
    for raw in _required_list(data["blocks"], "blocks"):
        item = _required_dict(raw, "block")
        _exact_fields(item, {"number", "hash", "parent_hash", "timestamp"}, "block")
        blocks.append(
            BlockSnapshot(
                BlockRef(
                    chain_id,
                    _required_int(item["number"], "block number"),
                    _required_str(item["hash"], "block hash"),
                    _required_int(item["timestamp"], "block timestamp"),
                ),
                _required_str(item["parent_hash"], "parent hash"),
            )
        )
    logs: list[LogRecord] = []
    for raw in _required_list(data["logs"], "logs"):
        item = _required_dict(raw, "log")
        _exact_fields(
            item,
            {
                "address",
                "topics",
                "data",
                "block_number",
                "block_hash",
                "transaction_hash",
                "transaction_index",
                "log_index",
                "removed",
            },
            "log",
        )
        topics = tuple(
            _required_str(value, "topic") for value in _required_list(item["topics"], "topics")
        )
        transaction_index = item["transaction_index"]
        if transaction_index is not None:
            transaction_index = _required_int(transaction_index, "transaction index")
        if type(item["removed"]) is not bool:
            raise TypeError("removed must be a boolean")
        logs.append(
            LogRecord(
                _required_str(item["address"], "log address"),
                topics,
                _required_str(item["data"], "log data"),
                _required_int(item["block_number"], "log block number"),
                _required_str(item["transaction_hash"], "transaction hash"),
                _required_int(item["log_index"], "log index"),
                _required_str(item["block_hash"], "log block hash"),
                transaction_index,
                item["removed"],
            )
        )
    log_filter = _required_dict(data["log_filter"], "log_filter")
    allowed_filter = {"fromBlock", "toBlock", "address", "topics"}
    if not set(log_filter) <= allowed_filter:
        raise ValueError("block range filter has invalid fields")
    address = log_filter.get("address")
    topics_value = log_filter.get("topics")
    filter_topics = (
        None
        if topics_value is None
        else tuple(
            _required_str(value, "filter topic")
            for value in _required_list(topics_value, "filter topics")
        )
    )
    dataset = BlockRangeDataset(
        ref.source,
        tuple(blocks),
        tuple(logs),
        None if address is None else _required_str(address, "filter address"),
        filter_topics,
    )
    if (
        dataset.chain_id != chain_id
        or dataset.start_block != _required_int(data["start_block"], "start block")
        or dataset.end_block != _required_int(data["end_block"], "end block")
        or dataset.log_filter != log_filter
        or dataset.payload != record.payload
        or dataset.blocks[-1].ref != ref.block
    ):
        raise ValueError("block range interpretation differs from retained payload")
    validate_block_range_exchanges(
        dataset, restore_exchanges(record.request_payload, record.raw_response_payload)
    )
    return dataset


def apply_verified_block_range(
    chain_store: CanonicalChainStore,
    *,
    stream_id: str,
    verified: VerifiedBlockRange,
) -> VerifiedIngestionResult:
    return VerifiedIngestionResult(
        verified,
        chain_store.apply(stream_id, verified.dataset),
    )


__all__ = [
    "IngestionTrust",
    "VerifiedBlockRange",
    "VerifiedIngestionResult",
    "apply_verified_block_range",
    "persist_and_verify_block_range",
    "replay_block_range_evidence",
]
