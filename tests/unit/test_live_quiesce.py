"""Missing HALT append only: no projections, balances, budgets, price or keys."""

import json
import sqlite3
import sys
from dataclasses import asdict

import pytest

from oasis_agent.live import quiesce
from oasis_agent.live.journal import LiveJournal
from oasis_agent.live.startup_state import acquire_existing_lock
from oasis_agent.models.common import canonical_json
from scripts import quiesce_live_services as cli
from tests.unit.test_live_lp import CONFIG
from tests.unit.test_live_offline_inspection import pair
from tests.unit.test_live_rearm import pair as rearm_pair
from tests.unit.test_live_rearm import review
from tests.unit.test_live_valuation import synthetic_feed_pin  # noqa: F401


def setup(tmp_path):
    a, b = pair(tmp_path)
    roots = []
    for role, store in zip(quiesce.ROLES, (a, b), strict=True):
        root = tmp_path / role
        root.mkdir(mode=0o700)
        state, _, lock = quiesce.paths(root, role)
        journal = store.journal_path
        store.path.rename(state)
        journal.rename(state.with_suffix(state.suffix + ".journal.sqlite"))
        for path in (lock, root / "STOP"):
            path.touch(mode=0o600)
        roots.append(root)
    return roots


def files(roots):
    return {str(p): p.read_bytes() for root in roots for p in root.iterdir() if p.is_file()}


def loaded(root, role):
    store = quiesce.StateStore(quiesce.paths(root, role)[0], require_existing=True)
    journal = LiveJournal(store.journal_path, require_existing=True)
    return store, journal.load(read_only=True)


@pytest.mark.parametrize("existing", [None, "private earlier halt"])
def test_review_and_apply_preserve_every_field_except_missing_halt(tmp_path, existing):
    roots = setup(tmp_path)
    right, _ = loaded(roots[1], "authority")
    if existing:
        state = right.load("live")
        state.halted = existing
        right.save(state)
    before = files(roots)
    original = {
        role: json.loads(loaded(root, role)[1])
        for role, root in zip(quiesce.ROLES, roots, strict=True)
    }
    report = quiesce.review_pair(*roots, CONFIG)
    assert files(roots) == before and report["rpc_requests"] == 0
    assert "private earlier halt" not in json.dumps(report)
    result = quiesce.apply_pair(*roots, CONFIG, report, report["plan_hash"])
    assert result["quiesce_applied"] and result["atomic_pair"] is False
    assert result["execution_eligible"] is False and not result["json_projections_written"]
    for role, root in zip(quiesce.ROLES, roots, strict=True):
        store, payload = loaded(root, role)
        state = json.loads(payload)
        expected = original[role]
        expected["halted"] = (
            existing if role == "authority" and existing else quiesce.MAINTENANCE_HALT
        )
        assert state == expected
        for name, content in before.items():
            if not name.endswith(".journal.sqlite"):
                assert quiesce.Path(name).read_bytes() == content
        assert store.load("live", read_only=True).halted == expected["halted"]
    assert not any("budget" in name for name in files(roots))
    second = quiesce.review_pair(*roots, CONFIG)
    frozen = files(roots)
    quiesce.apply_pair(*roots, CONFIG, second, second["plan_hash"])
    assert frozen == files(roots)  # Existing HALTs are exact no-ops.


def test_raw_legacy_defaults_are_not_added_by_halt_append(tmp_path):
    roots = setup(tmp_path)
    for role, root in zip(quiesce.ROLES, roots, strict=True):
        store, payload = loaded(root, role)
        raw = json.loads(payload)
        raw.pop("last_light_check")
        journal = LiveJournal(store.journal_path, require_existing=True)
        journal.load(read_only=True)
        journal.append(raw)
    report = quiesce.review_pair(*roots, CONFIG)
    quiesce.apply_pair(*roots, CONFIG, report, report["plan_hash"])
    assert all(
        "last_light_check" not in json.loads(loaded(root, role)[1])
        for role, root in zip(quiesce.ROLES, roots, strict=True)
    )


