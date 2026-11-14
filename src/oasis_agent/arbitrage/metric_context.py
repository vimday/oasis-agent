"""Bind builder declarations to retained parent/target header observations, never finality."""

from typing import Annotated, Any, Literal, Self

from pydantic import Field, model_validator

from ..evidence.public_rpc_plan import CHAIN_SOURCES, ChainSource
from ..models.common import canonical_json, sha256_hex
from .cycle_evidence import header
from .cycle_plan import Frozen
from .metric_builder import Digest


class ContextPlan(Frozen):
    schema_version: Literal["oasis-metric-builder-context-plan-v1"] = Field(alias="schema")
    chain_id: Annotated[int, Field(ge=1, le=1)]
    target_block: Annotated[int, Field(gt=0, lt=1 << 64)]
    target_timestamp: Annotated[int, Field(gt=0, lt=1 << 64)]
    sources: Annotated[tuple[ChainSource, ...], Field(min_length=1, max_length=2)]
    titan_capture_sha256: Digest
    titan_analysis_plan_sha256: Digest

    @model_validator(mode="after")
    def unique(self) -> Self:
        if len(set(self.sources)) != len(self.sources):
            raise ValueError("duplicate_context_source")
        if any(CHAIN_SOURCES[s][0] != self.chain_id for s in self.sources):
            raise ValueError("context_source_chain_mismatch")
        return self


def correlate(
    plan: ContextPlan, titan: dict[str, Any], observations: list[dict[str, Any]]
) -> dict[str, Any]:
    """Inputs come from the normal raw-byte replay entry point, not arbitrary parsed exports."""
    if (
        titan["capture_sha256"] != plan.titan_capture_sha256
        or titan["plan_sha256"] != plan.titan_analysis_plan_sha256
    ):
        raise ValueError("builder_capture_binding_mismatch")
    reasons = []
    if titan["status"] != "observed_declarations":
        reasons.append("builder_declarations_incomplete_or_rejected")
    selected = [r for s in titan["snapshots"] for r in s["selected"]]
    if not selected or any(
        r["target_block"] != plan.target_block
        or r["target_timestamp_seconds"] != plan.target_timestamp
        for r in selected
    ):
        reasons.append("builder_target_context_mismatch")
    expected = {(s, n) for s in plan.sources for n in (plan.target_block - 1, plan.target_block)}
    seen = set()
    headers: dict[tuple[str, int], dict[str, Any]] = {}
    for observation in observations:
        key = (observation["source"], observation["block_number"])
        if key not in expected or key in seen:
            raise ValueError("unexpected_or_duplicate_header_capture")
        seen.add(key)
        if observation["status"] != "complete":
            continue
        parsed = header(observation["raw_header"], plan.chain_id, key[1])
        headers[key] = {
            "number": parsed.ref.number,
            "hash": parsed.ref.block_hash,
            "parent_hash": parsed.parent_hash,
            "timestamp": parsed.ref.timestamp,
        }
    missing = sorted(expected - headers.keys())
    if missing:
        reasons.append("missing_required_header_observations")
    conflict = False
    for n in (plan.target_block - 1, plan.target_block):
        values = {canonical_json(v) for (s, number), v in headers.items() if number == n}
        if len(values) > 1:
            conflict = True
            reasons.append("source_header_disagreement")
    contexts = []
    complete = []
    for source in plan.sources:
        parent = headers.get((source, plan.target_block - 1))
        target = headers.get((source, plan.target_block))
        valid = bool(
            parent
            and target
            and target["parent_hash"] == parent["hash"]
            and target["timestamp"] == plan.target_timestamp
            and target["timestamp"] > parent["timestamp"]
        )
        if parent and target and not valid:
            conflict = True
            reasons.append("parent_target_link_or_timestamp_mismatch")
        contexts.append(
            {"source": source, "parent": parent, "target": target, "link_matches": valid}
        )
        if valid:
            complete.append({"chain_id": plan.chain_id, "parent": parent, "target": target})
    if not complete:
        reasons.append("no_complete_parent_target_observation")
    # Clear a prior-looking context on any contradiction, while retaining per-source evidence.
    candidate = (
        complete[0]
        if complete and not conflict and not any(r.startswith("builder_") for r in reasons)
        else None
    )
    return {
        "schema": "oasis-metric-builder-context-report-v1",
        "plan_sha256": sha256_hex(canonical_json(plan.model_dump(by_alias=True, mode="json"))),
        "status": "incomplete_or_rejected" if reasons else "header_observations_agree",
        "reasons": sorted(set(reasons)),
        "observed_context": candidate,
        "contexts": contexts,
        "missing": [{"source": s, "block_number": n} for s, n in missing],
        "captures": [{k: v for k, v in r.items() if k != "raw_header"} for r in observations],
        "titan_capture_sha256": titan["capture_sha256"],
        "independent_providers_verified": False,
        "canonical_finality_verified": False,
        "builder_overlay_landed_verified": False,
        "target_code_and_configuration_verified": False,
        "execution_eligible": False,
        "net_profit": None,
    }
