"""Receipts become declarations: two-provider cost agreement and fail-closed refusals.

The ledger explains a native decrease only when it equals the declared gas exactly, so a
producer that guesses is worse than one that refuses. Every test here is about that:
the same transaction must cost the same at both providers, the retained bytes must be
exactly the sequence the verifier checks, and a missing receipt, a moved block, a
disagreeing provider or a payer that is not the reconciled account must all refuse
instead of becoming a zero cost or a default.
"""

import json
import sqlite3
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest
from eth_abi.abi import encode

from oasis_agent.evidence.exact_capture import RecordedSession
from oasis_agent.evidence.rpc_capture import BoundedExchangeRecorder
from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.execution.account_capture import SessionFactory
from oasis_agent.execution.lp_broadcast import (
    BroadcastPolicy,
    LpBroadcastJournal,
    broadcast_policy_to_json,
)
from oasis_agent.execution.lp_receipts import (
    ReceiptObservation,
    gas_declarations,
    observe_receipt,
)
from oasis_agent.farming.transaction_costs import replay_transaction_cost
from oasis_agent.models.chain import BlockSnapshot
from oasis_agent.models.common import BlockRef, canonical_json, normalize_hash32
from oasis_agent.risk.portfolio_manifest import ChainInventory
from oasis_agent.rpc.transport import JsonRpcTransport
from scripts.run_account_risk_reconcile import _native_declarations
from scripts.run_lp_receipts import _requested, main
from tests.unit.test_account_risk_reader import ACCOUNT, InventoryFixture, chain_inventory, manifest

PRIMARY = "rpc_a"
SECONDARY = "rpc_b"
NITRO_CHAIN = 4663
OP_CHAIN = 8453
BLOCK_NUMBER = 57_634_560
BLOCK_HASH = "0x" + "aa" * 32
OTHER_HASH = "0x" + "cc" * 32
PARENT_HASH = "0x" + "bb" * 32
TIMESTAMP = 1_800_000_000
TRANSACTION = "0x" + "5a" * 32
OTHER_TRANSACTION = "0x" + "12" * 32
PAYER = ACCOUNT
TARGET = "0x" + "9f" * 20
GAS_USED = 100
GAS_PRICE = 2
L1_FEE = 7
GAS_USED_FOR_L1 = 20
OPERATOR_FEE = 11
# Nitro: execution (100 * 2) less its L1 share (20 * 2) plus that share again.
# OP: execution plus the receipt's L1 fee plus the pinned on-chain operator fee.
TOTALS = {NITRO_CHAIN: 200, OP_CHAIN: 218}


@dataclass
class ReceiptFixture:
    """One deterministic provider answer for one broadcast receipt at one pinned block."""

    chain_id: int = NITRO_CHAIN
    block_number: int = BLOCK_NUMBER
    block_hash: str = BLOCK_HASH
    parent_hash: str = PARENT_HASH
    timestamp: int = TIMESTAMP
    transaction_hash: str = TRANSACTION
    payer: str = PAYER
    status: int = 1
    tx_type: int = 2
    gas_used: int = GAS_USED
    gas_price: int = GAS_PRICE
    l1_fee: int = L1_FEE
    gas_used_for_l1: int = GAS_USED_FOR_L1
    operator_fee: int = OPERATOR_FEE
    has_receipt: bool = True
    header_hash: str | None = None
    methods: list[str] = field(default_factory=list)

    def post(self, payload: bytes) -> bytes:
        request = json.loads(payload)
        method = request["method"]
        self.methods.append(method)
        return canonical_json(
            {"jsonrpc": "2.0", "id": request["id"], "result": self._result(method)}
        )

    def _result(self, method: str) -> object:
        if method == "eth_chainId":
            return hex(self.chain_id)
        if method == "eth_getBlockByNumber":
            return {
                "number": hex(self.block_number),
                "hash": self.header_hash or self.block_hash,
                "parentHash": self.parent_hash,
                "timestamp": hex(self.timestamp),
            }
        if method == "eth_getTransactionReceipt":
            return self._receipt() if self.has_receipt else None
        if method == "eth_getCode":
            return "0x6000"
        if method == "eth_call":
            return "0x" + encode(["uint256"], [self.operator_fee]).hex()
        raise AssertionError(method)

    def _receipt(self) -> dict[str, str]:
        return {
            "transactionHash": self.transaction_hash,
            "blockHash": self.block_hash,
            "blockNumber": hex(self.block_number),
            "from": self.payer,
            "status": hex(self.status),
            "type": hex(self.tx_type),
            "gasUsed": hex(self.gas_used),
            "effectiveGasPrice": hex(self.gas_price),
            "l1Fee": hex(self.l1_fee),
            "gasUsedForL1": hex(self.gas_used_for_l1),
        }


