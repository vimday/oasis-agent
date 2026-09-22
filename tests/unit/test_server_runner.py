"""Background shadow-run coordinator: lifecycle, failure, concurrency."""

import asyncio
import sys
from dataclasses import replace

import pytest

from oasis_agent.server.audit import ServerOpsJournal
from oasis_agent.server.config import ServerShadowRunnerConfig
from oasis_agent.server.runner import RunRejected, ShadowRunCoordinator

from .server_helpers import make_server_config


def coordinator(tmp_path, *, command, timeout_seconds=30, max_concurrent=1):
    runner = ServerShadowRunnerConfig(command, timeout_seconds, max_concurrent)
    config = replace(make_server_config(tmp_path), shadow_runner=runner)
    journal = ServerOpsJournal(config.databases.ops)
    loop = asyncio.new_event_loop()
    coordinator = ShadowRunCoordinator(runner, journal, cwd=tmp_path, loop_factory=lambda: loop)
    return coordinator, journal, loop


def run_loop(loop, seconds=1.0):
    loop.run_until_complete(asyncio.sleep(seconds))


def test_trigger_queues_run_and_executes_success(tmp_path):
    coord, journal, loop = coordinator(tmp_path, command=(sys.executable, "-c", "print('ok')"))
    try:
        run = coord.trigger()
        assert run.status == "queued"
        run_loop(loop)
        recorded = journal.get_run(run.run_id)
        assert recorded.status == "succeeded"
        assert recorded.exit_code == 0
        assert "ok" in recorded.stdout_tail
    finally:
        coord.close()
        loop.close()


def test_default_child_environment_does_not_inherit_parent_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("OASIS_PRIVATE_KEY", "synthetic-parent-marker")
    monkeypatch.setenv("OASIS_SERVER_OPS_TOKEN", "synthetic-ops-marker")
    monkeypatch.setenv("ROBINHOOD_ALCHEMY_RPC_URL", "synthetic-paid-marker")
    coord, journal, loop = coordinator(
        tmp_path,
        command=(
            sys.executable,
            "-c",
            (
                "import os; assert not any(k in os.environ for k in "
                "('OASIS_PRIVATE_KEY', 'OASIS_SERVER_OPS_TOKEN', 'ROBINHOOD_ALCHEMY_RPC_URL')); "
                "assert os.environ.get('PATH'); print('isolated')"
            ),
        ),
    )
    try:
        run = coord.trigger()
        loop.run_until_complete(coord._tasks[run.run_id])
        recorded = journal.get_run(run.run_id)
        assert recorded.status == "succeeded"
        assert recorded.stdout_tail.strip() == "isolated"
    finally:
        loop.run_until_complete(coord.aclose())
        loop.close()
        journal.close()


def test_nonzero_exit_is_recorded_as_failure(tmp_path):
    coord, journal, loop = coordinator(
        tmp_path, command=(sys.executable, "-c", "import sys; sys.exit(3)")
    )
    try:
        run = coord.trigger()
        run_loop(loop)
        recorded = journal.get_run(run.run_id)
        assert recorded.status == "failed"
        assert recorded.exit_code == 3
    finally:
        coord.close()
        loop.close()


def test_timeout_kills_and_marks_failed(tmp_path):
    coord, journal, loop = coordinator(
        tmp_path,
        command=(sys.executable, "-c", "import time; time.sleep(30)"),
        timeout_seconds=1,
    )
    try:
        run = coord.trigger()
        run_loop(loop, seconds=3)
        recorded = journal.get_run(run.run_id)
        assert recorded.status == "failed"
        assert recorded.exit_code == -1
        assert "timed out" in recorded.stderr_tail
    finally:
        coord.close()
        loop.close()


def test_concurrency_limit_rejects_extra_triggers(tmp_path):
    coord, _journal, loop = coordinator(
        tmp_path,
        command=(sys.executable, "-c", "import time; time.sleep(2)"),
        max_concurrent=1,
    )
    try:
        first = coord.trigger()
        assert first.status == "queued"
        with pytest.raises(RunRejected, match="concurrency limit"):
            coord.trigger()
        # Drain the queued task so no coroutine is left un-awaited.
        run_loop(loop, seconds=3)
    finally:
        coord.close()
        loop.close()


def test_empty_command_rejected(tmp_path):
    coord, _journal, loop = coordinator(tmp_path, command=())
    try:
        with pytest.raises(RunRejected, match="empty"):
            coord.trigger()
    finally:
        coord.close()
        loop.close()


def test_spawn_failure_is_journaled_as_failed(tmp_path):
    coord, journal, loop = coordinator(tmp_path, command=("/nonexistent/oasis-binary", "--flag"))
    try:
        run = coord.trigger()
        run_loop(loop)
        recorded = journal.get_run(run.run_id)
        assert recorded is not None
        assert recorded.status == "failed"
        assert recorded.exit_code == -1
        assert "failed to start" in recorded.stderr_tail
    finally:
        coord.close()
        loop.close()


def test_stop_queued_run_never_spawns_and_is_idempotent(tmp_path):
    marker = tmp_path / "spawned"
    coord, journal, loop = coordinator(
        tmp_path, command=(sys.executable, "-c", f"open({str(marker)!r}, 'w').close()")
    )
    try:
        run = coord.trigger()
        assert coord.stop(run.run_id).status == "stopping"
        coord.stop(run.run_id)
        run_loop(loop, 0.1)
        assert not marker.exists()
        assert journal.get_run(run.run_id).status == "stopped"
        assert coord.stop(run.run_id).status == "stopped"
    finally:
        loop.run_until_complete(coord.aclose())
        loop.close()
        journal.close()


def test_shutdown_reaps_running_process_before_journal_close(tmp_path):
    coord, journal, loop = coordinator(
        tmp_path, command=(sys.executable, "-c", "import time; time.sleep(30)")
    )
    try:
        run = coord.trigger()
        run_loop(loop, 0.1)
        loop.run_until_complete(coord.aclose())
        assert journal.get_run(run.run_id).status == "stopped"
        assert not asyncio.all_tasks(loop)
        with pytest.raises(RunRejected, match="closed"):
            coord.trigger()
    finally:
        loop.close()
        journal.close()


def test_output_retention_is_bounded_and_secret_free(tmp_path):
    coord, journal, loop = coordinator(
        tmp_path,
        command=(
            sys.executable,
            "-c",
            "print('x' * 2000000); print('https://rpc.test/private-value')",
        ),
    )
    try:
        run = coord.trigger()
        run_loop(loop, 1)
        recorded = journal.get_run(run.run_id)
        assert recorded.status == "succeeded"
        assert len(recorded.stdout_tail) <= 2000
        assert "private-value" not in recorded.stdout_tail
    finally:
        loop.run_until_complete(coord.aclose())
        loop.close()
        journal.close()
