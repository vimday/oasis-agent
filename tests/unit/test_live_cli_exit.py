"""A persisted HALT must reach process supervisors as a failed run."""

import json
import sys
from types import SimpleNamespace

import pytest

from oasis_agent.live.runner import LiveLoop
from oasis_agent.live.state import LoopState, StateStore
from scripts import run_live_lp as cli


@pytest.mark.parametrize("mode", ["live", "shadow"])
@pytest.mark.parametrize("outcome", ["halt", "already_halted", "bounded", "stop_file"])
def test_run_exit_reflects_persisted_halt(tmp_path, monkeypatch, capsys, mode, outcome):
    state = StateStore(tmp_path / f"{mode}.state.json")
    reason = "live observation refused; operator review required before polling"
    if outcome == "already_halted":
        state.save(LoopState(mode=mode, halted=reason))
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_live_lp", "--mode", mode, "--state-dir", str(tmp_path), "--ticks", "1"]
        + (["--signer-socket", str(tmp_path / "unused.sock")] if mode == "live" else []),
    )
    monkeypatch.setenv("BASE_RPC_PRIMARY_ALT", "https://primary.invalid")
    monkeypatch.setenv("BASE_RPC_PRIMARY", "https://secondary.invalid")
    monkeypatch.setattr(cli, "require_live_start", lambda: None)
    monkeypatch.setattr(cli, "BudgetedRpc", lambda *a, **kw: object())
    monkeypatch.setattr(
        cli,
        "receipt_cost_reader",
        lambda config, *a, **kw: SimpleNamespace(
            chain_id=config.chain_id,
            account=config.account,
            native_decimals=config.decimals0,
        ),
    )

    def tick(loop):
        if outcome == "halt":
            loop.state.halted = reason
        elif outcome == "stop_file":
            loop.stop_file.touch()
        # Real tick/run persist the latch and break before sleeping on a halt.
        return "skip: budgeted live RPC read refused; details redacted"

    monkeypatch.setattr(LiveLoop, "_tick", tick)
    monkeypatch.setattr("oasis_agent.live.runner.time.sleep", lambda _: None)
    if outcome in {"halt", "already_halted"}:
        with pytest.raises(SystemExit) as caught:
            cli.main()
        assert caught.value.code == 3
        assert state.load(mode).halted == reason
    else:
        assert cli.main() is None
        assert state.load(mode).halted is None
    output = capsys.readouterr().out
    report = json.loads(output[output.index("{\n") :])
    assert report["halted"] == state.load(mode).halted
    assert report["mode"] == mode