def sessions(posters: Mapping[str, ReceiptFixture]) -> SessionFactory:
    """A fresh transport and recorder per call, exactly as the budgeted factory does."""

    def build(source: str) -> RecordedSession:
        recorder = BoundedExchangeRecorder()
        return (
            JsonRpcTransport("https://fixture.invalid", http=posters[source], recorder=recorder),
            recorder,
            source,
        )

    return build


def entry(chain_id: int = NITRO_CHAIN) -> ChainInventory:
    return chain_inventory(replace(InventoryFixture(), chain_id=chain_id))


def header(chain_id: int = NITRO_CHAIN) -> BlockSnapshot:
    return BlockSnapshot(BlockRef(chain_id, BLOCK_NUMBER, BLOCK_HASH, TIMESTAMP), PARENT_HASH)


def capture_calls(chain_id: int) -> list[str]:
    """The exact wire sequence one cost capture must issue, and nothing else."""
    calls = ["eth_chainId", "eth_getBlockByNumber", "eth_getTransactionReceipt"]
    if chain_id == OP_CHAIN:
        return [*calls, "eth_getCode", "eth_call", "eth_getBlockByNumber"]
    return [*calls, "eth_getBlockByNumber"]


def retain(
    tmp_path: Path,
    *,
    chain_id: int = NITRO_CHAIN,
    primary: ReceiptFixture | None = None,
    secondary: ReceiptFixture | None = None,
    transaction_hash: str = TRANSACTION,
) -> ReceiptObservation:
    store = EvidenceStore(tmp_path / "cost.db")
    try:
        return observe_receipt(
            store,
            entry(chain_id),
            transaction_hash=transaction_hash,
            sessions=sessions(
                {
                    PRIMARY: primary or ReceiptFixture(chain_id=chain_id),
                    SECONDARY: secondary or ReceiptFixture(chain_id=chain_id),
                }
            ),
        )
    finally:
        store.close()


@pytest.mark.parametrize("chain_id", [NITRO_CHAIN, OP_CHAIN])
def test_two_providers_must_agree_on_the_whole_native_cost(tmp_path, chain_id) -> None:
    """One pin, two independent captures, and only exact agreement becomes a declaration."""
    primary = ReceiptFixture(chain_id=chain_id)
    secondary = ReceiptFixture(chain_id=chain_id)
    observation = retain(tmp_path, chain_id=chain_id, primary=primary, secondary=secondary)
    assert observation.total_native == TOTALS[chain_id]
    assert observation.status == 1
    assert observation.account_key == f"{chain_id}:{ACCOUNT}"
    assert observation.payer == PAYER
    assert (observation.block_number, observation.block_hash) == (BLOCK_NUMBER, BLOCK_HASH)
    assert [reference.source for reference in observation.evidence] == [PRIMARY, SECONDARY]
    # The locating lookup is two calls on the primary provider only; the retained
    # evidence of both providers is exactly the sequence the verifier re-checks.
    assert primary.methods == [
        "eth_getTransactionReceipt",
        "eth_getBlockByNumber",
        *capture_calls(chain_id),
    ]
    assert secondary.methods == capture_calls(chain_id)


