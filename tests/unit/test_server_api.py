"""End-to-end API tests for the observation server (P0-P3)."""

import ast
import json
import pathlib
import re
import time
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

import oasis_agent.server as package
from oasis_agent.errors import ConfigurationError
from oasis_agent.server.app import create_app
from oasis_agent.server.config import ServerChainConfig, ServerShadowRunnerConfig
from oasis_agent.server.databases import ReadOnlyDatabases

from .server_helpers import build_shadow_db, make_server_config

TOKEN = "ops-token-alpha"
SECOND_TOKEN = "ops-token-beta"


def client_for(tmp_path, *, config=None, environ=None, **config_overrides):
    if config is None:
        config = make_server_config(tmp_path)
    for name, value in config_overrides.items():
        object.__setattr__(config, name, value)
    environ = {"OASIS_SERVER_OPS_TOKEN": TOKEN} if environ is None else environ
    return TestClient(create_app(config, environ=environ))


def test_readiness_unconfigured_fails_closed_with_code(tmp_path):
    config = make_server_config(tmp_path)
    object.__setattr__(config, "databases", replace(config.databases, readiness_manifest=None))
    with client_for(tmp_path, config=config) as client:
        response = client.get("/api/v1/readiness")
        assert response.status_code == 503
        body = response.json()
        assert body["code"] == "unconfigured"
        assert body["detail"] == "readiness manifest is not configured"


def test_readiness_corrupt_fails_closed_with_code(tmp_path):
    config = make_server_config(tmp_path)
    corrupt = tmp_path / "readiness-corrupt.json"
    corrupt.write_text("{not valid json", encoding="utf-8")
    object.__setattr__(config, "databases", replace(config.databases, readiness_manifest=corrupt))
    with client_for(tmp_path, config=config) as client:
        response = client.get("/api/v1/readiness")
        assert response.status_code == 503
        assert response.json()["code"] == "corrupt"


def test_health_reports_all_sources_verified(tmp_path):
    with client_for(tmp_path) as client:
        response = client.get("/api/v1/health")
        assert response.status_code == 200
        body = response.json()
        assert body["schema"] == "oasis.server-health.v1"
        assert body["ok"] is True
        assert body["sources"]["shadow"]["integrity"] == "verified"


def test_status_surfaces_readiness_and_latest_records(tmp_path):
    with client_for(tmp_path) as client:
        body = client.get("/api/v1/status").json()
        assert body["schema"] == "oasis.server-status.v1"
        assert body["execution_eligible"] is False
        assert body["readiness"]["stage"] == "reject"
        assert body["decision_count"] == 1
        assert body["latest_decision"]["decision"] == "hold"


def test_observations_pagination(tmp_path):
    with client_for(tmp_path) as client:
        body = client.get("/api/v1/observations?limit=1&offset=0").json()
        assert body["total"] == 1
        assert body["items"][0]["digest"]
        assert body["items"][0]["execution_eligible"] is False


def test_observations_reject_invalid_limit(tmp_path):
    with client_for(tmp_path) as client:
        assert client.get("/api/v1/observations?limit=0").status_code == 422
        assert client.get("/api/v1/observations?limit=1001").status_code == 422


def test_decisions_filter_and_detail(tmp_path):
    with client_for(tmp_path) as client:
        body = client.get("/api/v1/decisions?decision=hold").json()
        assert body["total"] == 1
        seq = body["items"][0]["seq"]
        detail = client.get(f"/api/v1/decisions/{seq}").json()
        assert detail["schema"] == "oasis.server.decision-detail.v1"
        assert detail["linked_health"] is not None
        assert client.get("/api/v1/decisions/999").status_code == 404


def test_evidence_list_and_detail(tmp_path):
    with client_for(tmp_path) as client:
        body = client.get("/api/v1/evidence").json()
        assert body["total"] == 1
        key = body["items"][0]["evidence_key"]
        detail = client.get(f"/api/v1/evidence/{key}").json()
        assert detail["record"]["content_hash"]
        assert client.get("/api/v1/evidence/nope").status_code == 404


