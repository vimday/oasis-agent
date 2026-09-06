import traceback
import urllib.error

import pytest

from oasis_agent.errors import RpcError
from oasis_agent.rpc.transport import JsonRpcTransport


@pytest.mark.parametrize(
    "error",
    [
        OSError("credential-marker"),
        urllib.error.URLError("credential-marker"),
        TimeoutError("credential-marker"),
    ],
)
def test_network_failures_are_redacted_and_retried(error):
    class Broken:
        calls = 0

        def post(self, payload):
            self.calls += 1
            raise error

    http = Broken()
    transport = JsonRpcTransport(
        "https://example.invalid", http=http, retries=1, backoff_seconds=0, jitter_seconds=0
    )
    with pytest.raises(RpcError) as exc:
        transport.request("eth_chainId", [])
    assert http.calls == 2
    assert "credential-marker" not in "".join(traceback.format_exception(exc.value))


def test_network_failure_never_retries_non_idempotent_method():
    class Broken:
        calls = 0

        def post(self, payload):
            self.calls += 1
            raise OSError("redacted")

    http = Broken()
    with pytest.raises(RpcError):
        JsonRpcTransport("https://example.invalid", http=http).request("unknown_write", [])
    assert http.calls == 1
