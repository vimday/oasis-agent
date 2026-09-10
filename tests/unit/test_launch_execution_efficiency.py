"""Latency improvements retain snapshot, nonce and submission boundaries."""

import time
from threading import Barrier, Event
from unittest.mock import Mock

import httpx
import pytest
from eth_abi import encode

from oasis_agent.sniping.config import ExecutionConfig
from oasis_agent.sniping.journal import ExecutionJournal
from oasis_agent.sniping.monitor import LivePool, Monitor
from oasis_agent.sniping.rpc import DualValue, JsonRpc, ProviderDisagreement, RpcError


def make_monitor(tmp_path, rpc):
    cfg = ExecutionConfig()
    journal = ExecutionJournal(tmp_path / "execution.db", {"test": True})
    monitor = Monitor(cfg, rpc, cfg.weth, None, journal, tmp_path / "stop", dry_run=True)
    return monitor, journal


def test_feed_reuses_only_same_hash_and_rechecks_expiry(tmp_path):
    rpc = Mock()
    now = int(time.time())
    raw = (
        "0x"
        + encode(
            ["uint80", "int256", "uint256", "uint256", "uint80"], [1, 3000 * 10**8, now, now, 1]
        ).hex()
    )
    rpc.dual_eth_call.return_value = DualValue(raw, raw)
    rpc.eth_call.return_value = "0x08"
    rpc.snapshot = {"blockHash": "0x" + "1" * 64}
    rpc.snapshot_number = 100
    monitor, journal = make_monitor(tmp_path, rpc)
    try:
        for _ in range(8):
            assert monitor._eth_usd_feed() == (3000 * 10**6, now, 100)
        assert rpc.dual_eth_call.call_count == rpc.eth_call.call_count == 1
        rpc.block_number.assert_not_called()
        rpc.snapshot = {"blockHash": "0x" + "2" * 64}
        rpc.snapshot_number = 101
        assert monitor._eth_usd_feed()[2] == 101
        assert rpc.dual_eth_call.call_count == 2
        monitor._feed_cache = (rpc.snapshot["blockHash"], (3000 * 10**6, now - 4000, 101))
        with pytest.raises(ProviderDisagreement, match="expired"):
            monitor._eth_usd_feed()
    finally:
        journal.close()


def test_three_preflight_reads_start_together(tmp_path):
    rpc = Mock()
    rpc.snapshot_number = 100
    monitor, journal = make_monitor(tmp_path, rpc)
    barrier = Barrier(3)

    def value(result):
        def call(*args):
            barrier.wait(timeout=2)
            return result

        return call

    rpc.estimate_gas.side_effect = value(100000)
    rpc.gas_price.side_effect = value(100)
    monitor._next_nonce = Mock(side_effect=value(7))
    try:
        gas, price, nonce = monitor._transaction_preflight({"to": monitor.config.weth})
        assert gas >= 100000 and price == 100 * monitor.config.gas_price_multiplier and nonce == 7
        assert journal.read()[-1]["kind"] == "preflight_complete"
        assert monitor.account is None
    finally:
        journal.close()


def test_gas_above_cap_is_rejected_instead_of_silently_truncated(tmp_path):
    rpc = Mock()
    monitor, journal = make_monitor(tmp_path, rpc)
    rpc.estimate_gas.return_value = monitor.config.max_gas + 1
    rpc.gas_price.return_value = 100
    monitor._next_nonce = Mock(return_value=0)
    try:
        with pytest.raises(RpcError, match="gas estimate exceeds cap"):
            monitor._transaction_preflight({})
        assert journal.read() == []
    finally:
        journal.close()


def test_nonce_checks_pending_state_on_both_sources(tmp_path):
    rpc = Mock()
    monitor, journal = make_monitor(tmp_path, rpc)
    rpc.dual.return_value = DualValue("0x7", "0x7")
    try:
        assert monitor._next_nonce() == 7
        rpc.dual.assert_called_with("eth_getTransactionCount", [monitor.wallet, "pending"])
        rpc.dual.return_value = DualValue("0x7", "0x8")
        with pytest.raises(ProviderDisagreement):
            monitor._next_nonce()
    finally:
        journal.close()


