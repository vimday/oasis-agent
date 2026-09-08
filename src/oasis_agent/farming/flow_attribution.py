from dataclasses import dataclass
from enum import StrEnum

from ..models.common import (
    BlockRef,
    EvidenceLevel,
    EvidenceRef,
    normalize_address,
    normalize_hash32,
)


class FlowClassification(StrEnum):
    EXTERNAL = "external"
    AFFILIATED = "affiliated"
    UNKNOWN = "unknown"


class FlowEvidenceClaim(StrEnum):
    CLASSIFICATION = "classification"
    AMOUNTS = "amounts"
    COSTS = "costs"


_CLASSIFICATION_EVIDENCE_KINDS = frozenset(
    {"flow_attribution", "synthetic_flow_fixture", "trace", "transaction_trace"}
)
_AMOUNT_EVIDENCE_KINDS = frozenset(
    {
        "canonical_block_range",
        "synthetic_flow_fixture",
        "swap_log",
        "receipt",
        "trace",
        "transaction_trace",
    }
)
_COST_EVIDENCE_KINDS = frozenset(
    {
        "synthetic_flow_fixture",
        "receipt",
        "trace",
        "transaction_trace",
        "price_markout",
    }
)


@dataclass(frozen=True, slots=True)
class FlowEvidence:
    """Evidence bound to one pool flow and one explicit claim."""

    reference: EvidenceRef
    transaction_hash: str
    pool_id: str
    log_index: int
    claim: FlowEvidenceClaim
    subject_block: BlockRef | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.reference, EvidenceRef):
            raise TypeError("reference must be an EvidenceRef")
        object.__setattr__(
            self, "transaction_hash", normalize_hash32(self.transaction_hash, "transaction hash")
        )
        object.__setattr__(self, "pool_id", normalize_hash32(self.pool_id, "pool id"))
        if (
            isinstance(self.log_index, bool)
            or not isinstance(self.log_index, int)
            or self.log_index < 0
        ):
            raise ValueError("log_index must be a nonnegative integer")
        if not isinstance(self.claim, FlowEvidenceClaim):
            raise TypeError("claim must be a FlowEvidenceClaim")
        supported_kinds = {
            FlowEvidenceClaim.CLASSIFICATION: _CLASSIFICATION_EVIDENCE_KINDS,
            FlowEvidenceClaim.AMOUNTS: _AMOUNT_EVIDENCE_KINDS,
            FlowEvidenceClaim.COSTS: _COST_EVIDENCE_KINDS,
        }[self.claim]
        if self.reference.kind not in supported_kinds:
            raise ValueError(f"unsupported {self.claim.value} evidence kind")
        if not all(
            (
                self.reference.request_hash,
                self.reference.raw_response_hash,
                self.reference.interpretation_hash,
            )
        ):
            raise ValueError(f"{self.claim.value} evidence requires complete provenance")
        if self.subject_block is None:
            object.__setattr__(self, "subject_block", self.reference.block)
        elif not isinstance(self.subject_block, BlockRef):
            raise TypeError("subject_block must be a BlockRef")
        if (
            self.reference.block is not None
            and self.subject_block is not None
            and self.subject_block.chain_id != self.reference.block.chain_id
        ):
            raise ValueError("evidence subject block belongs to a different chain")


