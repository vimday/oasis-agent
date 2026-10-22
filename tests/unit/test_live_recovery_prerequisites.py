"""Both service recovery entries require retained history, not a fresh empty state."""

import json
import sys
from dataclasses import asdict

import pytest

from oasis_agent.live.rpc_profile import LiveRpcProfile
from oasis_agent.live.state import LoopState, Pending, StateStore
from oasis_agent.models.common import canonical_json, sha256_hex
from oasis_agent.rpc.budget import RpcBudget
from scripts import run_live_authority, run_live_lp
from tests.unit.test_live_lp import CONFIG


@pytest.mark.parametrize("service", ["strategy", "authority"])
@pytest.mark.parametrize(
    "case",
    [
        "missing_journal",
        "projection_only",
        "missing_budget",
        "empty_budget",
        "foreign_budget",
        "missing_evidence",
        "empty_evidence",
        "idle",
        "idle_missing_projection",
    ],
)
def test_recovery_requires_history_and_reuses_budget_without_signing(
    tmp_path, monkeypatch, capsys, service, case
):
    module = run_live_authority if service == "authority" else run_live_lp
    directory = tmp_path / "state"
    directory.mkdir(mode=0o700)
    config = tmp_path / "config.json"
    config.write_text(json.dumps(asdict(CONFIG)))
    store = StateStore(directory / ("state.json" if service == "authority" else "live.state.json"))
    pending = Pending("swap", "0x" + "ab" * 32, 3, 1000, {}, CONFIG.position_manager)
    if case == "projection_only":
        store.save(LoopState("live", pending=pending))
    elif case != "missing_journal":
        store.bind(sha256_hex(canonical_json(asdict(CONFIG))))
        state = store.load("live")
        state.pending = None if case.startswith("idle") else pending
        state.halted = "retained operator halt"
        store.save(state)
        if case == "idle_missing_projection":
            store.path.unlink()
    budget = directory / "live.receipt-budget.db"
    if case == "empty_budget":
        budget.touch()
    elif case == "foreign_budget":
        with RpcBudget(budget, LiveRpcProfile(max_requests=1).policies(("other_a", "other_b"))):
            pass
    elif case in {"missing_evidence", "empty_evidence"}:
        with RpcBudget(budget, LiveRpcProfile().policies(("live_primary", "live_secondary"))):
            pass
        if case == "empty_evidence":
            (directory / ("evidence.db" if service == "authority" else "live.receipts.db")).touch()
    before = {p.name: p.read_bytes() for p in directory.iterdir()}

    def forbidden(*args, **kwargs):
        raise AssertionError("prerequisite check must precede RPC, evidence and key access")

    for name in ("BudgetedRpc", "receipt_cost_reader"):
        monkeypatch.setattr(module, name, forbidden)
    if service == "authority":
        for name in ("load_private_key", "prepare_endpoint", "TransactionAuthority"):
            monkeypatch.setattr(module, name, forbidden)
    else:
        monkeypatch.setattr(module, "TransactionSocketClient", forbidden)
        monkeypatch.setattr(module, "rpc_url", forbidden)
    args = ["recovery", "--config", str(config), "--state-dir", str(directory), "--reconcile-only"]
    if service == "strategy":
        args += ["--mode", "live"]
    monkeypatch.setattr(sys, "argv", args)
    monkeypatch.delenv("BASE_RPC_PRIMARY_ALT", raising=False)
    if case.startswith("idle"):
        module.main()
        assert "no pending transaction" in capsys.readouterr().out
    else:
        with pytest.raises(SystemExit) as caught:
            module.main()
        assert caught.value.code == 3
        assert "no pending transaction" not in capsys.readouterr().out
    after = {p.name: p.read_bytes() for p in directory.iterdir() if not p.name.endswith(".lock")}
    assert after == before


def test_strategy_recovery_does_not_create_an_absent_state_directory(tmp_path, monkeypatch):
    directory = tmp_path / "absent"
    monkeypatch.setattr(
        sys,
        "argv",
        ["recovery", "--mode", "live", "--reconcile-only", "--state-dir", str(directory)],
    )
    with pytest.raises(SystemExit) as caught:
        run_live_lp.main()
    assert caught.value.code == 3 and not directory.exists()
