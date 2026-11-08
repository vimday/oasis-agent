"""Read model and evaluation for a shadow spot run (pure; shared by API, console, CLI).

Reads the hash-verified journal through a read-only connection; a broken chain raises.
The evaluation answers the falsifiable question the shadow exists for: do `flip`
selections have better forward returns than `watch`/`skip`, and does the paper book
beat doing nothing after costs? Labels are provider spot claims; PnL is simulated.
"""

import json
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from pathlib import Path
from statistics import median
from typing import Any

from ..models.common import canonical_json, sha256_hex
from .spot_lifecycle import run_status

SCHEMA = "oasis.spot-agent-report.v1"
MIN_LABELS_FOR_VERDICT = 30


def read_journal(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Verify the full hash chain and policy binding with a read-only connection."""
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        row = db.execute("SELECT payload FROM execution_policy WHERE id=1").fetchone()
        if row is None:
            raise ValueError("spot journal has no policy")
        policy_raw = bytes(row[0])
        policy_digest = sha256_hex(policy_raw)
        previous: str = "0" * 64
        records: list[dict[str, Any]] = []
        for seq, kind, stored, digest in db.execute(
            "SELECT seq, kind, payload, digest FROM execution_events ORDER BY seq"
        ):
            raw = bytes(stored)
            record = json.loads(raw)
            if (
                not isinstance(record, dict)
                or seq != len(records) + 1
                or record.get("seq") != seq
                or record.get("kind") != kind
                or record.get("previous") != previous
                or record.get("policy_digest") != policy_digest
                or sha256_hex(raw) != digest
                or canonical_json(record) != raw
            ):
                raise ValueError("spot journal chain corrupted")
            if record.get("execution_eligible") is not False:
                raise ValueError("spot journal record claims execution authority")
            records.append(record)
            previous = digest
        return json.loads(policy_raw), records


def _bucket(values: list[int]) -> dict[str, Any]:
    if not values:
        return {"n": 0}
    ordered = sorted(values)
    wins = sum(v > 0 for v in values)
    return {
        "n": len(values),
        "mean_bps": sum(values) // len(values),
        "median_bps": int(median(ordered)),
        "p10_bps": ordered[len(ordered) // 10],
        "p90_bps": ordered[min(len(ordered) - 1, len(ordered) * 9 // 10)],
        "hit_rate_bps": wins * 10_000 // len(values),
    }


def signal_quality(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Forward-return distribution per decision action and horizon (observed labels only)."""
    observed: dict[tuple[str, int], list[int]] = defaultdict(list)
    unobserved: Counter[int] = Counter()
    for r in records:
        if r["kind"] != "outcome":
            continue
        if r["status"] == "observed":
            observed[r["action"], r["horizon_seconds"]].append(r["forward_return_bps"])
        else:
            unobserved[r["horizon_seconds"]] += 1
    horizons = sorted({h for _, h in observed} | set(unobserved))
    table = {
        str(h): {a: _bucket(observed[a, h]) for a in ("flip", "watch", "skip", "lp_candidate")}
        | {"unobserved": unobserved[h]}
        for h in horizons
    }
    verdict = "insufficient_samples"
    one_hour = observed.get(("flip", 3_600), [])
    baseline = observed.get(("watch", 3_600), []) + observed.get(("skip", 3_600), [])
    if len(one_hour) >= MIN_LABELS_FOR_VERDICT and len(baseline) >= MIN_LABELS_FOR_VERDICT:
        # Median, not mean: meme returns are heavy-tailed and one outlier must not decide.
        edge = int(median(one_hour)) - int(median(baseline))
        verdict = "flip_outperforms" if edge > 0 else "no_selection_edge"
    return {"by_horizon_seconds": table, "verdict_1h": verdict}


def paper_book(records: list[dict[str, Any]], initial_cash: int) -> dict[str, Any]:
    """Closed-trade PnL, drawdown over tick NAVs, and per-exit-reason breakdown."""
    trades = [r for r in records if r["kind"] in ("paper_sell", "live_sell")]
    navs = [r["nav_usd_micros"] for r in records if r["kind"] == "tick_summary"]
    peak, max_dd = initial_cash, 0
    for nav in navs:
        peak = max(peak, nav)
        max_dd = max(max_dd, peak - nav)
    by_reason: dict[str, list[int]] = defaultdict(list)
    for t in trades:
        by_reason[t["reason"]].append(t["pnl_usd_micros"])
    pnls = [t["pnl_usd_micros"] for t in trades]
    return {
        "initial_cash_usd_micros": initial_cash,
        "final_nav_usd_micros": navs[-1] if navs else initial_cash,
        "realized_pnl_usd_micros": sum(pnls),
        "closed_trades": len(trades),
        "winning_trades": sum(p > 0 for p in pnls),
        "max_drawdown_usd_micros": max_dd,
        "no_trade_baseline_pnl_usd_micros": 0,
        "by_exit_reason": {
            k: {"n": len(v), "pnl_usd_micros": sum(v)} for k, v in sorted(by_reason.items())
        },
    }


def operations(records: list[dict[str, Any]]) -> dict[str, Any]:
    ticks = [r for r in records if r["kind"] == "tick_summary"]
    failures = Counter(
        r.get("category", r.get("reason", "")) for r in records if r["kind"].endswith("_failed")
    )
    return {
        "ticks": len(ticks),
        "first_at": ticks[0]["at"] if ticks else None,
        "last_at": ticks[-1]["at"] if ticks else None,
        "provider_calls": sum(t["provider_calls"] for t in ticks),
        "halts": sum(r["kind"] == "halt_review" for r in records),
        "manage_only_ticks": sum(t.get("cycle") == "manage_only" for t in ticks),
        # Resource exhaustion is not a trading fault and never blocks resume; shown so the
        # operator knows why a run stopped and what it still held.
        "budget_stop": next((r for r in reversed(records) if r["kind"] == "budget_stop"), None),
        "failures": dict(sorted(failures.items())),
        "kinds": dict(sorted(Counter(r["kind"] for r in records).items())),
    }


def build_report(run_dir: Path) -> dict[str, Any]:
    policy, records = read_journal(run_dir / "journal.db")
    status = run_status(policy.get("schema"), records)
    live = status["mode"] == "live"
    starts = [r for r in records if r["kind"] == "live_start"]
    if live and not starts and any(r["kind"] == "tick_summary" for r in records):
        raise ValueError("live journal has ticks but no live_start record")
    # A live book starts from the wallet's observed USDC, never the configured paper cash.
    initial = (
        int(starts[0]["cash_usd_micros"])
        if starts
        else int(policy["config"]["initial_cash_usd_micros"])
    )
    summaries = [r for r in records if r["kind"] == "tick_summary"]
    return {
        "schema": SCHEMA,
        "mode": "live" if live else "shadow",
        # The same derivation resume uses: report, API and console cannot disagree with it.
        "run_status": status,
        "unresolved_live_orders": status["unknown_live_orders"],
        "execution_eligible": False,
        "evidence_level": "provider_claims_not_chain_attestations",
        "policy_sha256": sha256_hex(canonical_json(policy)),
        "operations": operations(records),
        "signal_quality": signal_quality(records),
        "paper_book": paper_book(records, initial),
        "open_positions": summaries[-1].get("positions", []) if summaries else [],
        # Live: cash is the wallet's own USDC; gas is a declared per-leg estimate paid in
        # ETH (not receipt fees); equity is strategy equity, never full-account NAV.
        "accounting": {
            "cash_basis": (
                "legacy_v1_cash_net_of_estimated_gas"
                if status["lifecycle"] == "live_v1_legacy"
                else "wallet_usdc_observed"
                if live
                else "paper"
            ),
            "estimated_gas_usd_micros": (
                int(summaries[-1].get("estimated_gas_usd_micros", 0)) if summaries else 0
            ),
            "gas_basis": "declared_per_leg_estimate_not_receipt_fees",
            "nav_scope": "strategy_equity_not_account_nav",
        },
    }


def recent(records: list[dict[str, Any]], kinds: frozenset[str], limit: int) -> list[Any]:
    return [r for r in reversed(records) if r["kind"] in kinds][:limit]
