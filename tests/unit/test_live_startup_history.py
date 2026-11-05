"""Startup must replay whole cost/cash history and agree on original capital."""

import json
import sqlite3
import sys
from contextlib import closing
from dataclasses import asdict, replace

import pytest

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.live import startup_state
from oasis_agent.live.rpc_profile import LiveRpcProfile
from oasis_agent.live.state import StateStore, day_of
from oasis_agent.models.common import canonical_json, sha256_hex
from oasis_agent.rpc.budget import RpcBudget
from scripts import inspect_live_startup, run_live_authority, run_live_lp
from tests.unit import test_live_approval_cash as approval
from tests.unit import test_live_receipt_costs as costs
from tests.unit import test_live_revert_cash as cash
from tests.unit.test_live_precheck_evidence import capture
from tests.unit.test_live_receipt_costs import position_config
from tests.unit.test_live_startup_state import prepare, snapshot
from tests.unit.test_live_valuation import NavFixture, synthetic_feed_pin  # noqa: F401


def approval_pair(tmp_path, monkeypatch, *, nonce_delta=0, trimmed=True):
    """Each service owns original synthetic raw proof, journal and new fixture budget."""
    for module in (costs, cash):
        monkeypatch.setattr(module, "PRIMARY", startup_state.SOURCES[0])
        monkeypatch.setattr(module, "SECONDARY", startup_state.SOURCES[1])
    config = replace(
        position_config(),
        expected_pins={
            **position_config().expected_pins,
            "permit2": approval.TEST_CONFIG.expected_pins["permit2"],
        },
    )
    monkeypatch.setattr(approval, "TEST_CONFIG", config)
    profile = LiveRpcProfile(max_requests=4, min_interval_ms=100)
    stores = {}
    for role in ("strategy", "authority"):
        directory = tmp_path / role
        directory.mkdir(mode=0o700)
        state_path, evidence_path, lock_path = startup_state.paths(directory, role)
        with closing(EvidenceStore(evidence_path)) as evidence:
            fixture = approval.ApprovalFixture()
            loop = approval.approval_loop(directory, evidence, fixture, replace(fixture))
            assert "confirmed" in loop.tick()
            state = loop.state
            nav_fixture = NavFixture(
                position_enabled=False,
                native_after=fixture.native_after,
                token_after=fixture.token_after,
                nonce_after=fixture.nonce_after + nonce_delta,
                block_number=fixture.block_number + 5,
                timestamp=fixture.timestamp + 10,
                block_hash="0x" + "88" * 32,
            )
            value, refs = capture(evidence, config, nav_fixture)
            state.nav_evidence = refs
            state.integer_risk().observe(
                day=day_of(nav_fixture.timestamp), at=nav_fixture.timestamp, nav=value.nav_micros
            )
            state.last_units = [value.wallet.native, value.wallet.token, 0]
            if trimmed:
                state.history = []
            loop.store.save(state)
        store = loop.store
        if store.path != state_path:
            journal = store.journal_path
            store.path.rename(state_path)
            store = StateStore(state_path, require_existing=True)
            journal.rename(store.journal_path)
        lock_path.touch()
        with RpcBudget(
            directory / "live.receipt-budget.db", profile.policies(startup_state.SOURCES)
        ) as budget:
            for source in startup_state.SOURCES:
                budget.reserve(source, now_ms=1000)
        stores[role] = store
    return config, profile, stores, fixture


@pytest.mark.parametrize("fault", ["basis", "external", "gas", "anchor"])
def test_startup_refuses_proven_nav_with_unreconciled_capital_or_history(tmp_path, fault):
    a, b = tmp_path / "strategy", tmp_path / "authority"
    config, profile, store = prepare(a, "strategy")
    prepare(b, "authority")
    state = store.load("live")
    if fault == "basis":
        state.exact_risk.basis += 1
    elif fault == "external":
        state.exact_risk.external += 1
    elif fault == "gas":
        state.gas_spent_wei += 1
    else:
        state.settled_blocks.append(
            {"number": 100, "hash": "0x" + "ab" * 32, "transaction_hash": "0x" + "cd" * 32}
        )
    store.save(state)
    before = snapshot(a), snapshot(b)
    with pytest.raises(ValueError):
        startup_state.inspect_pair(a, b, config, profile)
    assert before == (snapshot(a), snapshot(b))


