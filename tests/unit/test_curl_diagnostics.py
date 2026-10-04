"""Reproduce lost curl failure provenance through the real scoped budget path."""

import json
import subprocess
from dataclasses import asdict

import pytest

from oasis_agent.errors import RpcError
from oasis_agent.evidence.rpc_capture import BoundedExchangeRecorder
from oasis_agent.farming.router_activity import collect_router_activity
from oasis_agent.models.common import canonical_json, sha256_hex
from oasis_agent.rpc.budget import BudgetedPoster, BudgetPolicy, RpcBudget
from oasis_agent.rpc.curl_diagnostics import (
    ClassifiedCurlError,
    ClassifiedCurlPoster,
    classify_curl_failure,
)
from oasis_agent.rpc.transport import JsonRpcTransport
from scripts.capture_router_activity import Scope
from scripts.router_transport_diagnostics import DiagnosticRouterPoster
from scripts.run_v4_route_probe import CurlPoster
from tests.unit.test_router_activity import plan


@pytest.mark.parametrize(
    "code,category",
    [
        (5, "proxy_resolution_failed"),
        (6, "host_resolution_failed"),
        (7, "connection_failed"),
        (22, "http_error_status_unspecified"),
        (28, "timeout"),
        (35, "tls_handshake_failed"),
        (52, "empty_reply"),
        (55, "send_failed"),
        (56, "receive_failed"),
        (60, "tls_certificate_verification_failed"),
        (-9, "process_terminated"),
        (99, "other_curl_failure"),
    ],
)
def test_real_shared_adapter_sanitization_preserves_only_bounded_fields(
    monkeypatch, code, category
):
    def failed(command, **kwargs):
        raise subprocess.CalledProcessError(
            code, command, output=b"sensitive-upstream-marker", stderr=b"sensitive-upstream-marker"
        )

    monkeypatch.setattr(subprocess, "run", failed)
    wrapped = ClassifiedCurlPoster(CurlPoster("https://a.example"))
    with pytest.raises(ClassifiedCurlError) as result:
        wrapped.post(b"request")
    assert result.value.failure.category == category and result.value.failure.exit_code == code
    assert "sensitive" not in str(result.value) and "a.example" not in str(result.value)
    assert result.value.__cause__ is None and result.value.__suppress_context__


@pytest.mark.parametrize(
    "message",
    [
        "RPC HTTPS request failed",  # legacy timeout cannot be distinguished retrospectively
        "RPC HTTPS request failed (exit 0)",
        "RPC HTTPS request failed (exit 999)",
        "RPC HTTPS request failed (exit -129)",
        "RPC HTTPS request failed (exit 6) sensitive-upstream-marker",
    ],
)
def test_arbitrary_or_ambiguous_error_text_never_becomes_diagnostic(message):
    failure = classify_curl_failure(OSError(message))
    assert failure.category == "unclassified_transport_failure" and failure.exit_code is None


def test_no_stringification_of_foreign_error_and_success_bytes_unchanged(monkeypatch):
    class ForeignError(OSError):
        def __str__(self):
            raise AssertionError("do not stringify upstream errors")

    assert classify_curl_failure(ForeignError("sensitive-upstream-marker")).exit_code is None
    response = b'{"jsonrpc":"2.0","id":1,"result":"0x2105"}'
    calls = []

    def success(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout=response)

    monkeypatch.setattr(subprocess, "run", success)
    payload = b"original request"
    assert ClassifiedCurlPoster(CurlPoster("https://a.example")).post(payload) == response
    assert len(calls) == 1 and calls[0][1]["input"] is payload
    assert calls[0][1]["timeout"] == 25 and calls[0][1]["check"] is True
    assert "--retry" not in calls[0][0] and "--insecure" not in calls[0][0]


@pytest.mark.parametrize("code", [6, 22, 60])
def test_scoped_collect_failure_charged_once_and_journaled_before_rpc_redaction(
    tmp_path, monkeypatch, code
):
    root = tmp_path / "capture"
    root.mkdir()
    raw = canonical_json(
        {
            "schema": "oasis.router-activity-scope.v1",
            "purpose": "complete_outer_router_receipts_at_quote_block",
            "output_directory": str(root),
            "plan": asdict(plan()),
            "sources": [
                {"alias": "rpc_a", "env": "RPC_A", "endpoint": "https://a.example"},
                {"alias": "rpc_b", "env": "RPC_B", "endpoint": "https://b.example"},
            ],
            "max_requests_per_source": 20,
            "min_interval_ms": 3000,
            "automatic_retries": 0,
        }
    )
    config = root / "config.json"
    config.write_bytes(raw)
    scope = Scope.from_json(raw)
    calls = []

    def failed(command, **kwargs):
        calls.append(command)
        raise subprocess.CalledProcessError(code, command, stderr=b"sensitive-upstream-marker")

    monkeypatch.setattr(subprocess, "run", failed)
    adapter = DiagnosticRouterPoster(scope.sources[0], scope, config, raw, root)
    policies = tuple(BudgetPolicy(s.alias, 20, 3000) for s in scope.sources)
    poster = BudgetedPoster(adapter, root / "budget.db", policies, "rpc_a")
    recorder = BoundedExchangeRecorder()
    transport = JsonRpcTransport(
        scope.sources[0].endpoint, http=poster, retries=0, recorder=recorder
    )
    with pytest.raises(RpcError, match="details redacted"):
        collect_router_activity((transport, recorder, "rpc_a"), scope.plan)
    with RpcBudget(root / "budget.db", policies) as budget:
        assert budget.counts() == {"rpc_a": 1, "rpc_b": 0}
    assert len(calls) == 1
    wire = (root / "rpc_a-attempts.jsonl").read_bytes()
    diagnostic = (root / "rpc_a-transport-diagnostics.jsonl").read_bytes()
    assert len(wire.splitlines()) == len(diagnostic.splitlines()) == 1
    attempt, retained = json.loads(wire), json.loads(diagnostic)
    assert (
        attempt["response"] is None and attempt["transport_status"] == "transport_failure_redacted"
    )
    assert retained["request_sha256"] == sha256_hex(bytes.fromhex(attempt["request"]))
    assert retained["scope_sha256"] == sha256_hex(raw) and retained["curl_exit_code"] == code
    assert b"sensitive" not in wire + diagnostic and b"a.example" not in diagnostic
    assert not (root / "input.json").exists() and not (root / "report.json").exists()
    # The original scope validator is still in the path; this wrapper cannot send.
    with pytest.raises(ValueError, match="outside frozen scope"):
        adapter.post(canonical_json({"method": "eth_sendRawTransaction", "params": ["0x00"]}))
    assert len(calls) == 1 and (root / "rpc_a-attempts.jsonl").read_bytes() == wire