@pytest.mark.parametrize("chain_id", [NITRO_CHAIN, OP_CHAIN])
def test_retained_cost_evidence_replays_to_the_same_declared_amount(tmp_path, chain_id) -> None:
    """A declaration may only rest on bytes that reconstruct to the same cost offline."""
    observation = retain(tmp_path, chain_id=chain_id)
    store = EvidenceStore(tmp_path / "cost.db")
    try:
        for reference in observation.evidence:
            capture = replay_transaction_cost(
                store, reference, header=header(chain_id), transaction_hash=TRANSACTION
            )
            cost = capture.reconstruct()
            assert cost.total_native == observation.total_native == TOTALS[chain_id]
            assert cost.payer == PAYER
    finally:
        store.close()


def test_a_disagreeing_provider_refuses_the_receipt(tmp_path) -> None:
    with pytest.raises(ValueError, match="providers disagree on the receipt cost"):
        retain(tmp_path, secondary=ReceiptFixture(gas_used=GAS_USED + 1))


def test_a_provider_that_does_not_see_the_pinned_block_refuses_the_receipt(tmp_path) -> None:
    with pytest.raises(ValueError, match="cost block changed"):
        retain(tmp_path, secondary=ReceiptFixture(header_hash=OTHER_HASH))


def test_an_unobserved_receipt_refuses_instead_of_declaring_zero(tmp_path) -> None:
    with pytest.raises(ValueError, match="no observed receipt"):
        retain(tmp_path, primary=ReceiptFixture(has_receipt=False))


def test_a_payer_that_is_not_the_reconciled_account_is_refused(tmp_path) -> None:
    stranger = ReceiptFixture(payer="0x" + "ee" * 20)
    with pytest.raises(ValueError, match="payer is not the reconciled account"):
        retain(tmp_path, primary=stranger, secondary=stranger)
    db = sqlite3.connect(tmp_path / "cost.db")
    try:
        # Nothing is retained before every check has passed.
        assert db.execute("SELECT count(*) FROM evidence").fetchone() == (0,)
    finally:
        db.close()


def test_a_failed_transaction_still_reports_its_own_cost(tmp_path) -> None:
    """Gas is spent by a reverted transaction too, so it must still be declared."""
    observation = retain(
        tmp_path, primary=ReceiptFixture(status=0), secondary=ReceiptFixture(status=0)
    )
    assert observation.status == 0
    assert observation.total_native == TOTALS[NITRO_CHAIN]


def test_native_decimals_must_match_the_cost_policy(tmp_path) -> None:
    """A 6-decimal native asset would make a wei declaration silently wrong."""
    base = chain_inventory(InventoryFixture())
    native = base.native.model_copy(update={"decimals": 6})
    broken = base.model_copy(update={"assets": (native, *base.erc20_assets)})
    store = EvidenceStore(tmp_path / "cost.db")
    try:
        with pytest.raises(ValueError, match="decimals differ from the cost policy"):
            observe_receipt(
                store,
                broken,
                transaction_hash=TRANSACTION,
                sessions=sessions({PRIMARY: ReceiptFixture(), SECONDARY: ReceiptFixture()}),
            )
    finally:
        store.close()


def test_gas_declarations_sum_per_account_and_cover_the_manifest(tmp_path) -> None:
    first = retain(tmp_path, primary=ReceiptFixture(), secondary=ReceiptFixture())
    second = retain(
        tmp_path,
        primary=ReceiptFixture(transaction_hash=OTHER_TRANSACTION),
        secondary=ReceiptFixture(transaction_hash=OTHER_TRANSACTION),
        transaction_hash=OTHER_TRANSACTION,
    )
    portfolio = manifest(InventoryFixture())
    declared = gas_declarations(portfolio, (first, second))
    assert declared == {
        f"{NITRO_CHAIN}:{ACCOUNT}": {"gas_units": 2 * TOTALS[NITRO_CHAIN], "non_trading_units": 0}
    }


