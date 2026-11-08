"""Receipt native-fee items: same receipt, settled orders only, evidence never booked.

Synthetic RPC responses only, through the historical fixture path: LiveExecutor with
rpc_receipt_reader -> run_loop journal -> build_report. No network, wallet or signer.
"""

from pathlib import Path
from typing import Any

import pytest

from oasis_agent.launch.spot_executor import (
    APPROVED_WALLET,
    receipt_fees,
)
from oasis_agent.launch.spot_report import build_report, read_journal, receipt_fee_attribution

from .spot_legacy_fixture import LiveExecutor, rpc_receipt_reader
from .test_spot_agent import NOW, FakeCli
from .test_spot_executor import FakeWallet, gate
from .test_spot_runner import open_live, run_live

GAS, PRICE, L1 = 210_000, 5_000_000, 1_234_567


def mined(tx: str, **fields: Any) -> dict[str, Any]:
    return {
        "transactionHash": tx,
        "status": "0x1",
        "blockNumber": "0x7b",
        "from": APPROVED_WALLET,
        "gasUsed": hex(GAS),
        "effectiveGasPrice": hex(PRICE),
        "l1Fee": hex(L1),
    } | fields


class Transport:
    """One scripted eth_getTransactionReceipt answer per hash; counts every request."""

    def __init__(self, **fields: Any) -> None:
        self.fields, self.calls = fields, 0

    def request(self, method: str, params: list[Any]) -> Any:
        assert method == "eth_getTransactionReceipt"
        self.calls += 1
        raw = mined(params[0], **self.fields)
        return {k: v for k, v in raw.items() if v is not None}  # None deletes a field


def live_run(tmp_path: Path, receipt: Any) -> tuple[Path, FakeWallet]:
    run = tmp_path / "run"
    wallet = FakeWallet(usdc=1_000_000_000)
    wallet.fill_out = 200_000_000_000
    with open_live(run) as side:
        executor = LiveExecutor(
            gate(),
            read=wallet.read,
            submit=wallet.submit,
            receipt=receipt,
            emit=side.append,
            clock=lambda: NOW,
            sleep=lambda _s: None,
            settle_attempts=1,
        )
        assert run_live(run, executor, FakeCli(), ticks=1) == 0
    return run, wallet


def test_settled_order_carries_its_own_receipt_fee_items(tmp_path: Path) -> None:
    transport = Transport()
    run, wallet = live_run(tmp_path, rpc_receipt_reader(transport))
    # No extra provider call: one receipt request for the one submitted swap.
    assert len(wallet.submits) == 1 and transport.calls == 1
    report = build_report(run)
    fees = report["accounting"]["receipt_fees"]
    assert fees["settled_orders"] == 1 and fees["fills_not_attributed"] == 0
    (row,) = fees["rows"]
    assert row["execution"] == "observed" and row["execution_fee_wei"] == str(GAS * PRICE)
    assert row["l1_data"] == "observed" and row["l1_data_fee_wei"] == str(L1)
    assert fees["execution"]["observed_wei"] == str(GAS * PRICE)
    # Separate evidence: no USD, no total debit, no booking; the strategy book still
    # carries the declared estimate and the observed USDC only.
    assert fees["operator"] == "not_captured" and fees["total_native_debit"] == "not_derived"
    assert fees["usd_value"] == "not_converted" and fees["booked_in_strategy_pnl"] is False
    # Attribution scope is the journal match; funds settlement is not claimed as proven.
    assert fees["scope"] == "journal_matched_orders_only"
    assert fees["fund_settlement"] == "unverified_balance_delta_plus_sender_receipt"
    assert report["accounting"]["gas_basis"] == "declared_per_leg_estimate_not_receipt_fees"
    assert report["run_status"]["settled_live_orders"] == 1
    fill = next(r for r in read_journal(run / "journal.db")[1] if r["kind"] == "live_fill")
    assert fill["receipt_fees"]["execution"]["gas_used"] == str(GAS)


@pytest.mark.parametrize(
    ("fields", "execution", "l1"),
    [
        ({"gasUsed": None}, "missing", "observed"),
        ({"effectiveGasPrice": "0x01"}, "invalid", "observed"),  # non-canonical quantity
        ({"gasUsed": 210000}, "invalid", "observed"),  # JSON number, not a hex quantity
        ({"gasUsed": hex(1 << 64)}, "invalid", "observed"),  # wider than uint64
        ({"l1Fee": None}, "observed", "missing"),
        ({"l1Fee": "0xZZ"}, "observed", "invalid"),
    ],
)
def test_missing_or_malformed_items_never_block_a_proven_fill(
    tmp_path: Path, fields: dict[str, Any], execution: str, l1: str
) -> None:
    run, wallet = live_run(tmp_path, rpc_receipt_reader(Transport(**fields)))
    report = build_report(run)
    # The fill is still proven and booked, nothing was resubmitted, nothing reads as zero.
    assert len(wallet.submits) == 1 and report["run_status"]["resumable"] is False
    (row,) = report["accounting"]["receipt_fees"]["rows"]
    assert (row["execution"], row["l1_data"]) == (execution, l1)
    if execution != "observed":
        assert row["execution_fee_wei"] is None
        assert report["accounting"]["receipt_fees"]["execution"]["observed_orders"] == 0
    if l1 != "observed":
        assert row["l1_data_fee_wei"] is None


