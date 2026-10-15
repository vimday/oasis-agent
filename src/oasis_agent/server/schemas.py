"""Pydantic response models for the stable observation API envelopes.

Envelopes that are derived from the configured services (health, status,
chains, alerts, meta) are modeled here so the OpenAPI document describes them
and responses are validated on the way out. Endpoints that serve canonical
hash-linked journal records on purpose have no response model: their shape is
defined and validated by the read-only data layer, and a model here could
silently drop fields from an evidence record. Keeping the two kinds distinct
is the extensibility contract for adding new endpoints: model derived
envelopes, pass canonical records through untouched.
"""

from typing import Any

from pydantic import BaseModel, Field

# Envelope schema identifiers served by this API. The canonical journal
# schemas are defined in ``databases.py``; this tuple is the discoverable
# registry exposed through the ``/api/v1/meta`` endpoint.
ENVELOPE_SCHEMAS: tuple[str, ...] = (
    "oasis.live-status.v1",
    "oasis.server-meta.v1",
    "oasis.server-reader-metrics.v1",
    "oasis.server.execution-events.v1",
    "oasis.server-health.v1",
    "oasis.server-status.v1",
    "oasis.server-observations.v1",
    "oasis.server-decisions.v1",
    "oasis.server-decision-detail.v1",
    "oasis.server-evidence.v1",
    "oasis.server-evidence-detail.v1",
    "oasis.lp-release-assessment.v1",
    "oasis.server-execution-journal.v1",
    "oasis.server-run.v1",
    "oasis.server-runs.v1",
    "oasis.server-run-detail.v1",
    "oasis.server-ops.v1",
    "oasis.server-halt-proofs.v1",
    "oasis.server-gate-approvals.v1",
    "oasis.server-gate-approval.v1",
    "oasis.server-chains.v1",
    "oasis.server-chain-status.v1",
    "oasis.server-chain-pool.v1",
    "oasis.server-alerts.v1",
)

# Canonical journal schemas re-validated on every read; listed so consumers
# can discover what the hash chains may contain.
DATA_SCHEMAS: tuple[str, ...] = (
    "oasis.shadow-health.v1",
    "oasis.shadow-lp-decision.v1",
    "oasis.lp-execution-journal.v1",
    "oasis.lp-execution-journal.v2",
    "oasis.lp-execution-journal.v3",
)


class Envelope(BaseModel):
    # BaseModel.schema is a deprecated class method; keep the wire field while
    # using a collision-free Python attribute.
    schema_: str = Field(alias="schema")


class HealthSource(BaseModel):
    available: bool
    integrity: str


class HealthResponse(Envelope):
    ok: bool
    sources: dict[str, HealthSource]


class ReadinessPayload(Envelope):
    assessment_scope: str = "declared_gate_structure_only"
    as_of: str
    stage: str
    execution_eligible: bool
    blockers: list[str]
    digest: str


class StatusResponse(Envelope):
    stage: str | None
    execution_eligible: bool | None
    readiness: ReadinessPayload | None
    latest_decision: dict[str, Any] | None
    latest_health: dict[str, Any] | None
    halt_latched: bool
    decision_count: int
    health_count: int


class ChainItem(BaseModel):
    chain_id: int
    name: str
    rpc_configured: bool
    rpc_origin: str
    pool_manager: str


class ChainsResponse(Envelope):
    items: list[ChainItem]


class ChainStatusResponse(Envelope):
    chain_id: int
    rpc_chain_id: int
    block_number: int
    sample_balance_wei: int


class AlertItem(BaseModel):
    level: str
    rule: str
    message: str
    observed_at: str


class AlertsResponse(Envelope):
    total: int
    items: list[AlertItem]


class MetaEndpoint(BaseModel):
    path: str
    methods: list[str]
    console_view: str | None = None


class MetaResponse(Envelope):
    server: str
    version: str
    data_schemas: list[str]
    envelope_schemas: list[str]
    endpoints: list[MetaEndpoint]
    max_rows_per_page: int
    stream_path: str = "/api/v1/stream"