@dataclass(frozen=True, slots=True)
class PoolFlow:
    """One economically attributed swap flow in currency1 integer base units.

    Classification is an evidence-backed input. An address or common router is
    not enough to prove affiliation; callers must provide subject-bound evidence.
    """

    chain_id: int
    pool_id: str
    currency1: str
    currency1_decimals: int
    block: BlockRef
    transaction_hash: str
    log_index: int
    volume1: int
    lp_fee1: int
    gas_cost1: int
    adverse_selection_cost1: int
    self_trade_cost1: int = 0
    classification: FlowClassification = FlowClassification.UNKNOWN
    evidence_level: EvidenceLevel = EvidenceLevel.UNRESOLVED
    evidence: tuple[FlowEvidence, ...] = ()

    def __post_init__(self) -> None:
        if (
            isinstance(self.chain_id, bool)
            or not isinstance(self.chain_id, int)
            or self.chain_id <= 0
        ):
            raise ValueError("chain_id must be a positive integer")
        if not isinstance(self.block, BlockRef):
            raise TypeError("block must be a BlockRef")
        if self.block.chain_id != self.chain_id:
            raise ValueError("flow chain_id must match block")
        object.__setattr__(self, "pool_id", normalize_hash32(self.pool_id, "pool id"))
        object.__setattr__(self, "currency1", normalize_address(self.currency1))
        if (
            isinstance(self.currency1_decimals, bool)
            or not isinstance(self.currency1_decimals, int)
            or not 0 <= self.currency1_decimals <= 255
        ):
            raise ValueError("currency1_decimals must be an integer from 0 through 255")
        object.__setattr__(
            self, "transaction_hash", normalize_hash32(self.transaction_hash, "transaction hash")
        )
        if (
            isinstance(self.log_index, bool)
            or not isinstance(self.log_index, int)
            or self.log_index < 0
        ):
            raise ValueError("log_index must be a nonnegative integer")
        for name, value in (
            ("volume1", self.volume1),
            ("lp_fee1", self.lp_fee1),
            ("gas_cost1", self.gas_cost1),
            ("adverse_selection_cost1", self.adverse_selection_cost1),
            ("self_trade_cost1", self.self_trade_cost1),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        if self.lp_fee1 > self.volume1:
            raise ValueError("lp_fee1 cannot exceed volume1")
        if not isinstance(self.classification, FlowClassification):
            raise TypeError("classification must be a FlowClassification")
        if not isinstance(self.evidence_level, EvidenceLevel):
            raise TypeError("evidence_level must be an EvidenceLevel")

        evidence = tuple(self.evidence)
        if any(not isinstance(item, FlowEvidence) for item in evidence):
            raise TypeError("evidence must contain FlowEvidence values")
        object.__setattr__(self, "evidence", evidence)
        if len(set(evidence)) != len(evidence):
            raise ValueError("duplicate flow evidence")
        for item in evidence:
            if item.transaction_hash != self.transaction_hash:
                raise ValueError("evidence transaction does not match flow")
            if item.pool_id != self.pool_id:
                raise ValueError("evidence pool does not match flow")
            if item.log_index != self.log_index:
                raise ValueError("evidence log index does not match flow")
            if item.subject_block != self.block:
                raise ValueError("flow evidence subject block must match flow block")

        if self.self_trade_cost1 and self.classification is not FlowClassification.AFFILIATED:
            raise ValueError("self_trade_cost1 requires affiliated classification")
        evidence_claims = frozenset(item.claim for item in evidence)
        if (
            self.classification is not FlowClassification.UNKNOWN
            and FlowEvidenceClaim.CLASSIFICATION not in evidence_claims
        ):
            raise ValueError("explicit flow classification requires evidence references")
        if (
            self.classification is FlowClassification.UNKNOWN
            and self.evidence_level is EvidenceLevel.CONFIRMED
        ):
            raise ValueError("unknown flow cannot be confirmed")
        if (
            self.evidence_level is EvidenceLevel.CONFIRMED
            and not self.has_complete_economic_evidence
        ):
            raise ValueError("confirmed flow requires classification, amount, and cost evidence")

    @property
    def identity(self) -> tuple[int, str, str, int]:
        return self.chain_id, self.pool_id, self.transaction_hash, self.log_index

    @property
    def scope(self) -> tuple[int, str, str, int]:
        return (
            self.chain_id,
            self.pool_id,
            self.currency1,
            self.currency1_decimals,
        )

    @property
    def has_complete_economic_evidence(self) -> bool:
        claims = frozenset(item.claim for item in self.evidence)
        required = {
            FlowEvidenceClaim.AMOUNTS,
            FlowEvidenceClaim.COSTS,
        }
        if self.classification is not FlowClassification.UNKNOWN:
            required.add(FlowEvidenceClaim.CLASSIFICATION)
        return required.issubset(claims)


@dataclass(frozen=True, slots=True)
class PoolFlowAnalysis:
    flow_count: int
    volume1: int
    external_volume1: int
    affiliated_volume1: int
    unknown_volume1: int
    gross_lp_fee1: int
    external_lp_fee1: int
    affiliated_lp_fee1: int
    unknown_lp_fee1: int
    self_trade_cost1: int
    gas_cost1: int
    adverse_selection_cost1: int
    gross_fee_rate_bps: int
    external_fee_alpha1: int
    net_lp_pnl1: int
    unresolved_flow_count: int
    confirmed_flow_count: int
    all_flows_economically_evidenced: bool


def analyze_pool_flows(flows: tuple[PoolFlow, ...]) -> PoolFlowAnalysis:
    """Separate gross fee yield from conservative, externally sourced alpha."""

    normalized = tuple(flows)
    if normalized:
        scope = normalized[0].scope
        if any(flow.scope != scope for flow in normalized[1:]):
            raise ValueError("flow scope mismatch")
    identities = tuple(flow.identity for flow in normalized)
    if len(set(identities)) != len(identities):
        raise ValueError("duplicate flow identity")

    volume = external_volume = affiliated_volume = unknown_volume = 0
    gross_fee = external_fee = affiliated_fee = unknown_fee = 0
    self_trade_cost = gas_cost = adverse_selection = 0
    external_gas_cost = external_adverse_selection = confirmed_external_fee = 0
    unresolved = confirmed = 0

    for flow in normalized:
        volume += flow.volume1
        gross_fee += flow.lp_fee1
        gas_cost += flow.gas_cost1
        adverse_selection += flow.adverse_selection_cost1
        if flow.evidence_level is EvidenceLevel.CONFIRMED:
            confirmed += 1
        if flow.classification is FlowClassification.EXTERNAL:
            external_volume += flow.volume1
            external_fee += flow.lp_fee1
            if flow.evidence_level is EvidenceLevel.CONFIRMED:
                confirmed_external_fee += flow.lp_fee1
                external_gas_cost += flow.gas_cost1
                external_adverse_selection += flow.adverse_selection_cost1
            else:
                unresolved += 1
        elif flow.classification is FlowClassification.AFFILIATED:
            affiliated_volume += flow.volume1
            affiliated_fee += flow.lp_fee1
            self_trade_cost += flow.self_trade_cost1
            if flow.evidence_level is not EvidenceLevel.CONFIRMED:
                unresolved += 1
        else:
            unknown_volume += flow.volume1
            unknown_fee += flow.lp_fee1
            unresolved += 1

    gross_fee_rate_bps = gross_fee * 10_000 // volume if volume else 0
    external_alpha = confirmed_external_fee - external_gas_cost - external_adverse_selection
    # Affiliated and unknown fees are excluded at the consolidated beneficial-owner boundary.
    # They cannot establish outside demand and may only be internal transfers or unresolved flow.
    net_pnl = confirmed_external_fee - self_trade_cost - adverse_selection - gas_cost
    return PoolFlowAnalysis(
        len(normalized),
        volume,
        external_volume,
        affiliated_volume,
        unknown_volume,
        gross_fee,
        external_fee,
        affiliated_fee,
        unknown_fee,
        self_trade_cost,
        gas_cost,
        adverse_selection,
        gross_fee_rate_bps,
        external_alpha,
        net_pnl,
        unresolved,
        confirmed,
        bool(normalized)
        and all(
            flow.evidence_level is EvidenceLevel.CONFIRMED and flow.has_complete_economic_evidence
            for flow in normalized
        ),
    )


__all__ = [
    "FlowClassification",
    "FlowEvidence",
    "FlowEvidenceClaim",
    "PoolFlow",
    "PoolFlowAnalysis",
    "analyze_pool_flows",
]