@pytest.mark.parametrize("fault", ["approval", "state", "stop", "sidecar", "wal", "lock", "shared"])
def test_refusal_never_repairs_or_rewrites_originals(tmp_path, fault):
    roots = setup(tmp_path)
    report = quiesce.review_pair(*roots, CONFIG)
    store, _ = loaded(roots[0], "strategy")
    handle = None
    if fault == "approval":
        report["services"]["strategy"]["target_state_hash"] = "ab" * 32
    elif fault == "state":
        state = store.load("live")
        state.actions_today += 1
        store.save(state)
    elif fault == "stop":
        (roots[0] / "STOP").unlink()
    elif fault == "sidecar":
        quiesce.Path(str(store.journal_path) + "-journal").write_bytes(b"retained")
    elif fault == "wal":
        with sqlite3.connect(store.journal_path) as db:
            db.execute("PRAGMA journal_mode=WAL")
    elif fault == "lock":
        handle = acquire_existing_lock(roots[0] / "live.lock", exclusive=True)
    elif fault == "shared":
        roots[1] = roots[0]
    before = files(roots)
    try:
        with pytest.raises((ValueError, OSError)):
            quiesce.apply_pair(*roots, CONFIG, report, report["plan_hash"])
    finally:
        if handle:
            handle.close()
    assert files(roots) == before