def test_slow_primary_does_not_delay_secondary_receipt():
    rpc = JsonRpc("https://primary.invalid", "https://secondary.invalid", ExecutionConfig())
    started, release = Event(), Event()
    receipt = {"transactionHash": "0x" + "1" * 64, "status": "0x1"}

    def response(tx_hash, provider):
        if provider == "primary":
            started.set()
            release.wait(timeout=2)
            return None
        assert started.wait(timeout=1)
        return receipt

    rpc.get_transaction_receipt = Mock(side_effect=response)
    try:
        assert rpc.wait_receipt(receipt["transactionHash"], timeout_s=1) == receipt
        assert not release.is_set()
    finally:
        release.set()
        rpc.close()


@pytest.mark.parametrize("status", [401, 429])
def test_rejected_http_is_not_retried(status):
    rpc = JsonRpc("https://primary.invalid", "https://secondary.invalid", ExecutionConfig())
    response = httpx.Response(status, request=httpx.Request("POST", "https://primary.invalid"))
    rpc._client.post = Mock(return_value=response)
    try:
        with pytest.raises(RpcError, match=f"HTTP={status}"):
            rpc.call("eth_call", [])
        assert rpc._client.post.call_count == 1
    finally:
        rpc.close()


def test_uncertain_send_is_never_automatically_retried():
    rpc = JsonRpc("https://primary.invalid", "https://secondary.invalid", ExecutionConfig())
    rpc._client.post = Mock(side_effect=httpx.ReadTimeout("transport timeout"))
    try:
        with pytest.raises(RpcError, match="ReadTimeout"):
            rpc.call("eth_sendRawTransaction", ["0x00"])
        assert rpc._client.post.call_count == 1
    finally:
        rpc.close()


def test_exit_evaluation_tries_next_pool_when_first_reverts(tmp_path):
    rpc = Mock()
    rpc.snapshot_number = 100
    monitor, journal = make_monitor(tmp_path, rpc)
    pools = [LivePool(500, monitor.config.weth), LivePool(3000, monitor.config.target_ca)]
    evaluation = Mock(value_usd_micros=100, cost_basis_usd_micros=50, multiple=1, action="hold")
    monitor._evaluate_position = Mock(side_effect=[RpcError("SPL"), evaluation])
    monitor._wallet_laptop_balance = Mock(return_value=100)
    monitor._try_sell = Mock()
    try:
        monitor._sell_cycle(pools)
        assert monitor._evaluate_position.call_count == 2
        assert monitor._evaluate_position.call_args.args[0] == pools[1]
        monitor._try_sell.assert_not_called()
        assert journal.read()[0]["pool"] == pools[0].pool
    finally:
        journal.close()


def test_unknown_submission_blocks_exit_attempts(tmp_path):
    monitor, journal = make_monitor(tmp_path, Mock())
    monitor.submission_unknown = True
    monitor._evaluate_position = Mock()
    try:
        monitor._sell_cycle([LivePool(500, monitor.config.weth)])
        monitor._evaluate_position.assert_not_called()
    finally:
        journal.close()


def test_retries_share_one_rpc_time_budget(monkeypatch):
    clock = [0.0]
    rpc = JsonRpc("https://primary.invalid", "https://secondary.invalid", ExecutionConfig())

    def timeout(*args, **kwargs):
        clock[0] += kwargs["timeout"]
        raise httpx.ReadTimeout("time budget used")

    rpc._client.post = Mock(side_effect=timeout)
    monkeypatch.setattr("oasis_agent.sniping.rpc.time.monotonic", lambda: clock[0])
    try:
        with pytest.raises(RpcError, match="ReadTimeout"):
            rpc.call("eth_call", [])
        assert rpc._client.post.call_count == 1
        assert clock[0] == rpc.config.rpc_timeout_ms / 1000
    finally:
        rpc.close()