def test_readiness_endpoint(tmp_path):
    with client_for(tmp_path) as client:
        body = client.get("/api/v1/readiness").json()
        assert body["schema"] == "oasis.lp-release-assessment.v1"
        assert len(body["gates"]) == 9
        assert body["execution_eligible"] is False


def test_execution_journal_endpoint(tmp_path):
    with client_for(tmp_path) as client:
        body = client.get("/api/v1/journal/lp").json()
        assert body["schema"] == "oasis.server.execution-journal.v1"
        assert body["policy_hash"]
        assert body["total_events"] == 1


def test_root_serves_dashboard_html(tmp_path):
    with client_for(tmp_path) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]


def test_security_headers_and_no_store(tmp_path):
    with client_for(tmp_path) as client:
        response = client.get("/api/v1/health")
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["referrer-policy"] == "no-referrer"
        csp = response.headers["content-security-policy"]
        assert "default-src 'self'" in csp
        assert "frame-ancestors 'none'" in csp
        # The console reflects live state; it must never be cached.
        index = client.get("/")
        assert index.headers["cache-control"] == "no-store"
        assert "content-security-policy" in index.headers


def test_openapi_docs_available(tmp_path):
    with client_for(tmp_path) as client:
        assert client.get("/docs").status_code == 200


def test_ops_endpoint_requires_token(tmp_path):
    with client_for(tmp_path) as client:
        response = client.post("/api/v1/runs/shadow")
        assert response.status_code == 401


def test_ops_endpoint_rejects_wrong_token(tmp_path):
    with client_for(tmp_path) as client:
        response = client.post(
            "/api/v1/runs/shadow", headers={"Authorization": "Bearer wrong-token"}
        )
        assert response.status_code == 401


def test_ops_disabled_when_token_absent_and_not_required(tmp_path):
    with client_for(tmp_path, environ={}) as client:
        response = client.post("/api/v1/runs/shadow", headers={"Authorization": "Bearer anything"})
        assert response.status_code == 503


def test_ops_required_without_token_fails_closed_at_startup(tmp_path):
    config = make_server_config(tmp_path)
    object.__setattr__(config, "ops_token_required", True)
    app = create_app(config, environ={})
    with pytest.raises(ConfigurationError, match="not set"), TestClient(app):
        pass


def test_trigger_shadow_run_and_journal(tmp_path):
    config = make_server_config(tmp_path)
    object.__setattr__(
        config,
        "shadow_runner",
        ServerShadowRunnerConfig((".venv/bin/python", "-c", "print('shadow ok')"), 30, 1),
    )
    with client_for(tmp_path, config=config) as client:
        response = client.post("/api/v1/runs/shadow", headers={"Authorization": f"Bearer {TOKEN}"})
        assert response.status_code == 200
        body = response.json()
        assert body["schema"] == "oasis.server.run.v1"
        assert body["status"] == "queued"
        run_id = body["run_id"]
        # The run executes asynchronously on the app loop; poll briefly.
        detail = None
        for _ in range(20):
            detail = client.get(f"/api/v1/runs/{run_id}").json()
            if detail["status"] in {"succeeded", "failed"}:
                break
            time.sleep(0.1)
        assert detail["status"] == "succeeded"
        assert detail["exit_code"] == 0
        ops = client.get("/api/v1/ops").json()
        assert any(item["action"] == "trigger_shadow_run" for item in ops["items"])


def test_gate_approval_requires_second_operator(tmp_path):
    with client_for(
        tmp_path, environ={"OASIS_SERVER_OPS_TOKEN": f"{TOKEN},{SECOND_TOKEN}"}
    ) as client:
        headers_a = {"Authorization": f"Bearer {TOKEN}"}
        headers_b = {"Authorization": f"Bearer {SECOND_TOKEN}"}
        request = client.post(
            "/api/v1/p2/gates/wp0_correctness/approval?reason=review",
            headers=headers_a,
        )
        assert request.status_code == 200
        approval = request.json()
        assert approval["status"] == "pending"
        approval_id = approval["id"]

        # Same operator cannot confirm.
        same = client.post(
            f"/api/v1/p2/gates/wp0_correctness/approval/{approval_id}/confirm",
            headers=headers_a,
        )
        assert same.status_code == 409

        # Second operator confirms.
        confirmed = client.post(
            f"/api/v1/p2/gates/wp0_correctness/approval/{approval_id}/confirm",
            headers=headers_b,
        )
        assert confirmed.status_code == 200
        assert confirmed.json()["status"] == "approved"

        # A non-pending approval cannot be confirmed again.
        again = client.post(
            f"/api/v1/p2/gates/wp0_correctness/approval/{approval_id}/confirm",
            headers=headers_b,
        )
        assert again.status_code == 409


