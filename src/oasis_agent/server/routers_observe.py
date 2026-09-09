"""Read-only observation routes (P0) and the live decision stream (P1).

All data served here is re-validated from the hash-linked journals on every
request; integrity failures surface as HTTP 503 through the centralized
exception handlers so the frontend never sees a partial or tampered view.
Response payloads are the canonical journal records (``schema``/``digest``
preserved) so what the dashboard displays is exactly what the audit chain
contains. Filtering and pagination for the evidence endpoint are pushed into
SQL so large evidence sets never cross the process boundary in full.

Stable derived envelopes (health, status, meta) are validated by Pydantic
response models; journal-record endpoints are intentionally unmodeled so the
canonical records pass through field-for-field.
"""

import asyncio
import json
import logging
import time
from collections.abc import Sequence

from fastapi import APIRouter, HTTPException, Query, Request, WebSocket, WebSocketDisconnect

from ..models.common import sha256_hex
from . import __version__
from .chains import RpcProtocolError, decode_rpc_quantity
from .databases import Page, ReadOnlyError
from .deps import State
from .routers_launch import launch_data
from .schemas import (
    DATA_SCHEMAS,
    ENVELOPE_SCHEMAS,
    HealthResponse,
    HealthSource,
    MetaEndpoint,
    MetaResponse,
    ReadinessPayload,
    StatusResponse,
)

router = APIRouter(prefix="/api/v1", tags=["observe"])
logger = logging.getLogger(__name__)

_WS_POLL_SECONDS = 3.0
_WS_HEARTBEAT_SECONDS = 15.0
_SERVER_NAME = "oasis-agent-observation-server"


def _page(items: Sequence[dict[str, object]], offset: int, limit: int) -> Page:
    total = len(items)
    start = min(offset, total)
    end = min(offset + limit, total)
    sliced = tuple(items[start:end])
    next_seq = sliced[-1].get("seq") if sliced else None
    next_seq = int(next_seq) + 1 if isinstance(next_seq, int) and end < total else None
    return Page(items=sliced, total=total, next_seq=next_seq)


def _require_offset(offset: int, max_rows: int) -> int:
    if offset < 0:
        raise HTTPException(status_code=422, detail="offset must be nonnegative")
    return offset


def _require_limit(limit: int, max_rows: int) -> int:
    if not 1 <= limit <= max_rows:
        raise HTTPException(status_code=422, detail=f"limit must be 1..{max_rows}")
    return limit


@router.get("/meta", response_model=MetaResponse)
async def meta(request: Request) -> MetaResponse:
    """Self-describing API metadata: version, schemas and live routes."""
    endpoints: dict[str, tuple[str, ...]] = {}
    paths = request.app.openapi().get("paths", {})
    if not isinstance(paths, dict):
        raise TypeError("OpenAPI paths are malformed")
    allowed_methods = {"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"}
    for path, operations in paths.items():
        if not isinstance(path, str) or not path.startswith("/api/v1"):
            continue
        if not isinstance(operations, dict):
            continue
        methods = tuple(
            sorted(
                method.upper()
                for method in operations
                if isinstance(method, str) and method.upper() in allowed_methods
            )
        )
        if methods:
            endpoints[path] = methods
    return MetaResponse(
        schema="oasis.server-meta.v1",
        server=_SERVER_NAME,
        version=__version__,
        data_schemas=list(DATA_SCHEMAS),
        envelope_schemas=list(ENVELOPE_SCHEMAS),
        endpoints=[
            MetaEndpoint(path=path, methods=list(methods))
            for path, methods in sorted(endpoints.items())
        ],
    )


@router.get("/health", response_model=HealthResponse)
async def health(state: State) -> HealthResponse:
    sources = state.databases.sources_status()
    return HealthResponse(
        schema="oasis.server-health.v1",
        ok=all(source.available for source in sources.values()),
        sources={
            name: HealthSource(available=status.available, integrity=status.integrity)
            for name, status in sources.items()
        },
    )


