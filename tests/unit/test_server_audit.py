"""Append-only operations journal: audit records, run lifecycle, approvals."""

from __future__ import annotations

import pytest

from oasis_agent.server.audit import ServerOpsJournal


def journal(tmp_path):
    return ServerOpsJournal(tmp_path / "ops.db")


def test_record_appends_and_digests_params(tmp_path):
    with journal(tmp_path) as db:
        seq = db.record(
            action="trigger_shadow_run",
            actor="ops:abcdef",
            params={"command_digest": "0x" + "1" * 64},
            result="accepted",
            detail="run queued",
        )
        assert seq == 1
        rows = db.recent_ops(limit=10)
        assert len(rows) == 1
        assert rows[0]["action"] == "trigger_shadow_run"
        assert rows[0]["params_digest"] != "0x" + "1" * 64  # stored as digest, not raw


def test_detail_is_redacted(tmp_path):
    with journal(tmp_path) as db:
        db.record(
            action="test",
            actor="ops:x",
            params={},
            result="accepted",
            detail="token=super-secret and Bearer abcdef123456",
        )
        row = db.recent_ops(limit=1)[0]
        assert "super-secret" not in row["detail"]
        assert "abcdef123456" not in row["detail"]
        assert "token=***" in row["detail"]
        assert "Bearer ***" in row["detail"]


def test_run_lifecycle(tmp_path):
    with journal(tmp_path) as db:
        run = db.create_run(command=(".venv/bin/python", "scripts/run_lp_shadow.py"))
        assert run.status == "queued"
        assert run.exit_code is None
        assert db.get_run(run.run_id) is not None

        db.mark_started(run.run_id)
        assert db.get_run(run.run_id).status == "running"

        db.mark_finished(run.run_id, exit_code=0, stdout="all good", stderr="")
        recorded = db.get_run(run.run_id)
        assert recorded.status == "succeeded"
        assert recorded.exit_code == 0
        assert recorded.stdout_tail == "all good"


def test_run_failure_and_output_tail_truncation(tmp_path):
    with journal(tmp_path) as db:
        run = db.create_run(command=("cmd",))
        db.mark_finished(run.run_id, exit_code=3, stdout="x" * 5_000, stderr="boom")
        recorded = db.get_run(run.run_id)
        assert recorded.status == "failed"
        assert recorded.exit_code == 3
        assert len(recorded.stdout_tail) <= 2_000
        assert "boom" in recorded.stderr_tail


def test_list_runs_orders_newest_first(tmp_path):
    with journal(tmp_path) as db:
        first = db.create_run(command=("a",))
        second = db.create_run(command=("b",))
        rows = db.list_runs(limit=10)
        assert rows[0]["run_id"] == second.run_id
        assert rows[1]["run_id"] == first.run_id


def test_approval_requires_second_operator(tmp_path):
    with journal(tmp_path) as db:
        approval = db.request_approval(gate="wp0_correctness", actor="ops:alpha", reason="review")
        assert approval["status"] == "pending"

        # The requester cannot confirm their own approval.
        same = db.confirm_approval(approval_id=approval["id"], actor="ops:alpha")
        assert same["status"] == "pending"

        confirmed = db.confirm_approval(approval_id=approval["id"], actor="ops:beta")
        assert confirmed["status"] == "approved"
        assert confirmed["confirmed_by"] == "ops:beta"

        # Confirming again returns the current state without change.
        again = db.confirm_approval(approval_id=approval["id"], actor="ops:gamma")
        assert again["status"] == "approved"


def test_confirm_unknown_approval_returns_none(tmp_path):
    with journal(tmp_path) as db:
        assert db.confirm_approval(approval_id=999, actor="ops:x") is None


def test_list_approvals_filters_by_gate(tmp_path):
    with journal(tmp_path) as db:
        db.request_approval(gate="wp0_correctness", actor="ops:a", reason="r1")
        db.request_approval(gate="wp1_pool_access", actor="ops:b", reason="r2")
        rows = db.list_approvals(gate="wp0_correctness")
        assert len(rows) == 1
        assert rows[0]["gate"] == "wp0_correctness"


def test_update_rejects_unknown_run_field(tmp_path):
    with journal(tmp_path) as db:
        run = db.create_run(command=("a",))
        with pytest.raises(ValueError, match="unknown run field"):
            db._update_run(run.run_id, bogus=1)