def test_legacy_three_field_reader_and_old_logs_stay_not_captured(tmp_path: Path) -> None:
    run, _ = live_run(tmp_path, lambda _tx: (1, 123, APPROVED_WALLET))
    fees = build_report(run)["accounting"]["receipt_fees"]
    (row,) = fees["rows"]
    assert row["execution"] == row["l1_data"] == "not_captured"
    assert fees["execution"]["observed_wei"] == "0" and fees["execution"]["observed_orders"] == 0
    # A fill journaled before this field existed has no `receipt_fees` at all.
    records = read_journal(run / "journal.db")[1]
    old = [{k: v for k, v in r.items() if k != "receipt_fees"} for r in records]
    assert receipt_fee_attribution(old)["rows"][0]["execution"] == "not_captured"


def test_only_settled_orders_are_attributed_and_never_twice(tmp_path: Path) -> None:
    run, _ = live_run(tmp_path, rpc_receipt_reader(Transport()))
    records = read_journal(run / "journal.db")[1]
    fill = next(r for r in records if r["kind"] == "live_fill")
    # A duplicated fill makes the intent defective: neither copy is charged.
    duplicated = receipt_fee_attribution([*records, fill])
    assert duplicated["settled_orders"] == 0 and duplicated["fills_not_attributed"] == 2
    # A fill whose identity does not match its intent is not charged.
    other = "0x" + "e1" * 20
    assert fill["token"] != other
    foreign = [r | {"token": other} if r is fill else r for r in records]
    assert receipt_fee_attribution(foreign)["settled_orders"] == 0
    # A fill with no intent at all is visible as unattributed, never charged.
    orphan = receipt_fee_attribution([*records, fill | {"intent_id": "nobody"}])
    assert orphan["settled_orders"] == 1 and orphan["fills_not_attributed"] == 1
    assert orphan["execution"]["observed_wei"] == str(GAS * PRICE)


def test_tampered_fee_record_is_not_evidence(tmp_path: Path) -> None:
    run, _ = live_run(tmp_path, rpc_receipt_reader(Transport()))
    records = read_journal(run / "journal.db")[1]

    def with_fees(fees: Any) -> list[dict[str, Any]]:
        return [r | {"receipt_fees": fees} if r["kind"] == "live_fill" else r for r in records]

    fill = next(r for r in records if r["kind"] == "live_fill")
    good = fill["receipt_fees"]
    wrong_product = good | {"execution": good["execution"] | {"fee_wei": "1"}}
    for fees in (wrong_product, "x", {"status": "other"}, good | {"execution": 1}):
        (row,) = receipt_fee_attribution(with_fees(fees))["rows"]
        assert row["execution"] == "record_invalid" and row["execution_fee_wei"] is None


def test_receipt_fees_parser_is_total_and_never_raises() -> None:
    for raw in ({}, {"gasUsed": True}, {"gasUsed": None, "l1Fee": []}, {"l1Fee": hex(1 << 256)}):
        fees = receipt_fees(raw)
        assert fees["execution"]["status"] in ("missing", "invalid")
        assert fees["l1_data"]["status"] in ("missing", "invalid")
    zero = receipt_fees({"gasUsed": "0x0", "effectiveGasPrice": "0x0"})
    assert zero["execution"]["fee_wei"] == "0"  # an observed zero is a value, not a default


@pytest.mark.parametrize("bad_id", [[], {}, 7, None, ""])
def test_malformed_fill_id_is_unattributed_and_the_report_still_reads(
    tmp_path: Path, bad_id: Any
) -> None:
    run, _ = live_run(tmp_path, rpc_receipt_reader(Transport()))
    records = read_journal(run / "journal.db")[1]
    fill = next(r for r in records if r["kind"] == "live_fill")
    fees = receipt_fee_attribution([*records, fill | {"intent_id": bad_id}])
    # No unhashable crash; the real settled order keeps its fee, the stray fill is visible.
    assert fees["settled_orders"] == 1 and fees["fills_not_attributed"] == 1
    assert fees["execution"]["observed_wei"] == str(GAS * PRICE)


