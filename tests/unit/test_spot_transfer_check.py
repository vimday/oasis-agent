"""One bound spot fill: receipt Transfer nets of USDC and the order token vs the journal.

Synthetic retained receipts only, through the historical fixture path: LiveExecutor +
rpc_receipt_reader -> real run journal; two persisted transaction_cost proofs whose
receipts carry scripted logs; check_order_transfers and the report CLI read them
read-only. No network, wallet, key or broadcast.
"""

import json
import sys
from pathlib import Path
from typing import Any

import pytest

import scripts.report_spot_shadow as report_cli
from oasis_agent.evidence.store import EvidenceStore, ReadOnlyEvidenceStore
from oasis_agent.farming.owner_payments import TRANSFER
from oasis_agent.launch.onchainos_source import BASE_USDC
from oasis_agent.launch.spot_executor import APPROVED_WALLET
from oasis_agent.launch.spot_report import read_journal
from oasis_agent.launch.spot_transfer_check import check_order_transfers

from .spot_legacy_fixture import LiveExecutor, rpc_receipt_reader
from .test_spot_agent import NOW, FakeCli
from .test_spot_cost_binding import BLOCK, OTHER_BLOCK, PROVIDERS, World, cost_capture
from .test_spot_executor import FakeWallet, gate
from .test_spot_receipt_fees import Transport
from .test_spot_runner import open_live, run_live

ROUTER = "0x" + "70" * 20
STRANGER = "0x" + "51" * 20
UNRELATED_TOKEN = "0x" + "99" * 20


def word(address: str) -> str:
    return "0x" + "00" * 12 + address.removeprefix("0x")


def transfer(
    token: str,
    sender: str,
    recipient: str,
    amount: int,
    index: int,
    tx: str,
    *,
    block: str = BLOCK,
    number: int = 123,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "address": token,
        "topics": [TRANSFER, word(sender), word(recipient)],
        "data": "0x" + amount.to_bytes(32, "big").hex(),
        "blockNumber": hex(number),
        "blockHash": block,
        "transactionHash": tx,
        "transactionIndex": "0x0",
        "logIndex": hex(index),
        "removed": False,
    } | extra


