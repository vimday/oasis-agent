"""Operational checks are observational and cannot clear latches or sign."""

import json
import sqlite3
import sys
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import pytest

from oasis_agent.execution import account_capture
from oasis_agent.live import budget_rpc
from oasis_agent.live.exact_risk import ExactRisk
from oasis_agent.live.precheck import check_live
from oasis_agent.live.rpc_profile import LiveRpcProfile
from oasis_agent.live.runner import LiveLoop
from oasis_agent.live.state import LoopState, Pending, StateStore
from oasis_agent.models.common import canonical_json, sha256_hex
from oasis_agent.rpc.budget import RpcBudget
from scripts import run_live_lp as cli
from tests.unit.test_live_budget_rpc import Http
from tests.unit.test_live_lp import CONFIG, market


class NonceRpc:
    def __init__(self, pending: int = 3) -> None:
        self.pending = pending

    def nonce(self, _: str, block: str) -> int:
        return 3 if block == "latest" else self.pending


@pytest.mark.parametrize("fault", ["config", "corrupt", "readonly"])
def test_retained_precheck_checks_binding_before_rpc_without_writes(tmp_path, monkeypatch, fault):
    config = replace(CONFIG, usdc_oracle_max_age_seconds=90_000) if fault == "config" else CONFIG
    store = StateStore(tmp_path / "live.state.json")
    store.bind(sha256_hex(canonical_json(asdict(config))))
    store.save(LoopState("live"))
    if fault == "corrupt":
        with sqlite3.connect(store.journal_path) as db:
            db.execute("DROP TRIGGER no_update")
            db.execute("UPDATE events SET digest = ? WHERE seq = 1", ("0" * 64,))
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    connect = sqlite3.connect

    def readonly(database, **kwargs):
        assert database.endswith("?mode=ro")
        return connect(database, **kwargs)

    if fault == "readonly":
        monkeypatch.setattr(sqlite3, "connect", readonly)
        monkeypatch.setattr(LiveLoop, "observe", lambda _: market())
        monkeypatch.setattr(LiveLoop, "runtime_pins", lambda *_: CONFIG.expected_pins)
    else:

        def forbidden(*_):
            pytest.fail("invalid retained state must refuse before any chain observation")

        monkeypatch.setattr(LiveLoop, "observe", forbidden)
    result = check_live(CONFIG, store, NonceRpc(), NonceRpc(), stop_file=tmp_path / "STOP")
    assert result["operational_checks_passed"] == (fault == "readonly")
    if fault != "readonly":
        assert result["blockers"] == ["retained_state_unresolved"]
    assert not result["execution_eligible"] and not result["signer_contacted"]
    assert before == {p.name: p.read_bytes() for p in tmp_path.iterdir()}


@pytest.mark.parametrize(
    ("condition", "reason"),
    [
        ("ok", None),
        ("stop", "stop_file_present"),
        ("halt", "state_halted"),
        ("legacy", "historical_native_cost_incomplete"),
        ("pending", "pending_signed_commitment_missing"),
        ("nonce", "account_nonce_unresolved"),
        ("error", "chain_observation_unresolved"),
        ("signing", "signing_outcome_unresolved"),
        ("unproven_loss", "chain_observation_unresolved"),
    ],
)
def test_precheck_never_changes_state_or_creates_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, condition: str, reason: str | None
) -> None:
    store = StateStore(tmp_path / "live.state.json")
    state = LoopState("live")
    if condition == "halt":
        state.halted = "operator halt"
    if condition == "legacy":
        state.legacy_gas_cost_incomplete = True
    if condition == "pending":
        state.pending = Pending("swap", "0x" + "ab" * 32, 3, 0)
    if condition == "signing":
        state.signing_attempt = {"nonce": 3}
    if condition == "unproven_loss":
        state.exact_risk = ExactRisk(basis=10_000_000_000, checkpoint=10_000_000_000)
    store.save(state)
    before = store.path.read_bytes()
    stop = tmp_path / "STOP"
    if condition == "stop":
        stop.touch()

    def observe(loop: LiveLoop) -> Any:
        assert loop.signer is None and loop.receipt_costs is None
        if condition == "error":
            raise RuntimeError("https://provider.invalid/private-credential")
        return market()

    monkeypatch.setattr(LiveLoop, "observe", observe)
    monkeypatch.setattr(LiveLoop, "runtime_pins", lambda *_: CONFIG.expected_pins)
    result = check_live(
        CONFIG, store, NonceRpc(), NonceRpc(4 if condition == "nonce" else 3), stop_file=stop
    )
    assert result["operational_checks_passed"] == (condition == "ok")
    assert result["execution_eligible"] is False and result["signer_contacted"] is False
    if reason:
        assert reason in result["blockers"]
    assert "private-credential" not in str(result)
    assert store.path.read_bytes() == before


