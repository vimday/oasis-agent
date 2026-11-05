"""Offline original-journal inspection and untrusted redacted console declarations."""

import json
import os
import sqlite3
import sys
from copy import deepcopy
from dataclasses import asdict, replace

import pytest

from oasis_agent.live.journal import LiveJournal
from oasis_agent.live.startup_state import paths
from oasis_agent.models.common import canonical_json, sha256_hex
from oasis_agent.reporting.live_pair import read_pair_status
from oasis_agent.reporting.live_status import read_live_status
from oasis_agent.server.config import load_server_config
from scripts import inspect_live_state as cli
from tests.unit.server_helpers import make_server_config
from tests.unit.test_live_lp import CONFIG
from tests.unit.test_live_offline_inspection import pair, snapshot
from tests.unit.test_live_runtime_epoch import apply, prepare
from tests.unit.test_live_status import NOW, observed_state, write_state
from tests.unit.test_live_valuation import synthetic_feed_pin  # noqa: F401
from tests.unit.test_server_api import client_for


def retain(tmp_path, report):
    path = tmp_path / "inspection.json"
    path.write_bytes(canonical_json(report))
    return path


def reseal(report):
    report["evidence_hash"] = sha256_hex(
        canonical_json({key: value for key, value in report.items() if key != "evidence_hash"})
    )
    return report


def exported(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.time, "time", lambda: NOW)
    a, b = pair(tmp_path)
    return a, b, cli.export_pair_status(a.path, b.path, CONFIG)


def test_export_replays_both_originals_without_state_budget_or_sqlite_writes(tmp_path, monkeypatch):
    a, b = pair(tmp_path)
    before = snapshot(tmp_path)
    connect = sqlite3.connect
    opens = []

    def readonly(database, **kwargs):
        assert database.endswith("?mode=ro&immutable=1") and kwargs["uri"] is True
        opens.append(database)
        db = connect(database, **kwargs)
        db.set_authorizer(
            lambda action, *_: (
                sqlite3.SQLITE_DENY
                if action
                in (
                    sqlite3.SQLITE_PRAGMA,
                    sqlite3.SQLITE_INSERT,
                    sqlite3.SQLITE_UPDATE,
                    sqlite3.SQLITE_DELETE,
                    sqlite3.SQLITE_CREATE_TABLE,
                )
                else sqlite3.SQLITE_OK
            )
        )
        return db

    monkeypatch.setattr(sqlite3, "connect", readonly)
    report = cli.export_pair_status(a.path, b.path, CONFIG)
    assert len(opens) == 2 and snapshot(tmp_path) == before
    assert not report["execution_eligible"] and report["rpc_requests"] == 0
    path = retain(tmp_path, report)
    status = read_pair_status(
        path, now=report["inspected_at"], startup_config_hash=report["expected_config_hash"]
    )
    assert status.status == "aligned" and not status.blockers
    assert status.evidence_replayed is False


@pytest.mark.parametrize("kind", ["file", "broken_symlink"])
def test_pair_cli_refuses_start_with_stop_on_one_service(tmp_path, monkeypatch, capsys, kind):
    strategy_dir, authority_dir = tmp_path / "strategy", tmp_path / "authority"
    strategy_dir.mkdir()
    authority_dir.mkdir()
    a, _ = pair(strategy_dir)
    _, b = pair(authority_dir)
    stop = authority_dir / "STOP"
    if kind == "file":
        stop.write_text("private operator text")
    else:
        stop.symlink_to("unresolved-target")
    before = snapshot(strategy_dir), snapshot(authority_dir)
    output = tmp_path / "inspection.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "inspect",
            "--strategy-state",
            str(a.path),
            "--authority-state",
            str(b.path),
            "--pair-status-out",
            str(output),
        ],
    )
    with pytest.raises(SystemExit) as caught:
        cli.main()
    raw = capsys.readouterr().out
    view = json.loads(raw)
    assert caught.value.code == 3 and view["blockers"] == ["authority_stop_present"]
    assert view["status"] == "aligned" and view["execution_eligible"] is False
    assert view["inspection"]["services"]["authority"]["stop_present"] is True
    assert view["inspection"]["services"]["strategy"]["stop_present"] is False
    assert "private operator text" not in raw and "unresolved-target" not in raw
    assert (snapshot(strategy_dir), snapshot(authority_dir)) == before
    if kind == "broken_symlink":
        assert os.readlink(stop) == "unresolved-target"


def test_legacy_pair_report_keeps_digest_and_marks_stop_unverified(tmp_path, monkeypatch):
    _, _, report = exported(tmp_path, monkeypatch)
    for service in report["services"].values():
        service.pop("stop_present", None)
    path = retain(tmp_path, reseal(report))
    status = read_pair_status(path, now=NOW, startup_config_hash=report["expected_config_hash"])
    assert status.status == "aligned"
    assert status.blockers == ["strategy_stop_unverified", "authority_stop_unverified"]
    assert status.inspection.model_dump(by_alias=True) == report


