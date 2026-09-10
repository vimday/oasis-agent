"""The read-only dashboard must not invent process liveness or pool identities."""

import time
from dataclasses import replace
from unittest.mock import Mock

from eth_abi import encode
from fastapi.testclient import TestClient

from oasis_agent.server import routers_launch
from oasis_agent.server.app import create_app
from oasis_agent.server.routers_launch import _execution_items
from oasis_agent.sniping.config import ExecutionConfig
from oasis_agent.sniping.journal import ExecutionJournal
from oasis_agent.sniping.rpc import rejection_summary

from .server_helpers import make_server_config


def api(tmp_path, *, dry_run, observed_at, extra_events=()):
    config = make_server_config(tmp_path)
    path = tmp_path / "launch.db"
    policy = {
        "config": ExecutionConfig().model_dump(mode="json", by_alias=True),
        "dry_run": dry_run,
        "launch_execution_authorized": not dry_run,
    }
    with ExecutionJournal(path, policy) as journal:
        journal.append(
            "buy_failed",
            {
                "observed_at": observed_at,
                "error": "gas estimation failed",
                "pool": ExecutionConfig().weth,
                "fee": 500,
            },
        )
        for event in extra_events:
            journal.append("buy_failed", event)
    object.__setattr__(config, "databases", replace(config.databases, launch=path))
    return TestClient(create_app(config, environ={"OASIS_SERVER_OPS_TOKEN": "local-ui-test"}))


def test_live_policy_and_read_only_dashboard_are_separate(tmp_path):
    with api(tmp_path, dry_run=False, observed_at=int(time.time())) as client:
        response = client.get("/api/v1/launch/targets")
        assert response.status_code == 200
        data = response.json()
        assert data["dashboard_read_only"] is True
        assert data["status"] == "execution_journal"
        target = data["items"][0]
        assert target["execution_mode"] == "live"
        assert target["watcher_status"] == "recent_journal_activity"
        assert target["process_state"] == "not_verified_by_journal"
        assert target["buy_confirmed_count"] == 0
        assert data["trade_readiness"] == "not_verified"
        approval = client.get("/api/v1/launch/approvals").json()["items"][0]
        assert approval["status"] == "unverified"


def test_execution_journal_stream_delivers_launch_events(tmp_path):
    with (
        api(tmp_path, dry_run=False, observed_at=int(time.time())) as client,
        client.websocket_connect("/api/v1/stream") as websocket,
    ):
        assert websocket.receive_json()["type"] == "decisions"
        event = websocket.receive_json()
        assert event["type"] == "launch_decisions"
        assert event["items"][0]["kind"] == "buy_failed"


def test_old_observation_journal_is_not_reported_as_live(tmp_path):
    with api(tmp_path, dry_run=True, observed_at=1) as client:
        target = client.get("/api/v1/launch/targets").json()["items"][0]
        assert target["execution_mode"] == "observe"
        assert target["watcher_status"] == "stale_or_missing_activity"
        assert target["control_status"] == "not_independently_verified"


def test_buy_failure_does_not_fabricate_token_pair_or_liquidity_failure():
    records = [
        {
            "kind": "buy_failed",
            "pool": ExecutionConfig().weth,
            "fee": 500,
            "error": "signing failed",
            "digest": "first",
            "observed_at": 1,
        }
    ]
    item = _execution_items(records, "candidates")[0]
    assert item["token0"] == item["token1"] == "unverified"
    assert item["fee"] == 500 and item["tradable"] == "attempt_failed"
    assert "G7" not in item["gates"]
    records.append(
        {
            "kind": "quote_observation",
            "pool": ExecutionConfig().weth,
            "fdv_ok": True,
            "digest": "second",
            "observed_at": 2,
        }
    )
    updated = _execution_items(records, "candidates")[0]
    assert updated["tradable"] == "fdv_ok"
    assert updated["digest"] == "second" and "attempt" not in updated["gates"]


def test_launch_timestamps_and_staleness_are_explicit(tmp_path):
    with api(tmp_path, dry_run=False, observed_at=1) as client:
        target = client.get("/api/v1/launch/targets").json()["items"][0]
        assert target["activity_age_seconds"] > target["freshness_threshold_seconds"]
        assert target["last_seq"] == 1
        event = client.get("/api/v1/launch/decisions?latest=true").json()["items"][0]
        assert event["observed_at"] == 1
        candidate = client.get("/api/v1/launch/candidates").json()["items"][0]
        assert candidate["last_observed_at"] == 1


