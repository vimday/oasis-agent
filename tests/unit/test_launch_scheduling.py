"""Scheduling bounds latency between discovery passes without authorizing entry."""

from unittest.mock import Mock

from oasis_agent.sniping.config import ExecutionConfig
from oasis_agent.sniping.journal import ExecutionJournal
from oasis_agent.sniping.monitor import LivePool, Monitor
from oasis_agent.sniping.scheduling import PoolSchedule


def test_liquidity_at_tail_does_not_wait_for_whole_registry():
    schedule = PoolSchedule()
    pools = [f"pool-{i}" for i in range(300)]
    selected = schedule.select(pools, {pools[-1]})
    assert selected[0] == pools[-1]
    assert len(selected) == 8
    assert selected[1:] == pools[:7]


def test_repeated_event_flood_does_not_starve_quiet_pools():
    schedule = PoolSchedule()
    pools = [f"pool-{i}" for i in range(40)]
    seen = set()
    for _ in range(10):
        batch = schedule.select(pools, set(pools[:4]))
        assert len(batch) == len(set(batch)) == 8
        seen.update(batch)
    assert seen == set(pools)


def test_removed_pools_and_case_duplicates_never_enter_batch():
    schedule = PoolSchedule(2)
    assert schedule.select(["A", "a", "b"], {"A"}) == ["a", "b"]
    assert schedule.select(["B", "c"], {"a"}) == ["b", "c"]
    assert schedule.select([], {"c"}) == []


def test_monitor_refreshes_snapshot_and_observes_bounded_batch(tmp_path):

    cfg = ExecutionConfig()
    rpc = Mock()
    rpc.begin_snapshot.side_effect = [100, 105]
    with ExecutionJournal(tmp_path / "schedule.db", {"test": True}) as journal:
        monitor = Monitor(cfg, rpc, cfg.weth, None, journal, tmp_path / "stop", dry_run=True)
        monitor._live_v3_pools = Mock(
            return_value=[LivePool(500, f"0x{i:040x}") for i in range(1, 301)]
        )
        monitor._observe_quotes = Mock()
        monitor._cycle_once()
        batch, block = monitor._observe_quotes.call_args.args
        assert len(batch) == 8
        assert block == 105
        assert rpc.begin_snapshot.call_count == 2
        assert monitor.account is None
