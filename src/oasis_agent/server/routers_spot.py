"""Shadow spot agent read model. GET-only; journals open read-only and are re-verified."""

import json
import sqlite3
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query

from ..evidence.store import ReadOnlyEvidenceStore
from ..launch.spot_cost_binding import bind_order_cost
from ..launch.spot_lifecycle import LIVE_SCHEMAS
from ..launch.spot_report import build_report, read_journal, recent
from ..launch.spot_settlement_check import check_block_settlement
from ..launch.spot_transfer_check import check_order_transfers
from ..models.common import sha256_hex
from .deps import State

router = APIRouter(prefix="/api/v1/spot", tags=["spot"])
DECISIONS = frozenset(
    {
        "live_start",
        "live_intent",
        "live_fill",
        "live_halt",
        "budget_stop",
        "paper_buy",
        "paper_sell",
        "live_buy",
        "live_sell",
        "live_unresolved",
        "halt_review",
        "skip_entry",
        "mark_missed",
    }
)


def _mode(policy: dict[str, Any]) -> str:
    return "live" if policy.get("schema") in LIVE_SCHEMAS else "shadow"


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
    policy, records = _guard(_run_dir(state))
    items = recent(records, DECISIONS, limit)
    return {"execution_eligible": False, "mode": _mode(policy), "items": items, "total": len(items)}


@router.get("/assessments")
def assessments(
    state: State,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    action: Annotated[str | None, Query(pattern="^(flip|watch|skip|lp_candidate)$")] = None,
) -> dict[str, Any]:
    policy, records = _guard(_run_dir(state))
    rows = [
        r
        for r in recent(records, frozenset({"assessment"}), 5_000)
        if action is None or r["action"] == action
    ][:limit]
    return {"execution_eligible": False, "mode": _mode(policy), "items": rows, "total": len(rows)}


PROOF_SCHEMA = "oasis.spot-fill-proof.v1"
# Binding files name a few evidence references; anything larger is not one of them.
_PROOF_JSON_LIMIT = 1 << 20


def _bundle_json(path: Path) -> tuple[object, str]:
    """One bundle file read once, size-bounded; the digest is of exactly the parsed bytes."""
    with path.open("rb") as handle:
        raw = handle.read(_PROOF_JSON_LIMIT + 1)
    if len(raw) > _PROOF_JSON_LIMIT:
        raise ValueError("bundle file too large")
    return json.loads(raw), sha256_hex(raw)


@router.get("/fill-proof")
def fill_proof(state: State) -> dict[str, Any]:
    """The per-order proof checks of one `capture_spot_cost` bundle, as the report CLI runs them.

    One verified journal snapshot and one read-only evidence snapshot feed all three views.
    An unconfigured bundle is a state, not an error; a missing or unreadable one fails
    closed. Evidence only: nothing is booked, converted to USD or made executable.
    """
    run = _run_dir(state)
    bundle = state.config.databases.spot_proof
    if bundle is None:
        return {"schema": PROOF_SCHEMA, "configured": False, "execution_eligible": False}
    binding_file, evidence = bundle / "binding.json", bundle / "evidence.db"
    settlement_file = bundle / "settlement.json"
    settled = settlement_file.exists() or settlement_file.is_symlink()
    if (
        not binding_file.is_file()
        or not evidence.is_file()
        or (settled and not settlement_file.is_file())
    ):
        raise HTTPException(status_code=503, detail="spot proof bundle not found or incomplete")
    policy, records = _guard(run)
    try:
        binding, binding_digest = _bundle_json(binding_file)
        settlement, settlement_digest = _bundle_json(settlement_file) if settled else (None, None)
        store = ReadOnlyEvidenceStore(evidence)
        try:
            cost = bind_order_cost(records, binding, store)
            transfers = check_order_transfers(records, binding, store)
            block = (
                None
                if settlement is None
                else check_block_settlement(records, binding, settlement, store)
            )
        finally:
            store.close()
    except Exception as exc:  # unreadable bundle or store: fail closed, details never leak
        raise HTTPException(status_code=503, detail="spot proof unavailable or corrupt") from exc
    return {
        "schema": PROOF_SCHEMA,
        "configured": True,
        "execution_eligible": False,
        "mode": _mode(policy),
        "bundle": {"binding_sha256": binding_digest, "settlement_sha256": settlement_digest},
        "cost_replay": cost,
        "receipt_transfers": transfers,
        # None: the bundle has no settlement.json, so the whole-block check was not run.
        "block_settlement": block,
    }