@pytest.mark.parametrize("trimmed", [False, True])
def test_startup_replays_earlier_raw_receipt_cash_and_whole_gas_after_projection_trim(
    tmp_path, monkeypatch, trimmed
):
    config, profile, stores, fixture = approval_pair(tmp_path, monkeypatch, trimmed=trimmed)
    a, b = tmp_path / "strategy", tmp_path / "authority"
    before = snapshot(a), snapshot(b)
    report = startup_state.inspect_pair(a, b, config, profile)
    assert report["startup_prerequisites_passed"] and report["historical_only"]
    assert report["execution_eligible"] is False
    history_hash = sha256_hex(canonical_json((fixture._receipt(),)))
    left, right = (report["services"][role] for role in ("strategy", "authority"))
    assert left["settled_history_hash"] == right["settled_history_hash"] == history_hash
    assert left["settlement_count"] == right["settlement_count"] == 1
    assert left["settled_history_replayed"] is right["settled_history_replayed"] is True
    assert left["financial_continuity_hash"] == right["financial_continuity_hash"]
    assert stores["strategy"].load("live", read_only=True).gas_spent_wei == 218
    assert (snapshot(a), snapshot(b)) == before


@pytest.mark.parametrize("fault", ["gas", "anchor", "missing_cost", "changed_cost", "nonce"])
def test_complete_history_refusal_does_not_consume_budget_or_repair_proofs(
    tmp_path, monkeypatch, fault
):
    config, profile, stores, _ = approval_pair(
        tmp_path, monkeypatch, nonce_delta=int(fault == "nonce")
    )
    store = stores["strategy"]
    if fault in {"gas", "anchor"}:
        state = store.load("live", read_only=True)
        if fault == "gas":
            state.gas_spent_wei -= 1
        else:
            state.settled_blocks = []
        store.save(state)
    elif fault in {"missing_cost", "changed_cost"}:
        with sqlite3.connect(tmp_path / "strategy/live.receipts.db") as db:
            if fault == "missing_cost":
                changed = db.execute("DELETE FROM evidence WHERE kind='transaction_cost'")
            else:
                changed = db.execute(
                    "UPDATE evidence SET raw_response_payload=? WHERE kind='transaction_cost'",
                    (b"private provider diagnostic",),
                )
            assert changed.rowcount >= 2
    a, b = tmp_path / "strategy", tmp_path / "authority"
    before = snapshot(a), snapshot(b)
    with pytest.raises(ValueError, match="complete settlement history") as exc:
        startup_state.inspect_pair(a, b, config, profile)
    assert "private provider diagnostic" not in str(exc.value)
    assert (snapshot(a), snapshot(b)) == before


def test_independent_daily_checkpoints_are_preserved_not_forced_to_one_lower_value(tmp_path):
    a, b = tmp_path / "strategy", tmp_path / "authority"
    config, profile, left = prepare(a, "strategy")
    _, _, right = prepare(b, "authority")
    for i, store in enumerate((left, right), 1):
        state = store.load("live")
        state.exact_risk.daily_locked = i
        store.save(state)
    before = snapshot(a), snapshot(b)
    report = startup_state.inspect_pair(a, b, config, profile)
    assert report["startup_prerequisites_passed"]
    assert (snapshot(a), snapshot(b)) == before
    assert left.load("live", read_only=True).exact_risk.daily_locked == 1
    assert right.load("live", read_only=True).exact_risk.daily_locked == 2


