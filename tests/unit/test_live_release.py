"""Release refusal cannot be bypassed with CLI flags, state or missing config."""

import json
import os
import sys
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import pytest

from oasis_agent.live.chain import ChainError
from oasis_agent.live.config import LiveConfig
from oasis_agent.live.state import LoopState, StateStore
from oasis_agent.reporting import live_release as release_module
from oasis_agent.reporting.live_release import (
    AcceptancePermit,
    acceptance_allows,
    live_release,
    require_live_action,
    require_live_start,
)
from oasis_agent.reporting.live_status import read_live_status
from scripts import run_live_lp as cli
from tests.unit.test_live_lp import CONFIG, ScriptedRpc, loop_with, market


def close_release(monkeypatch: pytest.MonkeyPatch, permit: AcceptancePermit | None = None) -> None:
    """The owner's stop switch: closed release, optionally with one acceptance permit."""
    monkeypatch.setattr(release_module, "RELEASE_OPEN", False)
    monkeypatch.setattr(release_module, "ACCEPTANCE_PERMIT", permit)


NONCE3 = AcceptancePermit(kind="approve_usdc", account_nonce=3, authorized_by="test")


@pytest.mark.parametrize("args", [[], ["--readiness"], ["--signer-socket", "/absent.sock"]])
def test_release_cli_refuses_before_any_rpc_signer_or_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    args: list[str],
) -> None:
    def forbidden(*_: Any, **__: Any) -> Any:
        raise AssertionError("release refusal must precede external access")

    close_release(monkeypatch)
    monkeypatch.setattr(cli, "rpc_url", forbidden)
    monkeypatch.setattr(cli, "TransactionSocketClient", forbidden)
    path = tmp_path / "never-created"
    monkeypatch.setattr(sys, "argv", ["run", "--mode", "live", "--state-dir", str(path), *args])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 3 and not path.exists()
    report = json.loads(capsys.readouterr().out)
    assert report == live_release().model_dump()
    assert [b["code"] for b in report["blockers"]] == ["release_closed"]
    assert report["new_actions_allowed"] is False and report["owner_canary_authorized"] is True


@pytest.mark.parametrize("kind", ["swap", "mint", "burn", "approve_usdc", "approve_permit2"])
def test_internal_action_also_refuses_before_signing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    close_release(monkeypatch)
    loop = loop_with(tmp_path, ScriptedRpc())
    loop.mode = "live"
    with pytest.raises(ChainError, match="live release blocked"):
        loop._act(market(), (kind, {}))
    assert loop.state.pending is None


def test_committed_release_opens_the_bounded_canary_only() -> None:
    report = live_release()
    # The consumed nonce-3 permit is retired; the release names its receipt evidence.
    assert release_module.ACCEPTANCE_PERMIT is None and report.acceptance_permit is None
    assert report.new_actions_allowed is True and report.blockers == []
    assert report.release_evidence is not None
    assert "0xe0363189ebb4e995fa3bedd58e228da7d6bfdc8c10b6f98c3cba3c2b2c5dc4fd" in (
        report.release_evidence
    )
    # Opening never widens the owner's capital or loss limits.
    assert (report.capital_limit_usd, report.daily_loss_limit_usd) == (500, 200)
    assert report.cumulative_loss_limit_usd == 500
    require_live_start()
    for kind in ("approve_permit2", "swap", "mint", "burn"):
        require_live_action(kind, 4)


def test_closed_permit_admits_exactly_its_nonce(monkeypatch: pytest.MonkeyPatch) -> None:
    close_release(monkeypatch, NONCE3)
    report = live_release()
    # A permit never opens the general release or removes the blocker.
    assert report.new_actions_allowed is False and len(report.blockers) == 1
    assert report.acceptance_permit == NONCE3
    require_live_start()
    require_live_action("approve_usdc", 3)


@pytest.mark.parametrize(
    ("kind", "nonce"),
    [
        ("approve_usdc", 4),
        ("approve_usdc", 2),
        ("approve_usdc", True),  # bool is not a nonce
        ("approve_usdc", "3"),
        ("approve_usdc", None),
        ("approve_permit2", 3),
        ("swap", 3),
        ("mint", 3),
        ("burn", 3),
        (None, 3),
    ],
)
def test_acceptance_permit_admits_nothing_else(
    monkeypatch: pytest.MonkeyPatch, kind: Any, nonce: Any
) -> None:
    close_release(monkeypatch, NONCE3)
    assert not acceptance_allows(kind, nonce)
    with pytest.raises(ValueError, match="live release blocked"):
        require_live_action(kind, nonce)


