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
from .receipt_fee_contract import ITEM_STATES, read_fee_record
from .spot_budget import budget_ledger, legacy_error_charge
from .spot_lifecycle import reconcile_orders, run_status

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
    # Descriptive only. These are raw label rows: one token assessed in many cycles adds
    # many overlapping rows, and the flip and watch/skip groups are selected by the rule
    # itself, not randomized. A median gap here is not evidence of selection edge either
    # way; the frozen-sample experiment (spot_experiment) is the comparison to cite.
    verdict = "insufficient_label_rows"
    one_hour = observed.get(("flip", 3_600), [])
    baseline = observed.get(("watch", 3_600), []) + observed.get(("skip", 3_600), [])
    gap: int | None = None
    if len(one_hour) >= MIN_LABELS_FOR_VERDICT and len(baseline) >= MIN_LABELS_FOR_VERDICT:
        # Median, not mean: meme returns are heavy-tailed and one outlier must not decide.
        gap = int(median(one_hour)) - int(median(baseline))
        verdict = (
            "flip_median_higher_descriptive" if gap > 0 else "flip_median_not_higher_descriptive"
        )
    return {
        "by_horizon_seconds": table,
        "verdict_1h": verdict,
        "median_gap_1h_bps": gap,
        "verdict_basis": "descriptive_overlapping_label_rows_not_independent_not_causal",
    }


def paper_book(records: list[dict[str, Any]], initial_cash: int | None) -> dict[str, Any]:
    """Closed-trade PnL, drawdown over tick NAVs, and per-exit-reason breakdown.

    `initial_cash` None: a live book that was never observed (no live_start). Its cash,
    equity and drawdown are unknown and stay None; they never fall back to paper cash.
    """
    trades = [r for r in records if r["kind"] in ("paper_sell", "live_sell")]
    navs = [r["nav_usd_micros"] for r in records if r["kind"] == "tick_summary"]
    if initial_cash is None and navs:
        raise ValueError("unobserved book cannot have tick NAVs")
    peak, max_dd = initial_cash or 0, 0
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
        "max_drawdown_usd_micros": None if initial_cash is None else max_dd,
        "no_trade_baseline_pnl_usd_micros": 0,
        "by_exit_reason": {
            k: {"n": len(v), "pnl_usd_micros": sum(v)} for k, v in sorted(by_reason.items())
        },
    }


