"""Offline replay of retained public testnet data; never live-profit evidence."""

import json
import shutil
from pathlib import Path

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.farming.batch_settlement import reconcile_batch_settlement
from oasis_agent.farming.transaction_costs import replay_transaction_cost
from oasis_agent.farming.transaction_payments import replay_transaction_payments
from oasis_agent.models.common import BlockRef, EvidenceRef
from scripts.probe_transaction_payments import load_core

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / "reports/evidence/transaction-closure-2026-09-06"


def ref(raw):
    return EvidenceRef(**(raw | {"block": BlockRef(**raw["block"])}))


def test_real_six_nft_core_and_actual_payments_close_for_three_tokens(tmp_path):
    core = load_core(ROOT / "reports/evidence/base-sepolia-2026-09-06-recorded/resumed-http1")
    source = BASE / "base-payments-resumed"
    shutil.copyfile(source / "evidence.db", tmp_path / "payments.db")
    store = EvidenceStore(tmp_path / "payments.db")
    payment = replay_transaction_payments(
        store, ref(json.loads((source / "summary.json").read_text())["evidence"]), core
    )
    result = json.loads(reconcile_batch_settlement(payment))
    assert {p["token_id"] for p in result["positions"]} == {
        27345,
        27346,
        27364,
        27365,
        27368,
        27369,
    }
    assert len(result["pool_manager_residual_by_token"]) == 3
    assert set(result["pool_manager_residual_by_token"].values()) == {0}
    owner = next(
        a
        for a in result["actual_accounts"]
        if a["account"] == "0x08878c675a26157de155f82f260d41995aa14dfe"
        and a["token"] == "0x036cbd53842c5426634e7929541ec2318f3dcf7e"
    )
    assert owner["paid"] == owner["received"] == 277145937
    assert owner["net"] == 0
    assert result["execution_eligible"] is False
    store.close()


def test_real_op_transaction_cost_includes_l1_not_da_footprint_twice(tmp_path):
    core = load_core(ROOT / "reports/evidence/base-sepolia-2026-09-06-recorded/resumed-http1")
    source = BASE / "base-cost-retry"
    shutil.copyfile(source / "evidence.db", tmp_path / "cost.db")
    store = EvidenceStore(tmp_path / "cost.db")
    cost = replay_transaction_cost(
        store,
        ref(json.loads((source / "summary.json").read_text())["evidence"]),
        header=core.header,
        transaction_hash=core.entry.transaction_hash,
    ).reconstruct()
    assert cost.execution_native == 20085342000000
    assert cost.l1_native == 7643809493
    assert cost.operator_native == cost.blob_native == 0
    assert cost.total_native == 20092985809493
    store.close()
