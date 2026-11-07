"""Production startup preserves original files; no network, Docker or real keys."""

import fcntl
import json
import os
import sqlite3
import sys
from contextlib import closing
from dataclasses import asdict, replace

import pytest

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.live.chain import ChainError
from oasis_agent.live.rpc_profile import LiveRpcProfile
from oasis_agent.live.startup_state import (
    SOURCES,
    StartupBlocker,
    StartupRefused,
    acquire_existing_lock,
    inspect_pair,
    inspect_service,
    paths,
    startup_locks,
)
from oasis_agent.live.state import Pending, StateStore, day_of
from oasis_agent.models.common import canonical_json, sha256_hex
from oasis_agent.rpc.budget import RpcBudget
from scripts import inspect_live_startup, run_live_authority, run_live_lp, start_live_deployment
from tests.unit.gas_oracle_fixtures import synthetic_gas_pins
from tests.unit.test_live_precheck_evidence import capture
from tests.unit.test_live_receipt_costs import position_config
from tests.unit.test_live_valuation import NavFixture, synthetic_feed_pin  # noqa: F401


def prepare(directory, role, *, execution=False):
    directory.mkdir(mode=0o700)
    config, profile = position_config(), LiveRpcProfile(max_requests=3, min_interval_ms=100)
    if execution:
        config = replace(config, expected_pins=config.expected_pins | synthetic_gas_pins())
    state_path, evidence_path, lock_path = paths(directory, role)
    fixture = NavFixture(
        position_enabled=False, native_after=10**17, token_after=100_000_000, usdc_price=100_000_000
    )
    with closing(EvidenceStore(evidence_path)) as evidence:
        value, refs = capture(evidence, config, fixture)
    store = StateStore(state_path)
    store.bind(sha256_hex(canonical_json(asdict(config))))
    state = store.load("live")
    state.nav_evidence = refs
    state.integer_risk().observe(
        day=day_of(fixture.timestamp), at=fixture.timestamp, nav=value.nav_micros
    )
    state.last_units = [value.wallet.native, value.wallet.token, 0]
    store.save(state)
    lock_path.touch()
    with RpcBudget(directory / "live.receipt-budget.db", profile.policies(SOURCES)) as budget:
        for source in SOURCES:
            budget.reserve(source, now_ms=1000)
    return config, profile, store


def snapshot(directory):
    return {
        p.name: p.read_bytes() for p in directory.iterdir() if p.is_file() and not p.is_symlink()
    }


@pytest.mark.parametrize(
    "database,suffix",
    [("journal", "-journal"), ("evidence", "-wal"), ("budget", "-shm")],
)
@pytest.mark.parametrize("fault", ["wal_mode", "sidecar"])
def test_startup_refuses_sqlite_recovery_artifacts_before_connecting(
    tmp_path, monkeypatch, database, suffix, fault
):
    directory = tmp_path / "strategy"
    config, profile, store = prepare(directory, "strategy")
    database_path = {
        "journal": store.journal_path,
        "evidence": paths(directory, "strategy")[1],
        "budget": directory / "live.receipt-budget.db",
    }[database]
    sidecar = database_path.with_name(database_path.name + suffix)
    if fault == "wal_mode":
        with closing(sqlite3.connect(database_path)) as db:
            assert db.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        assert not any(
            os.path.lexists(str(database_path) + s) for s in ("-wal", "-shm", "-journal")
        )
    else:
        sidecar.symlink_to("missing-recovery-file")
    before = snapshot(directory)
    connect = sqlite3.connect
    opened = []

    def record_connection(*args, **kwargs):
        opened.append(args[0])
        return connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", record_connection)
    with pytest.raises(ValueError, match=r"SQLite (recovery|rollback)"):
        inspect_service(directory, "strategy", config, profile)
    assert not opened
    assert snapshot(directory) == before
    if fault == "sidecar":
        assert os.readlink(sidecar) == "missing-recovery-file"