def budget_state(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Current resource state from journal order: is the latest progress a budget stop?

    A stop followed by a later completed cycle is history, not the current state. This
    says only what the journal last recorded; it never claims a process is running.
    """
    for r in reversed(records):
        if r["kind"] in ("budget_stop", "tick_summary"):
            # None when a record does not state it: unknown, never silently zero.
            return {
                "stopped": r["kind"] == "budget_stop",
                "at": r.get("at"),
                "open_positions": r.get("open_positions"),
            }
    return {"stopped": False, "at": None, "open_positions": None}


def operations(records: list[dict[str, Any]], legacy_error_charge: int) -> dict[str, Any]:
    ledger = budget_ledger(records, legacy_error_charge)
    ticks = [r for r in records if r["kind"] == "tick_summary"]
    failures = Counter(
        r.get("category", r.get("reason", "")) for r in records if r["kind"].endswith("_failed")
    )
    return {
        "ticks": len(ticks),
        "first_at": ticks[0]["at"] if ticks else None,
        "last_at": ticks[-1]["at"] if ticks else None,
        # Logical calls journaled as made, failed cycles included (see `budget` for the
        # conservative charge resume uses).
        "provider_calls": ledger["recorded"],
        "budget": ledger,
        "budget_state": budget_state(records),
        "halts": sum(r["kind"] == "halt_review" for r in records),
        "manage_only_ticks": sum(t.get("cycle") == "manage_only" for t in ticks),
        # Resource exhaustion is not a trading fault and never blocks resume; shown so the
        # operator knows why a run stopped and what it still held.
        # History: the most recent budget stop ever journaled, with what it held then.
        "last_budget_stop": next(
            (r for r in reversed(records) if r["kind"] == "budget_stop"), None
        ),
        "failures": dict(sorted(failures.items())),
        "kinds": dict(sorted(Counter(r["kind"] for r in records).items())),
    }


# Kept as the report's public name for the shared item states.
FEE_ITEM_STATES = ITEM_STATES


def receipt_fee_attribution(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Per journal-matched live order, the native fee items its own receipt stated.

    A fee record is counted only if capture could have written it (receipt_fee_contract:
    same source, in-width values, exact product); anything else is `record_invalid`.

    Only intents the lifecycle ledger settles (one fill, one booking, a tx hash used by
    no other intent) are attributed, so a transaction is never charged twice and an
    unproven or mismatched fill is never charged at all. Fills outside that set stay
    visible as a count. These are evidence items in wei: they are not deducted from the
    strategy book (which still carries the declared USD gas estimate), not converted to
    USD, and their sum is not the total native debit (the OP operator fee is uncaptured).
    """
    settled = set(reconcile_orders(records).settled)
    fills = [r for r in records if r.get("kind") == "live_fill"]
    rows: list[dict[str, Any]] = []
    for fill in fills:
        # The lifecycle's own id rule (a non-empty str), checked before the set lookup,
        # so an array/object id is an unattributed fill and never an unhashable crash.
        key = fill.get("intent_id")
        if not isinstance(key, str) or key not in settled:
            continue
        # The one shared reading (receipt_fee_contract.read_fee_record): cost replay
        # uses exactly the same, so the two can never rank a record differently.
        reading = read_fee_record(fill.get("receipt_fees"))
        execution, l1 = reading.execution, reading.l1
        rows.append(
            {
                "intent_id": fill["intent_id"],
                "tx_hash": fill.get("tx_hash"),
                "side": fill.get("side"),
                "block_number": fill.get("block_number"),
                "execution": execution.state,
                "execution_fee_wei": None if execution.wei is None else str(execution.wei),
                "l1_data": l1.state,
                "l1_data_fee_wei": None if l1.wei is None else str(l1.wei),
            }
        )

    def total(key: str, state: str) -> dict[str, Any]:
        observed = [int(r[key]) for r in rows if r[state] == "observed"]
        return {
            "observed_wei": str(sum(observed)),
            "observed_orders": len(observed),
            "by_state": {s: sum(r[state] == s for r in rows) for s in ITEM_STATES},
        }

    return {
        "basis": "settled_live_orders_own_receipt_items",
        # `settled` is the journal lifecycle match (intent, fill, booking, unique tx), not
        # a proof of funds settlement: fills are still balance delta plus sender receipt.
        "scope": "journal_matched_orders_only",
        "fund_settlement": "unverified_balance_delta_plus_sender_receipt",
        "unit": "native_wei",
        "settled_orders": len(rows),
        "fills_not_attributed": len(fills) - len(rows),
        "execution": total("execution_fee_wei", "execution"),
        "l1_data": total("l1_data_fee_wei", "l1_data"),
        "operator": "not_captured",
        "total_native_debit": "not_derived",
        "usd_value": "not_converted",
        "booked_in_strategy_pnl": False,
        "rows": rows,
    }


def build_report(run_dir: Path) -> dict[str, Any]:
    """Default report of a run: one verified journal read, then `report_from_snapshot`."""
    policy, records = read_journal(run_dir / "journal.db")
    return report_from_snapshot(policy, records)


def report_from_snapshot(policy: dict[str, Any], records: list[dict[str, Any]]) -> dict[str, Any]:
    """The report of exactly one hash-verified journal prefix (see `read_journal`).

    A caller that derives more views (e.g. a cost replay) passes them the same
    `records`, so a whole output reflects one journal prefix and never two reads.
    """
    status = run_status(policy.get("schema"), records)
    live = status["mode"] == "live"
    starts = [r for r in records if r["kind"] == "live_start"]
    if live and not starts and any(r["kind"] == "tick_summary" for r in records):
        raise ValueError("live journal has ticks but no live_start record")
    # A live book starts from the wallet's observed USDC, never the configured paper cash.
    # A live run stopped before that read (or before live_start was journaled) has an
    # unobserved book: its cash and equity are unknown, not the paper default.
    observed = bool(starts) or not live
    initial = (
        int(starts[0]["cash_usd_micros"])
        if starts
        else int(policy["config"]["initial_cash_usd_micros"])
        if not live
        else None
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
        # Same legacy bound resume uses, from this run's own journaled policy and mode.
        "operations": operations(records, legacy_error_charge(policy)),
        "signal_quality": signal_quality(records),
        "paper_book": paper_book(records, initial),
        "open_positions": summaries[-1].get("positions", []) if summaries else [],
        # Live: cash is the wallet's own USDC; gas is a declared per-leg estimate paid in
        # ETH (not receipt fees); equity is strategy equity, never full-account NAV.
        "accounting": {
            "cash_basis": (
                "unobserved_no_live_start"
                if not observed
                else "legacy_v1_cash_net_of_estimated_gas"
                if status["lifecycle"] == "live_v1_legacy"
                else "wallet_usdc_observed"
                if live
                else "paper"
            ),
            "estimated_gas_usd_micros": (
                int(summaries[-1].get("estimated_gas_usd_micros", 0)) if summaries else 0
            ),
            "gas_basis": "declared_per_leg_estimate_not_receipt_fees",
            # Separate native evidence from the same receipts; never mixed into the above.
            "receipt_fees": receipt_fee_attribution(records),
            "nav_scope": "strategy_equity_not_account_nav",
            "book_observed": observed,
            # open_positions lists what this run's strategy book recorded, never a wallet
            # inventory: an empty list does not mean the wallet holds nothing.
            "positions_scope": "strategy_book_not_wallet_inventory",
        },
    }


def recent(records: list[dict[str, Any]], kinds: frozenset[str], limit: int) -> list[Any]:
    return [r for r in reversed(records) if r["kind"] in kinds][:limit]
