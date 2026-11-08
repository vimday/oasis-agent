"""Per-intent live order lifecycle, one run status for resume and report, legacy boundary."""

import itertools
from pathlib import Path
from typing import Any

import pytest

from oasis_agent.launch.spot_agent import AgentConfig, AgentState, reconcile_with_journal
from oasis_agent.launch.spot_lifecycle import (
    LEGACY_LIVE_SCHEMA,
    LIVE_SCHEMA,
    SHADOW_SCHEMA,
    reconcile_orders,
    run_status,
)
from oasis_agent.launch.spot_report import build_report, read_journal
from oasis_agent.sniping.journal import ExecutionJournal

TOKEN = "0x" + "ab" * 20
OTHER = "0x" + "cd" * 20
WALLET = "0x95a91b0afbc5fb3dbbffa1be8aaff5c14772a900"


GAS = 50_000
AMOUNT = {"buy": 20_000_000, "sell": 400_000}  # USDC base units in / token units in


def tx(n: int) -> str:
    return "0x" + f"{n:064x}"


def intent(key: str, side: str = "buy", token: str = TOKEN) -> dict[str, Any]:
    """Shape of LiveExecutor's live_intent (exact-input `amount` in the spent asset)."""
    return {
        "kind": "live_intent",
        "intent_id": key,
        "chain_id": 8453,
        "side": side,
        "token": token,
        "amount": str(AMOUNT[side]),
        "wallet": WALLET,
        "max_trade_usd_micros": 100_000_000,
        "slippage_bps": 100,
    }


def fill(key: str, txh: str, side: str = "buy", token: str = TOKEN) -> dict[str, Any]:
    """Shape of live_fill: intent fields plus Fill.record() with signed observed deltas."""
    usdc, units = (
        (-AMOUNT["buy"], AMOUNT["sell"])
        if side == "buy"
        else (
            30_000_000,
            -AMOUNT["sell"],
        )
    )
    return intent(key, side, token) | {
        "kind": "live_fill",
        "settlement": "unverified_balance_delta_plus_sender_receipt",
        "tx_hash": txh,
        "block_number": 123,
        "usdc_delta": str(usdc),
        "token_delta": str(units),
    }


def booking(key: str, txh: str, side: str = "buy", token: str = TOKEN) -> dict[str, Any]:
    """Shape of SpotAgent's live_buy/live_sell booking of that fill."""
    base = {
        "kind": f"live_{side}",
        "intent_id": key,
        "token": token,
        "tx_hash": txh,
        "block_number": 123,
        "units": str(AMOUNT["sell"]),
        "estimated_gas_usd_micros": GAS,
    }
    if side == "buy":
        return base | {
            "usdc_spent_base_units": AMOUNT["buy"],
            "cost_usd_micros": AMOUNT["buy"] + GAS,
        }
    return base | {
        "reason": "take_profit",
        "usdc_received_base_units": 30_000_000,
        "proceeds_usd_micros": 30_000_000 - GAS,
        "pnl_usd_micros": 30_000_000 - GAS - (AMOUNT["buy"] + GAS),
    }


def order(key: str, txh: str, side: str = "buy") -> list[dict[str, Any]]:
    return [intent(key, side), fill(key, txh, side), booking(key, txh, side)]


SUMMARY = {
    "kind": "tick_summary",
    "at": 1,
    "cash_usd_micros": 5,
    "nav_usd_micros": 5,
    "positions": [],
    "realized_pnl_usd_micros": 0,
    "closed_trades": 0,
    "winning_trades": 0,
    "provider_calls": 0,
}


def test_round_trip_settles_every_intent() -> None:
    records = [*order("a", tx(1)), *order("b", tx(2), "sell"), SUMMARY]
    ledger = reconcile_orders(records)
    assert ledger.settled == ("a", "b") and ledger.defects == ()
    status = run_status(LIVE_SCHEMA, records)
    assert status["resumable"] and status["unknown_live_orders"] == 0
    assert reconcile_with_journal(AgentState(0), records, LIVE_SCHEMA).cash_usd_micros == 5


