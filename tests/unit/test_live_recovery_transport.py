"""Recovery reads use the same bounded HTTPS client as the successful retry.

All HTTP processing below is in memory; no endpoint or credential is contacted.
"""

import io
import json
import urllib.request
from urllib.error import HTTPError

import pytest

from oasis_agent.errors import RpcError
from oasis_agent.rpc import transport
from scripts import capture_live_recovery as cli
from tests.unit.test_live_recovery_capture import setup as capture_setup
from tests.unit.test_live_rpc_transport import URL, memory_https, success
from tests.unit.test_live_valuation import synthetic_feed_pin  # noqa: F401


def memory_client(monkeypatch, reply):
    calls = memory_https(monkeypatch, reply)
    # Also cover the preceding collector's imported build_opener alias, so a
    # regression run can never escape the in-memory HTTPS stack.
    if hasattr(cli, "build_opener"):
        monkeypatch.setattr(cli, "build_opener", urllib.request.build_opener)
    return calls


def test_recovery_uses_the_existing_shared_user_agent_profile(monkeypatch):
    def reply(request, _):
        if request.get_header("User-agent") != transport._USER_AGENT:
            return 403, {}, b"synthetic client-profile rejection"
        return 200, {}, success(request, "0x2105")

    calls = memory_client(monkeypatch, reply)
    raw = cli.BoundedPoster(URL, 0.25).post(
        b'{"jsonrpc":"2.0","id":1,"method":"eth_chainId","params":[]}'
    )
    assert json.loads(raw)["result"] == "0x2105"
    assert len(calls) == 1 and calls[0].get_method() == "POST" and calls[0].full_url == URL
    assert calls[0].get_header("Content-type") == "application/json"
    assert calls[0].timeout == 0.25


@pytest.mark.parametrize("status", [403, 429])
def test_http_error_body_is_closed_without_reading_or_retrying(monkeypatch, status):
    class ErrorBody(io.BytesIO):
        def read(self, *_):
            pytest.fail("provider error bodies must never be consumed")

    body = ErrorBody(b"synthetic private provider diagnostic")
    attempts = []

    class Opener:
        def open(self, request, *, timeout):
            attempts.append((request, timeout))
            raise HTTPError(URL, status, "private provider diagnostic", {}, body)

    monkeypatch.setattr(urllib.request, "build_opener", lambda *_: Opener())
    if hasattr(cli, "build_opener"):
        monkeypatch.setattr(cli, "build_opener", urllib.request.build_opener)
    with pytest.raises(HTTPError) as error:
        cli.BoundedPoster(URL, 0.25).post(b"{}")
    assert error.value.code == status and len(attempts) == 1 and body.closed


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_shared_recovery_client_never_follows_a_redirect(monkeypatch, status):
    calls = memory_client(
        monkeypatch, lambda *_: (status, {"Location": "https://foreign.invalid/"}, b"")
    )
    with pytest.raises(RpcError, match="redirects"):
        cli.BoundedPoster(URL, 0.25).post(b"{}")
    assert len(calls) == 1 and calls[0].full_url == URL


def test_shared_recovery_client_caps_the_reply_and_has_no_retry(monkeypatch):
    calls = memory_client(
        monkeypatch, lambda *_: (200, {}, b"x" * (transport.MAX_RESPONSE_BYTES + 1))
    )
    with pytest.raises(RpcError, match="byte limit"):
        cli.BoundedPoster(URL, 0.25).post(b"{}")
    assert len(calls) == 1 and cli.MAX_RESPONSE == transport.MAX_RESPONSE_BYTES


def test_offline_capture_plan_discloses_the_shared_transport_profile(tmp_path, monkeypatch):
    _, plan, *_ = capture_setup(tmp_path, monkeypatch)
    assert plan["transport_profile"] == cli.TRANSPORT_PROFILE
    assert plan["total_requests"] == 197 and plan["retries"] == 0 and plan["max_seconds"] == 120