@pytest.mark.parametrize("legacy", [False, True])
def test_real_paired_epoch_and_stale_projection_are_distinguished(tmp_path, monkeypatch, legacy):
    data, review = prepare(tmp_path, monkeypatch, legacy=legacy)
    roots, target, *_ = data
    a, b = [
        cli.StateStore(cli_path)
        for cli_path in (paths(roots[0], "strategy")[0], paths(roots[1], "authority")[0])
    ]
    old = a.path.read_bytes()
    assert apply(data, review)["projection_complete"]
    a.path.write_bytes(old)  # A real committed epoch with a crash before JSON projection.
    before = {str(p): p.read_bytes() for root in roots for p in root.iterdir() if p.is_file()}
    report = cli.export_pair_status(a.path, b.path, target)
    assert all(s["runtime_epoch_present"] for s in report["services"].values())
    assert not report["services"]["strategy"]["projection_current"]
    assert report["services"]["authority"]["projection_current"]
    path = retain(tmp_path, report)
    status = read_pair_status(
        path, now=report["inspected_at"], startup_config_hash=report["expected_config_hash"]
    )
    assert status.status == "projection_incomplete"
    assert status.blockers == [
        "strategy_projection_incomplete",
        "strategy_halted",
        "strategy_stop_present",
        "authority_halted",
        "authority_stop_present",
    ]
    assert all(
        p.read_bytes() == before[str(p)]
        for root in roots
        for p in root.iterdir()
        if str(p) in before
    )


@pytest.mark.parametrize(
    "fault",
    [
        "chain",
        "projection",
        "hardlink",
        "symlink",
        "permissions",
        "wal",
        "sidecar",
        "same",
        "race",
        "stop_race",
    ],
)
def test_export_refuses_unresolved_originals_without_repair(tmp_path, monkeypatch, fault):
    a, b = pair(tmp_path)
    if fault == "chain":
        with sqlite3.connect(a.journal_path) as db:
            db.execute("DROP TRIGGER no_update")
            db.execute("UPDATE events SET digest = ? WHERE seq = 1", ("0" * 64,))
    elif fault == "projection":
        record = json.loads(a.path.read_bytes())
        record["halted"] = "forged"
        a.path.write_bytes(canonical_json(record))
    elif fault == "hardlink":
        os.link(a.journal_path, tmp_path / "alias")
    elif fault == "symlink":
        a.path.rename(tmp_path / "original")
        a.path.symlink_to(tmp_path / "original")
    elif fault == "permissions":
        a.journal_path.chmod(0o644)
    elif fault == "wal":
        with sqlite3.connect(a.journal_path) as db:
            assert db.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
    elif fault == "sidecar":
        (tmp_path / (a.journal_path.name + "-journal")).write_bytes(b"unresolved")
    elif fault == "same":
        b = a
    elif fault in {"race", "stop_race"}:
        original = LiveJournal._read

        def advance(self, db, **kwargs):
            result = original(self, db, **kwargs)
            if self.path == a.journal_path:
                if fault == "stop_race":
                    (b.path.parent / "STOP").touch()
                else:
                    state = b.load("live")
                    state.halted = "advanced"
                    b.save(state)
            return result

        monkeypatch.setattr(LiveJournal, "_read", advance)
    before = snapshot(tmp_path)
    with pytest.raises((ValueError, sqlite3.Error)):
        cli.export_pair_status(a.path, b.path, CONFIG)
    if fault not in {"race", "stop_race"}:
        assert snapshot(tmp_path) == before


@pytest.mark.parametrize(
    "fault",
    [
        "hash",
        "extra",
        "bool_rpc",
        "eligible",
        "historical",
        "future",
        "duplicate",
        "float_clock",
        "projection",
        "services",
        "raw_error",
        "null_stop",
    ],
)
def test_reader_rejects_corrupt_or_incoherent_declarations(tmp_path, monkeypatch, fault):
    _, _, report = exported(tmp_path, monkeypatch)
    if fault == "hash":
        report["evidence_hash"] = "0" * 64
    elif fault == "extra":
        report["runtime_approved"] = True
    elif fault == "bool_rpc":
        report["rpc_requests"] = False
    elif fault == "eligible":
        report["execution_eligible"] = 0
    elif fault == "historical":
        report["historical_only"] = 1
    elif fault == "future":
        report["inspected_at"] += 1
    elif fault == "float_clock":
        report["inspected_at"] = float(NOW)
    elif fault == "projection":
        report["services"]["strategy"]["projection_current"] = False
    elif fault == "services":
        report["services"]["third"] = deepcopy(report["services"]["strategy"])
    elif fault == "raw_error":
        report["services"]["strategy"]["halted"] = "private diagnostic"
    elif fault == "null_stop":
        report["services"]["strategy"]["stop_present"] = None
    if fault != "hash":
        reseal(report)
    path = retain(tmp_path, report)
    if fault == "duplicate":
        path.write_text(
            path.read_text().replace('"rpc_requests":0', '"rpc_requests":0,"rpc_requests":0')
        )
    status = read_pair_status(path, now=NOW)
    assert status.status == "invalid" and status.inspection is None
    assert "private diagnostic" not in status.model_dump_json()