def test_cli_check_refuses_duplicate_endpoints_without_signer_or_state_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "rpc_url", lambda _: "https://provider.invalid/secret")

    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("read-only check must never construct a signer")

    monkeypatch.setattr(cli, "TransactionSocketClient", forbidden)
    state_dir = tmp_path / "absent"
    monkeypatch.setattr(
        sys, "argv", ["check", "--mode", "live", "--check", "--state-dir", str(state_dir)]
    )
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 3 and not state_dir.exists()
    output = capsys.readouterr().out
    assert "precheck_inputs_unresolved" in output and "secret" not in output


def test_checking_new_state_never_persists_observations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = StateStore(tmp_path / "absent" / "live.state.json")
    monkeypatch.setattr(LiveLoop, "observe", lambda _: market())
    monkeypatch.setattr(LiveLoop, "runtime_pins", lambda *_: CONFIG.expected_pins)
    result = check_live(CONFIG, store, NonceRpc(), NonceRpc(), stop_file=tmp_path / "STOP")
    assert result["operational_checks_passed"] and not store.path.parent.exists()
    assert asdict(store.load("live")) == asdict(LoopState("live"))


def setup_budgeted_cli(tmp_path, monkeypatch):
    profile = LiveRpcProfile(max_requests=2, min_interval_ms=100)
    http = Http()
    monkeypatch.setattr(cli.LiveRpcProfile, "load", lambda _: profile)
    monkeypatch.setattr(cli, "rpc_url", lambda name: "https://" + name + ".invalid")
    monkeypatch.setattr(budget_rpc, "_UrlLibPoster", lambda *_: http)
    monkeypatch.setattr(account_capture, "_UrlLibPoster", lambda *_: http)

    def forbidden(*args, **kwargs):
        raise AssertionError("diagnostic must never construct a signer")

    monkeypatch.setattr(cli, "TransactionSocketClient", forbidden)
    monkeypatch.setattr(
        sys, "argv", ["check", "--mode", "live", "--check", "--state-dir", str(tmp_path)]
    )
    return profile, http


def test_cli_check_restarts_share_durable_budget_for_all_transports(tmp_path, monkeypatch, capsys):
    profile, http = setup_budgeted_cli(tmp_path, monkeypatch)
    path = tmp_path / "live.receipt-budget.db"
    policies = profile.policies(("live_primary", "live_secondary"))
    with RpcBudget(path, policies):
        pass
    store = StateStore(tmp_path / "live.state.json")
    store.save(LoopState("live"))
    before = store.path.read_bytes()

    def check(config, store, primary, secondary, *, stop_file, receipt_costs):
        primary.call("eth_chainId", [])
        secondary.call("eth_chainId", [])
        receipt_costs.sessions("live_primary")[0].request("eth_chainId", [])
        return {"operational_checks_passed": True, "execution_eligible": False, "observation": {}}

    monkeypatch.setattr(cli, "check_live", check)
    cli.main()
    report = json.loads(capsys.readouterr().out)
    assert report["rpc_budget_persistent"] is True
    assert report["observation"]["nav_evidence_ephemeral"] is True
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 3
    assert len(http.calls) == 3  # Second invocation refuses before its first HTTP call.
    with RpcBudget(path, policies, require_existing=True) as budget:
        assert budget.counts() == {"live_primary": 2, "live_secondary": 1}
    assert store.path.read_bytes() == before
    assert {p.name for p in tmp_path.iterdir()} == {"live.state.json", "live.receipt-budget.db"}


@pytest.mark.parametrize("kind", ["missing", "empty", "changed", "exhausted"])
def test_cli_precheck_has_no_fresh_quota_fallback(tmp_path, monkeypatch, capsys, kind):
    profile, http = setup_budgeted_cli(tmp_path, monkeypatch)
    path = tmp_path / "live.receipt-budget.db"
    policies = profile.policies(("live_primary", "live_secondary"))
    if kind == "empty":
        path.touch()
    elif kind in {"changed", "exhausted"}:
        with RpcBudget(path, policies) as budget:
            if kind == "exhausted":
                budget.reserve("live_primary", now_ms=1000)
                budget.reserve("live_primary", now_ms=1100)
        if kind == "changed":
            monkeypatch.setattr(
                cli.LiveRpcProfile, "load", lambda _: LiveRpcProfile(max_requests=3)
            )
    before = path.read_bytes() if path.exists() else None
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 3
    assert not http.calls
    assert (path.read_bytes() if path.exists() else None) == before
    assert not (tmp_path / "live.state.json").exists()
    assert "https://" not in capsys.readouterr().out
