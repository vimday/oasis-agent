"""Local-state reporting is bounded, redacted and never trading authorization."""

import json
import os
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from oasis_agent.live.chain import ChainError
from oasis_agent.live.state import LoopState, StateStore
from oasis_agent.reporting.live_status import MAX_STATE_BYTES, read_live_status
from oasis_agent.server.config import load_server_config
from tests.unit.server_helpers import make_server_config
from tests.unit.test_live_lp import CONFIG, ScriptedRpc, live_loop, market
from tests.unit.test_server_api import client_for

NOW = 1_791_470_000


def observed_state() -> dict:
    state = asdict(LoopState("live"))
    state["last_observation"] = {
        "chain_id": 8453,
        "account": CONFIG.account,
        "pool_id": CONFIG.pool_id,
        "config_hash": "ab" * 32,
        "block": 123,
        "block_hash": "0x" + "12" * 32,
        "block_timestamp": NOW - 5,
        "observed_at": NOW,
        "nav_usd": 480.0,
        "daily_loss_usd": 40.0,
        "cumulative_loss_usd": 20.0,
        "capital_limit_usd": 500.0,
        "daily_loss_limit_usd": 200.0,
        "cumulative_loss_limit_usd": 500.0,
    }
    return state


def write_state(tmp_path: Path, state: dict) -> Path:
    path = tmp_path / "live.state.json"
    path.write_text(json.dumps(state))
    return path


def test_status_never_infers_freshness_from_file_mtime_or_claims_execution(tmp_path: Path) -> None:
    path = write_state(tmp_path, observed_state())
    before = path.read_bytes()
    fresh = read_live_status(path, now=NOW)
    assert fresh.status == "observed"
    assert not fresh.execution_eligible and not fresh.evidence_replayed
    assert fresh.profitability == "unresolved"
    assert fresh.native_gas_units == "0"
    stale = read_live_status(path, now=NOW + 121)
    assert stale.status == "stale" and "observation_stale" in stale.blockers
    assert stale.state_hash == fresh.state_hash and path.read_bytes() == before


@pytest.mark.parametrize("change", ["missing", "future", "future_block", "old_block", "loss"])
def test_missing_clock_stale_and_exhausted_budget_are_explicit(tmp_path: Path, change: str) -> None:
    state = observed_state()
    if change == "missing":
        del state["last_observation"]
    elif change == "future":
        state["last_observation"]["observed_at"] += 1
    elif change == "future_block":
        state["last_observation"]["block_timestamp"] = NOW + 1
    elif change == "old_block":
        state["last_observation"]["block_timestamp"] = NOW - 121
    else:
        state["last_observation"]["daily_loss_usd"] = 200.0
    report = read_live_status(write_state(tmp_path, state), now=NOW)
    assert report.status == "stale" and report.blockers


@pytest.mark.parametrize("change", ["nan", "bool", "negative", "shadow", "huge", "pending"])
def test_invalid_financial_state_clears_all_numbers(tmp_path: Path, change: str) -> None:
    state = observed_state()
    if change == "nan":
        state["last_observation"]["nav_usd"] = float("nan")
    elif change == "bool":
        state["gas_spent_wei"] = False
    elif change == "negative":
        state["gas_spent_wei"] = -1
    elif change == "shadow":
        state["mode"] = "shadow"
    elif change == "huge":
        state["gas_spent_wei"] = 2**256
    else:
        state["pending"] = {"kind": "arbitrary", "tx_hash": "bad"}
    report = read_live_status(write_state(tmp_path, state), now=NOW)
    assert report.status == "invalid"
    assert report.observation is None and report.native_gas_units is None


