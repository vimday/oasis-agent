"""Actual urllib read/error/redirect processing with only in-memory responses."""

import io
import json
import urllib.request
from email.message import Message
from urllib.response import addinfourl

import pytest

from oasis_agent.errors import RpcError
from oasis_agent.live.budget_rpc import BudgetedRpc
from oasis_agent.live.chain import ChainError
from oasis_agent.live.rpc_profile import LiveRpcProfile
from oasis_agent.rpc.budget import RpcBudget
from oasis_agent.rpc.transport import JsonRpcTransport

URL = "https://fixture.invalid/?key=synthetic-credential"
LIMIT = 1_000_000
ANSWER = b'{"jsonrpc":"2.0","id":1,"result":"0x1"}'


def memory_https(monkeypatch, body, *, status=200, headers=None):
    calls, streams = [], []
    build = urllib.request.build_opener

    class Body(io.BytesIO):
        def __init__(self):
            super().__init__(body)
            self.read_sizes = []

        def read(self, size=-1):
            self.read_sizes.append(size)
            return super().read(size)

    class Wire(urllib.request.BaseHandler):
        handler_order = 0

        def https_open(self, request):
            calls.append(request)
            stream = Body()
            streams.append(stream)
            message = Message()
            for name, value in (headers or {}).items():
                message[name] = value
            response = addinfourl(stream, message, request.full_url, status)
            response.msg = "synthetic-credential"
            return response

    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: build(Wire(), *handlers))
    return calls, streams


@pytest.mark.parametrize("extra", [1, 500_000])
@pytest.mark.parametrize("headers", [{}, {"Content-Length": "1"}])
def test_oversized_read_is_terminal_bounded_and_closed(monkeypatch, extra, headers):
    # Leading whitespace keeps this valid JSON. The limit, not the decoder, must refuse it.
    body = b" " * (LIMIT + extra - len(ANSWER)) + ANSWER
    calls, streams = memory_https(monkeypatch, body, headers=headers)
    captured = []

    class Recorder:
        def record(self, exchange):
            captured.append(exchange)

    client = JsonRpcTransport(
        URL, retries=2, backoff_seconds=0, jitter_seconds=0, recorder=Recorder()
    )
    with pytest.raises(RpcError, match="response exceeds byte limit") as error:
        client.request("eth_chainId", [])
    assert "credential" not in str(error.value)
    assert len(calls) == 1 and streams[0].closed
    assert streams[0].read_sizes == [LIMIT + 1]
    assert not captured


@pytest.mark.parametrize("size", [len(ANSWER), LIMIT - 1, LIMIT])
def test_success_below_or_exactly_at_limit_reads_exact_bytes_and_closes(monkeypatch, size):
    body = b" " * (size - len(ANSWER)) + ANSWER
    calls, streams = memory_https(monkeypatch, body)
    captured = []

    class Recorder:
        def record(self, exchange):
            captured.append(exchange)

    assert JsonRpcTransport(URL, recorder=Recorder()).request("eth_chainId", []) == "0x1"
    assert captured[0].response_payload == body
    assert len(calls) == 1 and streams[0].closed
    assert streams[0].read_sizes == [LIMIT + 1]


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_forbidden_redirect_closes_body_without_reading_or_following(monkeypatch, status):
    calls, streams = memory_https(
        monkeypatch,
        b"synthetic-credential" * 100,
        status=status,
        headers={"Location": "https://other.invalid/"},
    )
    with pytest.raises(RpcError, match="redirects are forbidden") as error:
        JsonRpcTransport(URL, retries=2).request("eth_chainId", [])
    # Keep the exception/traceback alive, as BudgetedRpc does when translating
    # failures. Garbage collection must not be what closes the response.
    assert "credential" not in str(error.value)
    assert len(calls) == 1 and streams[0].closed
    assert streams[0].read_sizes == []


@pytest.mark.parametrize("status", [400, 403, 429, 500, 503])
def test_http_failures_close_without_reading_private_body(monkeypatch, status):
    calls, streams = memory_https(monkeypatch, URL.encode() * 100, status=status)
    with pytest.raises(RpcError, match="details redacted") as error:
        JsonRpcTransport(URL, retries=0).request("eth_chainId", [])
    assert "credential" not in str(error.value)
    assert len(calls) == 1 and streams[0].closed
    assert streams[0].read_sizes == []


def test_live_oversized_response_spends_once_and_cannot_renew_after_restart(monkeypatch, tmp_path):
    calls, streams = memory_https(monkeypatch, b" " * (LIMIT + 1) + ANSWER)
    profile = LiveRpcProfile(max_requests=1)
    policies = profile.policies(("live_primary", "live_secondary"))
    path = tmp_path / "live.receipt-budget.db"
    with RpcBudget(path, policies):
        pass
    for _ in range(2):
        client = BudgetedRpc(URL, tmp_path, "live_primary", profile, require_existing_budget=True)
        with pytest.raises(ChainError, match="details redacted"):
            client.call("eth_chainId", [])
    assert len(calls) == 1 and streams[0].closed
    assert streams[0].read_sizes == [LIMIT + 1]
    with RpcBudget(path, policies, require_existing=True) as budget:
        assert budget.counts() == {"live_primary": 1, "live_secondary": 0}


def test_http_failure_retries_remain_bounded_and_close_each_response(monkeypatch):
    calls, streams = memory_https(monkeypatch, URL.encode(), status=503)
    client = JsonRpcTransport(URL, retries=2, backoff_seconds=0, jitter_seconds=0)
    with pytest.raises(RpcError, match="details redacted"):
        client.request("eth_chainId", [])
    assert len(calls) == 3
    assert all(stream.closed and not stream.read_sizes for stream in streams)
    assert [json.loads(request.data)["id"] for request in calls] == [1, 2, 3]


def test_live_http_refusal_identifies_provider_method_and_status_without_a_second_read(
    monkeypatch, tmp_path
):
    calls, streams = memory_https(monkeypatch, URL.encode(), status=429)
    profile = LiveRpcProfile(max_requests=1)
    policies = profile.policies(("live_primary", "live_secondary"))
    path = tmp_path / "live.receipt-budget.db"
    with RpcBudget(path, policies):
        pass
    client = BudgetedRpc(URL, tmp_path, "live_secondary", profile, require_existing_budget=True)
    with pytest.raises(ChainError, match="live_secondary eth_getBlockByNumber http_429") as error:
        client.block()
    assert URL not in str(error.value)
    assert len(calls) == 1 and streams[0].closed and not streams[0].read_sizes
    with RpcBudget(path, policies, require_existing=True) as budget:
        assert budget.counts() == {"live_primary": 0, "live_secondary": 1}
