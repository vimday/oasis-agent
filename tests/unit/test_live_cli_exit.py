"""A HALT or skipped bounded live tick must reach the process supervisor."""

import json
import sys
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest

from oasis_agent.live.chain import ChainError
from oasis_agent.live.runner import LiveLoop
from oasis_agent.live.state import LoopState, StateStore
from scripts import run_live_lp as cli
from tests.unit.gas_oracle_fixtures import synthetic_gas_pins
from tests.unit.test_live_lp import CONFIG


@pytest.mark.parametrize("mode", ["live", "shadow"])
@pytest.mark.parametrize(
    "outcome", ["halt", "already_halted", "bounded", "stop_file", "rpc_refused"]
)
def test_run_exit_reflects_persisted_halt(tmp_path, monkeypatch, capsys, mode, outcome):
    config = replace(CONFIG, expected_pins=CONFIG.expected_pins | synthetic_gas_pins())
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(asdict(config)))
    state = StateStore(tmp_path / f"{mode}.state.json")
    reason = "live observation refused; operator review required before polling"
    if outcome == "already_halted":
        state.save(LoopState(mode=mode, halted=reason))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_live_lp",
            "--config",
            str(config_path),
            "--mode",
            mode,
            "--state-dir",
            str(tmp_path),
            "--ticks",
            "1",
        ]
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
            return "stopped: stop file present"
        elif outcome == "rpc_refused":
            raise ChainError("budgeted live RPC read refused; details redacted")
        return "block 10 | hold"

    monkeypatch.setattr(LiveLoop, "_tick", tick)
    monkeypatch.setattr("oasis_agent.live.runner.time.sleep", lambda _: None)
    halted = outcome in {"halt", "already_halted"}
    if halted or (mode == "live" and outcome in {"stop_file", "rpc_refused"}):
        with pytest.raises(SystemExit) as caught:
            cli.main()
        assert caught.value.code == 3
        assert state.load(mode).halted == (reason if halted else None)
    else:
        assert cli.main() is None
        assert state.load(mode).halted is None
    output = capsys.readouterr().out
    report = json.loads(output[output.index("{\n") :])
    assert report["halted"] == state.load(mode).halted
    assert report["mode"] == mode

    if mode == "live" and outcome == "rpc_refused":
        assert report["tick_status"] == "skip: budgeted live RPC read refused; details redacted"
