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

from .spot_executor import swap_call_bound

Record = dict[str, Any]
RESERVE, SETTLE = "budget_reserve", "budget_settle"


def full_cycle_calls(enrich_top: int, max_positions: int, calls_per_swap: int = 0) -> int:
    """Worst-case logical calls of one full cycle: hot + signals + enrich_top * (price,
    security, advanced, gmgn, 2 quotes) + per held position a sell quote and a security
    read + batched outcome price reads (at most MAX_PENDING / 40) + live swaps (every
    position sold and every slot bought)."""
    swaps = 2 * max_positions
    return 2 + enrich_top * 6 + max_positions * 2 + 50 + swaps * calls_per_swap


class BudgetLedgerError(ValueError):
    """The journal's budget events are malformed or contradictory; no budget is granted."""


def _count(value: object) -> int | None:
    """A non-negative int (never bool/float/str): the only valid call count."""
    return value if type(value) is int and value >= 0 else None


def budget_ledger(records: list[Record], legacy_error_charge: int) -> dict[str, Any]:
    """Logical calls charged to this run so far, and how much of it is confirmed.

    The charge is monotone over a valid journal: settling replaces a reservation's
    worst case with its (smaller or equal) confirmed usage, but no restart can forget
    spent calls or an unsettled reservation. Malformed or conflicting budget events
    raise BudgetLedgerError instead of silently becoming a lower charge.
    """
    first = next((i for i, r in enumerate(records) if r.get("kind") == RESERVE), len(records))
    legacy = records[:first]
    summaries = sum(int(r.get("provider_calls", 0)) for r in legacy if r["kind"] == "tick_summary")
    errors = sum(r.get("kind") == "tick_error" for r in legacy)
    stopped = max(
        (int(r.get("calls_used", 0)) for r in legacy if r.get("kind") == "budget_stop"),
        default=0,
    )
    legacy_charge = max(summaries + errors * legacy_error_charge, stopped)
    reserved: dict[str, int] = {}
    settled: dict[str, int] = {}
    # Every budget event is validated, including any before the first reservation: a
    # settlement there has nothing to settle and must not be silently ignored.
    for r in records:
        kind = r.get("kind")
        if kind not in (RESERVE, SETTLE):
            continue
        key = r.get("reserve_id")
        if not isinstance(key, str) or not key:
            raise BudgetLedgerError(f"{kind} without a reserve_id")
        if kind == RESERVE:
            amount = _count(r.get("reserved"))
            if amount is None:
                raise BudgetLedgerError("reservation is not a non-negative integer")
            if key in reserved and reserved[key] != amount:
                raise BudgetLedgerError("conflicting reservations for one reserve_id")
            reserved[key] = amount  # an identical repeat is idempotent
            continue
        used = _count(r.get("used"))
        if used is None:
            raise BudgetLedgerError("settled usage is not a non-negative integer")
        if key not in reserved:
            raise BudgetLedgerError("settlement without a prior reservation")
        if used > reserved[key]:
            # The reservation was meant to be a worst case; exceeding it breaks the
            # invariant and must be visible, not truncated to the lower number.
            raise BudgetLedgerError("settled usage exceeds its reservation")
        if key in settled and settled[key] != used:
            raise BudgetLedgerError("conflicting settlements for one reserve_id")
        settled[key] = used
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


def legacy_error_charge(policy: Record) -> int:
    """Worst-case logical calls of one legacy (unrecorded) failed tick for this run.

    Derived from the run's own journaled policy: its config and, for a live run, the
    executor's worst-case swap cost at the default settle attempts (the only setting the
    runner ever used). An unknown schema has no provable bound and is refused.
    """
    schema = policy.get("schema")
    config = policy.get("config") or {}
    enrich_top = _count(config.get("enrich_top", 8))
    max_positions = _count(config.get("max_positions", 3))
    if enrich_top is None or max_positions is None:
        raise BudgetLedgerError("run policy has no valid cycle bound")
    if schema == "oasis.spot-agent-shadow.v1":
        per_swap = 0
    elif schema in ("oasis.spot-agent-live.v1", "oasis.spot-agent-live.v2"):
        per_swap = swap_call_bound()  # the runner never overrides settle_attempts
    else:
        raise BudgetLedgerError("unknown run schema: no provable legacy bound")
    return full_cycle_calls(enrich_top, max_positions, per_swap)
