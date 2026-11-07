"""Shadow spot agent read model. GET-only; journals open read-only and are re-verified."""

import sqlite3
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query

from ..launch.spot_report import build_report, read_journal, recent
from .deps import State

router = APIRouter(prefix="/api/v1/spot", tags=["spot"])
DECISIONS = frozenset({"paper_buy", "paper_sell", "halt_review", "skip_entry", "mark_missed"})


def _run_dir(state: State) -> Path:
    path = state.config.databases.spot_run
    if path is None:
        raise HTTPException(status_code=404, detail="spot shadow run not configured")
    if not (path / "journal.db").is_file():
        raise HTTPException(status_code=503, detail="spot shadow journal not found")
    return path


def _guard(run: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    try:
        return read_journal(run / "journal.db")
    except (sqlite3.Error, ValueError, KeyError, TypeError, OSError) as exc:
        # json.JSONDecodeError is a ValueError; a broken chain must never render as data.
        raise HTTPException(status_code=503, detail="spot journal unavailable or corrupt") from exc


@router.get("/report")
def report(state: State) -> dict[str, Any]:
    run = _run_dir(state)
    _guard(run)
    try:
        return build_report(run)
    except Exception as exc:  # every failure fails closed; details never leak
        raise HTTPException(status_code=503, detail="spot report unavailable") from exc


@router.get("/decisions")
def decisions(state: State, limit: Annotated[int, Query(ge=1, le=500)] = 100) -> dict[str, Any]:
    _, records = _guard(_run_dir(state))
    items = recent(records, DECISIONS, limit)
    return {"execution_eligible": False, "mode": "shadow", "items": items, "total": len(items)}


@router.get("/assessments")
def assessments(
    state: State,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    action: Annotated[str | None, Query(pattern="^(flip|watch|skip|lp_candidate)$")] = None,
) -> dict[str, Any]:
    _, records = _guard(_run_dir(state))
    rows = [
        r
        for r in recent(records, frozenset({"assessment"}), 5_000)
        if action is None or r["action"] == action
    ][:limit]
    return {"execution_eligible": False, "mode": "shadow", "items": rows, "total": len(rows)}
