"""Free-provider diagnostics cannot silently retry or reset a spent quota."""

import json
from pathlib import Path

import pytest

from oasis_agent.models.common import canonical_json
from oasis_agent.rpc.budget import BudgetPolicy
from scripts import probe_free_base_rpc as probe

TX = "0x" + "11" * 32
BLOCK = "0x" + "22" * 32


class Http:
    def __init__(self, error=False):
        self.calls = []
        self.error = error

    def post(self, payload):
        req = json.loads(payload)
        self.calls.append(req["method"])
        if self.error:
            return canonical_json(
                {
                    "jsonrpc": "2.0",
                    "id": req["id"],
                    "error": {"code": -32005, "message": "rate limited"},
                }
            )
        value = {
            "eth_chainId": "0x2105",
            "eth_getTransactionReceipt": {
                "transactionHash": TX,
                "blockNumber": "0x1",
                "blockHash": BLOCK,
            },
            "eth_getBlockByNumber": {"hash": BLOCK},
            "debug_traceTransaction": {"type": "CALL"},
        }[req["method"]]
        return canonical_json({"jsonrpc": "2.0", "id": req["id"], "result": value})


def test_total_request_budget_survives_repeated_probe_invocations(tmp_path: Path, monkeypatch):
    http = Http()
    monkeypatch.setattr(probe, "_UrlLibPoster", lambda *_: http)
    monkeypatch.setattr(probe, "POLICIES", tuple(BudgetPolicy(k, 8, 0) for k in probe.PUBLIC))
    path = tmp_path / "budget.db"
    assert probe.probe("1rpc", TX, path)["checks"]["trace"]
    assert probe.probe("1rpc", TX, path)["checks"]["trace"]
    assert len(http.calls) == 8
    third = probe.probe("1rpc", TX, path)
    assert third["checks"]["refused_or_unavailable"]
    assert len(http.calls) == 8


def test_rate_limit_has_no_retry_and_retains_raw_refusal(tmp_path: Path, monkeypatch):
    http = Http(error=True)
    monkeypatch.setattr(probe, "_UrlLibPoster", lambda *_: http)
    result = probe.probe("1rpc", TX, tmp_path / "budget.db")
    assert http.calls == ["eth_chainId"]
    assert result["checks"]["refused_or_unavailable"]
    assert len(result["response"]["exchanges"]) == 1


def test_arbitrary_private_provider_is_rejected_before_network(tmp_path: Path, monkeypatch):
    def forbidden(*_):
        raise AssertionError("no network")

    monkeypatch.setattr(probe, "_UrlLibPoster", forbidden)
    with pytest.raises(ValueError, match="free endpoints"):
        probe.probe("private", TX, tmp_path / "budget.db")
