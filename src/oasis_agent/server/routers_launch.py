"""P0 launch diagnostics. Only GET routes and read-only SQLite connections."""

import json
import math
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query

from ..launch.models import Target
from ..models.common import canonical_json, sha256_hex
from ..shadow.sniper_runner import read_records
from .deps import State

router = APIRouter(prefix="/api/v1/launch", tags=["launch"])

Mode = str  # "shadow" for the retained shadow-launch journal, "execution" for the live one


def _execution_journal(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Read the live sniper ExecutionJournal with full chain verification.

    Mirrors the append-only invariants of `sniping.journal.ExecutionJournal`
    (sequence, previous-digest link, policy binding, canonical payloads) using
    only the read-only connection. Tampered or truncated chains raise 503.
    """
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        row = db.execute("SELECT payload FROM execution_policy WHERE id=1").fetchone()
        if row is None:
            raise ValueError("execution policy missing")
        payload = row[0]
        if not isinstance(payload, bytes):
            raise ValueError("execution policy must be a byte payload")
        if canonical_json(json.loads(payload)) != payload:
            raise ValueError("execution policy is not canonical")
        policy_digest = sha256_hex(payload)
        config = json.loads(payload)
        inner = config.get("config")
        if not isinstance(inner, dict):
            raise ValueError("execution policy is missing its config object")
        target = {
            "target_ca": inner["target_ca"],
            "chain_id": inner["chain_id"],
            "symbol": "LAPTOP" if inner["target_ca"] == Target().target_ca else "unverified",
            "max_fdv_usd_micros": inner.get("max_fdv_usd_micros"),
            "reference_supply": inner.get("reference_supply"),
            "token_decimals": inner.get("token_decimals")
            if inner.get("schema") == "oasis.launch-execution-config.v2"
            else 18,
            "buy_tranches_wei": inner.get("buy_tranches_wei"),
            "execution_mode": (
                "observe"
                if config.get("dry_run") is True
                else "live"
                if config.get("dry_run") is False
                else "unknown"
            ),
            "execution_authorized": config.get("launch_execution_authorized") is True,
        }
        previous = "0" * 64
        events: list[dict[str, Any]] = []
        for seq, kind, raw, digest in db.execute(
            "SELECT seq, kind, payload, digest FROM execution_events ORDER BY seq"
        ):
            record = json.loads(raw)
            if not isinstance(record, dict):
                raise ValueError("execution event must be an object")
            if (
                seq != len(events) + 1
                or record.get("seq") != seq
                or record.get("kind") != kind
                or record.get("previous") != previous
                or record.get("policy_digest") != policy_digest
                or sha256_hex(raw) != digest
                or canonical_json(record) != raw
            ):
                raise ValueError("execution journal chain corrupted")
            events.append(record | {"digest": digest})
            previous = digest
        return target, events


def launch_data(path: Path | None) -> tuple[dict[str, Any], list[dict[str, Any]], Mode]:
    if path is None:
        return Target().model_dump(mode="json"), [], "shadow"
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            db.execute("PRAGMA query_only=ON")
            tables = {
                str(row[0])
                for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
        if "execution_events" in tables:
            return (*_execution_journal(path), "execution")
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            records = read_records(db)
            row = db.execute("SELECT payload FROM launch_policy WHERE id=1").fetchone()
            target = Target.from_json(json.dumps(json.loads(row[0])["target"]))
            return target.model_dump(mode="json"), records, "shadow"
    except (sqlite3.Error, ValueError, KeyError, TypeError, UnicodeDecodeError) as exc:
        raise HTTPException(
            status_code=503, detail="launch journal unavailable or corrupt"
        ) from exc


LaunchData = tuple[dict[str, Any], list[dict[str, Any]], Mode]


def read_launch_snapshot(state: State) -> LaunchData:
    # A sync dependency runs file/SQLite verification off the async event loop.
    return launch_data(state.config.databases.launch)


Snapshot = Annotated[LaunchData, Depends(read_launch_snapshot)]


def envelope(
    items: list[dict[str, Any]], *, configured: bool, status: str | None = None
) -> dict[str, Any]:
    return {
        "schema": "oasis.launch-observation.v1",
        "execution_eligible": False,
        "dashboard_read_only": True,
        "trade_readiness": "not_verified",
        "evidence_level": "unresolved",
        "status": status or ("retained_only" if configured else "unconfigured"),
        "items": items,
        "total": len(items),
    }


def _execution_items(
    records: list[dict[str, Any]],
    endpoint: str,
    target: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Map live ExecutionJournal events to the launch tab table shapes."""
    if endpoint == "decisions":
        decision_items: list[dict[str, Any]] = []
        for record in records:
            reasons = [
                str(record[key])
                for key in ("reason", "error", "error_type")
                if record.get(key) is not None
            ]
            decision_items.append(
                {
                    "seq": record["seq"],
                    "observed_at": record.get("observed_at"),
                    "block": record.get("block"),
                    "pool": record.get("pool"),
                    "decision": record.get("kind", "event"),
                    "stand_down_reasons": reasons,
                    "digest": record["digest"],
                }
            )
        return decision_items
    if endpoint == "candidates":
        candidate_map: dict[str, dict[str, Any]] = {}
        for record in records:
            if record.get("kind") not in (
                "quote_observation",
                "quote_unavailable",
                "buy_skip",
                "buy_failed",
                "buy_dry_run",
            ):
                continue
            pool = record.get("pool")
            if not isinstance(pool, str) or not pool:
                continue
            item = candidate_map.setdefault(
                pool,
                {
                    "pool": pool,
                    "token0": "unverified",
                    "token1": "unverified",
                    "fee": record.get("fee"),
                    "snapshot": record.get("block"),
                    "gates": {},
                    "source_status": "live_journal",
                    "liquidity_source": "unresolved",
                    "withdrawal_authority": "unresolved",
                    "tradable": "unverified",
                    "digest": record["digest"],
                },
            )
            item.update(
                digest=record["digest"],
                snapshot=record.get("block"),
                last_event=record["kind"],
                last_seq=record.get("seq"),
                last_observed_at=record.get("observed_at"),
                gates={},
            )
            if record.get("kind") == "quote_observation":
                ok = record.get("fdv_ok") is True
                item["tradable"] = "fdv_ok" if ok else "fdv_blocked"
                item["gates"]["G5"] = {
                    "passed": ok,
                    "reasons": [] if ok else ["fdv_at_or_above_cap"],
                }
            elif record.get("kind") == "quote_unavailable":
                item["tradable"] = "unquoteable"
                item["gates"]["G7"] = {
                    "passed": False,
                    "reasons": [
                        str(record.get("error") or record.get("error_type") or "unavailable")
                    ],
                }
            elif record.get("kind") == "buy_failed":
                # buy_failed may be signing, gas estimation, allowance or receipt
                # failure. It is not sufficient evidence of missing liquidity.
                item["tradable"] = "attempt_failed"
                item["gates"]["attempt"] = {
                    "passed": False,
                    "reasons": [
                        str(record.get("error") or record.get("error_type") or "unavailable")
                    ],
                }
            elif record.get("kind") == "buy_skip":
                item["tradable"] = "entry_skipped"
                item["gates"]["entry"] = {
                    "passed": False,
                    "reasons": [str(record.get("reason") or "unverified")],
                }
        return list(candidate_map.values())
    if endpoint == "quotes":
        supply = target.get("reference_supply") if target else None
        cap = target.get("max_fdv_usd_micros") if target else None
        decimals = target.get("token_decimals") if target else None
        if type(supply) is not int or supply <= 0 or type(cap) is not int or cap <= 0:
            return []
        if type(decimals) is not int or not 0 <= decimals <= 77:
            return []
        quote_items: list[dict[str, Any]] = []
        for record in records:
            if record.get("kind") not in ("quote_observation", "buy_skip"):
                continue
            native = record.get("native_wei")
            received = record.get(
                "received_base" if record.get("kind") == "quote_observation" else "quote_base_units"
            )
            price = record.get("eth_usd_micros")
            if (
                type(native) is not int
                or type(received) is not int
                or type(price) is not int
                or native <= 0
                or received <= 0
                or price <= 0
            ):
                continue
            spend = native * price // 10**18
            numerator = native * price * supply * 10**decimals
            denominator = received * 10**18
            divisor = math.gcd(numerator, denominator)
            numerator //= divisor
            denominator //= divisor
            quote_items.append(
                {
                    "source": f"pool:{record.get('pool', '')[:12]}",
                    "spend_usd_micros": str(spend),
                    "native_wei": str(native),
                    "received_base_units": str(received),
                    "fdv_usd_micros_numerator": str(numerator),
                    "fdv_denominator": str(denominator),
                    "fdv_below_cap": numerator < cap * denominator,
                    "pool": record.get("pool"),
                    "snapshot": str(record.get("block")),
                    "valuation_status": "live_journal",
                    "observed_at": record.get("observed_at"),
                }
            )
        return quote_items
    return []


@router.get("/targets")
async def targets(state: State, snapshot: Snapshot) -> dict[str, Any]:
    target, records, mode = snapshot
    configured = state.config.databases.launch is not None
    if mode == "execution":
        halt = any(record.get("kind") == "halt_buys" for record in records)
        timestamps = [r["observed_at"] for r in records if type(r.get("observed_at")) is int]
        latest = max(timestamps) if timestamps else None
        fresh = latest is not None and 0 <= time.time() - latest <= 120
        cycle_records = [
            r for r in records if type(r.get("cycle")) is int and type(r.get("observed_at")) is int
        ]
        current_cycle = cycle_records[-1]["cycle"] if cycle_records else None
        cycle_times = []
        for record in reversed(cycle_records):
            if record["cycle"] != current_cycle:
                break
            cycle_times.append(record["observed_at"])
        item = target | {
            "current_cycle": current_cycle,
            "cycle_record_span_seconds": max(cycle_times) - min(cycle_times)
            if cycle_times
            else None,
            "server_time": int(time.time()),
            "activity_age_seconds": int(time.time()) - latest if latest is not None else None,
            "freshness_threshold_seconds": 120,
            "last_seq": records[-1]["seq"] if records else None,
            "watcher_status": "recent_journal_activity" if fresh else "stale_or_missing_activity",
            "control_status": "not_independently_verified",
            "stand_down": halt,
            "process_state": "not_verified_by_journal",
            "last_observed_at": latest,
            "last_event": records[-1]["kind"] if records else None,
            "buy_confirmed_count": sum(r["kind"] == "buy_confirmed" for r in records),
            "sell_confirmed_count": sum(r["kind"] == "sell_confirmed" for r in records),
            "latest_decision": records[-1]["digest"] if records else None,
        }
        return envelope([item], configured=configured, status="execution_journal")
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
        configured=configured,
    )


