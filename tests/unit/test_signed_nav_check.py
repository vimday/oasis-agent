"""Offline consumer tests: historical authentic prices, synthetic chain and clocks."""

import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from dataclasses import asdict, replace

import pytest

from oasis_agent.evidence.exact_capture import wire_envelope
from oasis_agent.evidence.store import EvidenceStore, ReadOnlyEvidenceStore
from oasis_agent.live.signed_nav import NAV_KIND, replay_signed_nav, verify_signed_nav_quorum
from oasis_agent.live.state import Position
from oasis_agent.models.common import canonical_json
from scripts import verify_signed_nav as cli
from tests.unit.test_live_receipt_costs import position_config
from tests.unit.test_live_signed_nav import candidate, fixture
from tests.unit.test_lp_receipts import PRIMARY, SECONDARY
from tests.unit.test_signed_prices import TIMESTAMP

POSITION = Position(123, -100, 100, 77, 0)


def check(store, refs, *, now=TIMESTAMP, position=POSITION, sources=(PRIMARY, SECONDARY)):
    return verify_signed_nav_quorum(
        store, position_config(), refs, sources=sources, position=position, now_ms=now
    )


@pytest.mark.parametrize("position", [False, True])
def test_quorum_replay_and_readonly_store_never_change_evidence(tmp_path, position):
    path = tmp_path / "evidence.db"
    with closing(EvidenceStore(path)) as store:
        expected, refs = candidate(store, position=position)
    before = path.read_bytes()
    with closing(ReadOnlyEvidenceStore(path)) as store:
        result = check(store, refs, position=POSITION if position else None)
        assert result["value"]["nav_micros"] == expected.nav_micros
        assert result["execution_eligible"] is False
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            store.put("forbidden", b"write", None, "test")
    assert path.read_bytes() == before


def test_readonly_store_does_not_create_or_migrate(tmp_path):
    path = tmp_path / "missing.db"
    with pytest.raises(sqlite3.OperationalError):
        ReadOnlyEvidenceStore(path)
    assert not path.exists()
    with closing(sqlite3.connect(path)) as db:
        db.execute("CREATE TABLE evidence (legacy TEXT)")
        db.commit()
    before = path.read_bytes()
    with closing(ReadOnlyEvidenceStore(path)) as store:
        assert store._connection.execute("PRAGMA table_info(evidence)").fetchall()[0][1] == "legacy"
    assert path.read_bytes() == before


def test_readonly_snapshot_stays_coherent_during_concurrent_append(tmp_path):
    path = tmp_path / "evidence.db"
    with closing(EvidenceStore(path)) as writer:
        writer._connection.execute("PRAGMA journal_mode = WAL")
        first = writer.put("test", b"first", None, "test")
        with closing(ReadOnlyEvidenceStore(path)) as reader:
            assert reader.get(first) == b"first"
            second = writer.put("test", b"second", None, "test")
            with pytest.raises(KeyError):
                reader.get(second)
        with closing(ReadOnlyEvidenceStore(path)) as reader:
            assert reader.get(second) == b"second"


@pytest.mark.parametrize("offset,passes", [(120000, True), (120001, False), (-1, False)])
def test_account_freshness_is_rechecked_at_consumer_time(tmp_path, offset, passes):
    with closing(EvidenceStore(tmp_path / "evidence.db")) as store:
        _, refs = candidate(store)
        if passes:
            assert check(store, refs, now=TIMESTAMP + offset)["account_age_ms"] == offset
        else:
            with pytest.raises(ValueError, match="stale or future"):
                check(store, refs, now=TIMESTAMP + offset)


