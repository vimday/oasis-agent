"""Live order lifecycle and run status: one read-only derivation for resume and reporting.

Every submitted swap journals one `live_intent` carrying a unique `intent_id` before it
is handed to the wallet. A proven swap adds one `live_fill` with that id, and the agent
books exactly one `live_buy`/`live_sell` with that id and the fill's tx hash. An unproven
one adds `live_unresolved` with that id. Matching is by id and checked field by field;
counts are never enough, and nothing is matched by position.

`run_status` is the single answer to "may this run resume trading, and if not, why".
`reconcile_with_journal` (resume) refuses exactly when it says `resumable` is False, and
the report/API/console render the same object, so they cannot disagree. A process's
liveness is not derivable from a journal; the status never claims a run is running.
"""

import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

Record = dict[str, Any]

SHADOW_SCHEMA = "oasis.spot-agent-shadow.v1"
# v2: intent ids on every lifecycle record; cash is wallet USDC only, gas tracked apart.
LIVE_SCHEMA = "oasis.spot-agent-live.v2"
# v1 live runs deducted the declared gas from cash and carried no intent ids, so neither
# their cash nor their order chain can be reinterpreted under v2. Read-only, never resumed.
LEGACY_LIVE_SCHEMA = "oasis.spot-agent-live.v1"
LIVE_SCHEMAS = frozenset({LIVE_SCHEMA, LEGACY_LIVE_SCHEMA})

TRADE_KINDS = frozenset({"paper_buy", "paper_sell", "live_buy", "live_sell"})
LIFECYCLE_KINDS = frozenset({"live_intent", "live_fill", "live_unresolved"})
# Any of these after the last tick summary means money may have moved unsummarized.
INTERRUPTING_KINDS = TRADE_KINDS | LIFECYCLE_KINDS
# Halt reasons that need a human: the wallet may hold something the agent cannot exit.
HUMAN_REVIEW_REASONS = frozenset({"live_sell_refused"})
# Halt reasons that are a transient observation gap; the agent keeps observing.
TRANSIENT_HALT_REASONS = frozenset({"exit_quote_unavailable"})
LIVE_HALT = "live_halt"
BOOKING_KIND = {"buy": "live_buy", "sell": "live_sell"}


@dataclass(frozen=True, slots=True)
class OrderLedger:
    """Per-intent reconciliation of one journal's live order chain."""

    intents: int
    settled: tuple[str, ...]  # one consistent fill and one consistent booking
    unresolved: tuple[str, ...]  # explicit live_unresolved: outcome unknown
    missing_outcome: tuple[str, ...]  # neither fill nor unresolved (e.g. a crash)
    unbooked: tuple[str, ...]  # proven fill the book never recorded
    defects: tuple[str, ...]  # structural errors: ids, duplicates, mismatches

    @property
    def unknown_orders(self) -> int:
        return len(self.unresolved) + len(self.missing_outcome)


def _id(record: Record) -> str | None:
    value = record.get("intent_id")
    return value if isinstance(value, str) and value else None


