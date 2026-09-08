"""Background coordinator for triggered observation runs.

The server never imports the shadow stack; it executes a fully parameterized
command from the configuration as an isolated subprocess and journals the
outcome (exit code, redacted output tails) in the operations database. This
keeps the API process free of broadcasting and signing paths while still
exposing run lifecycle to the dashboard.
"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .audit import OpsRun, ServerOpsJournal
from .config import ServerShadowRunnerConfig


@dataclass(frozen=True, slots=True)
class RunRejected(Exception):
    reason: str


class ShadowRunCoordinator:
    def __init__(
        self,
        runner: ServerShadowRunnerConfig,
        journal: ServerOpsJournal,
        *,
        cwd: Path,
        loop_factory: Callable[[], asyncio.AbstractEventLoop] | None = None,
    ) -> None:
        self._runner = runner
        self._journal = journal
        self._cwd = cwd
        self._active: set[str] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_factory = loop_factory

    def _event_loop(self) -> asyncio.AbstractEventLoop:
        loop = self._loop
        if loop is None or loop.is_closed():
            loop = self._loop_factory() if self._loop_factory else asyncio.new_event_loop()
            self._loop = loop
        return loop

    def trigger(self) -> OpsRun:
        if not self._runner.command:
            raise RunRejected("shadow runner command is empty")
        if len(self._active) >= self._runner.max_concurrent:
            raise RunRejected("shadow runner concurrency limit reached")
        run = self._journal.create_run(command=self._runner.command)
        self._active.add(run.run_id)
        loop = self._event_loop()
        loop.create_task(self._execute(run.run_id))
        return run

    async def _execute(self, run_id: str) -> None:
        try:
            self._journal.mark_started(run_id)
            try:
                process = await asyncio.create_subprocess_exec(
                    *self._runner.command,
                    cwd=str(self._cwd),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except OSError as exc:
                # Spawn failures (missing binary, permission, bad cwd) must
                # surface as a failed run, not leave the journal stuck running.
                self._journal.mark_finished(
                    run_id, exit_code=-1, stdout="", stderr=f"failed to start: {exc}"
                )
                return
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(), timeout=self._runner.timeout_seconds
                )
                exit_code = process.returncode if process.returncode is not None else -1
                self._journal.mark_finished(
                    run_id,
                    exit_code=exit_code,
                    stdout=stdout.decode("utf-8", errors="replace"),
                    stderr=stderr.decode("utf-8", errors="replace"),
                )
            except TimeoutError:
                try:
                    process.kill()
                    await process.wait()
                except ProcessLookupError:
                    pass
                self._journal.mark_finished(
                    run_id,
                    exit_code=-1,
                    stdout="",
                    stderr=f"timed out after {self._runner.timeout_seconds}s",
                )
        finally:
            self._active.discard(run_id)

    def get_run(self, run_id: str) -> OpsRun | None:
        return self._journal.get_run(run_id)

    def list_runs(self, *, limit: int) -> tuple[dict[str, object], ...]:
        return self._journal.list_runs(limit=limit)

    def close(self) -> None:
        # Only close a loop this coordinator created itself. An injected
        # factory (e.g. the ASGI lifespan) means the caller owns and closes
        # the loop; closing a running foreign loop raises RuntimeError.
        if self._loop is not None and self._loop_factory is None:
            self._loop.close()
        self._loop = None