@pytest.mark.parametrize(
    ("records", "reason"),
    [
        # Equal counts, but the fill/booking answer a different intent's tx.
        (
            [
                intent("a"),
                intent("b"),
                fill("a", tx(1)),
                fill("b", tx(2)),
                booking("a", tx(2)),
                booking("b", tx(1)),
            ],
            "booking_mismatch",
        ),
        # Equal counts, but the fill is for another token than the intent.
        ([intent("a"), fill("a", tx(1), token=OTHER), booking("a", tx(1))], "fill_mismatch"),
        ([intent("a"), fill("a", tx(1)), fill("a", tx(1)), booking("a", tx(1))], "duplicate_fill"),
        (
            [intent("a"), fill("a", tx(1)), booking("a", tx(1)), booking("a", tx(1))],
            "duplicate_booking",
        ),
        # A fill whose intent was never journaled cannot offset another open intent.
        ([intent("a"), fill("z", tx(1)), booking("z", tx(1))], "live_fill_without_intent"),
        ([intent("a"), fill("a", tx(1))], "live_fill_not_booked"),
        ([intent("a")], "live_intent_without_outcome"),
        ([intent("a"), intent("a") | {"kind": "live_unresolved"}], "live_order_unresolved"),
        ([intent("a") | {"intent_id": None}], "intent_without_id"),
    ],
)
def test_mismatched_lifecycles_block_resume_even_with_equal_counts(
    records: list[dict[str, Any]], reason: str
) -> None:
    records = [*records, SUMMARY]
    status = run_status(LIVE_SCHEMA, records)
    assert not status["resumable"] and reason in status["blocking_reasons"]
    with pytest.raises(ValueError, match=reason):
        reconcile_with_journal(AgentState(0), records, LIVE_SCHEMA)


def test_human_review_halt_blocks_without_any_unknown_order() -> None:
    records = [
        intent("a"),
        fill("a", tx(1)),
        booking("a", tx(1)),
        {"kind": "halt_review", "reason": "live_sell_refused", "token": TOKEN, "category": "x"},
        {"kind": "live_halt", "reasons": ["live_sell_refused"]},
        SUMMARY,
    ]
    status = run_status(LIVE_SCHEMA, records)
    assert status["unknown_live_orders"] == 0  # the old UI signal said "fine"
    assert not status["resumable"]
    assert {"human_review_halt", "live_halt_recorded"} <= set(status["blocking_reasons"])
    assert status["human_review"] == [{"token": TOKEN, "category": "x", "at": None}]


def test_transient_unpriceable_is_current_state_not_history_and_never_blocks() -> None:
    gap = {"kind": "halt_review", "reason": "exit_quote_unavailable", "token": TOKEN}
    old = run_status(SHADOW_SCHEMA, [gap, SUMMARY, SUMMARY])
    assert old["resumable"] and old["transient_unpriceable"] == [] and old["halts_total"] == 1
    now = run_status(SHADOW_SCHEMA, [SUMMARY, gap, SUMMARY])
    assert now["resumable"] and len(now["transient_unpriceable"]) == 1


def test_status_never_claims_a_running_process() -> None:
    assert run_status(SHADOW_SCHEMA, [SUMMARY])["process_state"] == "unknown_from_journal"


def live_journal(run: Path, schema: str, records: list[dict[str, Any]]) -> None:
    run.mkdir(parents=True)
    policy = {"schema": schema, "config": AgentConfig().model_dump(mode="json")}
    with ExecutionJournal(run / "journal.db", policy) as journal:
        for r in records:
            journal.append(r["kind"], r | {"execution_eligible": False})


def test_report_and_resume_share_one_verdict(tmp_path: Path) -> None:
    run = tmp_path / "run"
    start = {"kind": "live_start", "at": 1, "cash_usd_micros": 5}
    halt = {"kind": "halt_review", "reason": "live_sell_refused", "token": TOKEN}
    live_journal(
        run, LIVE_SCHEMA, [start, intent("a"), fill("a", tx(1)), booking("a", tx(1)), halt, SUMMARY]
    )
    report = build_report(run)
    assert report["run_status"]["resumable"] is False
    assert report["unresolved_live_orders"] == 0
    assert "human_review_halt" in report["run_status"]["blocking_reasons"]