@router.get("/decisions")
async def decisions(
    state: State,
    snapshot: Snapshot,
    limit: int = Query(default=50, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    latest: bool = Query(default=False),
) -> dict[str, Any]:
    if limit > state.config.max_rows_per_page:
        raise HTTPException(status_code=422, detail="limit exceeds configured maximum")
    _, records, mode = snapshot
    configured = state.config.databases.launch is not None
    if latest:
        records = list(reversed(records))
    if mode == "execution":
        result = envelope(
            _execution_items(records[offset : offset + limit], "decisions"),
            configured=configured,
            status="execution_journal",
        )
    else:
        result = envelope(records[offset : offset + limit], configured=configured)
    result["total"] = len(records)
    result["next_seq"] = offset + limit + 1 if offset + limit < len(records) else None
    return result


@router.get("/candidates")
async def candidates(state: State, snapshot: Snapshot) -> dict[str, Any]:
    _, records, mode = snapshot
    configured = state.config.databases.launch is not None
    if mode == "execution":
        return envelope(
            _execution_items(records, "candidates"),
            configured=configured,
            status="execution_journal",
        )
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
    return envelope(list(items.values()), configured=configured)


@router.get("/quotes")
async def quotes(state: State, snapshot: Snapshot) -> dict[str, Any]:
    target, records, mode = snapshot
    configured = state.config.databases.launch is not None
    if mode == "execution":
        return envelope(
            _execution_items(records, "quotes", target),
            configured=configured,
            status="execution_journal",
        )
    items = []
    if records:
        for observation in records[-1]["input"]["observations"]:
            for quote in observation["quotes"]:
                numerator = (
                    quote["spend_usd_micros"]
                    * target["reference_supply"]
                    * 10 ** observation["token_decimals"]
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
                        < target["max_fdv_usd_micros"] * quote["received_base_units"],
                        "valuation_status": "imported_unverified",
                    }
                )
    return envelope(items, configured=configured)


