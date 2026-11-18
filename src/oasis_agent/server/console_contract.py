"""Explicit API-to-console ownership; checked against OpenAPI in tests.

This is navigation metadata, never authorization. Routes and signer policies
retain responsibility for enforcing their own capabilities.
"""

CONSOLE_VIEWS: dict[str, str] = {
    "/api/v1/research/token-score": "research",
    "/api/v1/research/batch-score": "research",
    "/api/v1/meta": "system",
    "/api/v1/metrics": "system",
    "/api/v1/health": "system",
    "/api/v1/status": "overview",
    "/api/v1/live/status": "execution",
    "/api/v1/observations": "observations",
    "/api/v1/decisions": "decisions",
    "/api/v1/decisions/{seq}": "decisions",
    "/api/v1/evidence": "evidence",
    "/api/v1/evidence/{evidence_key}": "evidence",
    "/api/v1/readiness": "overview",
    "/api/v1/journal/lp": "execution",
    "/api/v1/journal/lp/events": "execution",
    "/api/v1/p2/halt-proofs": "execution",
    "/api/v1/p2/gates": "gates",
    "/api/v1/p2/gates/{gate}/approval": "gates",
    "/api/v1/p2/gates/{gate}/approval/{approval_id}/confirm": "gates",
    "/api/v1/runs": "ops",
    "/api/v1/runs/shadow": "ops",
    "/api/v1/runs/{run_id}": "ops",
    "/api/v1/runs/{run_id}/stop": "ops",
    "/api/v1/ops": "ops",
    "/api/v1/chains": "chains",
    "/api/v1/chains/{chain_id}/chain": "chains",
    "/api/v1/chains/{chain_id}/pool": "chains",
    "/api/v1/alerts": "chains",
    "/api/v1/configuration": "ops",
    "/api/v1/configuration/validate": "ops",
    "/api/v1/campaigns": "ops",
    "/api/v1/campaigns/rpc/{name}": "ops",
    "/api/v1/campaigns/{name}": "ops",
    "/api/v1/campaigns/{name}/configuration": "ops",
    "/api/v1/campaigns/{name}/apply": "ops",
    "/api/v1/campaigns/{name}/start": "ops",
    "/api/v1/campaigns/{name}/stop": "ops",
    "/api/v1/campaigns/{name}/snapshot": "ops",
    **{
        f"/api/v1/launch/{part}": "launch"
        for part in ("targets", "decisions", "candidates", "quotes", "approvals", "dashboard")
    },
    **{
        f"/api/v1/spot/{part}": "spot"
        for part in ("report", "decisions", "assessments", "fill-proof")
    },
    **{
        f"/api/v1/radar/{part}": "research"
        for part in ("summary", "catalysts", "engines", "portfolio")
    },
}