def test_legacy_v1_live_journal_is_read_only_and_cash_is_not_relabelled(
    tmp_path: Path,
) -> None:
    # Shape of a v1 live run: no intent ids, cash already net of the declared gas estimate.
    run = tmp_path / "v1"
    start = {"kind": "live_start", "at": 1, "cash_usd_micros": 5}
    v1 = [start, {"kind": "live_intent"}, {"kind": "live_fill"}, {"kind": "live_buy"}, SUMMARY]
    live_journal(run, LEGACY_LIVE_SCHEMA, v1)
    report = build_report(run)
    status = report["run_status"]
    assert status["lifecycle"] == "live_v1_legacy" and status["resumable"] is False
    assert status["blocking_reasons"] == ["legacy_live_lifecycle_v1"]
    assert status["unknown_live_orders_basis"] == "count_only_legacy"
    assert report["accounting"]["cash_basis"] == "legacy_v1_cash_net_of_estimated_gas"
    with pytest.raises(ValueError, match="legacy_live_lifecycle_v1"):
        reconcile_with_journal(AgentState(0), v1, LEGACY_LIVE_SCHEMA)


def test_shadow_journal_without_new_fields_still_resumes() -> None:
    paper = [{"kind": "paper_buy"}, {"kind": "paper_sell"}, SUMMARY]
    state = reconcile_with_journal(AgentState(0), paper, SHADOW_SCHEMA)
    assert state.cash_usd_micros == 5 and state.estimated_gas_usd_micros == 0


def mutate(records: list[dict[str, Any]], kind: str, **change: Any) -> list[dict[str, Any]]:
    """Copy `records`, changing (or with value None, removing) fields on one record kind."""
    out = []
    for original in records:
        r = dict(original)
        if r["kind"] == kind:
            for key, value in change.items():
                if value is None:
                    r.pop(key, None)
                else:
                    r[key] = value
        out.append(r)
    return out


BUY = order("a", tx(1))
SELL = order("b", tx(2), "sell")


@pytest.mark.parametrize(
    ("records", "reason"),
    [
        # Identity: absent on both sides must not count as equal.
        (mutate(BUY, "live_intent", wallet=None), "intent_identity_invalid"),
        (
            mutate(mutate(BUY, "live_intent", wallet=None), "live_fill", wallet=None),
            "intent_identity_invalid",
        ),
        (mutate(BUY, "live_intent", chain_id=None), "intent_identity_invalid"),
        (mutate(BUY, "live_intent", chain_id=True), "intent_identity_invalid"),
        (mutate(BUY, "live_intent", token="0xABAB" + "ab" * 18), "intent_identity_invalid"),
        (mutate(BUY, "live_fill", wallet="0x" + "11" * 20), "fill_mismatch"),
        (mutate(BUY, "live_fill", tx_hash=None), "fill_tx_hash_invalid"),
        (mutate(BUY, "live_fill", tx_hash="0x12"), "fill_tx_hash_invalid"),
        # One transaction, two intents: neither is settled.
        ([*BUY, *order("b", tx(1))], "tx_reused_across_intents"),
        # Fill amounts against the exact-input intent.
        (mutate(BUY, "live_fill", usdc_delta="-19999999"), "fill_amount_mismatch"),
        (mutate(BUY, "live_fill", usdc_delta="20000000"), "fill_amount_mismatch"),
        (mutate(BUY, "live_fill", token_delta="0"), "fill_amount_mismatch"),
        (mutate(BUY, "live_fill", token_delta="1.5"), "fill_amount_invalid"),
        (mutate(BUY, "live_intent", amount=None), "fill_amount_invalid"),
        (mutate(SELL, "live_fill", token_delta="-399999"), "fill_amount_mismatch"),
        (mutate(SELL, "live_fill", usdc_delta="-1"), "fill_amount_mismatch"),
        # Booking amounts against the fill and the production cost/proceeds formula.
        (mutate(BUY, "live_buy", units="999"), "booking_amount_mismatch"),
        (mutate(BUY, "live_buy", usdc_spent_base_units=7), "booking_amount_mismatch"),
        (mutate(BUY, "live_buy", cost_usd_micros=AMOUNT["buy"]), "booking_amount_mismatch"),
        (mutate(BUY, "live_buy", estimated_gas_usd_micros=True), "booking_amount_invalid"),
        (mutate(BUY, "live_buy", estimated_gas_usd_micros=-1), "booking_amount_invalid"),
        (mutate(BUY, "live_buy", units=None), "booking_amount_invalid"),
        (mutate(SELL, "live_sell", usdc_received_base_units=29_999_999), "booking_amount_mismatch"),
        (mutate(SELL, "live_sell", proceeds_usd_micros=30_000_000), "booking_amount_mismatch"),
        (mutate(SELL, "live_sell", units="1"), "booking_amount_mismatch"),
    ],
)
def test_single_field_defects_are_refused_and_never_settled(
    tmp_path: Path, records: list[dict[str, Any]], reason: str
) -> None:
    run = tmp_path / "run"
    start = {"kind": "live_start", "at": 1, "cash_usd_micros": 5}
    live_journal(run, LIVE_SCHEMA, [start, *records, SUMMARY])
    retained = read_journal(run / "journal.db")[1]
    status = build_report(run)["run_status"]
    assert reason in status["blocking_reasons"] and status["resumable"] is False
    # A defective chain is never counted as settled (shared tx: neither intent is).
    assert status["settled_live_orders"] == 0
    with pytest.raises(ValueError, match=reason):
        reconcile_with_journal(AgentState(0), retained, LIVE_SCHEMA)