class Order:
    """The bound fill: its token, tx and the journal's own signed deltas."""

    def __init__(self, world: World, side: str) -> None:
        fill = next(r for r in world.records if r["kind"] == "live_fill" and r["side"] == side)
        world.intent, world.tx = fill["intent_id"], fill["tx_hash"]
        self.world, self.token, self.tx = world, fill["token"], fill["tx_hash"]
        self.usdc, self.units = int(fill["usdc_delta"]), int(fill["token_delta"])

    def swap_logs(self) -> list[dict[str, Any]]:
        """The minimal two-leg swap that the journal fill describes."""
        w, tx = APPROVED_WALLET, self.tx
        usdc = (w, ROUTER, -self.usdc) if self.usdc < 0 else (ROUTER, w, self.usdc)
        units = (w, ROUTER, -self.units) if self.units < 0 else (ROUTER, w, self.units)
        return [
            transfer(BASE_USDC, *usdc, 0, tx),
            transfer(self.token, *units, 1, tx),
        ]

    def check(
        self, first: list[dict[str, Any]], second: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        world = self.world
        costs = [
            world.put(cost_capture(PROVIDERS[0], self.tx, logs=first)),
            world.put(
                cost_capture(PROVIDERS[1], self.tx, logs=first if second is None else second)
            ),
        ]
        binding = world.binding(costs)
        world.store.close()
        store = ReadOnlyEvidenceStore(world.db)
        try:
            return check_order_transfers(world.records, binding, store)
        finally:
            store.close()
            world.store = EvidenceStore(world.db)


def buy(tmp_path: Path) -> Order:
    return Order(World(tmp_path), "buy")


class SellWorld(World):
    """A buy, then a stop-loss sell, both proven with receipts that name BLOCK."""

    def __init__(self, tmp_path: Path) -> None:
        run = tmp_path / "run"
        cli, wallet = FakeCli(), FakeWallet(usdc=1_000_000_000)
        wallet.fill_out = 200_000_000_000
        receipt = rpc_receipt_reader(Transport(blockHash=BLOCK))
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
            assert run_live(run, executor, cli, ticks=1) == 0
            wallet.fill_out = 150_000_000  # USDC the sell returns
            cli.sell_ratio = (60, 100)  # stop-loss
            assert run_live(run, executor, cli, ticks=1) == 0
        self.run = run
        self.records = read_journal(run / "journal.db")[1]
        self.db = tmp_path / "evidence.db"
        self.store = EvidenceStore(self.db)
        self.intent = self.tx = ""


def test_buy_receipt_transfers_match_the_journal_fill(tmp_path: Path) -> None:
    order = buy(tmp_path)
    view = order.check(order.swap_logs())
    assert (view["status"], view["reason"]) == ("matched", None)
    assert view["assets"]["usdc"] == {
        "token": BASE_USDC,
        "received": "0",
        "sent": str(-order.usdc),
        "net": str(order.usdc),
    }
    assert view["assets"]["token"]["net"] == str(order.units) and order.units > 0
    assert view["journal"] == {"usdc_delta": str(order.usdc), "token_delta": str(order.units)}
    assert view["logs_in_scope"] == 2 and view["unit"] == "token_base_units"
    assert view["whole_block_balance"] == "not_proven" and view["fund_settlement"] == "not_proven"


def test_sell_receipt_transfers_match_the_journal_fill(tmp_path: Path) -> None:
    order = Order(SellWorld(tmp_path), "sell")
    assert order.usdc > 0 and order.units < 0
    view = order.check(order.swap_logs())
    assert view["status"] == "matched"
    assert view["assets"]["usdc"]["net"] == str(order.usdc)
    assert view["assets"]["token"] == {
        "token": order.token,
        "received": "0",
        "sent": str(-order.units),
        "net": str(order.units),
    }


def test_refund_and_split_legs_net_exactly_and_out_of_scope_logs_never_count(
    tmp_path: Path,
) -> None:
    order = buy(tmp_path)
    w, tx, spent, got = APPROVED_WALLET, order.tx, -order.usdc, order.units
    logs = [
        transfer(BASE_USDC, w, ROUTER, spent + 7, 0, tx),
        transfer(BASE_USDC, ROUTER, w, 7, 1, tx),  # refund in the same transaction
        transfer(order.token, ROUTER, w, got - 3, 2, tx),
        transfer(order.token, ROUTER, w, 3, 3, tx),  # second segment
        transfer(order.token, ROUTER, STRANGER, 10**9, 4, tx),  # not the wallet
        transfer(UNRELATED_TOKEN, ROUTER, w, 10**9, 5, tx),  # not USDC or the token
        transfer(BASE_USDC, w, ROUTER, 1, 6, tx) | {"topics": ["0x" + "ee" * 32]},  # other event
    ]
    view = order.check(logs)
    assert view["status"] == "matched" and view["logs_in_scope"] == 4
    assert view["assets"]["usdc"] == {
        "token": BASE_USDC,
        "received": "7",
        "sent": str(spent + 7),
        "net": str(-spent),
    }


@pytest.mark.parametrize(
    "mutate",
    [
        # Token flows away from the wallet instead of to it.
        lambda o, logs: [logs[0], transfer(o.token, APPROVED_WALLET, ROUTER, o.units, 1, o.tx)],
        # Token goes to another address: nothing in scope for the token.
        lambda o, logs: [logs[0], transfer(o.token, ROUTER, STRANGER, o.units, 1, o.tx)],
        # One base unit short of the journal.
        lambda o, logs: [logs[0], transfer(o.token, ROUTER, APPROVED_WALLET, o.units - 1, 1, o.tx)],
        # The token leg is missing entirely.
        lambda o, logs: logs[:1],
    ],
)
def test_receipt_that_contradicts_the_journal_is_a_mismatch(tmp_path: Path, mutate: Any) -> None:
    order = buy(tmp_path)
    view = order.check(mutate(order, order.swap_logs()))
    assert (view["status"], view["reason"]) == (
        "journal_mismatch",
        "journal_delta_differs_from_receipt_transfers",
    )


def test_same_net_from_different_log_rows_is_a_provider_conflict(tmp_path: Path) -> None:
    order = buy(tmp_path)
    one = order.swap_logs()
    w, tx = APPROVED_WALLET, order.tx
    two = [
        one[0],
        transfer(order.token, ROUTER, w, order.units - 1, 1, tx),
        transfer(order.token, ROUTER, w, 1, 2, tx),
    ]
    view = order.check(one, two)  # identical nets, different logs
    assert (view["status"], view["reason"]) == ("conflict", "providers_disagree_on_transfer_logs")
    assert view["assets"] is None


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda o, logs: [*logs, dict(logs[1])], "duplicate_log_index"),
        (
            lambda o, logs: [logs[0], logs[1] | {"transactionHash": "0x" + "ee" * 32}],
            "log_identity_mismatch",
        ),
        (lambda o, logs: [logs[0], logs[1] | {"blockHash": OTHER_BLOCK}], "log_identity_mismatch"),
        (lambda o, logs: [logs[0], logs[1] | {"blockNumber": hex(124)}], "log_identity_mismatch"),
        (lambda o, logs: [logs[0], logs[1] | {"removed": True}], "log_malformed"),
        (lambda o, logs: [logs[0], logs[1] | {"logIndex": "0x01"}], "log_malformed"),
        (lambda o, logs: [logs[0], logs[1] | {"data": "0x01"}], "transfer_abi_invalid"),
        (
            lambda o, logs: [logs[0], logs[1] | {"topics": [*logs[1]["topics"], word(ROUTER)]}],
            "transfer_abi_invalid",
        ),
        (lambda o, logs: [], "no_in_scope_transfer_logs"),
    ],
)
def test_malformed_or_foreign_logs_never_reach_a_match(
    tmp_path: Path, mutate: Any, reason: str
) -> None:
    order = buy(tmp_path)
    view = order.check(mutate(order, order.swap_logs()))
    assert (view["status"], view["reason"]) == ("unverified", reason)


