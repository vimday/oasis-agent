"""One bound spot fill: the wallet's whole-block native/USDC/token change vs that fill.

Synthetic retained evidence only: a real fixture journal, two persisted cost proofs and
two persisted block settlement proofs, checked read-only. No network, wallet or key.
"""

import json
from pathlib import Path
from typing import Any

import pytest

import scripts.report_spot_shadow as report_cli
from oasis_agent.evidence.store import EvidenceStore, ReadOnlyEvidenceStore
from oasis_agent.farming.block_settlement import persist_block_settlement
from oasis_agent.launch.onchainos_source import BASE_USDC
from oasis_agent.launch.spot_executor import APPROVED_WALLET
from oasis_agent.launch.spot_settlement_check import (
    SCHEMA,
    check_block_settlement,
    settlement_scope,
)

from .test_block_settlement import Chain, log
from .test_spot_cost_binding import HEADER, OPERATOR, PROVIDERS, cost_capture, ref_json
from .test_spot_receipt_fees import GAS, L1, PRICE
from .test_spot_transfer_check import ROUTER, STRANGER, Order, buy

FEE = GAS * PRICE + L1 + OPERATOR
NATIVE = 10**18
OTHER_TX = "0x" + "ce" * 32


class Scene:
    """A matched buy, its two cost proofs and two block settlement proofs."""

    def __init__(self, tmp_path: Path, order: Order | None = None) -> None:
        self.order: Order = buy(tmp_path) if order is None else order
        self.world = self.order.world
        fill = next(r for r in self.world.records if r["kind"] == "live_fill")
        self.wallet, self.tokens = settlement_scope(fill)
        self.usdc, self.units = self.order.usdc, self.order.units

    def receipt_logs(self) -> list[dict[str, Any]]:
        """The fill's own legs at the fixture's position, as the receipt retains them."""
        return self.order.swap_logs()

    def chain(self, logs: list[dict[str, Any]] | None = None, **change: Any) -> Chain:
        """The block, by default exactly this fill: balances move by the journal deltas."""
        usdc_before, token_before = 5 * 10**8, 7
        defaults: dict[str, Any] = {
            "native": (NATIVE, NATIVE - FEE),
            "balances": {
                BASE_USDC: (usdc_before, usdc_before + self.usdc),
                self.order.token: (token_before, token_before + self.units),
            },
            "supply": {BASE_USDC: (10**15, 10**15), self.order.token: (10**20, 10**20)},
            "code": {BASE_USDC: ("0x6001", "0x6001"), self.order.token: ("0x6002", "0x6002")},
            "decimals": {BASE_USDC: 6, self.order.token: 18},
        }
        scope: dict[str, Any] = {"account": self.wallet, "tokens": self.tokens, "header": HEADER}
        return Chain(self.receipt_logs() if logs is None else logs, **(scope | defaults | change))

    def retain(
        self,
        first: Chain | None = None,
        second: Chain | None = None,
        *,
        receipts: tuple[list[dict[str, Any]], list[dict[str, Any]]] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Persist both cost and both settlement proofs; return (cost, settlement) bindings."""
        world = self.world
        rows = receipts or (self.receipt_logs(), self.receipt_logs())
        costs = [
            world.put(cost_capture(p, self.order.tx, logs=r))
            for p, r in zip(PROVIDERS, rows, strict=True)
        ]
        chains = (first or self.chain(), second or first or self.chain())
        settlements = [
            ref_json(persist_block_settlement(world.store, c.capture(p)))
            for p, c in zip(PROVIDERS, chains, strict=True)
        ]
        declared = {
            "schema": SCHEMA,
            "intent_id": world.intent,
            "providers": list(PROVIDERS),
            "settlements": settlements,
        }
        return world.binding(costs), declared

    def check(
        self,
        first: Chain | None = None,
        second: Chain | None = None,
        *,
        receipts: tuple[list[dict[str, Any]], list[dict[str, Any]]] | None = None,
        binding: Any = None,
    ) -> dict[str, Any]:
        world = self.world
        cost, declared = self.retain(first, second, receipts=receipts)
        world.store.close()
        store = ReadOnlyEvidenceStore(world.db)
        try:
            return check_block_settlement(
                world.records,
                cost,
                declared if binding is None else binding(declared),
                store,
            )
        finally:
            store.close()
            world.store = EvidenceStore(world.db)


def test_a_fill_whose_block_moves_only_by_its_own_transfers_and_fee_reconciles(
    tmp_path: Path,
) -> None:
    scene = Scene(tmp_path)
    view = scene.check()
    assert (view["status"], view["reason"]) == ("reconciled", None)
    assert view["wallet"] == APPROVED_WALLET and view["transaction_index"] == 0
    assert view["native_wei"] == {
        "before": str(NATIVE),
        "after": str(NATIVE - FEE),
        "delta": str(-FEE),
        "receipt_fee_total": str(FEE),
        "residual": "0",
    }
    usdc, token = view["tokens"]["usdc"], view["tokens"]["token"]
    assert (usdc["balance_delta"], usdc["residual"]) == (str(scene.usdc), "0")
    assert (token["balance_delta"], token["transaction_net"]) == (
        str(scene.units),
        str(scene.units),
    )
    assert (usdc["decimals"], token["decimals"]) == (6, 18)
    assert view["block_transfer_logs"] == view["transaction_transfer_logs"] == 2
    assert view["account_nav"] == "not_proven" and view["execution_eligible"] is False


def bump(chain: Chain, token: str, units: int) -> Chain:
    before, after = chain.balances[token]
    chain.balances = chain.balances | {token: (before, after + units)}
    return chain


def test_a_token_balance_the_logs_do_not_explain_is_a_residual(tmp_path: Path) -> None:
    scene = Scene(tmp_path)
    view = scene.check(bump(scene.chain(), scene.order.token, 1))  # one unit more than logs
    assert (view["status"], view["reason"]) == (
        "residual",
        "token_balance_or_supply_not_explained_by_transfers",
    )
    assert view["tokens"]["token"]["residual"] == "1"


def test_a_supply_change_without_mint_or_burn_logs_is_a_residual(tmp_path: Path) -> None:
    scene = Scene(tmp_path)
    supply = {BASE_USDC: (10**15, 10**15 + 1), scene.order.token: (10**20, 10**20)}
    view = scene.check(scene.chain(supply=supply))
    assert view["status"] == "residual" and view["tokens"]["usdc"]["supply_residual"] == "1"


@pytest.mark.parametrize("extra", [1, -1])
def test_native_change_other_than_the_receipt_fee_is_a_residual(tmp_path: Path, extra: int) -> None:
    scene = Scene(tmp_path)
    view = scene.check(scene.chain(native=(NATIVE, NATIVE - FEE + extra)))
    assert (view["status"], view["reason"]) == (
        "residual",
        "native_balance_not_explained_by_receipt_fee",
    )
    assert view["native_wei"]["residual"] == str(extra)


def test_another_transaction_moving_the_wallet_in_the_block_is_not_reconciled(
    tmp_path: Path,
) -> None:
    scene = Scene(tmp_path)
    gift = log(BASE_USDC, STRANGER, scene.wallet, 9, 7, tx=OTHER_TX, pos=3)  # same block, later
    chain = bump(scene.chain([*scene.receipt_logs(), gift]), BASE_USDC, 9)
    view = scene.check(chain)
    assert (view["status"], view["reason"]) == (
        "unverified",
        "other_transactions_moved_wallet_in_block",
    )
    assert view["tokens"]["usdc"]["other_transaction_net"] == "9"


@pytest.mark.parametrize(
    "legs",
    [
        # Another transaction sends 5 in and takes 5 out: net zero, still not this fill alone.
        [(STRANGER, "wallet", 5, 7), ("wallet", STRANGER, 5, 8)],
        # A zero-amount transfer by another transaction still touches the wallet.
        [(STRANGER, "wallet", 0, 7)],
    ],
)
def test_other_transactions_touching_the_wallet_block_reconciliation_even_at_zero_net(
    tmp_path: Path, legs: list[tuple[str, str, int, int]]
) -> None:
    scene = Scene(tmp_path)
    w = scene.wallet
    extra = [
        log(
            BASE_USDC,
            w if s == "wallet" else s,
            w if r == "wallet" else r,
            a,
            i,
            tx=OTHER_TX,
            pos=3,
        )
        for s, r, a, i in legs
    ]
    view = scene.check(scene.chain([*scene.receipt_logs(), *extra]))
    assert (view["status"], view["reason"]) == (
        "unverified",
        "other_transactions_moved_wallet_in_block",
    )
    usdc = view["tokens"]["usdc"]
    assert (usdc["other_transaction_net"], usdc["other_transaction_logs"]) == ("0", len(legs))


def test_an_unrelated_transfer_in_the_block_does_not_block_reconciliation(
    tmp_path: Path,
) -> None:
    scene = Scene(tmp_path)
    others = log(scene.order.token, ROUTER, STRANGER, 10**6, 9, tx=OTHER_TX, pos=4)
    view = scene.check(scene.chain([*scene.receipt_logs(), others]))
    assert view["status"] == "reconciled" and view["block_transfer_logs"] == 3


def test_two_providers_with_different_block_answers_conflict(tmp_path: Path) -> None:
    scene = Scene(tmp_path)
    view = scene.check(scene.chain(), bump(scene.chain(), BASE_USDC, 1))
    assert (view["status"], view["reason"]) == (
        "conflict",
        "providers_disagree_on_block_settlement",
    )
    assert view["tokens"] is None


def test_block_logs_that_differ_from_the_receipt_rows_conflict(tmp_path: Path) -> None:
    # Same net, but the block lists the token leg as two rows while both receipts list one.
    scene = Scene(tmp_path)
    usdc, token = scene.receipt_logs()
    split = [
        usdc,
        token | {"data": "0x" + (scene.units - 1).to_bytes(32, "big").hex()},
        token | {"logIndex": hex(2), "data": "0x" + (1).to_bytes(32, "big").hex()},
    ]
    view = scene.check(scene.chain(split))
    assert (view["status"], view["reason"]) == (
        "conflict",
        "receipt_and_block_transfer_logs_differ",
    )


def test_a_receipt_that_does_not_match_the_journal_never_reconciles(tmp_path: Path) -> None:
    scene = Scene(tmp_path)
    usdc, token = scene.receipt_logs()
    short = token | {"data": "0x" + (scene.units - 1).to_bytes(32, "big").hex()}
    view = scene.check(receipts=([usdc, short], [usdc, short]))
    assert view["status"] == "journal_mismatch"
    assert view["reason"] == "receipt_transfers_journal_delta_differs_from_receipt_transfers"


@pytest.mark.parametrize(
    "origin",
    [
        {"kind": "frozen_input_reconstruction", "schema": "oasis.frozen-rpc.v1"},
        # Stating `unknown` is a claim, not an absence: it is never read as "no origin".
        {"kind": "unknown"},
    ],
)
def test_a_settlement_with_another_origin_than_the_cost_proofs_is_refused(
    tmp_path: Path, origin: dict[str, Any]
) -> None:
    # The cost proofs state no origin; a settlement stating any origin is another bundle.
    scene = Scene(tmp_path)
    world = scene.world
    stamped = [
        ref_json(persist_block_settlement(world.store, scene.chain().capture(p), origin=origin))
        for p in PROVIDERS
    ]
    view = scene.check(binding=lambda declared: declared | {"settlements": stamped})
    assert (view["status"], view["reason"]) == ("unbound", "settlement_origin_differs_from_cost")


def _same_record_twice(d: dict[str, Any]) -> dict[str, Any]:
    first = d["settlements"][0]
    return d | {"settlements": [first, first | {"source": PROVIDERS[1]}]}


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        # One record named for both providers (same content hash) is not two observations.
        (_same_record_twice, "settlement_provider_pair_invalid"),
        (lambda d: d | {"settlements": d["settlements"][::-1]}, "settlement_provider_pair_invalid"),
        (lambda d: d | {"providers": d["providers"][::-1]}, "settlement_binding_scope_mismatch"),
        (lambda d: d | {"intent_id": "x"}, "settlement_binding_scope_mismatch"),
        (lambda d: d | {"schema": "x"}, "settlement_binding_malformed"),
        (lambda d: d | {"extra": 1}, "settlement_binding_malformed"),
        (lambda d: d | {"settlements": d["settlements"][:1]}, "settlement_binding_malformed"),
        (
            lambda d: (
                d | {"settlements": [s | {"kind": "transaction_cost"} for s in d["settlements"]]}
            ),
            "settlement_provider_pair_invalid",
        ),
        (
            lambda d: (
                d
                | {
                    "settlements": [
                        d["settlements"][0],
                        d["settlements"][1] | {"content_hash": "0" * 64},
                    ]
                }
            ),
            "settlement_evidence_invalid",
        ),
        (lambda d: [], "settlement_binding_malformed"),
    ],
)
def test_a_settlement_binding_that_does_not_name_two_proofs_of_this_scope_is_refused(
    tmp_path: Path, change: Any, reason: str
) -> None:
    view = Scene(tmp_path).check(binding=change)
    assert (view["status"], view["reason"]) == ("unbound", reason)
    assert view["tokens"] is None and view["execution_eligible"] is False


def test_settlement_proofs_of_another_wallet_are_refused(tmp_path: Path) -> None:
    # Valid proofs of the same block and tokens, but for STRANGER: the scope is derived from
    # the fill, so they can never replay as this wallet's settlement.
    scene = Scene(tmp_path)
    world = scene.world
    other = scene.chain(account=STRANGER)
    foreign = [ref_json(persist_block_settlement(world.store, other.capture(p))) for p in PROVIDERS]
    view = scene.check(binding=lambda declared: declared | {"settlements": foreign})
    assert (view["status"], view["reason"]) == ("unbound", "settlement_evidence_invalid")


def run_report(scene: Scene, monkeypatch: Any, capsys: Any, *extra: str) -> tuple[int, Any]:
    capsys.readouterr()  # discard output from the historical fixture runner
    monkeypatch.setattr("sys.argv", ["report", "--run", str(scene.world.run), *extra])
    code = report_cli.main()
    return code, json.loads(capsys.readouterr().out or "null")


@pytest.mark.parametrize(
    ("chain_change", "expected", "code"),
    [({}, "reconciled", 0), ({"native": (NATIVE, NATIVE - FEE - 1)}, "residual", 2)],
)
def test_report_cli_adds_block_settlement_only_on_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    chain_change: dict[str, Any],
    expected: str,
    code: int,
) -> None:
    scene = Scene(tmp_path)
    cost, settlement = scene.retain(scene.chain(**chain_change))
    scene.world.store.close()
    cost_file, settlement_file = tmp_path / "cost.json", tmp_path / "settlement.json"
    cost_file.write_text(json.dumps(cost))
    settlement_file.write_text(json.dumps(settlement))
    evidence = ["--cost-binding", str(cost_file), "--evidence", str(scene.world.db)]
    exit_code, without = run_report(scene, monkeypatch, capsys, *evidence)
    assert exit_code == 0 and "block_settlement" not in without
    settle = ["--settlement-binding", str(settlement_file)]
    exit_code, out = run_report(scene, monkeypatch, capsys, *evidence, *settle)
    assert exit_code == 0 and out["block_settlement"]["status"] == expected
    assert {k: v for k, v in out.items() if k != "block_settlement"} == without
    strict = [*evidence, *settle, "--require-block-settlement"]
    assert run_report(scene, monkeypatch, capsys, *strict)[0] == code


def test_report_cli_refuses_settlement_flags_without_their_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    scene = Scene(tmp_path)
    scene.world.store.close()
    for argv in (
        ["--settlement-binding", str(tmp_path / "s.json")],
        ["--require-block-settlement"],
    ):
        with pytest.raises(SystemExit) as raised:
            run_report(scene, monkeypatch, capsys, *argv)
        assert raised.value.code == 2
