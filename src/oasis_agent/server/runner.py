"""Bounded lifecycle for operator-triggered observation subprocesses.

Only the server-configured command is executed. HTTP clients cannot supply
commands. Stop and shutdown reap the process group before publishing a terminal
state; output is drained incrementally so noisy children cannot exhaust memory.
"""

import asyncio
import os
import signal
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from .audit import OpsRun, ServerOpsJournal, _redact
from .config import ServerShadowRunnerConfig

_OUTPUT_LIMIT = 2_000
_LINE_LIMIT = 8_192


@dataclass(frozen=True, slots=True)
class RunRejected(Exception):
    reason: str


async def _drain(stream: asyncio.StreamReader | None) -> str:
    """Keep a redacted tail; discard oversized lines rather than split secrets."""
    if stream is None:
        return ""
    tail = ""
    pending = b""
    dropping = False
    while chunk := await stream.read(4096):
        parts = chunk.split(b"\n")
        for index, part in enumerate(parts):
            if not dropping:
                pending += part
                if len(pending) > _LINE_LIMIT:
                    pending = b""
                    dropping = True
            if index < len(parts) - 1:
                line = (
                    "[oversized output line omitted]"
                    if dropping
                    else _redact(pending.decode("utf-8", errors="replace"))
                )
                tail = (tail + line + "\n")[-_OUTPUT_LIMIT:]
                pending = b""
                dropping = False
    if pending and not dropping:
        tail = (tail + _redact(pending.decode("utf-8", errors="replace")))[-_OUTPUT_LIMIT:]
    return tail


class ShadowRunCoordinator:
    def __init__(
        self,
        runner: ServerShadowRunnerConfig,
        journal: ServerOpsJournal,
        *,
        cwd: Path,
        loop_factory: Callable[[], asyncio.AbstractEventLoop] | None = None,
        environ: Mapping[str, str] | None = None,
        pass_fds: tuple[int, ...] = (),
    ) -> None:
        self._runner = runner
        self._journal = journal
        self._cwd = cwd
        self._active: dict[str, asyncio.Event] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_factory = loop_factory
        self._closed = False
        self._environ = None if environ is None else dict(environ)
        self._pass_fds = pass_fds

    def _event_loop(self) -> asyncio.AbstractEventLoop:
        loop = self._loop
        if loop is None or loop.is_closed():
            loop = self._loop_factory() if self._loop_factory else asyncio.new_event_loop()
            self._loop = loop
        return loop

    def trigger(self) -> OpsRun:
        if self._closed:
            raise RunRejected("shadow runner is closed")
        if not self._runner.command:
            raise RunRejected("shadow runner command is empty")
        if len(self._active) >= self._runner.max_concurrent:
            raise RunRejected("shadow runner concurrency limit reached")
        loop = self._event_loop()
        run = self._journal.create_run(command=self._runner.command)
        self._active[run.run_id] = asyncio.Event()
        self._tasks[run.run_id] = loop.create_task(self._execute(run.run_id))
        return run

    def stop(self, run_id: str) -> OpsRun:
        run = self._journal.get_run(run_id)
        if run is None:
            raise RunRejected("run not found")
        if run.status in {"succeeded", "failed", "stopped"}:
            return run
        event = self._active.get(run_id)
        if event is None:
            if run.status in {"queued", "running", "stopping"}:
                raise RunRejected("run is not owned by this server; process state unknown")
            return run
        if not event.is_set():
            self._journal.mark_stopping(run_id)
            event.set()
        result = self._journal.get_run(run_id)
        if result is None:
            raise RuntimeError("run disappeared")
        return result

    @staticmethod
    def _kill_group(process: asyncio.subprocess.Process) -> None:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)

    async def _execute(self, run_id: str) -> None:
        process: asyncio.subprocess.Process | None = None
        readers: list[asyncio.Task[str]] = []
        waiters: list[asyncio.Task[object]] = []
        try:
            stop = self._active[run_id]
            if stop.is_set():
                self._journal.mark_stopped(run_id)
                return
            self._journal.mark_started(run_id)
            try:
                process = await asyncio.create_subprocess_exec(
                    *self._runner.command,
                    cwd=str(self._cwd),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                    env=self._environ,
                    pass_fds=self._pass_fds,
                )
            except OSError:
                self._journal.mark_finished(
                    run_id, exit_code=-1, stdout="", stderr="failed to start observation process"
                )
                return
            readers = [
                asyncio.create_task(_drain(process.stdout)),
                asyncio.create_task(_drain(process.stderr)),
            ]
            exited = asyncio.create_task(process.wait())
            stopped = asyncio.create_task(stop.wait())
            waiters = [exited, stopped]
            done, _ = await asyncio.wait(
                waiters, timeout=self._runner.timeout_seconds, return_when=asyncio.FIRST_COMPLETED
            )
            # Reap descendants too, including children that inherited output pipes
            # after the configured parent exited. Only this new session is signalled.
            self._kill_group(process)
            await process.wait()
            stdout, stderr = await asyncio.gather(*readers)
            if stop.is_set():
                self._journal.mark_stopped(run_id)
            else:
                self._journal.mark_finished(
                    run_id,
                    exit_code=exited.result() if exited in done else -1,
                    stdout=stdout,
                    stderr=stderr
                    if exited in done
                    else (f"timed out after {self._runner.timeout_seconds}s"),
                )
        finally:
            if process is not None:
                self._kill_group(process)
                await process.wait()
            for task in (*readers, *waiters):
                if not task.done():
                    task.cancel()
            await asyncio.gather(*readers, *waiters, return_exceptions=True)
            self._active.pop(run_id, None)
            self._tasks.pop(run_id, None)

    def get_run(self, run_id: str) -> OpsRun | None:
        return self._journal.get_run(run_id)

    def list_runs(self, *, limit: int) -> tuple[dict[str, object], ...]:
        return self._journal.list_runs(limit=limit)

    async def aclose(self) -> None:
        self._closed = True
        for run_id in tuple(self._active):
            self.stop(run_id)
        await asyncio.gather(*tuple(self._tasks.values()))

    def close(self) -> None:
        if self._active:
            raise RuntimeError("active runs require awaiting aclose before closing the journal")
        self._closed = True
        if self._loop is not None and self._loop_factory is None:
            self._loop.close()
        self._loop = None