@router.get("/approvals")
async def approvals(state: State, snapshot: Snapshot) -> dict[str, Any]:
    target, _, mode = snapshot
    configured = state.config.databases.launch is not None
    if mode == "execution":
        return envelope(
            [
                {
                    "target_ca": target["target_ca"],
                    "status": "unverified",
                    "reason": (
                        "execution journal configured; current on-chain allowances not verified"
                    ),
                }
            ],
            configured=configured,
            status="execution_journal",
        )
    return envelope(
        [
            {
                "target_ca": target["target_ca"],
                "status": "unresolved",
                "reason": "bounded allowance and native-buy path not verified",
            }
        ],
        configured=configured,
    )


@router.get("/dashboard")
async def dashboard(
    state: State,
    snapshot: Snapshot,
    include_legacy: bool = Query(default=False),
) -> dict[str, Any]:
    """All tables from one verified snapshot; filtering never edits recovery data."""
    target, records, mode = snapshot
    legacy = (
        [r for r in records if "details redacted" in str(r.get("error", ""))]
        if mode == "execution"
        else []
    )
    hidden = {r["seq"] for r in legacy} if not include_legacy else set()
    visible = [r for r in records if r["seq"] not in hidden]
    display_snapshot = (target, visible, mode)
    sections = {
        "targets": await targets(state, snapshot),
        "candidates": await candidates(state, snapshot),
        "quotes": await quotes(state, display_snapshot),
        "decisions": await decisions(
            state,
            display_snapshot,
            limit=min(50, state.config.max_rows_per_page),
            offset=0,
            latest=True,
        ),
        "approvals": await approvals(state, snapshot),
    }
    sections["quotes"]["items"] = sections["quotes"]["items"][-100:]
    sections["candidates"]["items"] = [
        item for item in sections["candidates"]["items"] if item.get("last_seq") not in hidden
    ]
    return {
        "sections": sections,
        "snapshot_seq": records[-1]["seq"] if records else None,
        "legacy_error_count": len(legacy),
        "hidden_legacy_count": len(hidden),
        "last_legacy_error_at": legacy[-1].get("observed_at") if legacy else None,
        "audit_records_deleted": False,
    }
