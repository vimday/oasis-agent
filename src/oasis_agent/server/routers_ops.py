"""Operation routes (P1) and control-console routes (P2).

Operation endpoints require a bearer token configured through the
environment; without a configured token they fail closed with 503 (translated
by the centralized handler for :class:`OpsDisabledError`). Every attempt is
written to the append-only operations journal, and parameters are recorded
only as digests so the audit trail cannot leak secrets. The actor is derived
from the token hash so a two-person approval requires two distinct tokens.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Query

from ..models.common import sha256_hex
from ..reporting.lp_readiness import LpGate
from .deps import State
from .runner import RunRejected

router = APIRouter(prefix="/api/v1", tags=["ops"])

_VALID_GATES = frozenset(gate.value for gate in LpGate)


def _require_limit(limit: int, maximum: int) -> int:
    if not 1 <= limit <= maximum:
        raise HTTPException(status_code=422, detail=f"limit must be 1..{maximum}")
    return limit


def _actor_from_token(authorization: str | None, state: State) -> str:
    state.authorizer.require_enabled()
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization[len("Bearer ") :]
    if not state.authorizer.check(token):
        raise HTTPException(status_code=401, detail="invalid token")
    return "ops:" + sha256_hex(token.encode())[:12]


Bearer = Annotated[str | None, Header()]
"""Bearer token injected from the ``Authorization`` header."""


@router.post("/runs/shadow")
async def trigger_shadow_run(
    state: State,
    authorization: Bearer = None,
) -> dict[str, object]:
    actor = _actor_from_token(authorization, state)
    if state.runner is None:
        raise HTTPException(status_code=503, detail="shadow runner is not configured")
    try:
        run = state.runner.trigger()
    except RunRejected as exc:
        raise HTTPException(status_code=409, detail=exc.reason) from exc
    state.journal.record(
        action="trigger_shadow_run",
        actor=actor,
        params={"command_digest": run.command_digest},
        result="accepted",
        detail=f"run {run.run_id} queued",
    )
    return {
        "schema": "oasis.server.run.v1",
        "run_id": run.run_id,
        "status": run.status,
        "created_at": run.created_at,
        "command_digest": run.command_digest,
    }


@router.get("/runs")
async def list_runs(
    state: State,
    limit: int = Query(default=20),
) -> dict[str, object]:
    limit = _require_limit(limit, 200)
    return {
        "schema": "oasis.server.runs.v1",
        "items": list(state.journal.list_runs(limit=limit)),
    }


@router.get("/runs/{run_id}")
async def run_detail(state: State, run_id: str) -> dict[str, object]:
    run = state.journal.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return {
        "schema": "oasis.server.run-detail.v1",
        "run_id": run.run_id,
        "created_at": run.created_at,
        "command_digest": run.command_digest,
        "status": run.status,
        "exit_code": run.exit_code,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "stdout_tail": run.stdout_tail,
        "stderr_tail": run.stderr_tail,
    }


@router.get("/ops")
async def list_ops(
    state: State,
    limit: int = Query(default=50),
) -> dict[str, object]:
    limit = _require_limit(limit, 500)
    return {
        "schema": "oasis.server.ops.v1",
        "items": list(state.journal.recent_ops(limit=limit)),
    }


@router.get("/p2/halt-proofs")
async def halt_proofs(state: State) -> dict[str, object]:
    events, _ = state.databases.execution_events()
    proofs: list[dict[str, object]] = []
    for event in events:
        recovery = event.get("recovery")
        if isinstance(recovery, dict):
            proofs.append(
                {
                    "intent_hash": event.get("intent_hash"),
                    "state": event.get("state"),
                    "recovery": recovery,
                    "event_digest": event.get("digest"),
                }
            )
    return {
        "schema": "oasis.server.halt-proofs.v1",
        "total": len(proofs),
        "items": proofs,
    }


@router.get("/p2/gates")
async def list_gate_approvals(state: State) -> dict[str, object]:
    return {
        "schema": "oasis.server.gate-approvals.v1",
        "items": list(state.journal.list_approvals()),
    }


@router.post("/p2/gates/{gate}/approval")
async def request_gate_approval(
    state: State,
    gate: str,
    reason: str = Query(min_length=1, max_length=500),
    authorization: Bearer = None,
) -> dict[str, object]:
    if gate not in _VALID_GATES:
        raise HTTPException(status_code=422, detail=f"unknown gate: {gate}")
    actor = _actor_from_token(authorization, state)
    approval = state.journal.request_approval(gate=gate, actor=actor, reason=reason)
    state.journal.record(
        action="request_gate_approval",
        actor=actor,
        params={"gate": gate, "approval_id": approval["id"]},
        result="accepted",
        detail="approval request recorded",
    )
    return {"schema": "oasis.server.gate-approval.v1", **approval}


@router.post("/p2/gates/{gate}/approval/{approval_id}/confirm")
async def confirm_gate_approval(
    state: State,
    gate: str,
    approval_id: int,
    authorization: Bearer = None,
) -> dict[str, object]:
    if gate not in _VALID_GATES:
        raise HTTPException(status_code=422, detail=f"unknown gate: {gate}")
    actor = _actor_from_token(authorization, state)
    approvals = state.journal.list_approvals(gate=gate)
    match = next((item for item in approvals if item["id"] == approval_id), None)
    if match is None:
        raise HTTPException(status_code=404, detail="approval not found")
    if str(match["status"]) != "pending":
        raise HTTPException(status_code=409, detail="approval is not pending")
    if str(match["requested_by"]) == actor:
        raise HTTPException(status_code=409, detail="confirmation requires a second operator")
    confirmed = state.journal.confirm_approval(approval_id=approval_id, actor=actor)
    if confirmed is None:
        raise HTTPException(status_code=404, detail="approval not found")
    state.journal.record(
        action="confirm_gate_approval",
        actor=actor,
        params={"gate": gate, "approval_id": approval_id},
        result="accepted",
        detail="approval confirmed by second operator",
    )
    return {"schema": "oasis.server.gate-approval.v1", **confirmed}
