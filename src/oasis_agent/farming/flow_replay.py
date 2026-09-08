from dataclasses import dataclass

from ..evidence.ingestion import IngestionTrust, VerifiedBlockRange
from ..models.common import (
    BlockRef,
    EvidenceLevel,
    normalize_address,
    normalize_hash32,
)
from ..models.v4 import PoolKey
from ..uniswap_v4.dataset_replay import DecodedPoolDataset, replay_pool_dataset
from ..uniswap_v4.replay import PoolAccountingReplay
from .flow_attribution import (
    FlowClassification,
    FlowEvidence,
    FlowEvidenceClaim,
    PoolFlow,
)


@dataclass(frozen=True, slots=True)
class ReplayPoolFlowObservation:
    """Evidence-backed facts about one swap, before economic enrichment."""

    chain_id: int
    pool_id: str
    currency0: str
    currency0_decimals: int
    currency1: str
    currency1_decimals: int
    block: BlockRef
    transaction_hash: str
    transaction_index: int
    log_index: int
    amount0: int
    amount1: int
    volume1: int
    effective_fee: int
    amount_evidence: FlowEvidence
    trust: IngestionTrust

    def __post_init__(self) -> None:
        if (
            isinstance(self.chain_id, bool)
            or not isinstance(self.chain_id, int)
            or self.chain_id <= 0
        ):
            raise ValueError("observation chain_id must be a positive integer")
        if not isinstance(self.block, BlockRef):
            raise TypeError("observation block must be a BlockRef")
        if self.block.chain_id != self.chain_id:
            raise ValueError("observation chain_id must match block")
        object.__setattr__(self, "pool_id", normalize_hash32(self.pool_id, "pool id"))
        object.__setattr__(self, "currency0", normalize_address(self.currency0))
        object.__setattr__(self, "currency1", normalize_address(self.currency1))
        if self.currency0 >= self.currency1:
            raise ValueError("observation currencies must be in canonical order")
        for name, value in (
            ("currency0_decimals", self.currency0_decimals),
            ("currency1_decimals", self.currency1_decimals),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 255:
                raise ValueError(f"{name} must be an integer from 0 through 255")
        object.__setattr__(
            self, "transaction_hash", normalize_hash32(self.transaction_hash, "transaction hash")
        )
        for name, value in (
            ("transaction_index", self.transaction_index),
            ("log_index", self.log_index),
            ("amount0", self.amount0),
            ("amount1", self.amount1),
            ("volume1", self.volume1),
            ("effective_fee", self.effective_fee),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
        if self.transaction_index < 0 or self.log_index < 0:
            raise ValueError("observation event positions must be nonnegative")
        if self.volume1 != abs(self.amount1):
            raise ValueError("volume1 must equal absolute currency1 amount")
        if self.volume1 < 0 or not 0 <= self.effective_fee <= 1_000_000:
            raise ValueError("observation amount or fee is outside bounds")
        if not isinstance(self.amount_evidence, FlowEvidence):
            raise TypeError("amount_evidence must be FlowEvidence")
        if self.amount_evidence.claim is not FlowEvidenceClaim.AMOUNTS:
            raise ValueError("amount_evidence must support the amounts claim")
        if self.amount_evidence.transaction_hash != self.transaction_hash:
            raise ValueError("amount evidence transaction does not match observation")
        if self.amount_evidence.pool_id != self.pool_id:
            raise ValueError("amount evidence pool does not match observation")
        if self.amount_evidence.log_index != self.log_index:
            raise ValueError("amount evidence log index does not match observation")
        if self.amount_evidence.subject_block != self.block:
            raise ValueError("amount evidence subject block does not match observation")
        if not isinstance(self.trust, IngestionTrust):
            raise TypeError("trust must be an IngestionTrust")

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


@dataclass(frozen=True, slots=True)
class PoolFlowEconomicAttribution:
    """Explicit, evidence-backed economic facts that replay cannot establish."""

    chain_id: int
    pool_id: str
    currency1: str
    currency1_decimals: int
    block: BlockRef
    transaction_hash: str
    log_index: int
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
            raise ValueError("attribution chain_id must be a positive integer")
        if not isinstance(self.block, BlockRef):
            raise TypeError("attribution block must be a BlockRef")
        if self.block.chain_id != self.chain_id:
            raise ValueError("attribution chain_id must match block")
        object.__setattr__(self, "pool_id", normalize_hash32(self.pool_id, "pool id"))
        object.__setattr__(self, "currency1", normalize_address(self.currency1))
        if (
            isinstance(self.currency1_decimals, bool)
            or not isinstance(self.currency1_decimals, int)
            or not 0 <= self.currency1_decimals <= 255
        ):
            raise ValueError("attribution currency1_decimals must be an integer from 0 through 255")
        object.__setattr__(
            self, "transaction_hash", normalize_hash32(self.transaction_hash, "transaction hash")
        )
        if (
            isinstance(self.log_index, bool)
            or not isinstance(self.log_index, int)
            or self.log_index < 0
        ):
            raise ValueError("attribution log_index must be a nonnegative integer")
        for name, value in (
            ("lp_fee1", self.lp_fee1),
            ("gas_cost1", self.gas_cost1),
            ("adverse_selection_cost1", self.adverse_selection_cost1),
            ("self_trade_cost1", self.self_trade_cost1),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        if not isinstance(self.classification, FlowClassification):
            raise TypeError("classification must be a FlowClassification")
        if not isinstance(self.evidence_level, EvidenceLevel):
            raise TypeError("evidence_level must be an EvidenceLevel")
        evidence = tuple(self.evidence)
        if any(not isinstance(item, FlowEvidence) for item in evidence):
            raise TypeError("evidence must contain FlowEvidence values")
        if len(set(evidence)) != len(evidence):
            raise ValueError("duplicate attribution evidence")
        object.__setattr__(self, "evidence", evidence)
        for item in evidence:
            if item.transaction_hash != self.transaction_hash:
                raise ValueError("attribution evidence transaction does not match")
            if item.pool_id != self.pool_id:
                raise ValueError("attribution evidence pool does not match")
            if item.log_index != self.log_index:
                raise ValueError("attribution evidence log index does not match")
            if item.subject_block != self.block:
                raise ValueError("attribution evidence subject block does not match")

    @property
    def identity(self) -> tuple[int, str, str, int]:
        return self.chain_id, self.pool_id, self.transaction_hash, self.log_index

    @property
    def scope(self) -> tuple[int, str, str, int]:
        return self.chain_id, self.pool_id, self.currency1, self.currency1_decimals


def _range_blocks(verified: VerifiedBlockRange) -> dict[int, BlockRef]:
    return {item.ref.number: item.ref for item in verified.dataset.blocks}


def _validate_decoded_replay(
    verified: VerifiedBlockRange,
    decoded: DecodedPoolDataset,
    replay: PoolAccountingReplay,
) -> PoolKey:
    if decoded.dataset_hash != verified.dataset.content_hash:
        raise ValueError("decoded pool inputs do not match verified dataset")
    if decoded.initialization.observation.pool_id != decoded.pool_id:
        raise ValueError("decoded pool inputs do not match initialization")
    if (
        replay.initial_state.pool_id != decoded.pool_id
        or replay.final_state.pool_id != decoded.pool_id
    ):
        raise ValueError("replay pool does not match decoded pool")
    if replay.processed_blocks != len(verified.dataset.blocks):
        raise ValueError("replay processed block count does not match verified dataset")
    key = decoded.initialization.observation.key
    if decoded.initialization.block != verified.dataset.blocks[0].ref:
        raise ValueError("decoded initialization does not match verified range start")
    if decoded.initialization.observation.pool_id != decoded.pool_id:
        raise ValueError("decoded initialization pool does not match dataset pool")
    blocks = _range_blocks(verified)
    if any(
        step.block.number not in blocks or blocks[step.block.number] != step.block
        for step in replay.steps
    ):
        raise ValueError("replay swap step is outside verified block lineage")
    expected = tuple(
        (event.block.number, event.transaction_index, event.log_index, event.transaction_hash)
        for event in decoded.swaps
    )
    actual = tuple(
        (step.block.number, step.transaction_index, step.log_index, step.transaction_hash)
        for step in replay.steps
    )
    if actual != expected:
        raise ValueError("replay swap steps do not match decoded swaps")
    # Recompute from retained logs: matching event IDs alone does not bind amounts,
    # fees, liquidity transitions, or the opening/closing state to the evidence.
    if replay != replay_pool_dataset(decoded, verified.dataset):
        raise ValueError("replay values do not match the retained dataset replay")
    return key


def derive_replay_flow_observations(
    verified: VerifiedBlockRange,
    decoded: DecodedPoolDataset,
    replay: PoolAccountingReplay,
    *,
    currency0_decimals: int,
    currency1_decimals: int,
) -> tuple[ReplayPoolFlowObservation, ...]:
    """Convert a verified PoolManager replay into amount-only flow observations."""

    key = _validate_decoded_replay(verified, decoded, replay)
    if (
        isinstance(currency0_decimals, bool)
        or not isinstance(currency0_decimals, int)
        or not 0 <= currency0_decimals <= 255
    ):
        raise ValueError("currency0_decimals must be an integer from 0 through 255")
    if (
        isinstance(currency1_decimals, bool)
        or not isinstance(currency1_decimals, int)
        or not 0 <= currency1_decimals <= 255
    ):
        raise ValueError("currency1_decimals must be an integer from 0 through 255")
    observations: list[ReplayPoolFlowObservation] = []
    seen: set[tuple[int, str, str, int]] = set()
    for step in replay.steps:
        identity = (step.block.chain_id, decoded.pool_id, step.transaction_hash, step.log_index)
        if identity in seen:
            raise ValueError("duplicate replay swap identity")
        seen.add(identity)
        evidence = FlowEvidence(
            verified.evidence,
            step.transaction_hash,
            decoded.pool_id,
            step.log_index,
            FlowEvidenceClaim.AMOUNTS,
            subject_block=step.block,
        )
        observations.append(
            ReplayPoolFlowObservation(
                step.block.chain_id,
                decoded.pool_id,
                key.currency0,
                currency0_decimals,
                key.currency1,
                currency1_decimals,
                step.block,
                step.transaction_hash,
                step.transaction_index,
                step.log_index,
                step.amount0,
                step.amount1,
                abs(step.amount1),
                step.fee,
                evidence,
                verified.trust,
            )
        )
    return tuple(observations)


def _validate_attribution(
    observation: ReplayPoolFlowObservation, attribution: PoolFlowEconomicAttribution
) -> None:
    if attribution.identity != observation.identity:
        raise ValueError("attribution identity does not match replay observation")
    if attribution.scope != observation.scope:
        raise ValueError("attribution scope does not match replay observation")
    if attribution.block != observation.block:
        raise ValueError("attribution block does not match replay observation")


def build_pool_flow_from_replay(
    observation: ReplayPoolFlowObservation,
    attribution: PoolFlowEconomicAttribution,
) -> PoolFlow:
    """Combine verified replay amounts with explicit economic attribution."""

    _validate_attribution(observation, attribution)
    if attribution.evidence_level is EvidenceLevel.CONFIRMED:
        if observation.trust is not IngestionTrust.PROVIDER_QUORUM:
            raise ValueError("confirmed flow requires provider quorum for replay amounts")
        # A Swap event attests deltas and effective fee, not LP fee ownership or
        # token0-fee valuation in currency1. That needs separate settlement evidence.
        if not any(
            item.claim is FlowEvidenceClaim.AMOUNTS
            and item.reference.kind in {"receipt", "trace", "transaction_trace"}
            for item in attribution.evidence
        ):
            raise ValueError("confirmed flow requires separate LP fee amount evidence")
    evidence = (observation.amount_evidence, *attribution.evidence)
    return PoolFlow(
        observation.chain_id,
        observation.pool_id,
        observation.currency1,
        observation.currency1_decimals,
        observation.block,
        observation.transaction_hash,
        observation.log_index,
        observation.volume1,
        attribution.lp_fee1,
        attribution.gas_cost1,
        attribution.adverse_selection_cost1,
        attribution.self_trade_cost1,
        attribution.classification,
        attribution.evidence_level,
        evidence,
    )


def build_pool_flows_from_replay(
    observations: tuple[ReplayPoolFlowObservation, ...],
    attributions: tuple[PoolFlowEconomicAttribution, ...],
) -> tuple[PoolFlow, ...]:
    """Join replay observations and economic enrichment one-to-one in replay order."""

    observation_ids = tuple(item.identity for item in observations)
    if len(set(observation_ids)) != len(observation_ids):
        raise ValueError("duplicate replay observation identity")
    attribution_ids = tuple(item.identity for item in attributions)
    if len(set(attribution_ids)) != len(attribution_ids):
        raise ValueError("duplicate attribution identity")
    if set(observation_ids) != set(attribution_ids):
        raise ValueError("replay observations and attributions do not match")
    by_id = {item.identity: item for item in attributions}
    return tuple(build_pool_flow_from_replay(item, by_id[item.identity]) for item in observations)


__all__ = [
    "PoolFlowEconomicAttribution",
    "ReplayPoolFlowObservation",
    "build_pool_flow_from_replay",
    "build_pool_flows_from_replay",
    "derive_replay_flow_observations",
]
