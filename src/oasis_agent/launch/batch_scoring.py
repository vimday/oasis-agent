"""Bind dynamic discovery to enriched multi-token scoring, without orders or invented data."""

from typing import Annotated, Literal

from pydantic import Field

from ..models.common import canonical_json, sha256_hex
from .discovery import DiscoveryItem, DiscoveryRequest, discover
from .models import Digest, StrictModel, UInt
from .scoring import Candidate, ScorePolicy, ScoreResult, score_candidate


class BatchRequest(StrictModel):
    discovery: DiscoveryRequest
    policy: ScorePolicy
    candidates: Annotated[tuple[Candidate, ...], Field(max_length=500)] = ()


class BatchItem(StrictModel):
    discovery: DiscoveryItem
    score: ScoreResult | None
    status: Literal["needs_enrichment", "discovery_blocked", "scored"]
    execution_eligible: Literal[False] = False


class BatchResult(StrictModel):
    schema_version: Literal["oasis.batch-score.v1"] = "oasis.batch-score.v1"
    request_sha256: Digest
    items: tuple[BatchItem, ...]
    held_position_scores: tuple[ScoreResult, ...]
    evaluated_at: UInt
    omitted_stale_or_future: UInt
    omitted_chain: UInt
    deferred_capacity: UInt
    execution_eligible: Literal[False] = False


def score_batch(request: BatchRequest) -> BatchResult:
    """A missing enrichment produces no score. Rank bindings cannot be swapped between tokens."""
    discovery = discover(request.discovery)
    candidates = {}
    held = []
    for candidate in request.candidates:
        key = candidate.chain_id, candidate.token
        if key in candidates:
            raise ValueError("duplicate enriched candidate")
        if candidate.evaluated_at != request.discovery.evaluated_at:
            raise ValueError("candidate evaluation must match discovery cycle")
        candidates[key] = candidate
        if candidate.position is not None:
            if candidate.chain_id not in request.discovery.policy.chains:
                raise ValueError("held position chain not configured")
            held.append(score_candidate(candidate, request.policy))
    discovered = {(item.chain_id, item.token) for item in discovery.items}
    unheld = {key for key, value in candidates.items() if value.position is None}
    if unheld - discovered:
        raise ValueError("enrichment outside selected discovery universe")
    items = []
    for item in discovery.items:
        enrichment = candidates.get((item.chain_id, item.token))
        score = None
        status: Literal["needs_enrichment", "discovery_blocked", "scored"] = "needs_enrichment"
        # Missing birth/ranking evidence prevents entry research promotion here, not just trading.
        if enrichment is not None and enrichment.position is not None:
            score = score_candidate(enrichment, request.policy)
            status = "scored"
        elif len(item.blockers) > 1:
            status = "discovery_blocked"
        elif enrichment is not None:
            for trend in enrichment.trends:
                matched = [
                    s
                    for s in request.discovery.signals
                    if (
                        s.provider == trend.provider
                        and s.chain_id == trend.chain_id
                        and s.token == trend.token
                        and s.kind == "trending"
                        and s.observed_at == trend.observed_at
                        and s.retrieved_at == trend.retrieved_at
                        and s.raw_sha256 == trend.raw_sha256
                        and s.rank == trend.rank
                        and s.window_seconds == trend.window_seconds
                    )
                ]
                newer = any(
                    s.provider == trend.provider
                    and s.chain_id == trend.chain_id
                    and s.token == trend.token
                    and s.kind == "trending"
                    and trend.observed_at < s.observed_at <= request.discovery.evaluated_at
                    for s in request.discovery.signals
                )
                if not matched or newer:
                    raise ValueError("candidate ranking does not bind latest discovery evidence")
            score = score_candidate(enrichment, request.policy)
            status = "scored"
        items.append(BatchItem(discovery=item, score=score, status=status))
    return BatchResult(
        request_sha256=sha256_hex(canonical_json(request.model_dump(mode="json"))),
        items=tuple(items),
        held_position_scores=tuple(sorted(held, key=lambda item: (item.chain_id, item.token))),
        evaluated_at=request.discovery.evaluated_at,
        omitted_stale_or_future=discovery.omitted_stale_or_future,
        omitted_chain=discovery.omitted_chain,
        deferred_capacity=discovery.deferred_capacity,
    )