def test_gate_approval_rejects_unknown_gate(tmp_path):
    with client_for(tmp_path) as client:
        response = client.post(
            "/api/v1/p2/gates/bogus/approval?reason=why",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        assert response.status_code == 422


def test_gate_approval_listing(tmp_path):
    with client_for(tmp_path) as client:
        body = client.get("/api/v1/p2/gates").json()
        assert body["schema"] == "oasis.server.gate-approvals.v1"


def test_halt_proofs_endpoint(tmp_path):
    with client_for(tmp_path) as client:
        body = client.get("/api/v1/p2/halt-proofs").json()
        assert body["schema"] == "oasis.server.halt-proofs.v1"
        assert body["total"] == 1
        assert body["items"][0]["recovery"]["recovery_ready"] is True


def test_chains_list(tmp_path):
    with client_for(tmp_path) as client:
        body = client.get("/api/v1/chains").json()
        assert body["schema"] == "oasis.server.chains.v1"
        assert body["items"] == []


def test_chain_status_with_mock_rpc(tmp_path):
    def handler(request):
        method = json.loads(request.content)["method"]
        result = {
            "eth_chainId": "0x4663",
            "eth_blockNumber": "0x10",
            "eth_getBalance": "0x0",
        }.get(method, "0x0")
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})

    config = make_server_config(
        tmp_path,
        chains=(ServerChainConfig(46630, "Robinhood Testnet", rpc_url="https://rpc.example"),),
    )
    app = create_app(config, environ={"OASIS_SERVER_OPS_TOKEN": TOKEN})
    with TestClient(app) as client:
        app.state.registry.client(46630, transport=httpx.MockTransport(handler))
        body = client.get("/api/v1/chains/46630/chain").json()
        assert body["rpc_chain_id"] == 0x4663
        assert body["block_number"] == 0x10
        # Unconfigured chain fails closed.
        assert client.get("/api/v1/chains/1/chain").status_code == 503


def test_chain_pool_with_mock_rpc(tmp_path):
    def handler(request):
        method = json.loads(request.content)["method"]
        result = {
            "eth_chainId": "0x4663",
            "eth_blockNumber": "0x10",
            "eth_getBalance": "0xde0b6b3a7640000",
            "eth_getCode": "0x6000",
        }.get(method, "0x0")
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})

    config = make_server_config(
        tmp_path,
        chains=(
            ServerChainConfig(
                46630,
                "Robinhood Testnet",
                rpc_url="https://rpc.example",
                pool_manager="0x" + "cd" * 20,
            ),
        ),
    )
    app = create_app(config, environ={"OASIS_SERVER_OPS_TOKEN": TOKEN})
    with TestClient(app) as client:
        app.state.registry.client(46630, transport=httpx.MockTransport(handler))
        body = client.get("/api/v1/chains/46630/pool").json()
        assert body["data"]["pool_manager_balance_wei"] == 10**18
        assert body["data"]["pool_manager_code_hash"]


def test_alerts_endpoint(tmp_path):
    with client_for(tmp_path) as client:
        body = client.get("/api/v1/alerts").json()
        assert body["schema"] == "oasis.server.alerts.v1"
        rules = {item["rule"] for item in body["items"]}
        assert "source_unavailable" not in rules


def test_websocket_stream_heartbeat(tmp_path):
    config = make_server_config(tmp_path)
    shadow = build_shadow_db(config.databases.shadow, decision_count=0)
    config = replace(config, databases=replace(config.databases, shadow=shadow))
    with (
        client_for(tmp_path, config=config) as client,
        client.websocket_connect("/api/v1/stream") as websocket,
    ):
        message = websocket.receive_json()
        assert message["type"] == "heartbeat"


