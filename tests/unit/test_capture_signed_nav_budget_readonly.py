"""Offline planning reads a real disposable quota ledger without taking writer locks."""

import sqlite3
from contextlib import closing

import pytest

from oasis_agent.rpc import budget as budget_module
from scripts import capture_signed_nav as cli
from tests.unit.test_capture_signed_nav import run, setup


def test_offline_plan_reads_existing_budget_while_another_writer_holds_lock(tmp_path, monkeypatch):
    paths, calls, _, before = setup(tmp_path, monkeypatch, capture=False)
    inode = paths["budget"].stat().st_ino
    original_connect = sqlite3.connect
    statements, connections = [], []

    def connect(*args, **kwargs):
        # Exercise actual SQLite locking without waiting ten seconds on the pre-fix path.
        kwargs["timeout"] = 0
        db = original_connect(*args, **kwargs)
        connections.append(args[0])
        db.set_trace_callback(statements.append)
        return db

    with closing(original_connect(paths["budget"], isolation_level=None)) as writer:
        writer.execute("BEGIN IMMEDIATE")
        rows = writer.execute("SELECT seq,payload,digest FROM rpc_usage ORDER BY seq").fetchall()
        monkeypatch.setattr(budget_module.sqlite3, "connect", connect)
        report = run(paths, 0)
        assert writer.in_transaction
        assert (
            writer.execute("SELECT seq,payload,digest FROM rpc_usage ORDER BY seq").fetchall()
            == rows
        )
        assert (
            report["request_counts_before"]
            == report["request_counts_after"]
            == {
                cli.SOURCES[0]: 1,
                cli.SOURCES[1]: 0,
            }
        )
        assert len(connections) == 2 and all(uri.endswith("?mode=ro") for uri in connections)
        assert statements.count("BEGIN") == 2 and "BEGIN IMMEDIATE" not in statements
        assert report["mode"] == "offline_plan" and not report["candidate_verified"]
        assert report["request_attempts"] == dict.fromkeys(cli.SOURCES, 0)
        assert not calls and paths["budget"].read_bytes() == before
        assert paths["budget"].stat().st_ino == inode
        assert not (paths["out"] / "evidence.db").exists()
        assert not (paths["out"] / "check-input.json").exists()


@pytest.mark.parametrize("fault", ["missing", "policy_mismatch", "missing_policy"])
def test_offline_plan_refuses_missing_or_invalid_original_budget_without_repair(
    tmp_path, monkeypatch, fault
):
    paths, calls, _, _ = setup(tmp_path, monkeypatch, capture=False)
    if fault == "missing":
        paths["budget"].unlink()
    elif fault == "policy_mismatch":
        paths["rpc-profile"].write_text('{"max_requests":63,"min_interval_ms":100}')
    else:
        with closing(sqlite3.connect(paths["budget"], isolation_level=None)) as db:
            db.execute("DELETE FROM rpc_budget_policy")
    before = paths["budget"].read_bytes() if paths["budget"].exists() else None
    inode = paths["budget"].stat().st_ino if paths["budget"].exists() else None
    report = run(paths, 3)
    assert report["refusal_stage"] == "existing_budget"
    assert not report["candidate_verified"] and not calls
    assert report["request_attempts"] == dict.fromkeys(cli.SOURCES, 0)
    assert (paths["budget"].read_bytes() if paths["budget"].exists() else None) == before
    assert (paths["budget"].stat().st_ino if paths["budget"].exists() else None) == inode
    assert not (paths["out"] / "evidence.db").exists()
    assert not (paths["out"] / "check-input.json").exists()