def test_unresolved_order_without_hash_stays_unknown_not_invalid() -> None:
    lost = intent("a") | {"kind": "live_unresolved", "category": "receipt_unavailable"}
    status = run_status(LIVE_SCHEMA, [intent("a"), lost, SUMMARY])
    assert status["blocking_reasons"] == ["live_order_unresolved"]
    assert status["unknown_live_orders"] == 1 and status["settled_live_orders"] == 0


def test_valid_buy_sell_round_trip_resumes_through_a_real_journal(tmp_path: Path) -> None:
    run = tmp_path / "run"
    live_journal(
        run,
        LIVE_SCHEMA,
        [{"kind": "live_start", "at": 1, "cash_usd_micros": 5}, *BUY, *SELL, SUMMARY],
    )
    status = build_report(run)["run_status"]
    assert status["resumable"] and status["settled_live_orders"] == 2


def test_duplicate_intent_is_that_intents_defect_not_a_global_reset() -> None:
    dup = [*BUY, intent("a"), *SELL, SUMMARY]
    status = run_status(LIVE_SCHEMA, dup)
    assert "duplicate_intent" in status["blocking_reasons"] and not status["resumable"]
    # The duplicated buy is not settled; the independent, valid sell still is.
    assert status["settled_live_orders"] == 1
    assert reconcile_orders(dup).settled == ("b",)


@pytest.mark.parametrize(
    ("kind", "reason"),
    [("live_intent", "intent_identity_invalid"), ("live_fill", "fill_mismatch")],
)
@pytest.mark.parametrize("side", [[], {}, ["buy"], None, 1, "BUY"])
def test_malformed_side_is_a_stable_refusal_not_a_type_error(
    tmp_path: Path, kind: str, reason: str, side: object
) -> None:
    run = tmp_path / "run"
    start = {"kind": "live_start", "at": 1, "cash_usd_micros": 5}
    bad = [r | {"side": side} if r["kind"] == kind else r for r in BUY]
    live_journal(run, LIVE_SCHEMA, [start, *bad, *SELL, SUMMARY])
    retained = read_journal(run / "journal.db")[1]
    report = build_report(run)  # never raises: report, API and console stay readable
    status = report["run_status"]
    assert reason in status["blocking_reasons"] and status["resumable"] is False
    # The malformed buy is never settled or charged; the independent sell still is.
    assert reconcile_orders(retained).settled == ("b",)
    fees = report["accounting"]["receipt_fees"]
    assert [r["intent_id"] for r in fees["rows"]] == ["b"]
    with pytest.raises(ValueError, match=reason):
        reconcile_with_journal(AgentState(0), retained, LIVE_SCHEMA)


