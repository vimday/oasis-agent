"""Operational checks are observational and cannot clear latches or sign."""

import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from oasis_agent.live.precheck import check_live
from oasis_agent.live.runner import LiveLoop
from oasis_agent.live.state import LoopState, Pending, StateStore
from scripts import run_live_lp as cli
from tests.unit.test_live_lp import CONFIG, market


class NonceRpc:
    def __init__(self, pending: int = 3) -> None:
        self.pending = pending

    def nonce(self, _: str, block: str) -> int:
        return 3 if block == "latest" else self.pending


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

    monkeypatch.setattr(cli, "SocketKeyProvider", forbidden)
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
