"""Bounded read admission, thread ownership and cancellation under real contention."""

import asyncio
import threading

import pytest

from oasis_agent.server.databases import ReadOnlyError
from oasis_agent.server.read_worker import ReadWorker

from .server_helpers import make_server_config


def test_slow_read_does_not_block_loop_or_release_cancelled_work_early(tmp_path):
    config = make_server_config(tmp_path)

    async def scenario():
        reader = ReadWorker(config, capacity=1)
        entered = threading.Event()
        release = threading.Event()
        loop_thread = threading.get_ident()

        def slow(database):
            assert threading.get_ident() != loop_thread
            entered.set()
            assert release.wait(timeout=5)
            return database.decision_chain()

        task = asyncio.create_task(reader.run(slow))
        try:
            # Deadline is a deadlock guard; the assertion depends on synchronization,
            # not a machine-specific throughput threshold.
            assert await asyncio.to_thread(entered.wait, 3)
            with pytest.raises(ReadOnlyError, match="busy"):
                await reader.run(lambda database: database.health_chain())
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            with pytest.raises(ReadOnlyError, match="busy"):
                await reader.run(lambda database: database.health_chain())
        finally:
            release.set()
            await reader.aclose()
        with pytest.raises(ReadOnlyError):
            await reader.run(lambda database: database.health_chain())

    asyncio.run(scenario())


def test_reader_reuses_thread_and_reports_measured_quantiles(tmp_path):
    async def scenario():
        reader = ReadWorker(make_server_config(tmp_path))
        try:
            first = await reader.run(
                lambda database: (threading.get_ident(), database.health_chain())
            )
            second = await reader.run(
                lambda database: (threading.get_ident(), database.health_chain())
            )
            assert first == second
            metrics = await reader.run(lambda _: reader.metrics())
            assert metrics["sample_count"] == 2
            assert metrics["read"]["p99"] >= metrics["read"]["p50"] >= 0
        finally:
            await reader.aclose()

    asyncio.run(scenario())
