"""Bounded, thread-confined journal reads for the asynchronous control plane.

Admission fails with 503 when full. Cancellation never frees a slot before
the underlying read finishes, and shutdown drains accepted work before closing
SQLite on its owning thread. No writes or signing capability are introduced.
"""

import asyncio
import sqlite3
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import TypeVar

from .config import ServerConfig
from .databases import ReadOnlyDatabases, ReadOnlyError

T = TypeVar("T")


class ReadWorker:
    def __init__(self, config: ServerConfig, *, capacity: int = 8) -> None:
        if type(capacity) is not int or capacity < 1:
            raise ValueError("positive read capacity required")
        self._config = config
        self._capacity = capacity
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="oasis-reader")
        self._database: ReadOnlyDatabases | None = None
        self._pending = 0
        self._closed = False
        self._samples: deque[tuple[int, int]] = deque(maxlen=256)

    def _read(self, operation: Callable[[ReadOnlyDatabases], T], submitted: int) -> T:
        started = time.perf_counter_ns()
        try:
            if self._database is None:
                self._database = ReadOnlyDatabases(self._config)
            return operation(self._database)
        except sqlite3.Error as exc:
            raise ReadOnlyError("observation database unavailable") from exc
        finally:
            self._samples.append((started - submitted, time.perf_counter_ns() - started))

    async def run(self, operation: Callable[[ReadOnlyDatabases], T]) -> T:
        if self._closed or self._pending >= self._capacity:
            raise ReadOnlyError("observation reader unavailable or busy", code="busy")
        self._pending += 1
        future = asyncio.get_running_loop().run_in_executor(
            self._executor, self._read, operation, time.perf_counter_ns()
        )

        def finished(completed: asyncio.Future[T]) -> None:
            self._pending -= 1
            # Also retrieve failures of work whose HTTP caller disconnected.
            if not completed.cancelled():
                completed.exception()

        future.add_done_callback(finished)
        return await asyncio.shield(future)

    def metrics(self) -> dict[str, object]:
        # Called on the worker via run() when serving API diagnostics.
        samples = tuple(self._samples)
        result: dict[str, object] = {
            "capacity": self._capacity,
            "sample_count": len(samples),
            "window": 256,
            "units": "nanoseconds",
            "scope": "observation_reader",
        }
        for index, stage in enumerate(("queue", "read")):
            values = sorted(sample[index] for sample in samples)
            result[stage] = {
                f"p{percentile}": values[(percentile * len(values) + 99) // 100 - 1]
                if values
                else None
                for percentile in (50, 95, 99)
            }
        return result

    def _close(self) -> None:
        if self._database is not None:
            self._database.close()
            self._database = None

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            future = asyncio.get_running_loop().run_in_executor(self._executor, self._close)
            await asyncio.shield(future)
        finally:
            self._executor.shutdown(wait=False)