def test_halt_pending_and_history_never_echo_arbitrary_fields(tmp_path: Path) -> None:
    state = observed_state()
    marker = "opaque-private-value-do-not-display"
    state.update(halted=marker, unexpected=marker)
    state["pending"] = {
        "kind": "swap",
        "tx_hash": "0x" + "ab" * 32,
        "nonce": 2**63,
        "signed_at": NOW,
        "intent": {"extra": marker},
        "raw_signed": marker,
    }
    state["history"] = [
        {
            "event": "reverted",
            "tx": "0x" + "ab" * 32,
            "native_gas_units": 2**64 + 1,
            "cost_evidence": [{"private": marker}],
            "cash_evidence": [{"private": marker}],
            "reason": marker,
        },
    ]
    report = read_live_status(write_state(tmp_path, state), now=NOW)
    assert report.status == "halted"
    assert "pending_requires_reconciliation" in report.blockers
    assert marker not in report.model_dump_json()
    assert report.recent_actions[0].native_gas_units == str(2**64 + 1)
    assert report.recent_actions[0].cash_references == 1 and not report.evidence_replayed
    assert report.pending.nonce == 2**63


def test_legacy_nonzero_gas_remains_incomplete(tmp_path: Path) -> None:
    state = observed_state()
    del state["legacy_gas_cost_incomplete"]
    state["gas_spent_wei"] = 100
    report = read_live_status(write_state(tmp_path, state), now=NOW)
    assert "historical_native_cost_incomplete" in report.blockers


def test_file_failures_are_redacted_and_never_create_a_state(tmp_path: Path) -> None:
    assert read_live_status(None).status == "unconfigured"
    missing = tmp_path / "absent"
    assert read_live_status(missing).status == "missing" and not missing.exists()
    target = write_state(tmp_path, observed_state())
    link = tmp_path / "link"
    link.symlink_to(target)
    assert read_live_status(link).status == "invalid"
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    assert read_live_status(fifo).status == "invalid"
    target.write_bytes(b" " * (MAX_STATE_BYTES + 1))
    assert read_live_status(target).status == "invalid"
    target.write_text('{"mode":"live","mode":"live"}')
    assert read_live_status(target).status == "invalid"


def test_read_only_endpoint_refreshes_after_atomic_replace_without_leaking_paths(
    tmp_path: Path,
) -> None:
    config = make_server_config(tmp_path)
    path = tmp_path / "local-state.json"
    config = replace(config, databases=replace(config.databases, live_state=path))
    with client_for(tmp_path, config=config) as client:
        assert client.get("/api/v1/live/status").json()["status"] == "missing"
        replacement = write_state(tmp_path, observed_state())
        replacement.replace(path)
        first = client.get("/api/v1/live/status")
        assert first.status_code == 200
        assert first.json()["observation"]["nav_usd"] == 480
        path.write_text("invalid sensitive text")
        response = client.get(
            "/api/v1/live/status", headers={"If-None-Match": first.headers["etag"]}
        )
        assert response.status_code == 200 and response.json()["status"] == "invalid"
        assert response.json()["observation"] is None
        assert str(path) not in response.text and "sensitive text" not in response.text
        assert client.post("/api/v1/live/status").status_code == 405
        meta = client.get("/api/v1/meta").json()
        endpoint = next(x for x in meta["endpoints"] if x["path"] == "/api/v1/live/status")
        assert endpoint["console_view"] == "execution" and endpoint["methods"] == ["GET"]


def test_config_can_monitor_a_not_yet_created_state(tmp_path: Path) -> None:
    config = tmp_path / "server.toml"
    missing = tmp_path / "not-started.json"
    config.write_text(f'[databases]\nlive_state = "{missing}"\n')
    assert load_server_config(config).databases.live_state == missing
    assert not missing.exists()


def test_loop_records_actual_observation_and_does_not_refresh_it_on_failure(tmp_path: Path) -> None:
    loop = live_loop(tmp_path, ScriptedRpc())
    loop.observe = market
    loop._decide = lambda _: None
    loop.tick()
    saved = StateStore(loop.store.path).load("shadow").last_observation
    assert saved["block"] == market().block
    assert saved["nav_usd"] == loop.nav_usd(market())
    assert saved["daily_loss_limit_usd"] == CONFIG.max_daily_loss_usd

    def fail():
        raise ChainError("provider unavailable")

    loop.observe = fail
    loop.tick()
    assert loop.state.last_observation == saved