@pytest.mark.parametrize("age,expected", [(0, "aligned"), (300, "aligned"), (301, "stale")])
def test_refresh_does_not_renew_inspection_or_nav(tmp_path, monkeypatch, age, expected):
    _, _, report = exported(tmp_path, monkeypatch)
    path = retain(tmp_path, report)
    status = read_pair_status(
        path, now=NOW + age, startup_config_hash=report["expected_config_hash"]
    )
    assert status.status == expected and status.inspection_age_seconds == age
    live = read_live_status(
        write_state(tmp_path, observed_state()), now=NOW + age, pair_inspection=path
    )
    assert live.observation_age_seconds == age
    assert live.pair.inspection.inspected_at == NOW
    assert not live.execution_eligible and not live.evidence_replayed


def test_configuration_and_pending_signing_conflicts_are_retained(tmp_path, monkeypatch):
    a, b, _ = exported(tmp_path, monkeypatch)
    old = replace(CONFIG, max_capital_usd=400)
    report = cli.export_pair_status(a.path, b.path, old)
    path = retain(tmp_path, report)
    status = read_pair_status(path, now=NOW, startup_config_hash="ab" * 32)
    assert status.status == "configuration_mismatch"
    assert status.blockers == [
        "strategy_configuration_mismatch",
        "authority_configuration_mismatch",
        "start_configuration_mismatch",
    ]
    report["services"]["authority"]["config_hash"] = "cd" * 32
    report["services"]["authority"]["has_signing_attempt"] = True
    path.write_bytes(canonical_json(reseal(report)))
    status = read_pair_status(path, now=NOW)
    assert "authority_has_signing_attempt" in status.blockers
    assert "service_configuration_disagreement" in status.blockers
    assert "start_configuration_unverified" in status.blockers


@pytest.mark.parametrize("kind", ["missing", "symlink", "fifo", "oversize"])
def test_reader_is_bounded_and_never_creates_inputs(tmp_path, kind):
    path = tmp_path / "private-input"
    if kind == "symlink":
        target = tmp_path / "target"
        target.write_text("private diagnostic")
        path.symlink_to(target)
    elif kind == "fifo":
        os.mkfifo(path)
    elif kind == "oversize":
        path.write_bytes(b" " * 32_769)
    names = list(tmp_path.iterdir())
    status = read_pair_status(path)
    assert status.status == ("missing" if kind == "missing" else "invalid")
    assert list(tmp_path.iterdir()) == names and status.inspection is None


def test_cli_exclusive_export_and_api_refresh(tmp_path, monkeypatch, capsys):
    a, b, report = exported(tmp_path, monkeypatch)
    target = tmp_path / "config.json"
    target.write_bytes(canonical_json(asdict(CONFIG)))
    out = tmp_path / "export.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "inspect",
            "--strategy-state",
            str(a.path),
            "--authority-state",
            str(b.path),
            "--config",
            str(target),
            "--pair-status-out",
            str(out),
        ],
    )
    with pytest.raises(SystemExit) as caught:
        cli.main()
    assert caught.value.code == 0 and json.loads(out.read_bytes()) == report
    before = out.read_bytes()
    with pytest.raises(SystemExit) as caught:
        cli.main()
    assert caught.value.code == 3 and out.read_bytes() == before
    config_path = tmp_path / "server.toml"
    config_path.write_text(f'[databases]\nlive_pair_inspection = "{out}"\n')
    assert load_server_config(config_path).databases.live_pair_inspection == out
    config = make_server_config(tmp_path)
    config = replace(config, databases=replace(config.databases, live_pair_inspection=out))
    with client_for(tmp_path, config=config) as client:
        first = client.get("/api/v1/live/status")
        assert first.status_code == 200 and first.json()["pair"]["inspection"] is not None
        out.write_text('{"private diagnostic":')
        second = client.get("/api/v1/live/status", headers={"If-None-Match": first.headers["etag"]})
        assert second.status_code == 200 and second.json()["pair"]["status"] == "invalid"
        assert second.json()["pair"]["inspection"] is None
        assert "private diagnostic" not in second.text
        assert client.post("/api/v1/live/status").status_code == 405
    capsys.readouterr()


def test_export_cli_never_enters_legacy_sqlite_path(tmp_path, monkeypatch, capsys):
    a, b = pair(tmp_path)
    target = tmp_path / "config.json"
    target.write_bytes(canonical_json(asdict(CONFIG)))
    out = tmp_path / "new-inspection.json"
    connect = sqlite3.connect
    opens = []

    def readonly(database, **kwargs):
        assert database.endswith("?mode=ro&immutable=1")
        opens.append(database)
        return connect(database, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", readonly)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "inspect",
            "--strategy-state",
            str(a.path),
            "--authority-state",
            str(b.path),
            "--config",
            str(target),
            "--pair-status-out",
            str(out),
        ],
    )
    before = snapshot(tmp_path)
    with pytest.raises(SystemExit) as caught:
        cli.main()
    assert caught.value.code == 0 and len(opens) == 2
    assert out.is_file()
    assert all((tmp_path / name).read_bytes() == content for name, content in before.items())
    assert json.loads(capsys.readouterr().out)["status"] == "aligned"