def test_prices_can_expire_while_account_block_is_still_recent(tmp_path):
    with closing(EvidenceStore(tmp_path / "evidence.db")) as store:
        f = replace(fixture(), timestamp=TIMESTAMP // 1000 + 1799)
        _, refs = candidate(store, f=f, observed=TIMESTAMP + 1799000)
        check(store, refs, now=TIMESTAMP + 1800000)
        with pytest.raises(ValueError):
            check(store, refs, now=TIMESTAMP + 1800001)


@pytest.mark.parametrize("fault", ["duplicate", "source", "position", "bool", "future_capture"])
def test_consumer_scope_and_capture_clock_cannot_be_changed(tmp_path, fault):
    with closing(EvidenceStore(tmp_path / "evidence.db")) as store:
        _, refs = candidate(store, observed=TIMESTAMP + (1000 if fault == "future_capture" else 0))
        kwargs = {}
        if fault == "duplicate":
            refs = (refs[0], refs[0])
        elif fault == "source":
            kwargs["sources"] = ("other", SECONDARY)
        elif fault == "position":
            kwargs["position"] = None
        elif fault == "bool":
            kwargs["position"] = replace(POSITION, opened_at=False)
        with pytest.raises(ValueError):
            check(store, refs, **kwargs)


def retain_capture(store, capture):
    return store.put(
        NAV_KIND,
        capture.payload(store, position_config()),
        capture.wallet.block,
        capture.wallet.source,
        request=wire_envelope(capture.exchanges, response=False),
        raw_response=wire_envelope(capture.exchanges, response=True),
    )


def test_individually_valid_proofs_with_same_rounded_nav_can_disagree(tmp_path):
    with closing(EvidenceStore(tmp_path / "evidence.db")) as store:
        _, refs = candidate(store)
        original = replay_signed_nav(store, refs[1], position_config())
        _, others = candidate(store, f=fixture(growth1=fixture().growth1 + 1))
        different = replay_signed_nav(store, others[1], position_config())
        forged = replace(original, exchanges=different.exchanges)
        assert forged.reconstruct(store, position_config()) == original.reconstruct(
            store, position_config()
        )
        ref = retain_capture(store, forged)
        replay_signed_nav(store, ref, position_config())  # Single-provider proof is consistent.
        with pytest.raises(ValueError, match="raw state disagrees"):
            check(store, (refs[0], ref))


def test_two_valid_captures_cannot_be_spliced_into_one_quorum(tmp_path):
    with closing(EvidenceStore(tmp_path / "evidence.db")) as store:
        _, refs = candidate(store)
        _, others = candidate(store, observed=TIMESTAMP + 1)
        with pytest.raises(ValueError, match="different price bindings"):
            check(store, (refs[0], others[1]), now=TIMESTAMP + 1)


def setup_cli(tmp_path):
    path = tmp_path / "evidence.db"
    with closing(EvidenceStore(path)) as store:
        _, refs = candidate(store)
    source, config, output = (
        tmp_path / name for name in ("request.json", "config.json", "out.json")
    )
    source.write_bytes(
        canonical_json(
            {
                "schema": "oasis.signed-nav-check-input.v1",
                "evidence": [asdict(r) for r in refs],
                "sources": [PRIMARY, SECONDARY],
                "position": asdict(POSITION),
            }
        )
    )
    config.write_bytes(canonical_json(asdict(position_config())))
    args = [
        "--input",
        str(source),
        "--config",
        str(config),
        "--evidence",
        str(path),
        "--out",
        str(output),
    ]
    return path, source, output, args


def test_cli_current_host_refuses_historical_evidence_without_modifying_it(tmp_path):
    path, _, output, args = setup_cli(tmp_path)
    before = path.read_bytes()
    process = subprocess.run(
        [sys.executable, "-m", "scripts.verify_signed_nav", *args],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert process.returncode == 3
    report = json.loads(output.read_bytes())
    assert report["clock_source"] == "host"
    assert report["network_calls"] == 0
    assert report["passed"] is False
    assert "result" not in report
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "finish,accepted", [(TIMESTAMP, True), (TIMESTAMP + 120001, False), (TIMESTAMP - 1, False)]
)
def test_cli_checks_completion_clock_and_preserves_existing_output(
    tmp_path, monkeypatch, finish, accepted
):
    path, _, output, args = setup_cli(tmp_path)
    before = path.read_bytes()
    clocks = iter([TIMESTAMP * 1_000_000, finish * 1_000_000])
    monkeypatch.setattr(cli.time, "time_ns", lambda: next(clocks))
    monkeypatch.setattr(sys, "argv", ["verify_signed_nav", *args])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == (0 if accepted else 3)
    report = json.loads(output.read_bytes())
    assert report["passed"] is accepted
    assert report["execution_eligible"] is False
    assert path.read_bytes() == before
    raw = output.read_bytes()
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    assert output.read_bytes() == raw


@pytest.mark.parametrize("kind", ["symlink", "fifo", "oversize", "duplicate", "malformed"])
def test_cli_rejects_bad_inputs_without_echo_or_database_creation(
    tmp_path, monkeypatch, kind, capsys
):
    source, output, database = (tmp_path / name for name in ("request", "out", "missing.db"))
    if kind == "symlink":
        source.symlink_to(database)
    elif kind == "fifo":
        os.mkfifo(source)
    elif kind == "oversize":
        source.write_bytes(b"x" * 64001)
    elif kind == "duplicate":
        source.write_bytes(b'{"schema":"private-marker","schema":"duplicate"}')
    else:
        source.write_text("private-marker")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "check",
            "--input",
            str(source),
            "--config",
            str(source),
            "--evidence",
            str(database),
            "--out",
            str(output),
        ],
    )
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 3
    captured = capsys.readouterr()
    assert "private-marker" not in captured.out + captured.err + output.read_text()
    assert not database.exists()
