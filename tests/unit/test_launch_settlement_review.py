"""Settlement must not reuse the decision snapshot or allow duplicate buys."""

from unittest.mock import Mock

import pytest
from eth_account import Account

from oasis_agent.sniping.config import ExecutionConfig
from oasis_agent.sniping.journal import ExecutionJournal
from oasis_agent.sniping.monitor import Monitor
from oasis_agent.sniping.rpc import DualValue, RpcError


def test_broadcast_success_remains_pending_until_accounting(tmp_path):
    cfg = ExecutionConfig()
    rpc = Mock()
    tx_hash = "0x" + "a" * 64
    rpc.wait_receipt.return_value = {
        "transactionHash": tx_hash,
        "status": "0x1",
        "blockNumber": "0x65",
        "blockHash": "0x" + "b" * 64,
        "gasUsed": "0x5208",
    }
    with ExecutionJournal(tmp_path / "j.db", {"review": True}) as journal:
        monitor = Monitor(cfg, rpc, cfg.weth, Mock(), journal, tmp_path / "stop")
        monitor._sign_and_bind = Mock(return_value=(tx_hash, "01"))
        monitor._broadcast({"nonce": 1})
        assert monitor.submission_unknown, (
            "Receipt is not yet reconciled: repeat buy must be blocked"
        )


def test_receipt_balance_uses_receipt_hash_not_decision_snapshot(tmp_path):
    cfg = ExecutionConfig()
    rpc = Mock()
    rpc.snapshot = {"blockHash": "0x" + "a" * 64, "requireCanonical": True}
    rpc.dual.return_value = DualValue("0x64", "0x64")
    block_hash = "0x" + "b" * 64
    with ExecutionJournal(tmp_path / "j.db", {"review": True}) as journal:
        monitor = Monitor(cfg, rpc, cfg.weth, None, journal, tmp_path / "stop")
        assert monitor._settled_token_balance(block_hash) == 100
        assert rpc.dual.call_args.args[1][1] == {
            "blockHash": block_hash,
            "requireCanonical": True,
        }


def test_missing_receipt_status_never_becomes_success(tmp_path):
    cfg = ExecutionConfig()
    rpc = Mock()
    rpc.wait_receipt.return_value = {"blockNumber": "0x65"}
    with ExecutionJournal(tmp_path / "j.db", {"review": True}) as journal:
        monitor = Monitor(cfg, rpc, cfg.weth, Mock(), journal, tmp_path / "stop")
        monitor._sign_and_bind = Mock(return_value=("0x" + "a" * 64, "01"))
        with pytest.raises(RpcError):
            monitor._broadcast({"nonce": 1})
        assert monitor.submission_unknown


def test_signing_normalizes_allowlisted_lowercase_router(tmp_path):
    cfg = ExecutionConfig()
    with ExecutionJournal(tmp_path / "checksum.db", {"test": True}) as journal:
        account = Account.create()
        monitor = Monitor(cfg, Mock(), account.address, account, journal, tmp_path / "stop")
        tx_hash, _ = monitor._sign_and_bind(
            {
                "nonce": 0,
                "chainId": 8453,
                "gas": 21000,
                "gasPrice": 1,
                "to": cfg.universal_router,
                "value": 0,
            }
        )
        assert len(tx_hash) == 66
        assert journal.read()[-1]["tx_hash"] == tx_hash
