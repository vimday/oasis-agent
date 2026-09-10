"""Entry scheduling must examine qualifying pools within the fixed budget."""

from threading import Barrier
from unittest.mock import Mock

from eth_abi import encode

from oasis_agent.sniping.config import ExecutionConfig
from oasis_agent.sniping.journal import ExecutionJournal
from oasis_agent.sniping.monitor import BuyResult, LivePool, Monitor


def test_expensive_first_pool_does_not_hide_qualifying_second_pool(tmp_path):
    cfg = ExecutionConfig()
    with ExecutionJournal(tmp_path / "j.db", {"test": True}) as journal:
        m = Monitor(cfg, Mock(), cfg.weth, None, journal, tmp_path / "stop")
        m.tranches_done = 1
        m._try_buy_tranche = Mock(
            side_effect=[
                BuyResult(False, fdv_skip=True),
                BuyResult(True),
            ]
        )
        pools = [LivePool(500, cfg.weth), LivePool(10000, cfg.target_ca)]
        m._buy_cycle(pools, 100)
        assert m._try_buy_tranche.call_count == 2
        assert not m.tranche_2_abandoned
        assert m._try_buy_tranche.call_args.args[1] == 10**17


def test_temporary_high_fdv_keeps_remaining_budget_available(tmp_path):
    cfg = ExecutionConfig()
    with ExecutionJournal(tmp_path / "j.db", {"test": True}) as journal:
        m = Monitor(cfg, Mock(), cfg.weth, None, journal, tmp_path / "stop")
        m.tranches_done = 1
        m._try_buy_tranche = Mock(return_value=BuyResult(False, fdv_skip=True))
        m._buy_cycle([LivePool(500, cfg.weth)], 100)
        assert not m.tranche_2_abandoned


def test_exhausted_budget_never_starts_another_buy(tmp_path):
    cfg = ExecutionConfig()
    with ExecutionJournal(tmp_path / "j.db", {"test": True}) as journal:
        m = Monitor(cfg, Mock(), cfg.weth, None, journal, tmp_path / "stop")
        m.tranches_done = 2
        m._try_buy_tranche = Mock()
        m._buy_cycle([LivePool(500, cfg.weth)], 100)
        m._try_buy_tranche.assert_not_called()


def test_fee_discovery_starts_both_sources_and_all_fees_concurrently():
    cfg = ExecutionConfig()
    m = object.__new__(Monitor)
    m.config = cfg
    m.rpc = Mock()
    bases = list(cfg.base_tokens)
    n_calls = 2 * len(bases) * len(cfg.v3_fees)
    barrier = Barrier(n_calls)

    def response(to, data, provider="primary"):
        barrier.wait(timeout=2)
        return "0x" + encode(["address"], [cfg.weth]).hex()

    m.rpc.eth_call.side_effect = response
    pools = m._live_v3_pools()
    assert [p.fee for p in pools] == list(cfg.v3_fees) * len(bases)
    assert [p.base for p in pools] == [base for base in bases for _ in cfg.v3_fees]
    assert m.rpc.eth_call.call_count == n_calls