@router.get("/status", response_model=StatusResponse)
async def status(state: State) -> StatusResponse:
    databases = state.databases
    decisions: tuple[dict[str, object], ...] = ()
    health: tuple[dict[str, object], ...] = ()
    # The overview endpoint degrades gracefully: if one journal is unreadable
    # the remaining verified state is still served, and health reflects it.
    try:
        decisions = databases.decision_chain()
        health = databases.health_chain()
    except ReadOnlyError:
        pass
    readiness_payload: ReadinessPayload | None = None
    try:
        assessment = databases.readiness()
        readiness_payload = ReadinessPayload(
            schema="oasis.lp-release-assessment.v1",
            as_of=assessment.as_of,
            stage=assessment.stage.value,
            execution_eligible=assessment.execution_eligible,
            blockers=list(assessment.blockers),
            digest=assessment.digest,
        )
    except ReadOnlyError:
        pass
    latest_decision = decisions[-1] if decisions else None
    latest_health = health[-1] if health else None
    return StatusResponse(
        schema="oasis.server-status.v1",
        stage=readiness_payload.stage if readiness_payload else None,
        execution_eligible=readiness_payload.execution_eligible if readiness_payload else None,
        readiness=readiness_payload,
        latest_decision=latest_decision,
        latest_health=latest_health,
        halt_latched=bool(latest_health and latest_health.get("decision") == "HALT"),
        decision_count=len(decisions),
        health_count=len(health),
    )


@router.get("/observations")
async def observations(
    state: State,
    limit: int = Query(default=50),
    offset: int = Query(default=0),
) -> dict[str, object]:
    limit = _require_limit(limit, state.config.max_rows_per_page)
    offset = _require_offset(offset, state.config.max_rows_per_page)
    page = _page(state.databases.health_chain(), offset, limit)
    return {
        "schema": "oasis.server-observations.v1",
        "integrity": "verified",
        "total": page.total,
        "next_seq": page.next_seq,
        "items": list(page.items),
    }


@router.get("/decisions")
async def decisions(
    state: State,
    limit: int = Query(default=50),
    offset: int = Query(default=0),
    decision: str | None = Query(default=None),
    strategy: str = Query(default="lp", pattern="^(lp|launch)$"),
) -> dict[str, object]:
    limit = _require_limit(limit, state.config.max_rows_per_page)
    offset = _require_offset(offset, state.config.max_rows_per_page)
    chain = (
        tuple(launch_data(state.config.databases.launch)[1])
        if strategy == "launch"
        else state.databases.decision_chain()
    )
    if decision is not None:
        chain = tuple(item for item in chain if item.get("decision") == decision)
    page = _page(chain, offset, limit)
    return {
        "schema": "oasis.server.decisions.v1",
        "integrity": "verified",
        "total": page.total,
        "next_seq": page.next_seq,
        "items": list(page.items),
    }


@router.get("/decisions/{seq}")
async def decision_detail(state: State, seq: int) -> dict[str, object]:
    chain = state.databases.decision_chain()
    health = state.databases.health_chain()
    match = next((item for item in chain if item.get("seq") == seq), None)
    if match is None:
        raise HTTPException(status_code=404, detail="decision not found")
    health_digest = match.get("health_digest")
    linked_health = (
        next((item for item in health if item.get("digest") == health_digest), None)
        if isinstance(health_digest, str)
        else None
    )
    return {
        "schema": "oasis.server.decision-detail.v1",
        "decision": match,
        "linked_health": linked_health,
    }


@router.get("/evidence")
async def evidence(
    state: State,
    kind: str | None = Query(default=None),
    chain_id: int | None = Query(default=None),
    source: str | None = Query(default=None),
    limit: int = Query(default=50),
    offset: int = Query(default=0),
) -> dict[str, object]:
    limit = _require_limit(limit, state.config.max_rows_per_page)
    offset = _require_offset(offset, state.config.max_rows_per_page)
    page = state.databases.evidence_page(
        kind=kind, chain_id=chain_id, source=source, limit=limit, offset=offset
    )
    return {
        "schema": "oasis.server.evidence.v1",
        "total": page.total,
        "items": list(page.items),
    }


@router.get("/evidence/{evidence_key}")
async def evidence_detail(state: State, evidence_key: str) -> dict[str, object]:
    record = state.databases.evidence_by_key(evidence_key)
    if record is None:
        raise HTTPException(status_code=404, detail="evidence not found")
    return {"schema": "oasis.server.evidence-detail.v1", "record": record}


