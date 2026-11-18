"""Opportunity radar read model. GET-only; bundles are size-bounded and SHA-256 verified."""

import json
import stat
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query

from ..models.common import sha256_hex
from .deps import AppState, State

router = APIRouter(prefix="/api/v1/radar", tags=["radar"])

_RADAR_JSON_LIMIT = 2 * 1024 * 1024
_METADATA_KEYS = frozenset({"schema_version", "mode", "observed_at_ms", "execution_eligible"})
_UNCONFIGURED_DETAIL = "opportunity radar run not configured"
_CORRUPT_DETAIL = "opportunity radar bundle unavailable or corrupt"


def _reject_json_constant(token: str) -> Any:
    raise ValueError(f"non-finite JSON constant: {token}")


def _read_bounded_json(path: Path) -> tuple[dict[str, Any], bytes]:
    """Read and parse one non-symlink regular JSON object file within ``_RADAR_JSON_LIMIT``."""
    st = path.lstat()
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise ValueError("radar artifact must be a regular non-symlink file")
    if st.st_size > _RADAR_JSON_LIMIT:
        raise ValueError("radar artifact exceeds bounded size limit")
    with path.open("rb") as handle:
        raw = handle.read(_RADAR_JSON_LIMIT + 1)
    if len(raw) > _RADAR_JSON_LIMIT:
        raise ValueError("radar artifact exceeds bounded size limit")
    parsed = json.loads(raw.decode("utf-8"), parse_constant=_reject_json_constant)
    if not isinstance(parsed, dict):
        raise ValueError("radar artifact must be a JSON object")
    return parsed, raw


def _extract_engines(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Extract and validate top-level engine sections from a radar report."""
    engines: dict[str, dict[str, Any]] = {}
    for key, value in report.items():
        if key in _METADATA_KEYS:
            continue
        if not isinstance(key, str) or not isinstance(value, dict):
            raise ValueError("radar engine section must be a JSON object")
        if "execution_eligible" in value and value["execution_eligible"] is not False:
            raise ValueError("radar engine section must not claim execution authority")
        engines[key] = value
    if not engines:
        raise ValueError("radar report must include at least one engine section")
    return engines


def _load_verified_radar(
    state: AppState,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]], str]:
    """Load and verify ``evidence_manifest.json`` and ``radar_report.json`` fail-closed."""
    radar_run = state.config.databases.radar_run
    if radar_run is None:
        raise HTTPException(status_code=404, detail=_UNCONFIGURED_DETAIL)
    try:
        manifest, _ = _read_bounded_json(radar_run / "evidence_manifest.json")
        report, report_bytes = _read_bounded_json(radar_run / "radar_report.json")
        digest = sha256_hex(report_bytes)
        expected_digest = manifest.get("radar_report_sha256")
        if not isinstance(expected_digest, str) or digest != expected_digest:
            raise ValueError("radar report SHA-256 mismatch")
        if report.get("execution_eligible") is not False:
            raise ValueError("radar report execution_eligible must be false")
        if manifest.get("execution_eligible") is not False:
            raise ValueError("radar manifest execution_eligible must be false")
        if type(report.get("observed_at_ms")) is not int:
            raise ValueError("radar report observed_at_ms must be an integer")
        engines = _extract_engines(report)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        # UnicodeDecodeError and json.JSONDecodeError are ValueError subclasses.
        raise HTTPException(status_code=503, detail=_CORRUPT_DETAIL) from exc
    return report, engines, digest


@router.get("/summary")
def summary(state: State) -> dict[str, Any]:
    """Return verified radar report metadata, engine inventory, and availability flags."""
    report, engine_sections, digest = _load_verified_radar(state)
    eqn_block = engine_sections.get("eqn_intelligence_and_launchpad_gate")
    eqn_news_raw = eqn_block.get("eqn_news") if isinstance(eqn_block, dict) else None
    eqn_news_summary = eqn_news_raw if isinstance(eqn_news_raw, dict) else None
    return {
        "schema_version": report.get("schema_version"),
        "mode": report.get("mode"),
        "observed_at_ms": report.get("observed_at_ms"),
        "radar_report_sha256": digest,
        "execution_eligible": False,
        "engine_count": len(engine_sections),
        "engine_names": sorted(engine_sections.keys()),
        "eqn_news_summary": eqn_news_summary,
        "portfolio_available": "stage7_portfolio_allocator" in engine_sections,
    }


@router.get("/catalysts")
def catalysts(
    state: State,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    actionable_only: bool = False,
) -> dict[str, Any]:
    """Return bounded EQN catalyst items and LaunchpadSafetyGate decisions."""
    _, engine_sections, digest = _load_verified_radar(state)
    eqn_block = engine_sections.get("eqn_intelligence_and_launchpad_gate", {})
    eqn_news = eqn_block.get("eqn_news", {})
    gate = eqn_block.get("launchpad_safety_gate", {})
    if not isinstance(eqn_news, dict) or not isinstance(gate, dict):
        raise HTTPException(status_code=503, detail=_CORRUPT_DETAIL)
    raw_items = eqn_news.get("top_items", [])
    if not isinstance(raw_items, list) or not all(isinstance(item, dict) for item in raw_items):
        raise HTTPException(status_code=503, detail=_CORRUPT_DETAIL)
    filtered = (
        [item for item in raw_items if item.get("actionable") is True]
        if actionable_only
        else list(raw_items)
    )
    items = filtered[:limit]
    return {
        "execution_eligible": False,
        "radar_report_sha256": digest,
        "status": eqn_news.get("status"),
        "item_count": eqn_news.get("item_count", 0),
        "counts_by_kind": eqn_news.get("counts_by_kind", {}),
        "risk_freeze_count": eqn_news.get("risk_freeze_count", 0),
        "nominated_verified_addresses": eqn_news.get("nominated_verified_addresses", []),
        "frozen_symbols": eqn_news.get("frozen_symbols", []),
        "items": items,
        "launchpad_safety_gate": gate,
    }


@router.get("/engines")
def engines(state: State, name: str | None = None) -> dict[str, Any]:
    """Return all verified radar engine sections or a single named engine."""
    report, engine_sections, digest = _load_verified_radar(state)
    if name is not None:
        if name not in engine_sections:
            raise HTTPException(status_code=404, detail="radar engine not found")
        selected: dict[str, dict[str, Any]] = {name: engine_sections[name]}
    else:
        selected = engine_sections
    return {
        "execution_eligible": False,
        "radar_report_sha256": digest,
        "schema_version": report.get("schema_version"),
        "engines": selected,
    }


@router.get("/portfolio")
def portfolio(state: State) -> dict[str, Any]:
    """Return Stage 7 portfolio allocator details when present in the verified radar bundle."""
    _, engine_sections, digest = _load_verified_radar(state)
    allocator = engine_sections.get("stage7_portfolio_allocator")
    return {
        "available": allocator is not None,
        "execution_eligible": False,
        "radar_report_sha256": digest,
        "portfolio": allocator,
    }