def test_failure_after_one_commit_keeps_added_halt_and_original_stop(tmp_path, monkeypatch):
    roots = setup(tmp_path)
    report = quiesce.review_pair(*roots, CONFIG)
    original = sqlite3.connect
    writes = []

    def fail_second(database, **kwargs):
        if database.endswith("?mode=rw"):
            writes.append(database)
            if len(writes) == 2:
                raise sqlite3.OperationalError("fixture failure")
        return original(database, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", fail_second)
    with pytest.raises(sqlite3.OperationalError):
        quiesce.apply_pair(*roots, CONFIG, report, report["plan_hash"])
    assert json.loads(loaded(roots[0], "strategy")[1])["halted"] == quiesce.MAINTENANCE_HALT
    assert json.loads(loaded(roots[1], "authority")[1])["halted"] is None
    assert all((root / "STOP").is_file() for root in roots)
    with pytest.raises(ValueError, match="exact reviewed"):
        quiesce.apply_pair(*roots, CONFIG, report, report["plan_hash"])


@pytest.mark.parametrize("apply", [False, True])
def test_cli_exact_approval_marker_and_no_automatic_retry(tmp_path, monkeypatch, capsys, apply):
    roots = setup(tmp_path)
    config = tmp_path / "config.json"
    config.write_bytes(canonical_json(asdict(CONFIG)))
    report = quiesce.review_pair(*roots, CONFIG)
    plan = tmp_path / "plan.json"
    plan.write_bytes(canonical_json(report))
    out = tmp_path / "attempt"
    args = [
        "quiesce",
        "--strategy-dir",
        str(roots[0]),
        "--authority-dir",
        str(roots[1]),
        "--config",
        str(config),
    ]
    if apply:
        args += [
            "--apply",
            "--plan-file",
            str(plan),
            "--approved-plan-hash",
            report["plan_hash"],
            "--out",
            str(out),
        ]
    monkeypatch.setattr(sys, "argv", args)
    before = files(roots)
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    result = json.loads(capsys.readouterr().out)
    assert not result["execution_eligible"]
    if apply:
        assert (out / "attempt.json").is_file()
        assert json.loads((out / "report.json").read_bytes())["quiesce_applied"] is True
        frozen = files(roots)
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 3 and files(roots) == frozen
    else:
        assert files(roots) == before and not out.exists()


def test_rearm_maintenance_halt_still_requires_existing_fresh_proofs(tmp_path, monkeypatch):
    data = rearm_pair(tmp_path, monkeypatch)
    for store in data[1].values():
        state = store.load("live")
        state.halted = quiesce.MAINTENANCE_HALT
        store.save(state)
    assert review(data)["rearm_prerequisites_passed"]
    next(iter(data[1].values())).journal_path.unlink()
    with pytest.raises((ValueError, OSError, sqlite3.Error)):
        review(data)


def test_locked_mode_change_is_refused_without_conversion_or_halt_write(tmp_path, monkeypatch):
    roots = setup(tmp_path)
    report = quiesce.review_pair(*roots, CONFIG)
    store, payload = loaded(roots[0], "strategy")
    projections = {
        str(quiesce.paths(root, role)[0]): quiesce.paths(root, role)[0].read_bytes()
        for role, root in zip(quiesce.ROLES, roots, strict=True)
    }
    connect = sqlite3.connect
    flips = []

    def race(database, **kwargs):
        if database.endswith("?mode=rw") and not flips:
            flips.append(True)
            with connect(store.journal_path) as other:
                assert other.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        return connect(database, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", race)
    with pytest.raises(ValueError, match="locked DELETE"):
        quiesce.apply_pair(*roots, CONFIG, report, report["plan_hash"])
    assert loaded(roots[0], "strategy")[1] == payload
    assert all(quiesce.Path(name).read_bytes() == value for name, value in projections.items())
    with connect(store.journal_path) as db:
        assert db.execute("PRAGMA journal_mode").fetchone() == ("wal",)


@pytest.mark.parametrize("fault", ["typed_halt", "size"])
def test_target_does_not_coerce_halt_or_exceed_journal_bound(tmp_path, monkeypatch, fault):
    roots = setup(tmp_path)
    raw = json.loads(loaded(roots[0], "strategy")[1])
    if fault == "typed_halt":
        raw["halted"] = False
    else:
        monkeypatch.setattr(quiesce, "MAX_SNAPSHOT", len(canonical_json(raw)) + 1)
    with pytest.raises(ValueError):
        quiesce._target(canonical_json(raw))


def test_cli_partial_failure_is_unknown_and_never_retries(tmp_path, monkeypatch, capsys):
    roots = setup(tmp_path)
    config = tmp_path / "config.json"
    config.write_bytes(canonical_json(asdict(CONFIG)))
    report = quiesce.review_pair(*roots, CONFIG)
    plan = tmp_path / "plan.json"
    plan.write_bytes(canonical_json(report))
    out = tmp_path / "attempt"
    connect = sqlite3.connect
    writes = []

    def fail(database, **kwargs):
        if database.endswith("?mode=rw"):
            writes.append(database)
            assert (out / "attempt.json").exists()
            if len(writes) == 2:
                raise sqlite3.OperationalError("private diagnostic")
        return connect(database, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", fail)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "quiesce",
            "--strategy-dir",
            str(roots[0]),
            "--authority-dir",
            str(roots[1]),
            "--config",
            str(config),
            "--apply",
            "--plan-file",
            str(plan),
            "--approved-plan-hash",
            report["plan_hash"],
            "--out",
            str(out),
        ],
    )
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 3
    result = json.loads(capsys.readouterr().out)
    assert result["quiesce_applied"] is None
    assert result["status"] == "unresolved_inspect_originals_no_retry"
    assert "private diagnostic" not in json.dumps(result)
    assert len(writes) == 2
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 3 and len(writes) == 2


def test_existing_readable_owned_lock_files_need_no_permission_rewrite(tmp_path):
    roots = setup(tmp_path)
    for role, root in zip(quiesce.ROLES, roots, strict=True):
        quiesce.paths(root, role)[2].chmod(0o644)
    before = files(roots)
    report = quiesce.review_pair(*roots, CONFIG)
    result = quiesce.apply_pair(*roots, CONFIG, report, report["plan_hash"])
    assert result["quiesce_applied"]
    for name, content in before.items():
        if not name.endswith(".journal.sqlite"):
            assert quiesce.Path(name).read_bytes() == content
    assert all(
        quiesce.paths(root, role)[2].stat().st_mode & 0o777 == 0o644
        for role, root in zip(quiesce.ROLES, roots, strict=True)
    )