@router.get("/readiness")
async def readiness(state: State) -> dict[str, object]:
    assessment = state.databases.readiness()
    payload = json.loads(assessment.payload)
    return {
        "schema": "oasis.lp-release-assessment.v1",
        "as_of": assessment.as_of,
        "stage": assessment.stage.value,
        "execution_eligible": assessment.execution_eligible,
        "blockers": list(assessment.blockers),
        "gates": payload["gates"],
        "digest": assessment.digest,
    }


@router.get("/journal/lp")
async def execution_journal(state: State) -> dict[str, object]:
    summary = state.databases.execution_summary()
    return {"schema": "oasis.server.execution-journal.v1", **summary}


@router.get("/chains/{chain_id}/pool")
async def chain_pool(state: State, chain_id: int) -> dict[str, object]:
    registry = state.registry
    client = registry.client(chain_id)
    if not client.available:
        raise HTTPException(status_code=503, detail="chain RPC not configured")
    chain_call, block_call = await asyncio.gather(
        client.call("eth_chainId", []),
        client.call("eth_blockNumber", []),
    )
    result: dict[str, object] = {
        "chain_id": chain_id,
        "rpc_chain_id": decode_rpc_quantity(chain_call.result, "chain id"),
        "block_number": decode_rpc_quantity(block_call.result, "block number"),
    }
    chain_config = next(
        (chain for chain in state.config.chains if chain.chain_id == chain_id), None
    )
    if chain_config is not None and chain_config.pool_manager:
        balance, code = await asyncio.gather(
            client.call("eth_getBalance", [chain_config.pool_manager, "latest"]),
            client.call("eth_getCode", [chain_config.pool_manager, "latest"]),
        )
        if not isinstance(code.result, str) or not code.result.startswith("0x"):
            raise RpcProtocolError("pool manager code is not hex data")
        try:
            code_bytes = bytes.fromhex(code.result[2:])
        except ValueError as exc:
            raise RpcProtocolError("pool manager code is not hex data") from exc
        result["pool_manager_code_hash"] = sha256_hex(code_bytes)
        result["pool_manager_balance_wei"] = decode_rpc_quantity(
            balance.result, "pool manager balance"
        )
    return {"schema": "oasis.server.chain-pool.v1", "data": result}


@router.websocket("/stream")
async def stream(websocket: WebSocket) -> None:
    await websocket.accept()
    state = websocket.app.state
    last_decision_seq = 0
    last_launch_seq = 0
    sent_any = False
    last_heartbeat = 0.0
    try:
        while True:
            try:
                chain = state.databases.decision_chain()
            except ReadOnlyError:
                chain = ()
            latest_seq = max(
                (int(item["seq"]) for item in chain if isinstance(item.get("seq"), int)),
                default=0,
            )
            now = time.monotonic()
            if latest_seq > last_decision_seq:
                new_records = tuple(item for item in chain if int(item["seq"]) > last_decision_seq)
                last_decision_seq = latest_seq
                sent_any = True
                await websocket.send_json({"type": "decisions", "items": list(new_records)})
            elif not sent_any or now - last_heartbeat >= _WS_HEARTBEAT_SECONDS:
                # Heartbeats are rate-limited so an idle stream costs one small
                # frame per heartbeat window instead of one per poll cycle.
                sent_any = True
                last_heartbeat = now
                await websocket.send_json({"type": "heartbeat", "seq": latest_seq})
            if state.config.databases.launch is not None:
                try:
                    _, launch_records = launch_data(state.config.databases.launch)
                except HTTPException:
                    await websocket.send_json({"type": "launch_error", "status": "unavailable"})
                    await websocket.close(code=1011)
                    return
                fresh = [item for item in launch_records if item["seq"] > last_launch_seq]
                if fresh:
                    last_launch_seq = fresh[-1]["seq"]
                    await websocket.send_json({"type": "launch_decisions", "items": fresh})
            await asyncio.sleep(_WS_POLL_SECONDS)
    except WebSocketDisconnect:
        return
    except Exception as exc:  # noqa: BLE001 - transport failure ends the stream
        logger.debug("decision stream closed: %s", exc)
        return