def reconcile_orders(records: list[Record]) -> OrderLedger:
    defects: set[str] = set()
    intents: dict[str, Record] = {}
    duplicated: set[str] = set()
    for r in records:
        if r.get("kind") != "live_intent":
            continue
        if (key := _id(r)) is None:
            defects.add("intent_without_id")
        elif key in intents:
            # Attributed to that intent: it cannot also count as settled.
            duplicated.add(key)
        else:
            intents[key] = r

    def bound(kind: str) -> dict[str, list[Record]]:
        grouped: dict[str, list[Record]] = {}
        for r in records:
            if r.get("kind") != kind:
                continue
            key = _id(r)
            if key is None or key not in intents:
                defects.add(f"{kind}_without_intent")
            else:
                grouped.setdefault(key, []).append(r)
        return grouped

    fills, unresolved = bound("live_fill"), bound("live_unresolved")
    bookings: dict[str, list[Record]] = {}
    for kind in BOOKING_KIND.values():
        for key, rows in bound(kind).items():
            bookings.setdefault(key, []).extend(rows)

    settled: list[str] = []
    open_unresolved: list[str] = []
    missing: list[str] = []
    unbooked: list[str] = []
    # One swap is one transaction: a hash claimed by more than one intent proves none of
    # them. Ownership is built from EVERY fill bound to a known intent (not only each
    # intent's first), as sets, so neither a second fill nor row order can let one of
    # the claimants through. Orphan fills are already `live_fill_without_intent` defects
    # and claim nothing here, so they cannot taint an unrelated order.
    tx_owners: dict[str, set[str]] = {}
    for key, rows in fills.items():
        for f in rows:
            if (claimed := _hash(f.get("tx_hash"))) is not None:
                tx_owners.setdefault(claimed, set()).add(key)
    shared = {key for owners in tx_owners.values() if len(owners) > 1 for key in owners}
    for key, intent in intents.items():
        got, lost, booked = fills.get(key, []), unresolved.get(key, []), bookings.get(key, [])
        problems: set[str] = set()
        if key in duplicated:
            problems.add("duplicate_intent")
        if len(got) > 1:
            problems.add("duplicate_fill")
        if len(booked) > 1:
            problems.add("duplicate_booking")
        if got and lost:
            problems.add("fill_and_unresolved")
        identity = _identity(intent)
        if identity is None:
            problems.add("intent_identity_invalid")
        if not got:
            if booked:
                problems.add("booking_without_fill")
            # An unresolved order legitimately has no hash: it stays unknown and blocks.
            (open_unresolved if lost else missing).append(key)
            defects.update(problems)
            continue
        fill = got[0]
        tx = _hash(fill.get("tx_hash"))
        if tx is None:
            problems.add("fill_tx_hash_invalid")
        if key in shared:
            problems.add("tx_reused_across_intents")
        if identity is not None and _identity(fill) != identity:
            problems.add("fill_mismatch")
        problems.update(_fill_amount_problems(intent, fill))
        if not booked:
            unbooked.append(key)
            defects.update(problems)
            continue
        problems.update(_booking_problems(intent, fill, booked[0]))
        if problems:
            defects.update(problems)
        else:
            # Only a chain with no defect at all counts as settled.
            settled.append(key)
    return OrderLedger(
        intents=len(intents),
        settled=tuple(settled),
        unresolved=tuple(open_unresolved),
        missing_outcome=tuple(missing),
        unbooked=tuple(unbooked),
        defects=tuple(sorted(defects)),
    )


_ADDRESS = re.compile(r"^0x[0-9a-f]{40}$")
_TX = re.compile(r"^0x[0-9a-f]{64}$")
_INT = re.compile(r"^-?(0|[1-9][0-9]{0,77})$")


def _address(value: object) -> str | None:
    """Canonical lowercase address, or None; never treats two absent values as equal."""
    return value if isinstance(value, str) and _ADDRESS.fullmatch(value) else None


def _hash(value: object) -> str | None:
    return value if isinstance(value, str) and _TX.fullmatch(value) else None


def _integer(value: object) -> int | None:
    """An exact integer written as a canonical decimal string or a plain int; never bool."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return int(value) if isinstance(value, str) and _INT.fullmatch(value) else None


def _identity(record: Record) -> tuple[str, str, str, int] | None:
    """(side, token, wallet, chain_id) when every field is present and well formed."""
    side, chain = record.get("side"), record.get("chain_id")
    token, wallet = _address(record.get("token")), _address(record.get("wallet"))
    # Type first: a JSON array/object side is unhashable and must be an invalid identity,
    # never a TypeError from the dict lookup.
    if type(side) is not str or side not in BOOKING_KIND or token is None or wallet is None:
        return None
    # Exactly the int 8453: 8453.0 and True compare equal under `==` but are not chain ids.
    if type(chain) is not int or chain != 8453:
        return None
    return str(side), token, wallet, 8453


def _fill_amount_problems(intent: Record, fill: Record) -> set[str]:
    """The fill spent exactly the intent's exact-input amount and received something."""
    amount = _integer(intent.get("amount"))
    usdc, units = _integer(fill.get("usdc_delta")), _integer(fill.get("token_delta"))
    if amount is None or amount <= 0 or usdc is None or units is None:
        return {"fill_amount_invalid"}
    spent, received = (-usdc, units) if intent.get("side") == "buy" else (-units, usdc)
    return set() if spent == amount and received > 0 else {"fill_amount_mismatch"}


