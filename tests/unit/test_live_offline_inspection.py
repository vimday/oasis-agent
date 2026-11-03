"""Offline inspection verifies retained histories without taking write authority."""

import json
import sqlite3
import sys
from dataclasses import asdict, replace

import pytest

from oasis_agent.live.journal import LiveJournal
from oasis_agent.live.state import Pending, Position, StateStore
from oasis_agent.models.common import canonical_json, sha256_hex
from scripts import inspect_live_state as cli
from tests.unit.test_live_lp import CONFIG


def pair(tmp_path, config=CONFIG):
    stores = []
    for name in ("strategy", "authority"):
        store = StateStore(tmp_path / f"{name}.json")
        store.bind(sha256_hex(canonical_json(asdict(config))))
        state = store.load("live")
        state.position = Position(123, -100, 100, 77, len(stores))
        store.save(state)
        stores.append(store)
    return stores


def snapshot(path):
    return {p.name: p.read_bytes() for p in path.iterdir() if p.is_file()}


def test_read_only_inspection_preserves_bytes_and_uses_read_only_sqlite(tmp_path, monkeypatch):
    a, b = pair(tmp_path)
    before = snapshot(tmp_path)
    connect = sqlite3.connect
    modes = []

    def readonly(database, **kwargs):
        modes.append(database)
        assert database.endswith("?mode=ro") and kwargs["uri"] is True
        db = connect(database, **kwargs)
        # Even session-level PRAGMA mutations are not part of this read path.
        db.set_authorizer(
            lambda action, *_: (
                sqlite3.SQLITE_DENY
                if action in (sqlite3.SQLITE_PRAGMA, sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE)
                else sqlite3.SQLITE_OK
            )
        )
        return db

    monkeypatch.setattr(sqlite3, "connect", readonly)
    report = cli.inspect(a.path, b.path, CONFIG)
    assert len(modes) == 2
    assert report["inspection_passed"] and not report["blockers"]
    assert report["historical_only"] and not report["execution_eligible"]
    assert report["services"]["strategy"]["journal_tip"] == a.journal.revision
    digest = report.pop("evidence_hash")
    assert digest == sha256_hex(canonical_json(report))
    assert snapshot(tmp_path) == before


def test_old_binding_is_reported_without_migration_or_latch_clear(tmp_path):
    old = replace(CONFIG, usdc_oracle_max_age_seconds=90_000)
    a, b = pair(tmp_path, old)
    state = a.load("live")
    state.halted = "untrusted private diagnostic must not be echoed"
    a.save(state)
    before = snapshot(tmp_path)
    report = cli.inspect(a.path, b.path, CONFIG)
    assert report["inspection_passed"]
    assert set(report["blockers"]) == {
        "strategy_halted",
        "strategy_configuration_mismatch",
        "authority_configuration_mismatch",
    }
    assert "private diagnostic" not in json.dumps(report)
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("fault", ["pending", "signing", "position", "legacy"])
def test_service_blockers_are_retained(tmp_path, fault):
    a, b = pair(tmp_path)
    state = a.load("live")
    if fault == "pending":
        state.pending = Pending("mint", "0x" + "ab" * 32, 7, 100)
        blocker = "strategy_has_pending"
    elif fault == "signing":
        state.signing_attempt = {"nonce": 7}
        blocker = "strategy_has_signing_attempt"
    elif fault == "position":
        state.position.liquidity += 1
        blocker = "service_position_disagreement"
    else:
        state.legacy_gas_cost_incomplete = True
        blocker = "strategy_legacy_gas_cost_incomplete"
    a.save(state)
    before = snapshot(tmp_path)
    assert blocker in cli.inspect(a.path, b.path, CONFIG)["blockers"]
    assert before == snapshot(tmp_path)


def test_same_journal_cannot_stand_for_two_services(tmp_path):
    a, _ = pair(tmp_path)
    with pytest.raises(ValueError, match="distinct service journals"):
        cli.inspect(a.path, a.path, CONFIG)


def test_corrupt_chain_is_refused_not_reported_as_healthy(tmp_path):
    a, b = pair(tmp_path)
    with sqlite3.connect(a.journal_path) as db:
        db.execute("DROP TRIGGER no_update")
        db.execute("UPDATE events SET digest = ? WHERE seq = 1", ("0" * 64,))
    before = snapshot(tmp_path)
    with pytest.raises(ValueError, match="journal chain"):
        cli.inspect(a.path, b.path, CONFIG)
    assert before == snapshot(tmp_path)


def test_stale_projection_uses_verified_tip_without_rewriting_projection(tmp_path):
    a, b = pair(tmp_path)
    old_projection = a.path.read_bytes()
    state = a.load("live")
    state.halted = "new halt"
    a.save(state)
    a.path.write_bytes(old_projection)  # A crash may leave an older committed projection.
    before = snapshot(tmp_path)
    assert "strategy_halted" in cli.inspect(a.path, b.path, CONFIG)["blockers"]
    assert snapshot(tmp_path) == before


def test_missing_journal_is_never_created_and_cli_redacts_refusal(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "inspect",
            "--strategy-state",
            str(tmp_path / "secret-input"),
            "--authority-state",
            str(tmp_path / "other"),
        ],
    )
    with pytest.raises(SystemExit) as caught:
        cli.main()
    assert caught.value.code == 3 and not list(tmp_path.iterdir())
    output = capsys.readouterr().out
    assert "secret-input" not in output
    assert json.loads(output)["blockers"] == ["retained_state_unresolved"]


def test_read_only_missing_file_does_not_recreate_it(tmp_path):
    a, _ = pair(tmp_path)
    journal = LiveJournal(a.journal_path, require_existing=True)
    a.journal_path.unlink()
    with pytest.raises(sqlite3.OperationalError):
        journal.load(read_only=True)
    assert not a.journal_path.exists()


@pytest.mark.parametrize("blocked", [False, True])
def test_cli_exit_and_no_state_changes(tmp_path, monkeypatch, capsys, blocked):
    a, b = pair(tmp_path)
    if blocked:
        state = a.load("live")
        state.halted = "halt"
        a.save(state)
    before = snapshot(tmp_path)
    monkeypatch.setattr(
        sys, "argv", ["inspect", "--strategy-state", str(a.path), "--authority-state", str(b.path)]
    )
    with pytest.raises(SystemExit) as caught:
        cli.main()
    assert caught.value.code == (3 if blocked else 0)
    assert not json.loads(capsys.readouterr().out)["execution_eligible"]
    assert snapshot(tmp_path) == before
