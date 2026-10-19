"""Status service cannot dispatch signing and bounds every accepted connection."""

import json
import socket
import tempfile
import threading
from pathlib import Path

import pytest

from oasis_agent.execution.lp_signer_daemon import prepare_endpoint
from oasis_agent.live import authority_socket
from oasis_agent.live.authority_socket import SCHEMA, read_frame, serve_status_connections
from oasis_agent.models.common import canonical_json
from tests.unit.test_live_lp import CONFIG


@pytest.mark.parametrize("fault", ["", "sign", "foreign", "noncanonical", "oversized"])
def test_status_protocol_has_no_signing_capability_and_counts_refusals(fault):
    requests, reads = [], []
    public = {"account": CONFIG.account, "pending": None, "signing_unresolved": False}

    def status():
        reads.append(1)
        return {"padding": "x" * 33_000} if fault == "oversized" else public

    with tempfile.TemporaryDirectory(prefix="oa-status-", dir=Path("/tmp").resolve()) as name:
        root = Path(name)
        root.chmod(0o700)
        endpoint = root / "status.sock"
        with prepare_endpoint(endpoint) as server:
            thread = threading.Thread(
                target=lambda: requests.append(
                    serve_status_connections(
                        server,
                        status,
                        account=CONFIG.account,
                        max_requests=1,
                        ttl_seconds=5,
                    )
                )
            )
            thread.start()
            try:
                request = {"schema": SCHEMA, "account": CONFIG.account, "operation": "status"}
                if fault == "foreign":
                    request["account"] = "0x" + "11" * 20
                elif fault == "sign":
                    request = {
                        "schema": SCHEMA,
                        "account": CONFIG.account,
                        "fields": [8453, 0, 1, 1, 21000, CONFIG.account, 0, "0x", []],
                        "context": {},
                    }
                raw = canonical_json(request)
                if fault == "noncanonical":
                    raw = b" " + raw
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.settimeout(5)
                    client.connect(str(endpoint))
                    client.sendall(raw + b"\n")
                    reply = json.loads(read_frame(client))
                assert reply == (
                    {"schema": SCHEMA, "refused": True}
                    if fault
                    else {"schema": SCHEMA, "status": public}
                )
            finally:
                thread.join(timeout=6)
            assert not thread.is_alive() and requests == [1]
            assert len(reads) == (1 if fault in {"", "oversized"} else 0)


@pytest.mark.parametrize("count,ttl", [(0, 1), (True, 1), (1001, 1), (1, 0), (1, True), (1, 86401)])
def test_status_limits_refuse_before_socket_access(count, ttl):
    with pytest.raises(ValueError, match="budget"):
        serve_status_connections(
            None, lambda: {}, account=CONFIG.account, max_requests=count, ttl_seconds=ttl
        )


def test_partial_frame_cannot_extend_status_deadline(monkeypatch):
    times = iter([0.2, 0.7, 1.1])
    monkeypatch.setattr(authority_socket.time, "monotonic", lambda: next(times))
    timeouts, reads = [], []

    class DrippingClient:
        def settimeout(self, value):
            timeouts.append(value)

        def recv(self, limit):
            reads.append(limit)
            return b"x"

    with pytest.raises(TimeoutError, match="deadline"):
        read_frame(DrippingClient(), deadline=1.0)
    assert len(reads) == 2 and timeouts == pytest.approx([0.8, 0.3])