@pytest.mark.parametrize("role", ["strategy", "authority"])
@pytest.mark.parametrize(
    "fault",
    [
        None,
        "missing_state",
        "missing_journal",
        "empty_journal",
        "missing_evidence",
        "empty_evidence",
        "corrupt_evidence",
        "missing_budget",
        "empty_budget",
        "fresh_budget",
        "foreign_budget",
        "exhausted_budget",
        "corrupt_budget",
        "config",
        "stop",
        "broken_stop",
        "halt",
        "pending",
        "signing",
        "flow",
        "legacy",
        "loss",
        "baseline",
    ],
)
def test_original_runtime_refuses_without_mutation(tmp_path, role, fault):
    directory = tmp_path / role
    config, profile, store = prepare(directory, role)
    state_path, evidence_path, _ = paths(directory, role)
    budget_path = directory / "live.receipt-budget.db"
    files = {
        "state": state_path,
        "journal": store.journal_path,
        "evidence": evidence_path,
        "budget": budget_path,
    }
    if fault and fault.startswith("missing_"):
        files[fault.removeprefix("missing_")].unlink()
    elif fault and fault.startswith("empty_"):
        files[fault.removeprefix("empty_")].write_bytes(b"")
    elif fault == "corrupt_evidence":
        with sqlite3.connect(evidence_path) as db:
            db.execute("UPDATE evidence SET raw_response_payload=?", (b"corrupt",))
    elif fault in {"fresh_budget", "foreign_budget"}:
        budget_path.unlink()  # Synthetic fault only; never production manipulation.
        selected = replace(profile, max_requests=2) if fault == "foreign_budget" else profile
        with RpcBudget(budget_path, selected.policies(SOURCES)):
            pass
    elif fault == "exhausted_budget":
        with RpcBudget(budget_path, profile.policies(SOURCES)) as budget:
            budget.reserve(SOURCES[0], now_ms=1100)
            budget.reserve(SOURCES[0], now_ms=1200)
    elif fault == "corrupt_budget":
        with sqlite3.connect(budget_path) as db:
            db.execute("UPDATE rpc_usage SET digest=?", ("0" * 64,))
    elif fault == "config":
        config = replace(config, max_actions_per_day=config.max_actions_per_day - 1)
    elif fault == "stop":
        (directory / "STOP").touch()
    elif fault == "broken_stop":
        (directory / "STOP").symlink_to(directory / "absent")
    elif fault:
        state = store.load("live")
        if fault == "halt":
            state.halted = "operator review required"
        elif fault == "pending":
            state.pending = Pending("swap", "0x" + "ab" * 32, 3, 1000, {}, config.position_manager)
        elif fault == "signing":
            state.signing_attempt = {"nonce": 3}
        elif fault == "flow":
            state.acknowledge_flow = True
        elif fault == "legacy":
            state.legacy_gas_cost_incomplete = True
        elif fault == "loss":
            state.exact_risk.daily_locked = 200_000_000
        else:
            state.exact_risk = None
        store.save(state)
    before = snapshot(directory)
    if fault:
        with pytest.raises((ValueError, OSError, sqlite3.Error, ChainError)):
            inspect_service(directory, role, config, profile)
    else:
        result = inspect_service(directory, role, config, profile)
        assert result["checkpoint_verified"] and result["rpc_remaining"] == dict.fromkeys(
            SOURCES, 2
        )
    assert snapshot(directory) == before


@pytest.mark.parametrize("shared", [None, "journal", "evidence", "budget"])
def test_pair_holds_both_original_locks_and_refuses_shared_files(tmp_path, shared):
    a, b = tmp_path / "strategy", tmp_path / "authority"
    config, profile, first = prepare(a, "strategy")
    _, _, second = prepare(b, "authority")
    if shared:
        left, right = {
            "journal": (first.journal_path, second.journal_path),
            "evidence": (a / "live.receipts.db", b / "evidence.db"),
            "budget": (a / "live.receipt-budget.db", b / "live.receipt-budget.db"),
        }[shared]
        right.unlink()
        os.link(left, right)
        with pytest.raises(ValueError, match="distinct original"):
            inspect_pair(a, b, config, profile)
        return
    before = snapshot(a), snapshot(b)
    result = inspect_pair(a, b, config, profile)
    assert result["startup_prerequisites_passed"] and result["historical_only"]
    assert not result["execution_eligible"]
    with (a / "live.lock").open("rb") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            inspect_pair(a, b, config, profile)
    with (
        startup_locks({"strategy": a, "authority": b}),
        (b / "authority.lock").open("rb") as lock,
        pytest.raises(BlockingIOError),
    ):
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert (snapshot(a), snapshot(b)) == before