def aliased(position: str) -> list[dict[str, Any]]:
    """Order a (good-looking) and order b, where b also carries a fill claiming a's tx.

    `position` places b's aliasing fill before ("first") or after ("last") b's own fill.
    A third, unrelated order c must be unaffected.
    """
    b_own, b_alias = fill("b", tx(2)), fill("b", tx(1))
    b_fills = [b_alias, b_own] if position == "first" else [b_own, b_alias]
    return [
        *order("a", tx(1)),
        intent("b"),
        *b_fills,
        booking("b", tx(2)),
        *order("c", tx(3), "sell"),
        SUMMARY,
    ]


@pytest.mark.parametrize("position", ["first", "last"])
def test_tx_claimed_by_two_intents_settles_neither_in_any_order(
    tmp_path: Path, position: str
) -> None:
    records = aliased(position)
    ledger = reconcile_orders(records)
    # Neither claimant of tx(1) is settled, whatever the row order; c is independent.
    assert ledger.settled == ("c",)
    assert "tx_reused_across_intents" in ledger.defects
    assert "duplicate_fill" in ledger.defects  # b's own duplicate stays its own defect
    run = tmp_path / "run"
    start = {"kind": "live_start", "at": 1, "cash_usd_micros": 5}
    live_journal(run, LIVE_SCHEMA, [start, *records])
    retained = read_journal(run / "journal.db")[1]
    report = build_report(run)
    status = report["run_status"]
    assert status["resumable"] is False and status["settled_live_orders"] == 1
    assert "tx_reused_across_intents" in status["blocking_reasons"]
    fees = report["accounting"]["receipt_fees"]
    assert [r["intent_id"] for r in fees["rows"]] == ["c"]
    assert fees["fills_not_attributed"] == 3  # a's fill and both of b's
    with pytest.raises(ValueError, match="tx_reused_across_intents"):
        reconcile_with_journal(AgentState(0), retained, LIVE_SCHEMA)


def test_tx_ownership_is_independent_of_row_order() -> None:
    # Every permutation of the three fills yields the same ledger verdict.
    base = aliased("last")
    fills = [r for r in base if r["kind"] == "live_fill"]
    rest = [r for r in base if r["kind"] != "live_fill"]
    verdicts = {
        (reconcile_orders([*rest[:4], *p, *rest[4:]]).settled)
        for p in itertools.permutations(fills)
    }
    assert verdicts == {("c",)}


def test_orphan_fill_claiming_a_tx_does_not_taint_the_order_that_owns_it() -> None:
    orphan = fill("ghost", tx(1))  # no live_intent named "ghost"
    ledger = reconcile_orders([*BUY, orphan, *SELL, SUMMARY])
    # The orphan is an explicit defect that blocks resume; it claims no ownership.
    assert "live_fill_without_intent" in ledger.defects
    assert ledger.settled == ("a", "b")
    assert run_status(LIVE_SCHEMA, [*BUY, orphan, SUMMARY])["resumable"] is False


def test_two_independent_orders_still_settle() -> None:
    ledger = reconcile_orders([*order("a", tx(1)), *order("b", tx(2)), SUMMARY])
    assert ledger.settled == ("a", "b") and ledger.defects == ()


@pytest.mark.parametrize("chain", [8453.0, True, "8453", 8454])
def test_chain_id_must_be_the_integer_8453(chain: object) -> None:
    records = [
        *mutate(mutate(BUY, "live_intent", chain_id=chain), "live_fill", chain_id=chain),
        SUMMARY,
    ]
    status = run_status(LIVE_SCHEMA, records)
    assert "intent_identity_invalid" in status["blocking_reasons"]
    assert status["settled_live_orders"] == 0
    assert run_status(LIVE_SCHEMA, [*BUY, SUMMARY])["resumable"] is True
