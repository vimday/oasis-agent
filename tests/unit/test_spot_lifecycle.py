"""Per-intent live order lifecycle, one run status for resume and report, legacy boundary."""

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
from oasis_agent.launch.spot_report import build_report
from oasis_agent.sniping.journal import ExecutionJournal

TOKEN = "0x" + "ab" * 20
OTHER = "0x" + "cd" * 20
WALLET = "0x95a91b0afbc5fb3dbbffa1be8aaff5c14772a900"


def intent(key: str, side: str = "buy", token: str = TOKEN) -> dict[str, Any]:
    return {
        "kind": "live_intent",
        "intent_id": key,
        "side": side,
        "token": token,
        "wallet": WALLET,
        "chain_id": 8453,
    }


def fill(key: str, tx: str, side: str = "buy", token: str = TOKEN) -> dict[str, Any]:
    return intent(key, side, token) | {"kind": "live_fill", "tx_hash": tx}


def booking(key: str, tx: str, side: str = "buy", token: str = TOKEN) -> dict[str, Any]:
    return {"kind": f"live_{side}", "intent_id": key, "token": token, "tx_hash": tx}


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
    records = [
        intent("a"),
        fill("a", "0x1"),
        booking("a", "0x1"),
        intent("b", "sell"),
        fill("b", "0x2", "sell"),
        booking("b", "0x2", "sell"),
        SUMMARY,
    ]
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
                fill("a", "0x1"),
                fill("b", "0x2"),
                booking("a", "0x2"),
                booking("b", "0x1"),
            ],
            "booking_mismatch",
        ),
        # Equal counts, but the fill is for another token than the intent.
        ([intent("a"), fill("a", "0x1", token=OTHER), booking("a", "0x1")], "fill_mismatch"),
        ([intent("a"), fill("a", "0x1"), fill("a", "0x1"), booking("a", "0x1")], "duplicate_fill"),
        (
            [intent("a"), fill("a", "0x1"), booking("a", "0x1"), booking("a", "0x1")],
            "duplicate_booking",
        ),
        # A fill whose intent was never journaled cannot offset another open intent.
        ([intent("a"), fill("z", "0x1"), booking("z", "0x1")], "live_fill_without_intent"),
        ([intent("a"), fill("a", "0x1")], "live_fill_not_booked"),
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
        fill("a", "0x1"),
        booking("a", "0x1"),
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
        run, LIVE_SCHEMA, [start, intent("a"), fill("a", "0x1"), booking("a", "0x1"), halt, SUMMARY]
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