@pytest.mark.parametrize("fault", [None, "missing_stop", "authority_pending"])
def test_post_stop_replay_requires_private_markers_and_both_journals_resolved(tmp_path, fault):
    strategy, authority = tmp_path / "strategy", tmp_path / "authority"
    config, profile, _ = prepare(strategy, "strategy")
    _, _, signer = prepare(authority, "authority")
    for directory in (strategy, authority):
        (directory / "STOP").touch(mode=0o600)
    if fault == "missing_stop":
        (authority / "STOP").unlink()
    elif fault == "authority_pending":
        state = signer.load("live")
        state.pending = Pending("swap", "0x" + "ab" * 32, 3, 1000, {}, config.position_manager)
        signer.save(state)

    before = snapshot(strategy), snapshot(authority)
    if fault:
        with pytest.raises(StartupRefused) as exc:
            inspect_pair(strategy, authority, config, profile, expect_stop=True)
        assert exc.value.role == "authority"
        assert exc.value.code == (
            StartupBlocker.STOP_UNVERIFIED
            if fault == "missing_stop"
            else StartupBlocker.RECONCILIATION_REQUIRED
        )
    else:
        report = inspect_pair(strategy, authority, config, profile, expect_stop=True)
        assert report["stop_markers_present"] is True
        assert report["historical_only"] is True and report["execution_eligible"] is False
        assert all(service["settled_history_replayed"] for service in report["services"].values())
    assert (snapshot(strategy), snapshot(authority)) == before


@pytest.mark.parametrize("lost", ["state", "journal", "evidence", "budget"])
def test_runtime_open_never_recreates_files_lost_after_preflight(tmp_path, lost):
    directory = tmp_path / "strategy"
    config, profile, original = prepare(directory, "strategy")
    inspect_service(directory, "strategy", config, profile)
    store = StateStore(original.path, require_existing=True)
    store.bind(sha256_hex(canonical_json(asdict(config))))
    state = store.load("live")
    selected = {
        "state": original.path,
        "journal": original.journal_path,
        "evidence": directory / "live.receipts.db",
        "budget": directory / "live.receipt-budget.db",
    }[lost]
    selected.unlink()
    with pytest.raises((ValueError, sqlite3.Error)):
        if lost == "evidence":
            EvidenceStore(selected, require_existing=True)
        elif lost == "budget":
            RpcBudget(selected, profile.policies(SOURCES), require_existing=True)
        else:
            store.bind(sha256_hex(canonical_json(asdict(config))))
    if lost in {"state", "journal"}:
        with pytest.raises(ValueError):
            store.save(state)
    assert not selected.exists()


@pytest.mark.parametrize("role", ["strategy", "authority"])
@pytest.mark.parametrize("fault", ["halt", "lost_after_first_check"])
def test_production_cli_checks_before_rpc_environment_key_or_signer(
    tmp_path, monkeypatch, role, fault
):
    directory = tmp_path / role
    config, profile, store = prepare(directory, role, execution=True)
    module = run_live_lp if role == "strategy" else run_live_authority
    config_path, profile_path = tmp_path / "config.json", tmp_path / "profile.json"
    config_path.write_bytes(canonical_json(asdict(config)))
    profile_path.write_bytes(canonical_json(asdict(profile)))
    if fault == "halt":
        state = store.load("live")
        state.halted = "preserved"
        store.save(state)
    else:
        calls = []

        def check(*args):
            result = inspect_service(*args)
            calls.append(1)
            if len(calls) == 1:
                store.journal_path.unlink()  # Synthetic inter-check loss only.
            return result

        monkeypatch.setattr(module, "inspect_service", check)

    def forbidden(*args, **kwargs):
        pytest.fail("prerequisites must precede network/environment/key/signer access")

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
    monkeypatch.delenv("BASE_RPC_PRIMARY_ALT", raising=False)
    before = snapshot(directory)
    with pytest.raises((SystemExit, ValueError, OSError)):
        module.main()
    expected = (
        before
        if fault == "halt"
        else {k: v for k, v in before.items() if k != store.journal_path.name}
    )
    assert snapshot(directory) == expected


def test_readonly_budget_cannot_reserve_or_repair_and_evidence_schema_is_not_migrated(tmp_path):
    path = tmp_path / "budget.db"
    policies = LiveRpcProfile().policies(SOURCES)
    with RpcBudget(path, policies) as budget:
        budget.reserve(SOURCES[0], now_ms=1)
    before = path.read_bytes()
    with RpcBudget(path, policies, read_only=True) as budget:
        assert budget.counts()[SOURCES[0]] == 1
        with pytest.raises(sqlite3.OperationalError):
            budget.reserve(SOURCES[1], now_ms=1)
    assert path.read_bytes() == before
    legacy = tmp_path / "legacy.db"
    with sqlite3.connect(legacy) as db:
        db.execute("CREATE TABLE evidence (content_hash TEXT PRIMARY KEY, payload BLOB)")
    before = legacy.read_bytes()
    with pytest.raises(ValueError, match="schema"):
        EvidenceStore(legacy, require_existing=True)
    assert legacy.read_bytes() == before


