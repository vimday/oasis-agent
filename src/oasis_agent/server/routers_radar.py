"""Opportunity radar read model. GET-only; bundles are size-bounded and SHA-256 verified."""

import json
import math
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


def _assert_non_executing(section: dict[str, Any], label: str) -> None:
    if "execution_eligible" in section and section["execution_eligible"] is not False:
        raise ValueError(f"{label} execution_eligible must be false")
    if "release_permitted" in section and section["release_permitted"] is not False:
        raise ValueError(f"{label} release_permitted must be false")


def _opt_int(section: dict[str, Any], key: str, *, nonneg: bool = False) -> int | None:
    if key not in section or section[key] is None:
        return None
    value = section[key]
    if type(value) is not int:
        raise ValueError(f"{key} must be a strict integer")
    if nonneg and value < 0:
        raise ValueError(f"{key} must be non-negative")
    return value


def _opt_number(section: dict[str, Any], key: str) -> int | float | None:
    if key not in section or section[key] is None:
        return None
    value = section[key]
    if type(value) is bool or not isinstance(value, (int, float)):
        raise ValueError(f"{key} must be numeric")
    if not math.isfinite(float(value)):
        raise ValueError(f"{key} must be finite")
    return value


def _opt_str(section: dict[str, Any], key: str) -> str | None:
    if key not in section or section[key] is None:
        return None
    value = section[key]
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return value


def _opt_bool(section: dict[str, Any], key: str) -> bool | None:
    if key not in section or section[key] is None:
        return None
    value = section[key]
    if type(value) is not bool:
        raise ValueError(f"{key} must be a boolean")
    return value


def _find_section(
    sources: tuple[dict[str, Any] | None, ...],
    keys: tuple[str, ...],
) -> object | None:
    for src in sources:
        if src is None:
            continue
        for key in keys:
            if key in src and src[key] is not None:
                val: object = src[key]
                return val
    return None


def _require_dict_section(raw: object, label: str) -> dict[str, Any] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(f"{label} must be a JSON object")
    _assert_non_executing(raw, label)
    return raw


def _build_capital_policy(raw_policy: dict[str, Any] | None) -> dict[str, Any]:
    if raw_policy is None:
        return {
            "available": False,
            "schema_version": None,
            "total_capital_usd_micros": None,
            "max_daily_loss_usd_micros": None,
            "max_cumulative_loss_usd_micros": None,
            "reserve_buffer_bps": None,
            "max_single_clip_share_bps": None,
            "min_clip_usd_micros": None,
            "assumed_tail_stress_bps": None,
            "max_feed_age_seconds": None,
            "config_sha256": None,
        }
    return {
        "available": True,
        "schema_version": _opt_str(raw_policy, "schema_version"),
        "total_capital_usd_micros": _opt_int(raw_policy, "total_capital_usd_micros", nonneg=True),
        "max_daily_loss_usd_micros": _opt_int(raw_policy, "max_daily_loss_usd_micros", nonneg=True),
        "max_cumulative_loss_usd_micros": _opt_int(
            raw_policy, "max_cumulative_loss_usd_micros", nonneg=True
        ),
        "reserve_buffer_bps": _opt_int(raw_policy, "reserve_buffer_bps", nonneg=True),
        "max_single_clip_share_bps": _opt_int(raw_policy, "max_single_clip_share_bps", nonneg=True),
        "min_clip_usd_micros": _opt_int(raw_policy, "min_clip_usd_micros", nonneg=True),
        "assumed_tail_stress_bps": _opt_int(raw_policy, "assumed_tail_stress_bps", nonneg=True),
        "max_feed_age_seconds": _opt_int(raw_policy, "max_feed_age_seconds", nonneg=True),
        "config_sha256": _opt_str(raw_policy, "config_sha256"),
    }


