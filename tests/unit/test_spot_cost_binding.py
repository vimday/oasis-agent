"""One journal-matched spot order bound to two retained full-cost proofs (offline only).

Production path with synthetic inputs: LiveExecutor + rpc_receipt_reader over a scripted
receipt -> real run journal; two TransactionCostCapture records from distinct declared
providers persisted with persist_transaction_cost; scripts.report_spot_shadow reads both
read-only. No network, wallet, key or broadcast.
"""

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from eth_abi.abi import encode

import oasis_agent.launch.spot_report as spot_report
import scripts.report_spot_shadow as report_cli
from oasis_agent.evidence.store import EvidenceStore, ReadOnlyEvidenceStore
from oasis_agent.farming.transaction_costs import (
    OP_ORACLE,
    OP_SELECTOR,
    TransactionCostCapture,
    persist_transaction_cost,
)
from oasis_agent.launch.spot_cost_binding import SCHEMA, bind_order_cost
from oasis_agent.launch.spot_executor import APPROVED_WALLET
from oasis_agent.launch.spot_lifecycle import reconcile_orders
from oasis_agent.launch.spot_report import read_journal, receipt_fee_attribution
from oasis_agent.models.chain import BlockSnapshot
from oasis_agent.models.common import BlockRef, EvidenceRef, canonical_json
from oasis_agent.rpc.transport import RpcExchange
from oasis_agent.sniping.journal import ExecutionJournal

from .spot_legacy_fixture import rpc_receipt_reader
from .test_spot_receipt_fees import GAS, L1, PRICE, Transport, live_run

BLOCK = "0x" + "b1" * 32
OTHER_BLOCK = "0x" + "b2" * 32
PARENT = "0x" + "a0" * 32
HEADER = BlockSnapshot(BlockRef(8453, 123, BLOCK, 1_800_000_000), PARENT)
OPERATOR = 11
PROVIDERS = ["alchemy", "drpc"]
UNSTATED: Any = object()  # a receipt field that is not stated at all (legacy bundles)


def cost_capture(
    source: str,
    tx: str,
    header: BlockSnapshot = HEADER,
    *,
    status: int = 1,
    payer: str = APPROVED_WALLET,
    operator: int = OPERATOR,
    l1: int = L1,
    logs: list[dict[str, Any]] | None = None,
    transaction_index: object = "0x0",
) -> TransactionCostCapture:
    ref = header.ref
    hdr = {
        "number": hex(ref.number),
        "hash": ref.block_hash,
        "parentHash": header.parent_hash,
        "timestamp": hex(ref.timestamp or 0),
    }
    receipt = {
        "transactionHash": tx,
        "blockHash": ref.block_hash,
        "blockNumber": hex(ref.number),
        "status": hex(status),
        "from": payer,
        "type": "0x2",
        "gasUsed": hex(GAS),
        "effectiveGasPrice": hex(PRICE),
        "l1Fee": hex(l1),
    }
    if logs is not None:  # absent by default: older fixtures carry no logs
        receipt["logs"] = logs  # type: ignore[assignment]
        if transaction_index is not UNSTATED:  # None is written as JSON null
            receipt["transactionIndex"] = transaction_index  # type: ignore[assignment]
    pin = {"blockHash": ref.block_hash, "requireCanonical": True}
    specs = [
        ("eth_chainId", [], hex(ref.chain_id)),
        ("eth_getBlockByNumber", [hex(ref.number), False], hdr),
        ("eth_getTransactionReceipt", [tx], receipt),
        ("eth_getCode", [OP_ORACLE, pin], "0x6000"),
        (
            "eth_call",
            [{"to": OP_ORACLE, "data": OP_SELECTOR + encode(["uint256"], [GAS]).hex()}, pin],
            "0x" + encode(["uint256"], [operator]).hex(),
        ),
        ("eth_getBlockByNumber", [hex(ref.number), False], hdr),
    ]
    exchanges = tuple(
        RpcExchange(
            m,
            i,
            canonical_json({"jsonrpc": "2.0", "id": i, "method": m, "params": p}),
            canonical_json({"jsonrpc": "2.0", "id": i, "result": r}),
            1,
        )
        for i, (m, p, r) in enumerate(specs, 1)
    )
    return TransactionCostCapture(header, tx, source, exchanges)