def test_journal_change_between_checkpoint_and_whole_history_refuses(tmp_path, monkeypatch):
    a, b = tmp_path / "strategy", tmp_path / "authority"
    config, profile, _ = prepare(a, "strategy")
    prepare(b, "authority")
    original = startup_state._journal_history

    def advance(store, state):
        writer = StateStore(store.path, require_existing=True)
        updated = writer.load("live")
        updated.actions_today += 1
        writer.save(updated)
        return original(store, state)

    monkeypatch.setattr(startup_state, "_journal_history", advance)
    with pytest.raises(ValueError, match="changed before history"):
        startup_state.inspect_pair(a, b, config, profile)


@pytest.mark.parametrize("role", ["strategy", "authority"])
def test_production_history_guard_precedes_rpc_environment_and_key_access(
    tmp_path, monkeypatch, capsys, role
):
    directory = tmp_path / role
    config, profile, store = prepare(directory, role, execution=True)
    state = store.load("live")
    state.gas_spent_wei = 1
    store.save(state)
    config_path, profile_path = tmp_path / "config.json", tmp_path / "profile.json"
    config_path.write_bytes(canonical_json(asdict(config)))
    profile_path.write_bytes(canonical_json(asdict(profile)))
    module = run_live_lp if role == "strategy" else run_live_authority

    def forbidden(*args, **kwargs):
        pytest.fail("history must be verified before RPC, signer or key capability")

    for name in ("BudgetedRpc", "receipt_cost_reader"):
        monkeypatch.setattr(module, name, forbidden)
    if role == "strategy":
        monkeypatch.setattr(module, "rpc_url", forbidden)
        monkeypatch.setattr(module, "TransactionSocketClient", forbidden)
    else:
        monkeypatch.setattr(module, "load_private_key", forbidden)
        monkeypatch.setattr(module, "prepare_endpoint", forbidden)
    argv = [
        "run",
        "--config",
        str(config_path),
        "--rpc-profile",
        str(profile_path),
        "--state-dir",
        str(directory),
        "--require-existing-runtime",
    ]
    argv += (
        ["--mode", "live", "--signer-socket", "/unused"]
        if role == "strategy"
        else ["--socket", "/unused"]
    )
    monkeypatch.setattr(sys, "argv", argv)
    before = snapshot(directory)
    with pytest.raises(SystemExit) as exc:
        module.main()
    assert exc.value.code == 3
    assert "Original production runtime state unresolved." in capsys.readouterr().err
    assert snapshot(directory) == before


def test_cli_preserves_proof_and_redacts_whole_history_refusal(tmp_path, monkeypatch, capsys):
    config, profile, stores, _ = approval_pair(tmp_path, monkeypatch)
    state = stores["strategy"].load("live")
    state.gas_spent_wei += 1
    stores["strategy"].save(state)
    config_path, profile_path = tmp_path / "config.json", tmp_path / "profile.json"
    config_path.write_bytes(canonical_json(asdict(config)))
    profile_path.write_bytes(canonical_json(asdict(profile)))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "inspect",
            "--strategy-dir",
            str(tmp_path / "strategy"),
            "--authority-dir",
            str(tmp_path / "authority"),
            "--config",
            str(config_path),
            "--rpc-profile",
            str(profile_path),
        ],
    )
    before = snapshot(tmp_path / "strategy"), snapshot(tmp_path / "authority")
    with pytest.raises(SystemExit) as exc:
        inspect_live_startup.main()
    assert exc.value.code == 3
    report = json.loads(capsys.readouterr().out)
    assert report["startup_prerequisites_passed"] is False and report["execution_eligible"] is False
    assert report["blockers"] == ["original_runtime_state_unresolved"]
    assert str(tmp_path) not in json.dumps(report)
    assert before == (snapshot(tmp_path / "strategy"), snapshot(tmp_path / "authority"))