def test_websocket_stream_pushes_new_decisions(tmp_path):
    config = make_server_config(tmp_path)
    shadow = build_shadow_db(config.databases.shadow, decision_count=0)
    config = replace(config, databases=replace(config.databases, shadow=shadow))
    with (
        client_for(tmp_path, config=config) as client,
        client.websocket_connect("/api/v1/stream") as websocket,
    ):
        first = websocket.receive_json()
        assert first["type"] == "heartbeat"
        # A new decision appears on the next poll cycle. The read-only
        # connection was opened on the app event loop, so the swap must
        # run there too (sqlite is thread-affine).
        rebuild = build_shadow_db(config.databases.shadow, decision_count=1)
        config = replace(config, databases=replace(config.databases, shadow=rebuild))

        def _swap() -> None:
            client.app.state.databases.close()
            client.app.state.databases = ReadOnlyDatabases(config)

        client.portal.call(_swap)
        second = websocket.receive_json()
        assert second["type"] == "decisions"
        assert len(second["items"]) == 1


def test_server_package_never_imports_broadcast_capability():
    root = pathlib.Path(package.__file__).parent
    forbidden = ("web3", "eth_account", "lp_signer", "broadcast", "sign")
    for path in sorted(root.rglob("*.py")):
        # Inspect imports at every nesting level. Match identifiers rather than
        # substrings: the stdlib signal module is process control, not signing.
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            names = [alias.name for alias in node.names]
            if isinstance(node, ast.ImportFrom) and node.module:
                names.append(node.module)
            for name in names:
                for part in name.split("."):
                    for token in forbidden:
                        assert not (part == token or part.startswith(token + "_")), (
                            f"{path.name} imports forbidden capability {name}"
                        )


def _dashboard_html() -> str:
    root = pathlib.Path(package.__file__).parent
    return (root / "static" / "index.html").read_text(encoding="utf-8")


def test_meta_endpoint_describes_api(tmp_path):
    with client_for(tmp_path) as client:
        body = client.get("/api/v1/meta").json()
        assert body["schema"] == "oasis.server-meta.v1"
        assert body["server"] == "oasis-agent-observation-server"
        assert body["version"]
        assert "oasis.server-health.v1" in body["envelope_schemas"]
        assert "oasis.shadow-lp-decision.v1" in body["data_schemas"]
        paths = {endpoint["path"] for endpoint in body["endpoints"]}
        assert "/api/v1/health" in paths
        assert "/api/v1/alerts" in paths
        methods = {endpoint["path"]: endpoint["methods"] for endpoint in body["endpoints"]}
        assert "POST" in methods["/api/v1/runs/shadow"]


def test_etag_conditional_get(tmp_path):
    with client_for(tmp_path) as client:
        first = client.get("/api/v1/health")
        etag = first.headers["etag"]
        assert etag.startswith('"')
        cached = client.get("/api/v1/health", headers={"If-None-Match": etag})
        assert cached.status_code == 304
        assert cached.content == b""
        # Mutations and the console page are never etagged.
        assert "etag" not in client.post("/api/v1/runs/shadow").headers
        assert "etag" not in client.get("/").headers


def test_request_id_header(tmp_path):
    with client_for(tmp_path) as client:
        response = client.get("/api/v1/health")
        request_id = response.headers["x-request-id"]
        assert len(request_id) == 12
        assert request_id.isalnum()


def test_dashboard_views_match_tabs():
    html = _dashboard_html()
    tabs = set(re.findall(r'data-tab="([^"]+)"', html))
    views = set(re.findall(r'registerView\("([^"]+)"', html))
    # The extensibility contract: every navigation button is wired to a
    # registered view and no view is registered without a tab.
    assert tabs == views
    assert tabs == {
        "overview",
        "decisions",
        "evidence",
        "execution",
        "ops",
        "gates",
        "chains",
        "launch",
    }


def test_dashboard_core_capabilities_present():
    html = _dashboard_html()
    for marker in (
        "registerView",
        "document.createDocumentFragment",
        "Intl.DateTimeFormat",
        "RETRY_BASE_MS",
        "debounce(",
        "AbortSignal.timeout",
    ):
        assert marker in html, f"dashboard is missing core capability {marker}"