def _build_headroom(raw_headroom: dict[str, Any] | None) -> dict[str, Any]:
    if raw_headroom is None:
        return {
            "available": False,
            "deployable_capital_usd_micros": None,
            "deployable_notional_usd_micros": None,
            "max_clip_usd_micros": None,
            "min_clip_usd_micros": None,
            "reserve_floor_usd_micros": None,
            "remaining_daily_loss_usd_micros": None,
            "remaining_cumulative_loss_usd_micros": None,
            "locked_inventory_usd_micros": None,
            "pending_commitments_usd_micros": None,
            "can_deploy": False,
            "blocked_reason": "headroom_unavailable",
        }
    deployable_capital = _opt_int(raw_headroom, "deployable_capital_usd_micros", nonneg=True)
    max_clip = _opt_int(raw_headroom, "max_clip_usd_micros", nonneg=True)
    if max_clip is None:
        max_clip = _opt_int(raw_headroom, "max_max_clip_usd_micros", nonneg=True)
    deployable_notional = _opt_int(raw_headroom, "deployable_notional_usd_micros", nonneg=True)
    if deployable_notional is None:
        deployable_notional = max_clip if max_clip is not None else deployable_capital

    remaining_daily = _opt_int(raw_headroom, "remaining_daily_loss_usd_micros", nonneg=True)
    if remaining_daily is None:
        remaining_daily = _opt_int(
            raw_headroom, "final_remaining_daily_loss_usd_micros", nonneg=True
        )
    remaining_cum = _opt_int(raw_headroom, "remaining_cumulative_loss_usd_micros", nonneg=True)
    if remaining_cum is None:
        remaining_cum = _opt_int(
            raw_headroom, "final_remaining_cumulative_loss_usd_micros", nonneg=True
        )
    can_deploy = _opt_bool(raw_headroom, "can_deploy")
    blocked_reason = _opt_str(raw_headroom, "blocked_reason")
    if blocked_reason is None:
        blocked_reason = _opt_str(raw_headroom, "block_reason")
    return {
        "available": True,
        "deployable_capital_usd_micros": deployable_capital,
        "deployable_notional_usd_micros": deployable_notional,
        "max_clip_usd_micros": max_clip,
        "min_clip_usd_micros": _opt_int(raw_headroom, "min_clip_usd_micros", nonneg=True),
        "reserve_floor_usd_micros": _opt_int(raw_headroom, "reserve_floor_usd_micros", nonneg=True),
        "remaining_daily_loss_usd_micros": remaining_daily,
        "remaining_cumulative_loss_usd_micros": remaining_cum,
        "locked_inventory_usd_micros": _opt_int(
            raw_headroom, "locked_inventory_usd_micros", nonneg=True
        ),
        "pending_commitments_usd_micros": _opt_int(
            raw_headroom, "pending_commitments_usd_micros", nonneg=True
        ),
        "can_deploy": False if can_deploy is None else can_deploy,
        "blocked_reason": blocked_reason,
    }


def _build_oos_telemetry(raw_oos: dict[str, Any] | None) -> dict[str, Any]:
    if raw_oos is None:
        return {
            "available": False,
            "oos_trades_count": None,
            "oos_positive_trades_count": None,
            "oos_lcb_net_edge_bps": None,
            "oos_mean_net_edge_bps": None,
            "oos_total_net_profit_usd": None,
            "oos_total_net_profit_usd_micros": None,
            "oos_max_drawdown_usd": None,
            "ticks_completed": None,
            "window_label": None,
        }
    return {
        "available": True,
        "oos_trades_count": _opt_int(raw_oos, "oos_trades_count", nonneg=True),
        "oos_positive_trades_count": _opt_int(raw_oos, "oos_positive_trades_count", nonneg=True),
        "oos_lcb_net_edge_bps": _opt_number(raw_oos, "oos_lcb_net_edge_bps"),
        "oos_mean_net_edge_bps": _opt_number(raw_oos, "oos_mean_net_edge_bps"),
        "oos_total_net_profit_usd": _opt_number(raw_oos, "oos_total_net_profit_usd"),
        "oos_total_net_profit_usd_micros": _opt_int(raw_oos, "oos_total_net_profit_usd_micros"),
        "oos_max_drawdown_usd": _opt_number(raw_oos, "oos_max_drawdown_usd"),
        "ticks_completed": _opt_int(raw_oos, "ticks_completed", nonneg=True),
        "window_label": _opt_str(raw_oos, "window_label"),
    }


