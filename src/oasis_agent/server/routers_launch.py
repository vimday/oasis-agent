"""P0 launch diagnostics. Only GET routes and read-only SQLite connections."""

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from ..launch.models import Target
from ..shadow.sniper_runner import read_records
from .deps import State

router = APIRouter(prefix="/api/v1/launch", tags=["launch"])


def launch_data(path: Path | None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if path is None:
        return Target().model_dump(mode="json"), []
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            records = read_records(db)
            row = db.execute("SELECT payload FROM launch_policy WHERE id=1").fetchone()
            target = Target.from_json(json.dumps(json.loads(row[0])["target"]))
            return target.model_dump(mode="json"), records
    except (sqlite3.Error, ValueError, KeyError, TypeError) as exc:
        raise HTTPException(
            status_code=503, detail="launch journal unavailable or corrupt"
        ) from exc


def envelope(items: list[dict[str, Any]], *, configured: bool) -> dict[str, Any]:
    return {
        "schema": "oasis.launch-observation.v1",
        "execution_eligible": False,
        "evidence_level": "unresolved",
        "status": "retained_only" if configured else "unconfigured",
        "items": items,
        "total": len(items),
    }


@router.get("/targets")
async def targets(state: State) -> dict[str, Any]:
    target, records = launch_data(state.config.databases.launch)
    return envelope(
        [
            target
            | {
                "watcher_status": "not_connected",
                "control_status": "unverified",
                "stand_down": records[-1]["decision"] == "stand_down" if records else True,
                "latest_decision": records[-1]["digest"] if records else None,
            }
        ],
        configured=state.config.databases.launch is not None,
    )


@router.get("/decisions")
async def decisions(
    state: State,
    limit: int = Query(default=50, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    if limit > state.config.max_rows_per_page:
        raise HTTPException(status_code=422, detail="limit exceeds configured maximum")
    _, records = launch_data(state.config.databases.launch)
    result = envelope(
        records[offset : offset + limit], configured=state.config.databases.launch is not None
    )
    result["total"] = len(records)
    result["next_seq"] = offset + limit + 1 if offset + limit < len(records) else None
    return result


@router.get("/candidates")
async def candidates(state: State) -> dict[str, Any]:
    _, records = launch_data(state.config.databases.launch)
    items: dict[str, dict[str, Any]] = {}
    for record in records:
        for observation in record["input"]["observations"]:
            items[observation["pool"]] = {
                key: observation[key] for key in ("pool", "token0", "token1", "snapshot")
            }
            items[observation["pool"]].update(
                gates=record["gates"],
                source_status="imported_unverified",
                liquidity_source="unresolved",
                withdrawal_authority="unresolved",
                tradable="unverified",
                digest=record["digest"],
            )
    return envelope(list(items.values()), configured=state.config.databases.launch is not None)


@router.get("/quotes")
async def quotes(state: State) -> dict[str, Any]:
    _, records = launch_data(state.config.databases.launch)
    items = []
    if records:
        for observation in records[-1]["input"]["observations"]:
            for quote in observation["quotes"]:
                numerator = (
                    quote["spend_usd_micros"] * 1_000_000_000 * 10 ** observation["token_decimals"]
                )
                items.append(
                    {key: str(value) for key, value in quote.items()}
                    | {
                        "pool": observation["pool"],
                        "source": observation["source"],
                        "snapshot": observation["snapshot"],
                        "fdv_usd_micros_numerator": str(numerator),
                        "fdv_denominator": str(quote["received_base_units"]),
                        "fdv_below_cap": numerator
                        < 200_000_000_000_000 * quote["received_base_units"],
                        "valuation_status": "imported_unverified",
                    }
                )
    return envelope(items, configured=state.config.databases.launch is not None)


@router.get("/approvals")
async def approvals(state: State) -> dict[str, Any]:
    target, _ = launch_data(state.config.databases.launch)
    return envelope(
        [
            {
                "target_ca": target["target_ca"],
                "status": "unresolved",
                "reason": "bounded allowance and native-buy path not verified",
            }
        ],
        configured=state.config.databases.launch is not None,
    )
