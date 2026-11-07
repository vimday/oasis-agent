"""Authenticated configuration and lifecycle routes for managed observations."""

import asyncio
import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException
from pydantic import Field, SecretStr

from ..launch.models import StrictModel
from ..launch.spot_report import build_report
from .campaigns import CampaignError, CampaignManager
from .config import ServerConfig, ServerDatabaseConfig
from .databases import ReadOnlyDatabases
from .deps import State
from .routers_launch import launch_data
from .routers_ops import Bearer, _actor_from_token

router = APIRouter(prefix="/api/v1/campaigns", tags=["campaigns"])
Revision = Annotated[int, Field(ge=1, le=10000)]


class SaveCampaign(StrictModel):
    expected_revision: Annotated[int, Field(ge=0, le=10000)]
    document: Annotated[str, Field(min_length=2, max_length=131072)]


class ApplyCampaign(StrictModel):
    expected_revision: Revision
    revision: Revision


class StartCampaign(StrictModel):
    revision: Revision
    request_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]


class RpcProfile(StrictModel):
    url: Annotated[SecretStr, Field(min_length=1, max_length=4096)]


def manager(state: State) -> CampaignManager:
    if state.campaigns is None:
        raise HTTPException(status_code=503, detail="campaign controller unavailable")
    return state.campaigns


@router.get("")
async def campaigns(state: State) -> dict[str, Any]:
    return manager(state).listing()


@router.post("/rpc/{name}")
async def set_rpc(
    name: str, body: RpcProfile, state: State, authorization: Bearer = None
) -> dict[str, Any]:
    actor = _actor_from_token(authorization, state)
    manager(state).set_rpc(name, body.url.get_secret_value(), actor)
    return {"name": name, "storage": "session", "configured": True}


@router.get("/{name}")
async def campaign(name: str, state: State) -> dict[str, Any]:
    return manager(state).head(name)


@router.post("/{name}/configuration")
async def save_campaign(
    name: str, body: SaveCampaign, state: State, authorization: Bearer = None
) -> dict[str, Any]:
    actor = _actor_from_token(authorization, state)
    try:
        return manager(state).save(name, body.document, body.expected_revision, actor)
    except CampaignError:
        raise
    except (ValueError, TypeError, KeyError, ArithmeticError, RecursionError) as exc:
        raise HTTPException(status_code=422, detail="invalid campaign configuration") from exc


@router.post("/{name}/apply")
async def apply_campaign(
    name: str, body: ApplyCampaign, state: State, authorization: Bearer = None
) -> dict[str, Any]:
    actor = _actor_from_token(authorization, state)
    return manager(state).apply(name, body.expected_revision, body.revision, actor)


@router.post("/{name}/start")
async def start_campaign(
    name: str, body: StartCampaign, state: State, authorization: Bearer = None
) -> dict[str, Any]:
    actor = _actor_from_token(authorization, state)
    try:
        return await manager(state).start(name, body.revision, actor, body.request_id)
    except (OSError, sqlite3.Error) as exc:
        raise HTTPException(
            status_code=503, detail="could not prepare retained observation inputs"
        ) from exc


@router.post("/{name}/stop")
async def stop_campaign(name: str, state: State, authorization: Bearer = None) -> dict[str, Any]:
    actor = _actor_from_token(authorization, state)
    return manager(state).stop(name, actor)


def _snapshot(kind: str, path: Path, config: ServerConfig) -> dict[str, Any]:
    if kind == "spot":
        if not (path / "journal.db").is_file():
            return {"status": "journal_not_created", "items": [], "execution_eligible": False}
        try:
            report = build_report(path)
        except (sqlite3.Error, ValueError, KeyError, TypeError, OSError) as exc:
            raise HTTPException(
                status_code=503, detail="spot journal unavailable or corrupt"
            ) from exc
        return {"status": "retained", "items": [report], "execution_eligible": False}
    if not path.is_file():
        return {"status": "journal_not_created", "items": [], "execution_eligible": False}
    if kind in {"market", "archive"}:
        if path.stat().st_size > 131072:
            raise ValueError("research status exceeds size bound")
        result = json.loads(path.read_bytes())
        if (
            not isinstance(result, dict)
            or result.get("schema")
            != (
                "oasis.market-archive-status.v1"
                if kind == "archive"
                else "oasis.lp-research-run.v1"
            )
            or result.get("execution_eligible") is not False
        ):
            raise ValueError("invalid research status")
        diagnostics = {}
        for name in ("economics.json", "coverage.json"):
            report_path = path.parent / name
            if report_path.is_file():
                if report_path.stat().st_size > 262144:
                    diagnostics[name] = {
                        "preview": "size_limit",
                        "message": "Report exceeds preview limit; inspect the local artifact.",
                    }
                    continue
                diagnostics[name] = json.loads(report_path.read_bytes())
        return {
            "status": "retained",
            "items": [result],
            "diagnostics": diagnostics,
            "execution_eligible": False,
        }
    if kind == "launch":
        target, records, mode = launch_data(path)
        return {
            "status": "retained",
            "target": target,
            "items": records[-50:],
            "total": len(records),
            "mode": mode,
            "execution_eligible": False,
        }
    readonly = ReadOnlyDatabases(replace(config, databases=ServerDatabaseConfig(shadow=path)))
    try:
        decisions = readonly.decision_chain()
        return {
            "status": "retained",
            "items": list(decisions[-50:]),
            "total": len(decisions),
            "execution_eligible": False,
        }
    finally:
        readonly.close()


@router.get("/{name}/snapshot")
async def snapshot(name: str, state: State) -> dict[str, Any]:
    kind, path = manager(state).journal_path(name)
    if path is None:
        result: dict[str, Any] = {"status": "no_run", "items": [], "execution_eligible": False}
    else:
        result = await asyncio.to_thread(_snapshot, kind, path, state.config)
    # Retained amounts never pass through JavaScript Number, including in the
    # raw-evidence panel. Only status/metadata lives in the outer API envelope.
    return {
        "status": result["status"],
        "document": json.dumps(result, indent=2),
        "execution_eligible": False,
    }
