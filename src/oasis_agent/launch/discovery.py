"""Bounded multi-token discovery, never a trading qualification or fixed-token list."""

from collections import defaultdict
from typing import Annotated, Literal

from pydantic import Field, model_validator

from ..models.common import canonical_json, sha256_hex
from .models import Address, Digest, Positive, StrictModel, UInt

Provider = Literal["gmgn", "okx", "fomo", "dexscreener"]


class DiscoverySignal(StrictModel):
    provider: Provider
    chain_id: Positive
    token: Address
    kind: Literal["trending", "new_pair", "social", "profile"]
    observed_at: UInt
    retrieved_at: UInt
    raw_sha256: Digest
    rank: Annotated[int, Field(ge=1, le=100)] | None = None
    created_at: UInt | None = None
    window_seconds: Positive | None = None

    @model_validator(mode="after")
    def coherent(self) -> DiscoverySignal:
        if int(self.token, 16) == 0 or self.observed_at > self.retrieved_at:
            raise ValueError("nonzero token and coherent capture times required")
        if self.created_at is not None and self.created_at > self.observed_at:
            raise ValueError("creation cannot follow observation")
        if self.kind == "trending" and self.window_seconds is None:
            raise ValueError("trending window required")
        return self


class DiscoveryPolicy(StrictModel):
    chains: Annotated[tuple[Positive, ...], Field(min_length=1, max_length=16)]
    trend_window_seconds: Positive = 300
    max_age_seconds: Annotated[int, Field(ge=1, le=3600)] = 120
    min_pair_age_seconds: UInt = 120
    new_pair_age_seconds: Positive = 86400
    max_candidates: Annotated[int, Field(ge=1, le=500)] = 100

    @model_validator(mode="after")
    def unique_chains(self) -> DiscoveryPolicy:
        if len(set(self.chains)) != len(self.chains):
            raise ValueError("duplicate discovery chain")
        if self.min_pair_age_seconds > self.new_pair_age_seconds:
            raise ValueError("minimum age exceeds new-pair window")
        return self


class DiscoveryRequest(StrictModel):
    evaluated_at: UInt
    policy: DiscoveryPolicy
    signals: Annotated[tuple[DiscoverySignal, ...], Field(max_length=2000)]


class DiscoveryItem(StrictModel):
    chain_id: Positive
    token: Address
    sources: tuple[Provider, ...]
    evidence_hashes: tuple[Digest, ...]
    attention_bps: Annotated[int, Field(ge=0, le=10000)]
    age_class: Literal["unknown", "immature", "new", "established", "conflicting"]
    blockers: tuple[str, ...]
    next_action: Literal["collect_evidence", "wait_age", "review_identity"]
    execution_eligible: Literal[False] = False


class DiscoveryResult(StrictModel):
    schema_version: Literal["oasis.discovery.v1"] = "oasis.discovery.v1"
    request_sha256: Digest
    items: tuple[DiscoveryItem, ...]
    omitted_stale_or_future: UInt
    omitted_chain: UInt
    deferred_capacity: UInt
    execution_eligible: Literal[False] = False


def discover(request: DiscoveryRequest) -> DiscoveryResult:
    """Deduplicate by chain/address; social/profile counts never boost tradability.

    Ranking is enrichment priority only. Repeated source signals cannot multiply influence.
    Retained request hash binds excluded rows too, preventing silent survivor-only reports.
    """
    groups: dict[tuple[int, str], list[DiscoverySignal]] = defaultdict(list)
    stale = offchain = 0
    now, policy = request.evaluated_at, request.policy
    for signal in request.signals:
        if signal.chain_id not in policy.chains:
            offchain += 1
        elif not (
            signal.observed_at <= signal.retrieved_at <= now
            and 0 <= now - signal.observed_at <= policy.max_age_seconds
        ):
            stale += 1
        else:
            groups[signal.chain_id, signal.token].append(signal)
    items = []
    for (chain, token), signals in groups.items():
        sources = tuple(sorted({s.provider for s in signals}))
        # Ambiguous same-time ranks cannot be resolved by choosing the most flattering value.
        latest: dict[Provider, DiscoverySignal] = {}
        conflicts = set()
        for source in sources:
            rows = [s for s in signals if s.provider == source and s.kind == "trending"]
            if not rows:
                continue
            timestamp = max(s.observed_at for s in rows)
            rows = [s for s in rows if s.observed_at == timestamp]
            if len({(s.rank, s.window_seconds) for s in rows}) > 1:
                conflicts.add(source)
            else:
                latest[source] = rows[0]
        ranks = [
            (101 - rank) * 100
            for p in ("gmgn", "okx")
            if p in latest
            and latest[p].window_seconds == policy.trend_window_seconds
            and (rank := latest[p].rank) is not None
        ]
        attention = min(ranks) if len(ranks) == 2 else ranks[0] // 2 if ranks else 0
        created = {s.created_at for s in signals if s.created_at is not None}
        age: Literal["unknown", "immature", "new", "established", "conflicting"] = "unknown"
        if len(created) > 1:
            age = "conflicting"
        elif created:
            elapsed = now - next(iter(created))
            age = (
                "immature"
                if elapsed < policy.min_pair_age_seconds
                else "new"
                if elapsed <= policy.new_pair_age_seconds
                else "established"
            )
        blockers = ["chain_security_and_executable_market_evidence_required"]
        if not {"gmgn", "okx"}.issubset(latest) or len(ranks) != 2:
            blockers.append("gmgn_okx_rank_confluence_missing")
        if conflicts:
            blockers.append("conflicting_source_ranks")
        if age in ("unknown", "conflicting", "immature"):
            blockers.append("pair_age_" + age)
        items.append(
            DiscoveryItem(
                chain_id=chain,
                token=token,
                sources=sources,
                evidence_hashes=tuple(sorted({s.raw_sha256 for s in signals})),
                attention_bps=attention,
                age_class=age,
                blockers=tuple(sorted(blockers)),
                next_action="review_identity"
                if conflicts or age == "conflicting"
                else "wait_age"
                if age == "immature"
                else "collect_evidence",
            )
        )
    items.sort(key=lambda item: (-item.attention_bps, item.chain_id, item.token))
    return DiscoveryResult(
        request_sha256=sha256_hex(canonical_json(request.model_dump(mode="json"))),
        items=tuple(items[: policy.max_candidates]),
        omitted_stale_or_future=stale,
        omitted_chain=offchain,
        deferred_capacity=max(0, len(items) - policy.max_candidates),
    )