def _build_pair_capacities(raw_pairs: object | None) -> dict[str, Any]:
    if raw_pairs is None:
        return {"available": False, "pair_count": 0, "items": []}
    items: list[dict[str, Any]] = []
    if isinstance(raw_pairs, list):
        for idx, entry in enumerate(raw_pairs):
            if not isinstance(entry, dict):
                raise ValueError(f"pair_capacities[{idx}] must be a JSON object")
            _assert_non_executing(entry, f"pair_capacities[{idx}]")
            items.append(dict(entry))
    elif isinstance(raw_pairs, dict):
        _assert_non_executing(raw_pairs, "pair_capacities")
        if "items" in raw_pairs:
            nested = raw_pairs["items"]
            if not isinstance(nested, list):
                raise ValueError("pair_capacities.items must be a list")
            for idx, entry in enumerate(nested):
                if not isinstance(entry, dict):
                    raise ValueError(f"pair_capacities.items[{idx}] must be a JSON object")
                _assert_non_executing(entry, f"pair_capacities.items[{idx}]")
                items.append(dict(entry))
        else:
            skip_keys = frozenset({"available", "execution_eligible", "release_permitted"})
            for pair_name, entry in sorted(raw_pairs.items()):
                if pair_name in skip_keys:
                    continue
                if not isinstance(entry, dict):
                    raise ValueError(f"pair_capacities[{pair_name}] must be a JSON object")
                _assert_non_executing(entry, f"pair_capacities[{pair_name}]")
                items.append({"pair": pair_name, **entry})
    else:
        raise ValueError("pair_capacities must be a JSON object or list")

    return {
        "available": True,
        "pair_count": len(items),
        "items": items,
    }


def _extract_capacity_payload(
    report: dict[str, Any],
    engine_sections: dict[str, dict[str, Any]],
    digest: str,
) -> dict[str, Any]:
    capacity_container = _require_dict_section(
        _find_section(
            (engine_sections,),
            ("capacity_and_readiness", "profit_capacity", "profit_engine_capacity"),
        ),
        "capacity_and_readiness",
    )
    shadow_container = _require_dict_section(
        _find_section(
            (engine_sections,),
            ("multicall_shadow_campaign", "shadow_campaign", "shadow_summary"),
        ),
        "shadow_campaign",
    )
    cross_venue_container = _require_dict_section(
        engine_sections.get("aleph_cross_venue_and_saga"),
        "aleph_cross_venue_and_saga",
    )
    sources = (capacity_container, shadow_container, engine_sections, cross_venue_container)

    raw_policy = _require_dict_section(
        _find_section(sources, ("capital_policy", "risk_policy")),
        "capital_policy",
    )
    raw_headroom = _require_dict_section(
        _find_section(sources, ("headroom", "headroom_summary")),
        "headroom",
    )
    raw_oos = _require_dict_section(
        _find_section(sources, ("oos_telemetry", "oos_execution")),
        "oos_telemetry",
    )
    raw_pairs = _find_section(sources, ("pair_capacities",))
    raw_freshness = _require_dict_section(
        _find_section(sources, ("freshness",)),
        "freshness",
    )

    capital_policy = _build_capital_policy(raw_policy)
    headroom = _build_headroom(raw_headroom)
    oos_telemetry = _build_oos_telemetry(raw_oos)
    pair_capacities = _build_pair_capacities(raw_pairs)

    stale_flag = False
    freshness_status = "verified_snapshot"
    max_age_seconds = capital_policy["max_feed_age_seconds"]
    if raw_freshness is not None:
        parsed_stale = _opt_bool(raw_freshness, "stale")
        if parsed_stale is not None:
            stale_flag = parsed_stale
        parsed_status = _opt_str(raw_freshness, "status")
        if parsed_status is not None:
            freshness_status = parsed_status
        parsed_max_age = _opt_int(raw_freshness, "max_age_seconds", nonneg=True)
        if parsed_max_age is not None:
            max_age_seconds = parsed_max_age

    return {
        "schema_version": report.get("schema_version"),
        "mode": report.get("mode"),
        "observed_at_ms": report.get("observed_at_ms"),
        "radar_report_sha256": digest,
        "execution_eligible": False,
        "release_permitted": False,
        "integrity": {
            "status": "verified",
            "manifest_verified": True,
            "radar_report_sha256": digest,
        },
        "freshness": {
            "status": freshness_status,
            "observed_at_ms": report.get("observed_at_ms"),
            "max_age_seconds": max_age_seconds,
            "stale": stale_flag,
        },
        "capital_policy": capital_policy,
        "headroom": headroom,
        "oos_telemetry": oos_telemetry,
        "pair_capacities": pair_capacities,
    }


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


@router.get("/capacity")
def capacity(state: State) -> dict[str, Any]:
    """Return verified capital policy, headroom, OOS telemetry, and pair capacity metrics."""
    report, engine_sections, digest = _load_verified_radar(state)
    try:
        return _extract_capacity_payload(report, engine_sections, digest)
    except (ValueError, TypeError, KeyError) as exc:
        raise HTTPException(status_code=503, detail=_CORRUPT_DETAIL) from exc