def test_receipt_without_logs_and_unbound_order_are_not_matched(tmp_path: Path) -> None:
    order = buy(tmp_path)
    world = order.world
    costs = [world.put(cost_capture(p, order.tx)) for p in PROVIDERS]  # no logs key
    world.store.close()
    store = ReadOnlyEvidenceStore(world.db)
    try:
        legacy = check_order_transfers(world.records, world.binding(costs), store)
        unbound = check_order_transfers(world.records, world.binding(costs, intent_id="x"), store)
        malformed = check_order_transfers(world.records, [], store)
    finally:
        store.close()
    assert (legacy["status"], legacy["reason"]) == ("unverified", "receipt_logs_missing")
    assert (unbound["status"], unbound["reason"]) == ("unbound", "cost_binding_order_not_matched")
    assert malformed["status"] == "unbound" and malformed["assets"] is None


def test_report_cli_shows_the_check_and_default_report_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    order = buy(tmp_path)
    world = order.world
    costs = [world.put(cost_capture(p, order.tx, logs=order.swap_logs())) for p in PROVIDERS]
    binding = tmp_path / "binding.json"
    binding.write_text(json.dumps(world.binding(costs)))
    world.store.close()
    capsys.readouterr()
    monkeypatch.setattr(sys, "argv", ["x", "--run", str(world.run)])
    assert report_cli.main() == 0
    default = json.loads(capsys.readouterr().out)
    assert "receipt_transfers" not in default and "cost_replay" not in default
    argv = ["x", "--run", str(world.run), "--cost-binding", str(binding)]
    monkeypatch.setattr(sys, "argv", [*argv, "--evidence", str(world.db)])
    assert report_cli.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert out["cost_replay"]["status"] == "replayed"
    assert out["receipt_transfers"]["status"] == "matched"
    assert {
        k: v for k, v in out.items() if k not in ("cost_replay", "receipt_transfers")
    } == default


@pytest.mark.parametrize("has_logs", [True, False])
def test_report_can_require_two_receipts_to_corroborate_the_fill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    has_logs: bool,
) -> None:
    order = buy(tmp_path)
    world = order.world
    logs = order.swap_logs() if has_logs else []
    costs = [world.put(cost_capture(p, order.tx, logs=logs)) for p in PROVIDERS]
    binding = tmp_path / "binding.json"
    binding.write_text(json.dumps(world.binding(costs)))
    world.store.close()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "report",
            "--run",
            str(world.run),
            "--cost-binding",
            str(binding),
            "--evidence",
            str(world.db),
            "--require-receipt-match",
        ],
    )
    capsys.readouterr()  # discard output from the historical fixture runner
    assert report_cli.main() == (0 if has_logs else 2)
    result = json.loads(capsys.readouterr().out)
    assert result["receipt_transfers"]["status"] == ("matched" if has_logs else "unverified")
    assert result["run_status"]["resumable"] is False
    assert result["execution_eligible"] is False
