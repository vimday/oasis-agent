"""Run-scoped logical call budget, persisted as reserve/settle pairs in the run journal.

Unit: one logical provider query as counted by the sources' and executor's `calls`
(one CLI invocation the agent asked for). Transport retries inside `paced`, and the
read-only live preflight that runs before the journal is opened, are not counted.

Before any work that can call a provider the runner journals `budget_reserve` with the
worst case it may spend; afterwards it journals `budget_settle` with the calls it
actually made. On restart `budget_ledger` charges every settled amount, and every
reservation that was never settled at its full reserved amount: a process killed
mid-cycle may have spent up to that, and the true number is unknowable. The charge can
only grow; nothing here ever refunds or resets it.

Journals written before reservations existed are charged a conservative bound: their
tick summaries, plus a full worst-case cycle for every failed tick (whose calls were
never recorded), and never less than the last `budget_stop.calls_used` they journaled.
"""

from typing import Any

Record = dict[str, Any]
RESERVE, SETTLE = "budget_reserve", "budget_settle"


def full_cycle_calls(enrich_top: int, max_positions: int, calls_per_swap: int = 0) -> int:
    """Worst-case logical calls of one full cycle: hot + signals + enrich_top * (price,
    security, advanced, gmgn, 2 quotes) + per held position a sell quote and a security
    read + batched outcome price reads (at most MAX_PENDING / 40) + live swaps (every
    position sold and every slot bought)."""
    swaps = 2 * max_positions
    return 2 + enrich_top * 6 + max_positions * 2 + 50 + swaps * calls_per_swap


def budget_ledger(records: list[Record], legacy_error_charge: int) -> dict[str, Any]:
    """Logical calls charged to this run so far, and how much of it is confirmed."""
    first = next((i for i, r in enumerate(records) if r.get("kind") == RESERVE), len(records))
    legacy, current = records[:first], records[first:]
    summaries = sum(int(r.get("provider_calls", 0)) for r in legacy if r["kind"] == "tick_summary")
    errors = sum(r.get("kind") == "tick_error" for r in legacy)
    stopped = max(
        (int(r.get("calls_used", 0)) for r in legacy if r.get("kind") == "budget_stop"),
        default=0,
    )
    legacy_charge = max(summaries + errors * legacy_error_charge, stopped)
    reserved: dict[str, int] = {}
    settled: dict[str, int] = {}
    for r in current:
        key = r.get("reserve_id")
        if r.get("kind") == RESERVE and isinstance(key, str):
            reserved[key] = int(r["reserved"])
        elif r.get("kind") == SETTLE and isinstance(key, str) and key in reserved:
            settled.setdefault(key, int(r["used"]))  # first settle wins; never refunded
    confirmed = sum(settled.values())
    open_reserved = sum(v for k, v in reserved.items() if k not in settled)
    return {
        "unit": "logical_provider_query",
        "charged": legacy_charge + confirmed + open_reserved,
        "confirmed": confirmed,
        # Calls journaled as made: legacy tick summaries plus settled cycles. Excludes
        # the conservative legacy failure charge and unsettled reservations.
        "recorded": summaries + confirmed,
        # Reservations of interrupted work: charged in full, not confirmed calls.
        "unsettled_reserved": open_reserved,
        "unsettled_reservations": len(reserved) - len(settled),
        "legacy_charged": legacy_charge,
        "legacy_error_ticks": errors,
        "basis": "reserve_settle" if not legacy else "legacy_bound_plus_reserve_settle",
    }