def captured(records: list[dict[str, Any]]) -> dict[str, Any]:
    fees: dict[str, Any] = next(r for r in records if r["kind"] == "live_fill")["receipt_fees"]
    return fees


def attributed(records: list[dict[str, Any]], fees: Any) -> dict[str, Any]:
    swapped = [r | {"receipt_fees": fees} if r["kind"] == "live_fill" else r for r in records]
    return receipt_fee_attribution(swapped)


def execution_item(gas: int, price: int, fee: int) -> dict[str, Any]:
    return {
        "status": "observed",
        "gas_used": str(gas),
        "effective_gas_price_wei": str(price),
        "fee_wei": str(fee),
    }


@pytest.mark.parametrize(
    ("gas", "price", "fee"),
    [
        (1 << 64, 1, 1 << 64),  # gasUsed wider than uint64
        (1, 1 << 256, 1 << 256),  # price wider than uint256
        (1 << 63, 1 << 193, 1 << 256),  # in-width factors, product overflows uint256
        (GAS, PRICE, GAS * PRICE + 1),  # forged product
    ],
)
def test_report_rejects_execution_items_capture_cannot_emit(
    tmp_path: Path, gas: int, price: int, fee: int
) -> None:
    run, _ = live_run(tmp_path, rpc_receipt_reader(Transport()))
    records = read_journal(run / "journal.db")[1]
    good = captured(records)
    fees = attributed(records, good | {"execution": execution_item(gas, price, fee)})
    (row,) = fees["rows"]
    assert row["execution"] == "record_invalid" and row["execution_fee_wei"] is None
    assert fees["execution"]["observed_orders"] == 0 and fees["execution"]["observed_wei"] == "0"
    # The independent L1 item of the same record is still read on its own terms.
    assert row["l1_data"] == "observed" and row["l1_data_fee_wei"] == str(L1)


def test_report_accepts_the_exact_capture_bounds_and_legal_zero(tmp_path: Path) -> None:
    run, _ = live_run(tmp_path, rpc_receipt_reader(Transport()))
    records = read_journal(run / "journal.db")[1]
    good = captured(records)
    top_gas = (1 << 64) - 1
    for item, l1 in (
        (execution_item(top_gas, 1, top_gas), (1 << 256) - 1),  # largest values capture emits
        (execution_item(0, 0, 0), 0),  # observed zero is a value
    ):
        record = good | {"execution": item, "l1_data": {"status": "observed", "fee_wei": str(l1)}}
        (row,) = attributed(records, record)["rows"]
        assert row["execution"] == row["l1_data"] == "observed"
        assert row["execution_fee_wei"] == item["fee_wei"] and row["l1_data_fee_wei"] == str(l1)
    # The capture side agrees on the same bounds.
    assert (
        receipt_fees({"gasUsed": hex(top_gas), "effectiveGasPrice": "0x1"})["execution"]["status"]
        == "observed"
    )
    assert (
        receipt_fees({"gasUsed": hex(1 << 64), "effectiveGasPrice": "0x1"})["execution"]["status"]
        == "invalid"
    )


def test_report_rejects_l1_above_width_and_unknown_source(tmp_path: Path) -> None:
    run, _ = live_run(tmp_path, rpc_receipt_reader(Transport()))
    records = read_journal(run / "journal.db")[1]
    good = captured(records)
    wide = good | {"l1_data": {"status": "observed", "fee_wei": str(1 << 256)}}
    (row,) = attributed(records, wide)["rows"]
    assert row["l1_data"] == "record_invalid" and row["execution"] == "observed"
    for source in ("eth_getTransactionByHash", None, 1):
        replaced = {k: v for k, v in good.items() if k != "source"}
        if source is not None:
            replaced["source"] = source
        fees = attributed(records, replaced)
        (row,) = fees["rows"]
        assert row["execution"] == row["l1_data"] == "record_invalid"
        assert fees["execution"]["observed_orders"] == fees["l1_data"]["observed_orders"] == 0


def test_receipt_rpc_states_are_distinct_from_journal_contract_states(tmp_path: Path) -> None:
    run, _ = live_run(tmp_path, rpc_receipt_reader(Transport(gasUsed=None, l1Fee="0xZZ")))
    by_state = build_report(run)["accounting"]["receipt_fees"]
    # missing / invalid: what the receipt itself stated; record_invalid: the journal line.
    assert by_state["execution"]["by_state"]["missing"] == 1
    assert by_state["l1_data"]["by_state"]["invalid"] == 1
    assert by_state["execution"]["by_state"]["record_invalid"] == 0