def header_json(header: BlockSnapshot = HEADER) -> dict[str, Any]:
    ref = header.ref
    return {
        "chain_id": ref.chain_id,
        "number": ref.number,
        "block_hash": ref.block_hash,
        "timestamp": ref.timestamp,
        "parent_hash": header.parent_hash,
    }


def ref_json(ref: EvidenceRef) -> dict[str, Any]:
    out = {k: getattr(ref, k) for k in ref.__slots__}  # type: ignore[attr-defined]
    out["block"] = (
        None
        if ref.block is None
        else {
            k: getattr(ref.block, k)
            for k in ref.block.__slots__  # type: ignore[attr-defined]
        }
    )
    return out


class World:
    """One run with a proven buy whose receipt names BLOCK, plus an evidence store."""

    def __init__(self, tmp_path: Path, *, fill_out: int = 200_000_000_000, **receipt: Any) -> None:
        fields = {"blockHash": BLOCK} | receipt
        reader = rpc_receipt_reader(Transport(**fields))
        self.run, _ = live_run(tmp_path, reader, fill_out=fill_out)
        self.records = read_journal(self.run / "journal.db")[1]
        fill = next(r for r in self.records if r["kind"] == "live_fill")
        self.intent, self.tx = fill["intent_id"], fill["tx_hash"]
        self.db = tmp_path / "evidence.db"
        self.store = EvidenceStore(self.db)

    def put(self, capture: TransactionCostCapture) -> dict[str, Any]:
        return ref_json(persist_transaction_cost(self.store, capture))

    def pair(self, **capture: Any) -> list[dict[str, Any]]:
        return [self.put(cost_capture(p, self.tx, **capture)) for p in PROVIDERS]

    def binding(self, costs: list[dict[str, Any]], **change: Any) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "intent_id": self.intent,
            "header": header_json(),
            "providers": list(PROVIDERS),
            "costs": costs,
        } | change

    def bind(self, binding: object, records: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        self.store.close()
        store = ReadOnlyEvidenceStore(self.db)
        try:
            return bind_order_cost(self.records if records is None else records, binding, store)
        finally:
            store.close()
            self.store = EvidenceStore(self.db)


def test_matched_order_replays_full_native_fee_from_two_providers(tmp_path: Path) -> None:
    world = World(tmp_path)
    view = world.bind(world.binding(world.pair()))
    assert view["status"] == "replayed" and view["intent_id"] == world.intent
    assert view["native_wei"] == {
        "execution": str(GAS * PRICE),
        "l1": str(L1),
        "blob": "0",
        "operator": str(OPERATOR),
        "total": str(GAS * PRICE + L1 + OPERATOR),
    }
    assert view["providers"] == PROVIDERS and len(set(view["evidence"])) == 2
    assert view["block"]["block_hash"] == BLOCK and view["tx_hash"] == world.tx
    assert view["payer"] == APPROVED_WALLET
    assert view["scope"] == "this_order_only" and view["booked_in_strategy_pnl"] is False
    assert view["usd_value"] == "not_converted" and view["execution_eligible"] is False


def test_legal_zero_operator_fee_is_a_value(tmp_path: Path) -> None:
    world = World(tmp_path)
    view = world.bind(world.binding(world.pair(operator=0)))
    assert view["status"] == "replayed" and view["native_wei"]["operator"] == "0"


@pytest.mark.parametrize(
    ("mutate", "status", "reason"),
    [
        # Provider pairing: one provider twice, a reversed or foreign declaration.
        (
            lambda w, c: w.binding(c, providers=["alchemy", "alchemy"]),
            "unbound",
            "provider_pair_invalid",
        ),
        (
            lambda w, c: w.binding(c, providers=["drpc", "alchemy"]),
            "unbound",
            "provider_pair_invalid",
        ),
        (lambda w, c: w.binding([c[0], c[0]]), "unbound", "provider_pair_invalid"),
        (lambda w, c: w.binding(c[:1]), "unbound", "provider_pair_invalid"),
        # The fill's own block: other chain, other height, same height with another hash.
        (
            lambda w, c: w.binding(c, header=header_json() | {"chain_id": 84532}),
            "unbound",
            "block_mismatch",
        ),
        (
            lambda w, c: w.binding(c, header=header_json() | {"number": 124}),
            "unbound",
            "block_mismatch",
        ),
        (
            lambda w, c: w.binding(c, header=header_json() | {"block_hash": OTHER_BLOCK}),
            "unbound",
            "block_mismatch",
        ),
        # Orders the ledger does not match, and malformed declarations: refused, not raised.
        (lambda w, c: w.binding(c, intent_id="nobody"), "unbound", "order_not_matched"),
        (lambda w, c: w.binding(c, intent_id=[]), "unbound", "binding_malformed"),
        (lambda w, c: w.binding(c) | {"extra": 1}, "unbound", "binding_malformed"),
        (lambda w, c: w.binding(c, schema="x"), "unbound", "binding_malformed"),
        (lambda w, c: w.binding(c, header=[]), "unbound", "binding_malformed"),
        (lambda w, c: w.binding({}), "unbound", "binding_malformed"),
        (lambda w, c: [], "unbound", "binding_malformed"),
    ],
)
def test_binding_refusals(tmp_path: Path, mutate: Any, status: str, reason: str) -> None:
    world = World(tmp_path)
    view = world.bind(mutate(world, world.pair()))
    assert (view["status"], view["reason"]) == (status, reason)
    assert view["native_wei"] is None


def test_evidence_must_be_this_receipt_and_retained_raw(tmp_path: Path) -> None:
    world = World(tmp_path)
    good = world.pair()
    # A proof of another transaction at the same block cannot rebuild for this one.
    other = [world.put(cost_capture(p, "0x" + "ee" * 32)) for p in PROVIDERS]
    view = world.bind(world.binding([good[0], other[1]]))
    assert (view["status"], view["reason"]) == ("unbound", "evidence_invalid_1")
    # A reference with no retained record, or a forged content hash.
    missing = good[1] | {"content_hash": "0" * 64}
    view = world.bind(world.binding([good[0], missing]))
    assert (view["status"], view["reason"]) == ("unbound", "evidence_invalid_1")


def test_forged_interpretation_is_refused_against_its_raw_envelope(tmp_path: Path) -> None:
    world = World(tmp_path)
    good = world.pair()
    record = world.store.get_record(
        EvidenceRef(**(good[0] | {"block": BlockRef(**good[0]["block"])}))
    )
    payload = json.loads(record.payload)
    payload["cost"]["total_native"] = 1
    forged = world.store.put(
        "transaction_cost",
        canonical_json(payload),
        HEADER.ref,
        PROVIDERS[0],
        request=json.loads(record.request_payload or b"null"),
        raw_response=json.loads(record.raw_response_payload),
        interpretation=json.loads(record.interpretation_payload or b"null"),
    )
    view = world.bind(world.binding([ref_json(forged), good[1]]))
    assert (view["status"], view["reason"]) == ("unbound", "evidence_invalid_0")


@pytest.mark.parametrize(
    ("first", "second", "reason"),
    [
        ({}, {"operator": OPERATOR + 1}, "providers_disagree"),  # never only the total
        ({"payer": "0x" + "11" * 20}, {"payer": "0x" + "11" * 20}, "identity_mismatch"),
        ({"status": 0}, {"status": 0}, "status_mismatch"),
        ({"l1": L1 + 5}, {"l1": L1 + 5}, "single_receipt_conflict"),
    ],
)
def test_conflicts_are_kept_not_upgraded(
    tmp_path: Path, first: dict[str, Any], second: dict[str, Any], reason: str
) -> None:
    world = World(tmp_path)
    costs = [
        world.put(cost_capture(PROVIDERS[0], world.tx, **first)),
        world.put(cost_capture(PROVIDERS[1], world.tx, **second)),
    ]
    view = world.bind(world.binding(costs))
    assert (view["status"], view["reason"]) == ("conflict", reason)
    assert view["native_wei"] is None


@pytest.mark.parametrize(
    ("receipt", "strip"),
    [({"blockHash": None}, False), ({"blockHash": "0x12"}, False), ({}, True)],
)
def test_height_only_or_unusable_receipt_block_is_never_upgraded(
    tmp_path: Path, receipt: dict[str, Any], strip: bool
) -> None:
    world = World(tmp_path, **receipt)
    records = world.records
    if strip:  # a fill journaled before receipt_block existed: height only
        records = [
            r
            | {"receipt_fees": {k: v for k, v in r["receipt_fees"].items() if k != "receipt_block"}}
            if r["kind"] == "live_fill"
            else r
            for r in records
        ]
    view = world.bind(world.binding(world.pair()), records)
    assert (view["status"], view["reason"]) == ("unbound", "receipt_block_unknown")


def test_duplicate_fill_order_has_no_binding(tmp_path: Path) -> None:
    world = World(tmp_path)
    fill = next(r for r in world.records if r["kind"] == "live_fill")
    view = world.bind(world.binding(world.pair()), [*world.records, fill])
    assert (view["status"], view["reason"]) == ("unbound", "order_not_matched")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_cli_default_report_unchanged_and_inputs_never_rewritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = World(tmp_path)
    binding_path = tmp_path / "binding.json"
    binding_path.write_text(json.dumps(world.binding(world.pair())))
    world.store.close()
    journal = world.run / "journal.db"
    before = (digest(journal), digest(world.db))
    capsys.readouterr()  # drop what the live run fixture itself printed

    monkeypatch.setattr(sys, "argv", ["x", "--run", str(world.run)])
    assert report_cli.main() == 0
    default = json.loads(capsys.readouterr().out)
    assert "cost_replay" not in default

    argv = ["x", "--run", str(world.run), "--cost-binding", str(binding_path)]
    monkeypatch.setattr(sys, "argv", [*argv, "--evidence", str(world.db)])
    assert report_cli.main() == 0
    bound = json.loads(capsys.readouterr().out)
    assert bound["cost_replay"]["status"] == "replayed"
    # Everything else is the default report; resume/strategy fields do not move.
    optional = {"cost_replay", "receipt_transfers"}
    assert set(bound) - set(default) == optional
    assert {k: v for k, v in bound.items() if k not in optional} == default
    assert (digest(journal), digest(world.db)) == before

    monkeypatch.setattr(sys, "argv", argv)  # one flag without the other
    with pytest.raises(SystemExit):
        report_cli.main()
    monkeypatch.setattr(sys, "argv", [*argv, "--evidence", str(tmp_path / "absent.db")])
    assert report_cli.main() == 1
    assert not (tmp_path / "absent.db").exists()  # never created


def with_fees(records: list[dict[str, Any]], change: Any) -> list[dict[str, Any]]:
    return [
        r | {"receipt_fees": change(r["receipt_fees"])} if r["kind"] == "live_fill" else r
        for r in records
    ]


@pytest.mark.parametrize(
    ("change", "report_state"),
    [
        # Whole record not producible by capture: report and replay both refuse it.
        (lambda f: f | {"source": "eth_getTransactionByHash"}, "record_invalid"),
        (lambda f: f | {"status": "other"}, "record_invalid"),
        # not_captured carrying a nested hash: the hash is not evidence either.
        (lambda f: f | {"status": "not_captured"}, "not_captured"),
        # Valid items, but the block entry itself is not a canonical observed hash.
        (
            lambda f: f | {"receipt_block": {"status": "observed", "block_hash": BLOCK.upper()}},
            "observed",
        ),
        (lambda f: f | {"receipt_block": {"status": "missing", "block_hash": BLOCK}}, "observed"),
    ],
)
def test_block_pin_only_from_a_record_capture_could_write(
    tmp_path: Path, change: Any, report_state: str
) -> None:
    world = World(tmp_path)
    records = with_fees(world.records, change)
    view = world.bind(world.binding(world.pair()), records)
    assert (view["status"], view["reason"]) == ("unbound", "receipt_block_unknown")
    # The fee report reads the very same record through the same function.
    (row,) = receipt_fee_attribution(records)["rows"]
    assert row["execution"] == row["l1_data"] == report_state


@pytest.mark.parametrize(
    "execution",
    [
        {
            "status": "observed",
            "gas_used": str(GAS),
            "effective_gas_price_wei": str(PRICE),
            "fee_wei": "1",
        },  # forged product
        {
            "status": "observed",
            "gas_used": str(1 << 64),
            "effective_gas_price_wei": "1",
            "fee_wei": str(1 << 64),
        },  # gasUsed wider than uint64
        {"status": "observed"},  # observed without values
        "x",
    ],
)
def test_invalid_journaled_item_is_neither_evidence_nor_conflict(
    tmp_path: Path, execution: Any
) -> None:
    world = World(tmp_path)
    records = with_fees(world.records, lambda f: f | {"execution": execution})
    # The report calls it record_invalid and counts nothing ...
    (row,) = receipt_fee_attribution(records)["rows"]
    assert row["execution"] == "record_invalid" and row["execution_fee_wei"] is None
    # ... and the replay must not treat it as a contradicting single-receipt value.
    view = world.bind(world.binding(world.pair()), records)
    assert view["status"] == "replayed"
    assert view["native_wei"]["execution"] == str(GAS * PRICE)


def test_valid_single_receipt_conflict_is_still_refused(tmp_path: Path) -> None:
    world = World(tmp_path)
    other = GAS * (PRICE + 1)
    item = {
        "status": "observed",
        "gas_used": str(GAS),
        "effective_gas_price_wei": str(PRICE + 1),
        "fee_wei": str(other),
    }
    records = with_fees(world.records, lambda f: f | {"execution": item})
    assert receipt_fee_attribution(records)["rows"][0]["execution"] == "observed"
    view = world.bind(world.binding(world.pair()), records)
    assert (view["status"], view["reason"]) == ("conflict", "single_receipt_conflict")


def test_cli_report_is_one_journal_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A writer appends a complete second order right after the CLI's journal read.

    Before the fix the CLI read the journal twice, so run_status came from one prefix
    and cost_replay from another. Now both derive from the single snapshot.
    """
    world = World(tmp_path)
    journal = world.run / "journal.db"
    records = read_journal(journal)[1]
    lifecycle = [r for r in records if r["kind"] in ("live_intent", "live_fill", "live_buy")]
    late_id, late_tx = "late" + "0" * 28, "0x" + "5e" * 32
    late = []
    for r in lifecycle:
        copy = r | {"intent_id": late_id}
        if "tx_hash" in copy:
            copy["tx_hash"] = late_tx
        late.append(
            {k: v for k, v in copy.items() if k not in ("seq", "previous", "policy_digest")}
        )
    world.store.close()
    world.store = EvidenceStore(world.db)
    late_costs = [world.put(cost_capture(p, late_tx)) for p in PROVIDERS]
    binding_path = tmp_path / "binding.json"
    binding_path.write_text(json.dumps(world.binding(late_costs, intent_id=late_id)))
    world.store.close()

    real_read = spot_report.read_journal
    appended: list[bool] = []

    def read_then_append(path: Path) -> Any:
        # A concurrent writer lands a complete order right after the FIRST read, wherever
        # that read happens (CLI or report module); later reads see the longer journal.
        snapshot = real_read(path)
        if not appended:
            appended.append(True)
            with ExecutionJournal(journal, snapshot[0]) as writer:
                for r in late:
                    writer.append(r["kind"], r)
        return snapshot

    monkeypatch.setattr(spot_report, "read_journal", read_then_append)
    monkeypatch.setattr(report_cli, "read_journal", read_then_append)
    capsys.readouterr()
    argv = ["x", "--run", str(world.run), "--cost-binding", str(binding_path)]
    monkeypatch.setattr(sys, "argv", [*argv, "--evidence", str(world.db)])
    assert report_cli.main() == 0
    out = json.loads(capsys.readouterr().out)
    # One prefix: the late order is in neither half of this output.
    assert out["run_status"]["settled_live_orders"] == 1
    assert (out["cost_replay"]["status"], out["cost_replay"]["reason"]) == (
        "unbound",
        "order_not_matched",
    )
    # The append really happened: a fresh report sees both orders, and the late one binds.
    fresh = read_journal(journal)[1]
    assert reconcile_orders(fresh).settled[-1] == late_id
    store = ReadOnlyEvidenceStore(world.db)
    try:
        bound = bind_order_cost(fresh, json.loads(binding_path.read_text()), store)
    finally:
        store.close()
    assert bound["status"] == "replayed"