@pytest.mark.parametrize("kind", ["swap", "mint", "burn", "approve_permit2"])
def test_internal_non_permitted_action_refuses_under_permit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    close_release(monkeypatch, NONCE3)
    loop = loop_with(tmp_path, ScriptedRpc())
    loop.mode = "live"
    with pytest.raises(ChainError, match="live release blocked"):
        loop._act(market(), (kind, {}))
    assert loop.state.pending is None
    # A refused action halts instead of re-polling and spending the lifetime RPC budget.
    assert loop.state.halted is not None and kind in loop.state.halted
    assert loop.state.history[-1]["event"] == "halt"


def test_refused_action_halts_the_tick_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    close_release(monkeypatch, NONCE3)
    loop = loop_with(tmp_path, ScriptedRpc())
    loop.mode = "live"
    with pytest.raises(ChainError):
        loop._act(market(), ("approve_permit2", {}))
    assert loop._tick().startswith("halted: release gate refused approve_permit2")


def test_without_permit_startup_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    close_release(monkeypatch)
    assert live_release().acceptance_permit is None
    with pytest.raises(ValueError, match="live release blocked"):
        require_live_start()
    assert not acceptance_allows("approve_usdc", 3)


def test_missing_state_still_exposes_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    response = read_live_status(tmp_path / "absent")
    assert response.status == "missing"
    assert response.release == live_release()
    close_release(monkeypatch)
    response = read_live_status(tmp_path / "absent")
    assert response.release.new_actions_allowed is False


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("max_daily_loss_usd", 201),
        ("max_cumulative_loss_usd", 501),
        ("max_daily_loss_usd", True),
        ("max_actions_per_day", 13),
        ("approval_usdc", 700_000_001),
        ("approval_days", 31),
        ("poll_seconds", 0),
        ("gas_reserve_wei", 0),
        ("oracle_max_age_seconds", 1801),
        ("usdc_oracle_max_age_seconds", 90_001),
        ("usdc_oracle_max_age_seconds", 0),
        ("max_oracle_deviation_bps", 41),
        ("swap_slippage_bps", 51),
        ("recenter_cooldown_seconds", 599),
        ("decimals1", 18),
        ("pool_id", "0x" + "ab" * 32),
        ("secondary_rpc_env", "https://secret.invalid"),
    ],
)
def test_config_cannot_widen_authority_or_confuse_units(name: str, value: Any) -> None:
    with pytest.raises(ValueError):
        replace(CONFIG, **{name: value}).validate()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("fee", 500.0),
        ("fee", "500"),
        ("fee", True),
        ("tick_spacing", 0),
        ("tick_spacing", -10),
        ("tick_spacing", "10"),
        ("unknown", 1),
    ],
)
def test_pool_config_rejects_coercion_and_extra_fields(
    tmp_path: Path, name: str, value: Any
) -> None:
    raw = asdict(CONFIG)
    raw["pool"][name] = value
    path = tmp_path / "config.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError):
        LiveConfig.load(path)


def test_duplicate_config_is_not_last_value_wins(tmp_path: Path) -> None:
    raw = json.dumps(asdict(CONFIG))
    path = tmp_path / "config.json"
    path.write_text(raw[:-1] + ', "max_daily_loss_usd": 1}')
    with pytest.raises(ValueError, match="duplicate"):
        LiveConfig.load(path)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("initial_nav_usd", float("nan")),
        ("daily_loss_usd", float("inf")),
        ("daily_loss_usd", -1),
        ("external_flow_usd", float("nan")),
        ("gas_spent_wei", -1),
        ("actions_today", True),
        ("acknowledge_flow", "false"),
        ("last_units", [0, -1, 0]),
        ("last_units", [0, 0]),
    ],
)
def test_corrupt_risk_state_is_refused_on_read_and_write(
    tmp_path: Path, name: str, value: Any
) -> None:
    store = StateStore(tmp_path / "state.json")
    state = LoopState("live")
    store.save(state)
    original = store.path.read_bytes()
    setattr(state, name, value)
    with pytest.raises(ValueError):
        store.save(state)
    assert store.path.read_bytes() == original
    raw = json.loads(original)
    raw[name] = value
    store.path.write_text(json.dumps(raw))
    with pytest.raises(ValueError):
        store.load("live")


def test_state_reader_rejects_symlink_fifo_and_duplicates(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.json")
    store.save(LoopState("live"))
    target = store.path.with_name("other.json")
    target.symlink_to(store.path)
    with pytest.raises(OSError):
        StateStore(target).load("live")
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(ValueError, match="regular"):
        StateStore(fifo).load("live")
    raw = store.path.read_text()
    store.path.write_text(raw[:-1] + ', "daily_loss_usd": 0}')
    with pytest.raises(ValueError, match="duplicate"):
        store.load("live")


def test_state_writer_never_follows_predictable_temp_symlink(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.json")
    unrelated = tmp_path / "preserve.txt"
    unrelated.write_text("preserve")
    store.path.with_suffix(".tmp").symlink_to(unrelated)
    store.save(LoopState("live"))
    assert unrelated.read_text() == "preserve"
    assert store.load("live").gas_spent_wei == 0
