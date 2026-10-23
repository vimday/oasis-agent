"""Free-provider diagnostics cannot silently retry or reset a spent quota."""

import json
import sys
from pathlib import Path

import pytest

from oasis_agent.models.common import canonical_json
from oasis_agent.rpc.budget import BudgetPolicy, RpcBudget
from scripts import probe_free_base_rpc as probe

TX = "0x" + "11" * 32
BLOCK = "0x" + "22" * 32


@pytest.fixture(autouse=True)
def isolated_budgets(tmp_path, monkeypatch):
    for attribute, policies in (("BUDGET", "POLICIES"), ("DISCOVERY_BUDGET", "DISCOVERY_POLICIES")):
        path = tmp_path / attribute
        frozen = tuple(BudgetPolicy(p.source, p.max_requests, 0) for p in getattr(probe, policies))
        monkeypatch.setattr(probe, attribute, path)
        monkeypatch.setattr(probe, policies, frozen)
        with RpcBudget(path, frozen):
            pass


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
    assert probe.probe("1rpc", TX)["capability_verified"]
    assert probe.probe("1rpc", TX)["capability_verified"]
    assert len(http.calls) == 8
    third = probe.probe("1rpc", TX)
    assert third["checks"]["refused_or_unavailable"]
    assert len(http.calls) == 8


def test_rate_limit_has_no_retry_and_retains_raw_refusal(tmp_path: Path, monkeypatch):
    http = Http(error=True)
    monkeypatch.setattr(probe, "_UrlLibPoster", lambda *_: http)
    result = probe.probe("1rpc", TX)
    assert http.calls == ["eth_chainId"]
    assert result["checks"]["refused_or_unavailable"]
    assert len(result["response"]["exchanges"]) == 1
    assert result["failure_stage"] == "chain" and not result["capability_verified"]


def test_arbitrary_private_provider_is_rejected_before_network(tmp_path: Path, monkeypatch):
    def forbidden(*_):
        raise AssertionError("no network")

    monkeypatch.setattr(probe, "_UrlLibPoster", forbidden)
    with pytest.raises(ValueError, match="free endpoints"):
        probe.probe("private", TX)


@pytest.mark.parametrize("source", ["quicknode-demo", "onfinality"])
def test_new_source_stops_after_one_attempt_and_cannot_reuse_partial_budget(
    tmp_path, monkeypatch, source
):
    http = Http(error=True)
    monkeypatch.setattr(probe, "_UrlLibPoster", lambda *_: http)
    result = probe.probe(source, TX)
    assert result["checks"]["refused_or_unavailable"] and len(http.calls) == 1
    result = probe.probe(source, TX)
    assert result["failure_stage"] == "budget" and not result["capability_verified"]
    assert len(http.calls) == 1


@pytest.mark.parametrize("source", tuple(probe.PUBLIC | probe.DISCOVERY))
def test_cli_forbids_budget_override(tmp_path, monkeypatch, source):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "probe",
            "--provider",
            source,
            "--transaction",
            TX,
            "--budget",
            str(tmp_path / "reset.db"),
            "--out",
            str(tmp_path / "out"),
        ],
    )
    with pytest.raises(SystemExit) as caught:
        probe.main()
    assert caught.value.code == 2
    assert not (tmp_path / "reset.db").exists() and not (tmp_path / "out").exists()
    assert probe.DISCOVERY_BUDGET.is_absolute()


@pytest.mark.parametrize("mode", ["missing", "corrupt", "symlink", "insufficient"])
@pytest.mark.parametrize("source", ["1rpc", "onfinality"])
def test_unusable_original_budget_refuses_before_network(tmp_path, monkeypatch, mode, source):
    path = probe.BUDGET if source in probe.PUBLIC else probe.DISCOVERY_BUDGET
    policies = probe.POLICIES if source in probe.PUBLIC else probe.DISCOVERY_POLICIES
    if mode == "missing":
        path.unlink()
    elif mode == "corrupt":
        path.write_bytes(b"not sqlite")
    elif mode == "symlink":
        target = tmp_path / "moved.db"
        path.rename(target)
        path.symlink_to(target)
    else:
        with RpcBudget(path, policies, require_existing=True) as ledger:
            for i in range(next(p.max_requests for p in policies if p.source == source) - 3):
                ledger.reserve(source, now_ms=i)
    calls = []
    monkeypatch.setattr(probe, "_UrlLibPoster", lambda *_: calls.append("constructed"))
    result = probe.probe(source, TX)
    assert not result["capability_verified"] and not calls
    assert result["request"]["exchanges"] == []
    if mode == "missing":
        assert not path.exists()
    if mode == "corrupt":
        assert path.read_bytes() == b"not sqlite"


def test_disappearing_ledger_cannot_be_recreated_or_authorize_followup(tmp_path, monkeypatch):
    class Disappearing(Http):
        def post(self, payload):
            result = super().post(payload)
            probe.BUDGET.unlink()
            return result

    http = Disappearing()
    monkeypatch.setattr(probe, "_UrlLibPoster", lambda *_: http)
    result = probe.probe("1rpc", TX)
    assert http.calls == ["eth_chainId"]
    assert result["request_counts"] is None and not result["capability_verified"]
    assert result["failure_stage"] == "budget_finalization"
    assert not probe.BUDGET.exists()


def test_renamed_cli_outputs_and_working_directory_do_not_renew_budget(tmp_path, monkeypatch):
    http = Http()
    monkeypatch.setattr(probe, "_UrlLibPoster", lambda *_: http)
    monkeypatch.chdir(tmp_path)
    for i, exit_code in enumerate((0, 0, 3)):
        out = tmp_path / f"report-{i}.json"
        monkeypatch.setattr(
            sys, "argv", ["probe", "--provider", "1rpc", "--transaction", TX, "--out", str(out)]
        )
        with pytest.raises(SystemExit) as caught:
            probe.main()
        assert caught.value.code == exit_code
        assert json.loads(out.read_text())["capability_verified"] == (exit_code == 0)
    assert len(http.calls) == 8
    assert not (tmp_path / "reports").exists()


def test_cli_refuses_dangling_output_symlink_before_probe(tmp_path, monkeypatch):
    out = tmp_path / "out.json"
    out.symlink_to(tmp_path / "missing")
    monkeypatch.setattr(
        sys, "argv", ["probe", "--provider", "1rpc", "--transaction", TX, "--out", str(out)]
    )
    monkeypatch.setattr(probe, "probe", lambda *_: pytest.fail("must not probe"))
    with pytest.raises(SystemExit) as caught:
        probe.main()
    assert caught.value.code == 2 and not out.exists()