def test_rpc_diagnostics_keep_categories_without_provider_secrets():

    sentinel = "https://example.invalid/opaque/provider-credential"
    assert (
        rejection_summary(
            {"code": -32000, "message": "execution reverted " + sentinel, "data": sentinel}
        )
        == "execution_reverted code=-32000"
    )
    assert (
        rejection_summary({"code": -32005, "message": "rate limit " + sentinel})
        == "rate_limited code=-32005"
    )
    assert (
        rejection_summary({"code": sentinel, "message": sentinel}) == "node_rejected code=unknown"
    )


def test_rpc_known_revert_reason_is_preserved():

    error = {
        "code": 3,
        "message": "execution reverted",
        "data": "0x08c379a0" + encode(["string"], ["SPL"]).hex(),
    }
    assert rejection_summary(error) == "execution_reverted code=3 revert=SPL"
    error["data"] = "0x08c379a0" + encode(["string"], ["unknown sensitive input"]).hex()
    assert "sensitive" not in rejection_summary(error)


def test_cycle_span_does_not_merge_same_number_across_restart(tmp_path):
    events = [
        {"cycle": 6, "observed_at": 1},
        {"cycle": 1, "observed_at": 100},
        {"cycle": 6, "observed_at": 110},
        {"cycle": 6, "observed_at": 112},
    ]
    with api(tmp_path, dry_run=False, observed_at=1, extra_events=events) as client:
        target = client.get("/api/v1/launch/targets").json()["items"][0]
        assert target["current_cycle"] == 6
        assert target["cycle_record_span_seconds"] == 2


def test_dashboard_verifies_once_and_hides_legacy_without_deleting(tmp_path, monkeypatch):
    cfg = ExecutionConfig()
    events = [
        {
            "pool": cfg.weth,
            "observed_at": 2,
            "error": "RpcError: RPC rejected eth_call (details redacted)",
        },
        {
            "pool": cfg.target_ca,
            "observed_at": 3,
            "error": "RPC primary eth_call: execution_reverted code=3 revert=SPL",
        },
    ]
    verify = Mock(wraps=routers_launch._execution_journal)
    monkeypatch.setattr(routers_launch, "_execution_journal", verify)
    with api(tmp_path, dry_run=False, observed_at=1, extra_events=events) as client:
        data = client.get("/api/v1/launch/dashboard").json()
        assert verify.call_count == 1
        assert data["snapshot_seq"] == 3
        assert data["hidden_legacy_count"] == 1
        assert data["audit_records_deleted"] is False
        assert [p["pool"] for p in data["sections"]["candidates"]["items"]] == [cfg.target_ca]
        assert [d["seq"] for d in data["sections"]["decisions"]["items"]] == [3, 1]
        full = client.get("/api/v1/launch/dashboard?include_legacy=true").json()
        assert full["hidden_legacy_count"] == 0
        assert len(full["sections"]["candidates"]["items"]) == 2
        assert client.get("/api/v1/launch/decisions").json()["total"] == 3


def test_stream_reconnect_bounds_backlog_and_reports_truncation(tmp_path):
    events = [{"observed_at": i + 2} for i in range(80)]
    with (
        api(tmp_path, dry_run=False, observed_at=1, extra_events=events) as client,
        client.websocket_connect("/api/v1/stream") as websocket,
    ):
        assert websocket.receive_json()["type"] == "decisions"
        event = websocket.receive_json()
        assert event["type"] == "launch_decisions"
        assert len(event["items"]) == 50
        assert event["history_truncated"] is True
        assert event["total_new_records"] == event["last_seq"] == 81
        assert event["items"][-1]["seq"] == 81


def test_quote_display_uses_policy_and_exact_fdv_without_premature_rounding():
    records = [
        {
            "kind": "quote_observation",
            "native_wei": 1,
            "received_base": 1,
            "eth_usd_micros": 1,
            "fdv_ok": True,
        }
    ]
    target = {"reference_supply": 10**9, "max_fdv_usd_micros": 10**9, "token_decimals": 18}
    item = _execution_items(records, "quotes", target)[0]
    assert item["spend_usd_micros"] == "0"
    assert item["fdv_usd_micros_numerator"] == str(10**9)
    assert item["fdv_below_cap"] is False
    assert _execution_items(records, "quotes", {}) == []
