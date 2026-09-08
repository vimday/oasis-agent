from dataclasses import dataclass
from enum import StrEnum

from ..models.common import EvidenceRef
from ..rpc.collector import BlockRangeDataset, ProviderAgreement, require_provider_agreement
from ..rpc.transport import RpcExchange
from .chain_store import CanonicalChainStore, ChainApplyResult
from .range_validation import validate_block_range_exchanges
from .rpc_capture import persist_block_range_evidence
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
]