def test_gas_declarations_refuse_a_repeated_transaction(tmp_path) -> None:
    observed = retain(tmp_path)
    portfolio = manifest(InventoryFixture())
    with pytest.raises(ValueError, match="one observation per transaction"):
        gas_declarations(portfolio, (observed, observed))


def test_gas_declarations_refuse_an_undeclared_account(tmp_path) -> None:
    observed = retain(tmp_path)
    stranger = manifest(replace(InventoryFixture(), account="0x" + "ab" * 20))
    with pytest.raises(ValueError, match="not a declared manifest account"):
        gas_declarations(stranger, (observed,))


def test_the_declaration_is_what_the_reconciliation_observer_consumes(tmp_path) -> None:
    """The producer and the consumer must fit without an operator rewriting the file."""
    observed = retain(tmp_path, chain_id=OP_CHAIN)
    portfolio = manifest(replace(InventoryFixture(), chain_id=OP_CHAIN))
    path = tmp_path / "declarations.json"
    path.write_bytes(canonical_json(gas_declarations(portfolio, (observed,))) + b"\n")
    assert _native_declarations(path, portfolio) == {f"{OP_CHAIN}:{ACCOUNT}": (TOTALS[OP_CHAIN], 0)}


def test_a_transaction_must_name_a_declared_chain_and_a_unique_hash() -> None:
    portfolio = manifest(InventoryFixture())
    assert _requested([f"{NITRO_CHAIN}:{TRANSACTION}"], portfolio) == {
        normalize_hash32(TRANSACTION): chain_inventory(InventoryFixture())
    }
    with pytest.raises(ValueError, match="at least one broadcast transaction"):
        _requested([], portfolio)
    with pytest.raises(ValueError, match="declared chain and hash"):
        _requested([TRANSACTION], portfolio)
    with pytest.raises(ValueError, match="declared chain and hash"):
        _requested([f"{OP_CHAIN}:{TRANSACTION}"], portfolio)
    with pytest.raises(ValueError, match="one request per transaction"):
        _requested([f"{NITRO_CHAIN}:{TRANSACTION}"] * 2, portfolio)


def test_the_cli_refuses_a_transaction_the_journal_never_sent(tmp_path, monkeypatch) -> None:
    """A declaration can only describe a transaction this system signed and sent."""
    portfolio = manifest(InventoryFixture())
    policy = BroadcastPolicy(
        chain_ids=frozenset({NITRO_CHAIN}),
        account=ACCOUNT,
        targets=frozenset({TARGET}),
        max_value_wei=0,
        max_gas_limit=1_000_000,
        max_fee_per_gas=10**10,
        max_total_gas_cost_wei=10**16,
    )
    config = tmp_path / "config"
    config.mkdir()
    (config / "manifest.json").write_bytes(portfolio.model_dump_json().encode())
    (config / "policy.json").write_bytes(broadcast_policy_to_json(policy))
    journal = LpBroadcastJournal(config / "broadcast.db", policy=policy)
    journal.close()
    (config / ".env").write_text("rpc_a=https://a.invalid\nrpc_b=https://b.invalid\n")
    output = tmp_path / "out"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_lp_receipts",
            "--manifest",
            str(config / "manifest.json"),
            "--policy",
            str(config / "policy.json"),
            "--journal",
            str(config / "broadcast.db"),
            "--transaction",
            f"{NITRO_CHAIN}:{TRANSACTION}",
            "--output",
            str(output),
            "--rpc",
            f"{NITRO_CHAIN}:{PRIMARY}:{SECONDARY}",
            "--env-file",
            str(config / ".env"),
        ],
    )
    with pytest.raises(SystemExit) as exit_info:
        main()
    assert exit_info.value.code == 1
    assert not output.exists()
