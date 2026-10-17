"""Direct polling and raw acquisition spend one shared persistent read quota."""

import json

import pytest

from oasis_agent.execution import account_capture
from oasis_agent.live import budget_rpc
from oasis_agent.live.chain import ChainError
from oasis_agent.live.rpc_profile import LiveRpcProfile
from oasis_agent.models.common import canonical_json
from oasis_agent.rpc.budget import RpcBudget


class Http:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def post(self, payload):
        req = json.loads(payload)
        self.calls.append(req["method"])
        if self.fail:
            raise OSError("synthetic private endpoint error")
        return canonical_json({"jsonrpc": "2.0", "id": req["id"], "result": "0x1"})


def test_direct_reads_and_raw_acquisition_share_quota_across_restart(tmp_path, monkeypatch):
    http = Http()
    monkeypatch.setattr(budget_rpc, "_UrlLibPoster", lambda *_: http)
    monkeypatch.setattr(account_capture, "_UrlLibPoster", lambda *_, **__: http)
    profile = LiveRpcProfile(max_requests=2, min_interval_ms=100)
    client = budget_rpc.BudgetedRpc("https://unused.invalid", tmp_path, "live_primary", profile)
    assert client.uint("eth_blockNumber", []) == 1
    pair = ("live_primary", "live_secondary")
    path = tmp_path / "live.receipt-budget.db"
    session = account_capture.one_shot_session(
        "live_primary",
        urls={"live_primary": "https://unused.invalid"},
        policies=profile.policies(pair),
        budget=path,
    )
    assert session[0].request("eth_chainId", []) == "0x1"
    restarted = budget_rpc.BudgetedRpc("https://unused.invalid", tmp_path, "live_primary", profile)
    with pytest.raises(ChainError, match="read refused"):
        restarted.nonce("0x" + "11" * 20)
    assert http.calls == ["eth_blockNumber", "eth_chainId"]
    with RpcBudget(path, profile.policies(pair)) as budget:
        assert budget.counts() == {"live_primary": 2, "live_secondary": 0}


def test_direct_read_failure_spends_once_and_never_retries(tmp_path, monkeypatch):
    http = Http(fail=True)
    monkeypatch.setattr(budget_rpc, "_UrlLibPoster", lambda *_: http)
    profile = LiveRpcProfile(max_requests=1)
    client = budget_rpc.BudgetedRpc("https://unused.invalid", tmp_path, "live_primary", profile)
    for _ in range(2):
        with pytest.raises(ChainError, match="details redacted") as error:
            client.block()
        assert "private endpoint" not in str(error.value)
    assert http.calls == ["eth_getBlockByNumber"]


def test_read_interface_cannot_broadcast(tmp_path, monkeypatch):
    http = Http()
    monkeypatch.setattr(budget_rpc, "_UrlLibPoster", lambda *_: http)
    client = budget_rpc.BudgetedRpc(
        "https://unused.invalid", tmp_path, "live_primary", LiveRpcProfile()
    )
    with pytest.raises(ChainError):
        client.call("eth_sendRawTransaction", ["0x00"])
    assert not http.calls
