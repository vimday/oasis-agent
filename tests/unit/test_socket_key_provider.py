"""External-provider protocol and policy integration; fixture keys only."""

import json
import socket
import tempfile
import threading
from pathlib import Path

import pytest

from oasis_agent.execution import socket_key_provider as transport
from oasis_agent.execution.socket_key_provider import SCHEMA, SocketKeyProvider
from oasis_agent.models.common import canonical_json
from tests.unit.test_lp_signer_sidecar import (
    ACCOUNT,
    KEY,
    FixtureKeyProvider,
    make_request,
    make_sidecar,
)


def response(request, **changes):
    data = json.loads(request)
    r, s, parity = FixtureKeyProvider(KEY).sign(bytes.fromhex(data["digest"][2:]))
    return canonical_json(
        {**data, "r": f"0x{r:064x}", "s": f"0x{s:064x}", "y_parity": parity, **changes}
    )


def test_real_private_socket_signs_exact_policy_envelope(tmp_path):
    with tempfile.TemporaryDirectory(prefix="oasis-sig-", dir=Path("/tmp").resolve()) as directory:
        path = Path(directory) / "authority.sock"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(path))
            path.chmod(0o600)
            server.listen(1)
            server.settimeout(3)
            received = []

            def authority():
                with server.accept()[0] as client:
                    client.settimeout(3)
                    request = bytearray()
                    while not request.endswith(b"\n"):
                        request.extend(client.recv(512))
                    received.append(json.loads(request))
                    client.sendall(response(request) + b"\n")

            worker = threading.Thread(target=authority)
            worker.start()
            provider = SocketKeyProvider(path, ACCOUNT)
            try:
                with make_sidecar(tmp_path, ACCOUNT, provider=provider) as sidecar:
                    result = sidecar.handle(make_request(ACCOUNT))
                    assert result.outcome == "signed"
                    assert result.digest == received[0]["digest"]
                    assert received[0]["schema"] == SCHEMA
            finally:
                worker.join(4)
            assert not worker.is_alive()


@pytest.mark.parametrize(
    "changes",
    [
        {"account": "0x" + "11" * 20},
        {"digest": "0x" + "00" * 32},
        {"y_parity": True},
        {"r": "0x0"},
        {"s": "0x" + "ff" * 32},
        {"extra": "field"},
    ],
)
def test_malformed_signature_or_binding_is_rejected(monkeypatch, changes):
    provider = SocketKeyProvider(Path("/private/tmp/unused.sock"), ACCOUNT)
    monkeypatch.setattr(provider, "_exchange", lambda request: response(request, **changes))
    with pytest.raises(RuntimeError, match="authority failed"):
        provider.sign(b"a" * 32)


def test_transport_failure_cannot_leak_details_or_retry(tmp_path, monkeypatch):
    provider = SocketKeyProvider(Path("/private/tmp/unused.sock"), ACCOUNT)
    calls = []

    def failure(request):
        calls.append(request)
        raise OSError("sensitive-backend-detail")

    monkeypatch.setattr(provider, "_exchange", failure)
    with make_sidecar(tmp_path, ACCOUNT, provider=provider) as sidecar:
        result = sidecar.handle(make_request(ACCOUNT))
        assert result.outcome == "denied"
        assert "sensitive-backend-detail" not in str(result)
        assert sidecar.handle(make_request(ACCOUNT, nonce=92)).outcome == "denied"
    assert len(calls) == 1


def test_socket_permissions_and_digest_width_fail_closed(tmp_path):
    path = tmp_path / "not-a-socket"
    path.write_text("not a signing endpoint")
    with pytest.raises(ValueError):
        SocketKeyProvider(Path("relative.sock"), ACCOUNT)
    with tempfile.TemporaryDirectory(prefix="oasis-sig-", dir=Path("/tmp").resolve()) as directory:
        endpoint = Path(directory) / "plain"
        endpoint.write_text("not a socket")
        endpoint.chmod(0o600)
        provider = SocketKeyProvider(endpoint, ACCOUNT)
        with pytest.raises(RuntimeError):
            provider.sign(b"a" * 32)
        with pytest.raises(ValueError, match="32-byte"):
            provider.sign(b"a")


@pytest.mark.parametrize(
    "chunks",
    [
        [b"x" * 512] * 4 + [b"x"],
        [b"{}\nextra"],
        [b"{}\n\n"],
        [b""],
    ],
)
def test_transport_framing_is_bounded_and_never_retried(monkeypatch, chunks):
    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def connect(self, path):
            pass

        def settimeout(self, timeout):
            assert 0 < timeout <= 3

        def sendall(self, payload):
            assert payload.endswith(b"\n")

        def recv(self, count):
            assert count <= 512
            return chunks.pop(0)

    connects = []

    def connect(*args):
        connects.append(1)
        return Connection()

    provider = SocketKeyProvider(Path("/private/tmp/unused.sock"), ACCOUNT)
    monkeypatch.setattr(provider, "_identity", lambda: (1, 2))
    monkeypatch.setattr(transport.socket, "socket", connect)
    with pytest.raises(RuntimeError):
        provider.sign(b"a" * 32)
    assert connects == [1]
