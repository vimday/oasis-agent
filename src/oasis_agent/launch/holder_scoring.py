"""Replay retained holder RPC evidence before deriving candidate holder fields.

Supply coverage is conditional on standard truthful ERC-20 semantics. It proves neither
beneficial ownership nor contract safety. Every result remains a research proposal.
"""

from dataclasses import asdict
from typing import Annotated, Literal

from pydantic import Field

from ..evidence.store import EvidenceStore
from ..models.common import EvidenceRef, canonical_json, sha256_hex
from .holder_capture import HolderQuorum, replay_holder_evidence
from .models import Address, Digest, Hash, Positive, StrictModel
from .scoring import Candidate, ScorePolicy, ScoreResult, score_candidate


class HolderIdentity(StrictModel):
    chain_id: Positive
    token: Address
    runtime_hash: Hash
    token_decimals: Annotated[int, Field(ge=0, le=255)]


class HolderScore(StrictModel):
    schema_version: Literal["oasis.holder-bound-score.v1"] = "oasis.holder-bound-score.v1"
    original_candidate_sha256: Digest
    holder_binding_sha256: Digest
    holder_evidence_level: Literal["two_source_raw_replay_standard_erc20_conditional"] = (
        "two_source_raw_replay_standard_erc20_conditional"
    )
    candidate: Candidate
    score: ScoreResult
    execution_eligible: Literal[False] = False


def score_with_holders(
    candidate: Candidate,
    policy: ScorePolicy,
    *,
    store: EvidenceStore,
    references: tuple[EvidenceRef, EvidenceRef],
    identity: HolderIdentity,
) -> HolderScore:
    """Reject identity/time/replay failures; never inherit imported holder pass flags."""
    if len(references) != 2:
        raise ValueError("two holder references required")
    try:
        quorum = HolderQuorum(*(replay_holder_evidence(store, ref) for ref in references))
    except KeyError:
        raise ValueError("holder evidence reference unavailable") from None
    c = candidate.chain
    if (identity.chain_id, identity.token) != (candidate.chain_id, candidate.token):
        raise ValueError("holder identity differs from candidate")
    for capture in (quorum.left, quorum.right):
        sample = capture.observation
        block = sample.snapshot
        if (block.chain_id, sample.target_ca, sample.runtime_hash, sample.token_decimals) != (
            identity.chain_id,
            identity.token,
            identity.runtime_hash,
            identity.token_decimals,
        ) or (
            block.chain_id,
            sample.target_ca,
            block.number,
            block.block_hash,
            block.timestamp,
        ) != (c.chain_id, c.token, c.block_number, c.block_hash, c.observed_at):
            raise ValueError("holder evidence identity or snapshot differs from candidate")
        if not (
            block.timestamp <= sample.retrieved_at <= candidate.evaluated_at
            and 0 <= candidate.evaluated_at - block.timestamp <= policy.max_age_seconds
        ):
            raise ValueError("holder evidence stale or acquired after decision")
    sample = quorum.left.observation
    balances = sorted((item.balance_base_units for item in sample.balances), reverse=True)
    if not balances:
        raise ValueError("no positive holder balances")
    supply = sample.total_supply_base_units
    binding_hash = sha256_hex(
        canonical_json(
            {
                "identity": identity.model_dump(mode="json"),
                "references": [
                    asdict(ref) for ref in sorted(references, key=lambda ref: ref.source)
                ],
            }
        )
    )
    # Preserve non-holder provenance: this adapter only replaces holder-derived fields.
    data = candidate.model_dump(mode="json")
    data["chain"].update(
        holder_coverage="pass" if sample.complete else "fail",
        holder_count=len(balances),
        holder_top10_bps=(sum(balances[:10]) * 10000 + supply - 1) // supply,
        # Raw balanceOf does not establish common beneficial ownership.
        common_control_analysis="unknown",
        common_control_bps=10000,
    )
    bound = Candidate.model_validate(data)
    return HolderScore(
        original_candidate_sha256=sha256_hex(canonical_json(candidate.model_dump(mode="json"))),
        holder_binding_sha256=binding_hash,
        candidate=bound,
        score=score_candidate(bound, policy),
    )