def test_inspector_cli_is_readonly_and_redacts_refusal(tmp_path, monkeypatch, capsys):
    a, b = tmp_path / "strategy", tmp_path / "authority"
    config, profile, _ = prepare(a, "strategy")
    prepare(b, "authority")
    config_path, profile_path = tmp_path / "config.json", tmp_path / "profile.json"
    config_path.write_bytes(canonical_json(asdict(config)))
    profile_path.write_bytes(canonical_json(asdict(profile)))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "inspect",
            "--strategy-dir",
            str(a),
            "--authority-dir",
            str(b),
            "--config",
            str(config_path),
            "--rpc-profile",
            str(profile_path),
        ],
    )
    before = snapshot(a), snapshot(b)
    with pytest.raises(SystemExit) as exc:
        inspect_live_startup.main()
    assert exc.value.code == 0
    raw = capsys.readouterr().out.encode()
    monkeypatch.setattr(start_live_deployment, "command", lambda *_: raw)
    validated = start_live_deployment.state_preflight(
        "synthetic-image",
        {},
        config_hash=sha256_hex(canonical_json(asdict(config))),
        rpc_max_requests=profile.max_requests,
    )
    assert validated == json.loads(raw) and validated["execution_eligible"] is False
    assert (snapshot(a), snapshot(b)) == before
    (b / "STOP").write_text("private operator text")
    before = snapshot(a), snapshot(b)
    with pytest.raises(SystemExit) as exc:
        inspect_live_startup.main()
    raw = capsys.readouterr().out
    refusal = json.loads(raw)
    assert exc.value.code == 3 and refusal["blockers"] == ["stop_present"]
    assert refusal["blocked_service"] == "authority"
    assert refusal["startup_prerequisites_passed"] is refusal["execution_eligible"] is False
    assert "private operator text" not in raw
    assert (snapshot(a), snapshot(b)) == before


def test_stale_projection_cannot_hide_newer_journal_halt(tmp_path):
    directory = tmp_path / "strategy"
    config, profile, store = prepare(directory, "strategy")
    projection = store.path.read_bytes()
    state = store.load("live")
    state.halted = "new journal halt after projection interruption"
    store.journal.append(asdict(state))
    with pytest.raises(ValueError, match="halt"):
        inspect_service(directory, "strategy", config, profile)
    assert store.path.read_bytes() == projection


def test_lock_replacement_between_check_and_open_refuses(tmp_path, monkeypatch):
    a, b = tmp_path / "strategy", tmp_path / "authority"
    config, profile, _ = prepare(a, "strategy")
    prepare(b, "authority")
    original_open = os.open
    lock = a / "live.lock"
    replaced = []

    def raced(path, *args, **kwargs):
        if path == lock and not replaced:
            replaced.append(True)
            path.rename(a / "original-lock")
            path.touch()
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", raced)
    with pytest.raises(ValueError, match="lock changed"):
        inspect_pair(a, b, config, profile)


@pytest.mark.parametrize("exclusive", [False, True])
def test_lock_path_replaced_after_acquisition_refuses_both_probe_and_runtime(
    tmp_path, monkeypatch, exclusive
):
    lock_path = tmp_path / "live.lock"
    lock_path.touch()
    original_flock = fcntl.flock

    def raced(handle, operation):
        original_flock(handle, operation)
        lock_path.rename(tmp_path / "old-lock")
        lock_path.touch()

    monkeypatch.setattr(fcntl, "flock", raced)
    with pytest.raises(ValueError, match="lock changed"):
        acquire_existing_lock(lock_path, exclusive=exclusive)


@pytest.mark.parametrize(
    "option",
    [
        "--readiness",
        "--check",
        "--report",
        "--clear-halt",
        "--drop-pending",
        "--acknowledge-flow",
        "--reconcile-only",
        "shadow",
    ],
)
def test_strategy_production_flag_is_never_silently_ignored(monkeypatch, option):
    argv = [
        "run",
        "--mode",
        "shadow" if option == "shadow" else "live",
        "--require-existing-runtime",
    ]
    if option != "shadow":
        argv.append(option)
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as exc:
        run_live_lp.main()
    assert exc.value.code == 2


@pytest.mark.parametrize("option", ["--status-only", "--reconcile-only"])
def test_authority_production_flag_is_never_silently_ignored(monkeypatch, option):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run",
            "--config",
            "/unused",
            "--state-dir",
            "/unused",
            "--require-existing-runtime",
            option,
        ],
    )
    with pytest.raises(SystemExit) as exc:
        run_live_authority.main()
    assert exc.value.code == 2
