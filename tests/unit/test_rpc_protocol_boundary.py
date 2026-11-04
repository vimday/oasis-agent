"""Hostile responses cannot enter live reads or renew their persistent quota.

All HTTP responses and ledgers here are synthetic; no endpoint is contacted.
"""

import json

import pytest

from oasis_agent.errors import RpcError
from oasis_agent.evidence.rpc_validation import decode_exchange
from oasis_agent.live import budget_rpc
from oasis_agent.live.chain import ChainError
from oasis_agent.live.rpc_profile import LiveRpcProfile
from oasis_agent.rpc.budget import RpcBudget
from oasis_agent.rpc.transport import JsonRpcTransport, RpcExchange

BAD_RESPONSES = [
    b'{"jsonrpc":"2.0","id":true,"result":"0x1"}',
    b'{"jsonrpc":"2.0","id":1.0,"result":"0x1"}',
    b'{"jsonrpc":"2.0","id":99,"id":1,"result":"0x1"}',
    b'{"jsonrpc":"2.0","id":1,"result":"0x2","result":"0x1"}',
    b'{"jsonrpc":"1.0","jsonrpc":"2.0","id":1,"result":"0x1"}',
    b'{"jsonrpc":"2.0","id":1,"result":{"nonce":"0x2","nonce":"0x1"}}',
    b'{"jsonrpc":"2.0","id":1,"error":null,"result":"0x1"}',
    b'{"jsonrpc":"2.0","id":1,"error":{"code":-1,"message":"denied"},"result":"0x1"}',
    b'{"jsonrpc":"2.0","id":1,"result":NaN}',
    b'{"jsonrpc":"2.0","id":1,"result":{"fee":Infinity}}',
    b'{"jsonrpc":"2.0","id":1,"result":1e999}',
    b'{"jsonrpc":"2.0","id":2,"result":"0x1"}',
    b'{"jsonrpc":"2.0","id":"1","result":"0x1"}',
    b'{"jsonrpc":"2.0","id":1,"error":{"code":true,"message":"private diagnostic"}}',
    b'{"jsonrpc":"2.0","id":1,"error":{"code":-32603.0,"message":"private diagnostic"}}',
    b'{"jsonrpc":"2.0","id":1,"error":{"code":-1,"message":null}}',
    b'{"jsonrpc":"2.0","id":1}',
    b'[{"jsonrpc":"2.0","id":1,"result":"0x1"}]',
    b'{"jsonrpc":"2.0","id":1,"result":"private diagnostic"',
    b'{"jsonrpc":"2.0","id":1,"result":"\xff"}',
]


class RawHttp:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, payload):
        self.calls.append(payload)
        return self.response


class Recorder:
    def __init__(self):
        self.exchanges = []

    def record(self, exchange):
        self.exchanges.append(exchange)


@pytest.mark.parametrize("response", BAD_RESPONSES)
def test_live_read_rejects_hostile_envelope_without_refund_or_restart_retry(
    tmp_path, monkeypatch, response
):
    http = RawHttp(response)
    monkeypatch.setattr(budget_rpc, "_UrlLibPoster", lambda *_: http)
    profile = LiveRpcProfile(max_requests=1)
    policies = profile.policies(("live_primary", "live_secondary"))
    path = tmp_path / "live.receipt-budget.db"
    with RpcBudget(path, policies):
        pass
    for _ in range(2):
        client = budget_rpc.BudgetedRpc(
            "https://unused.invalid",
            tmp_path,
            "live_primary",
            profile,
            require_existing_budget=True,
        )
        with pytest.raises(ChainError, match="details redacted") as error:
            client.call("eth_chainId", [])
        assert "private diagnostic" not in str(error.value)
        assert error.value.__suppress_context__
    assert len(http.calls) == 1
    with RpcBudget(path, policies, require_existing=True) as budget:
        assert budget.counts() == {"live_primary": 1, "live_secondary": 0}


@pytest.mark.parametrize("response", BAD_RESPONSES)
def test_malformed_response_is_terminal_even_when_read_retries_are_configured(response):
    http, recorder = RawHttp(response), Recorder()
    client = JsonRpcTransport(
        "https://unused.invalid",
        http=http,
        recorder=recorder,
        retries=2,
        backoff_seconds=0,
        jitter_seconds=0,
    )
    with pytest.raises(RpcError, match="invalid JSON-RPC response"):
        client.request("eth_chainId", [])
    assert len(http.calls) == 1
    # Retain the rejected wire bytes as evidence, never a permissive interpretation.
    assert recorder.exchanges[0].response_payload == response
    with pytest.raises((ValueError, TypeError)):
        decode_exchange(recorder.exchanges[0])


@pytest.mark.parametrize("value", [None, False, 0, "0x0", [], {}, 1.25])
def test_valid_falsy_and_finite_results_preserve_wire_bytes_and_replay(value):
    response = json.dumps({"jsonrpc": "2.0", "id": 1, "result": value}, indent=2).encode()
    http, recorder = RawHttp(response), Recorder()
    client = JsonRpcTransport("https://unused.invalid", http=http, recorder=recorder)
    assert client.request("eth_chainId", []) == value
    params, decoded = decode_exchange(recorder.exchanges[0])
    assert params == [] and decoded["result"] == value
    assert recorder.exchanges[0].response_payload == response
    assert len(http.calls) == 1


def test_valid_retryable_error_keeps_explicit_bounded_retry_and_both_raw_exchanges():
    recorder = Recorder()

    class TransientHttp:
        def post(self, payload):
            request_id = json.loads(payload)["id"]
            envelope = {"jsonrpc": "2.0", "id": request_id}
            if request_id == 1:
                envelope["error"] = {"code": -32603, "message": "transient"}
            else:
                envelope["result"] = "0x1"
            return json.dumps(envelope).encode()

    client = JsonRpcTransport(
        "https://unused.invalid",
        http=TransientHttp(),
        recorder=recorder,
        retries=1,
        backoff_seconds=0,
        jitter_seconds=0,
    )
    assert client.request("eth_chainId", []) == "0x1"
    assert len(recorder.exchanges) == 2
    for exchange in recorder.exchanges:
        decode_exchange(exchange)
    assert [exchange.attempt for exchange in recorder.exchanges] == [1, 2]


def test_offline_exchange_refuses_boolean_metadata_even_with_valid_envelope():
    request = b'{"jsonrpc":"2.0","id":1,"method":"eth_chainId","params":[]}'
    response = b'{"jsonrpc":"2.0","id":1,"result":"0x1"}'
    with pytest.raises(ValueError):
        decode_exchange(RpcExchange("eth_chainId", True, request, response, 1))