def _booking_problems(intent: Record, fill: Record, booking: Record) -> set[str]:
    """The book recorded exactly what the fill observed, under the production formula."""
    side = intent.get("side")
    if (
        booking.get("kind") != BOOKING_KIND.get(str(side))
        or _address(booking.get("token")) != _address(intent.get("token"))
        or _hash(booking.get("tx_hash")) != _hash(fill.get("tx_hash"))
    ):
        return {"booking_mismatch"}
    usdc, units = _integer(fill.get("usdc_delta")), _integer(fill.get("token_delta"))
    gas = _integer(booking.get("estimated_gas_usd_micros"))
    booked_units = _integer(booking.get("units"))
    if usdc is None or units is None or gas is None or gas < 0 or booked_units is None:
        return {"booking_amount_invalid"}
    if side == "buy":
        spent, cost = (
            _integer(booking.get("usdc_spent_base_units")),
            _integer(booking.get("cost_usd_micros")),
        )
        # cost basis = observed USDC spent + declared gas estimate (paid in ETH)
        ok = spent == -usdc and booked_units == units and cost == spent + gas
    else:
        received = _integer(booking.get("usdc_received_base_units"))
        proceeds = _integer(booking.get("proceeds_usd_micros"))
        # proceeds = observed USDC received - declared gas estimate
        ok = received == usdc and booked_units == -units and proceeds == received - gas
    return set() if ok else {"booking_amount_mismatch"}


def _last_tick_window(records: list[Record]) -> list[Record]:
    """Records of the last completed tick: after the previous summary, up to the last."""
    summaries = [i for i, r in enumerate(records) if r.get("kind") == "tick_summary"]
    if not summaries:
        return []
    start = summaries[-2] + 1 if len(summaries) > 1 else 0
    return records[start : summaries[-1]]


def run_status(schema: str | None, records: list[Record]) -> dict[str, Any]:
    """Whether a run may resume trading, and every reason it may not.

    `schema` is the journal policy's schema; None means the current live lifecycle.
    """
    kinds = Counter(str(r.get("kind")) for r in records)
    legacy = schema == LEGACY_LIVE_SCHEMA
    live = schema in LIVE_SCHEMAS or any(kinds[k] for k in LIFECYCLE_KINDS | {LIVE_HALT})
    blocking: set[str] = set()
    ledger = reconcile_orders(records)
    if legacy:
        blocking.add("legacy_live_lifecycle_v1")
        # No intent ids: only a count is available, and it is labelled as such.
        unknown = max(kinds["live_intent"] - kinds["live_fill"], kinds["live_unresolved"])
    else:
        unknown = ledger.unknown_orders
        blocking.update(ledger.defects)
        if ledger.missing_outcome:
            blocking.add("live_intent_without_outcome")
        if ledger.unresolved:
            blocking.add("live_order_unresolved")
        if ledger.unbooked:
            blocking.add("live_fill_not_booked")
    if kinds[LIVE_HALT]:
        blocking.add("live_halt_recorded")
    human = [
        {"token": r.get("token"), "category": r.get("category"), "at": r.get("at")}
        for r in records
        if r.get("kind") == "halt_review" and r.get("reason") in HUMAN_REVIEW_REASONS
    ]
    if human:
        blocking.add("human_review_halt")
    summaries = [i for i, r in enumerate(records) if r.get("kind") == "tick_summary"]
    tail = records[summaries[-1] + 1 :] if summaries else records
    if any(r.get("kind") in INTERRUPTING_KINDS for r in tail):
        blocking.add("interrupted_tick" if summaries else "trades_without_tick_summary")
    transient = [
        {"token": r.get("token"), "missed_marks": r.get("missed_marks"), "kind": r.get("kind")}
        for r in _last_tick_window(records)
        if r.get("kind") in ("mark_missed", "halt_review")
        and r.get("reason") in TRANSIENT_HALT_REASONS
    ]
    return {
        "mode": "live" if live else "shadow",
        "lifecycle": "live_v1_legacy" if legacy else ("live_v2" if live else "paper"),
        "resumable": not blocking,
        "blocking_reasons": sorted(blocking),
        "unknown_live_orders": unknown,
        "unknown_live_orders_basis": "count_only_legacy" if legacy else "per_intent",
        "settled_live_orders": 0 if legacy else len(ledger.settled),
        "human_review": human,
        # Current observation gaps (last completed tick only); never blocking on their own.
        "transient_unpriceable": transient,
        "halts_total": kinds["halt_review"],
        "last_tick_at": records[summaries[-1]].get("at") if summaries else None,
        # A journal cannot prove a process is alive; only the operator's process can.
        "process_state": "unknown_from_journal",
    }
