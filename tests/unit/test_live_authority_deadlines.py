"""Synthetic sockets and a monotonic clock exercise total protocol deadlines."""

import os
import stat
from types import SimpleNamespace

import pytest

from oasis_agent.live import authority_socket as protocol
from oasis_agent.models.common import canonical_json
from tests.unit.test_live_lp import CONFIG


class Clock:
    now = 0.0

    def monotonic(self):
        return self.now


class Connection:
    def __init__(self, clock, chunks, *, connect_delay=0, send_delay=0):
        self.clock, self.chunks = clock, iter(chunks)
        self.connect_delay, self.send_delay = connect_delay, send_delay
        self.timeout = 999
        self.timeouts, self.sent = [], []
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed = True

    def settimeout(self, timeout):
        self.timeout = timeout
        self.timeouts.append(timeout)

    def wait(self, delay):
        if delay > self.timeout:
            self.clock.now += self.timeout
            raise TimeoutError("synthetic socket timeout")
        self.clock.now += delay

    def connect(self, _):
        self.wait(self.connect_delay)

    def sendall(self, raw):
        self.wait(self.send_delay)
        self.sent.append(raw)

    def recv(self, limit):
        delay, raw = next(self.chunks)
        assert len(raw) <= limit
        self.wait(delay)
        return raw


class Endpoint:
    """Only filesystem metadata is stubbed; no socket or production key is opened."""

    def resolve(self):
        return self

    def is_absolute(self):
        return True

    @property
    def parent(self):
        return SimpleNamespace(stat=lambda: SimpleNamespace(st_uid=os.getuid(), st_mode=0o700))

    def lstat(self):
        return SimpleNamespace(st_uid=os.getuid(), st_mode=stat.S_IFSOCK | 0o600)


@pytest.mark.parametrize("case", ["drip", "stages", "at_deadline", "valid", "slow_first_reply"])
def test_client_deadline_covers_connect_send_and_whole_reply(monkeypatch, case):
    clock = Clock()
    monkeypatch.setattr(protocol.time, "monotonic", clock.monotonic)
    raw = canonical_json({"schema": protocol.SCHEMA, "status": {"pending": None}}) + b"\n"
    if case == "drip":
        connection = Connection(clock, [(0.4, raw[:1]), (0.4, raw[1:2]), (0.4, raw[2:])])
    elif case == "stages":
        connection = Connection(clock, [(0.4, raw)], connect_delay=0.4, send_delay=0.4)
    elif case == "at_deadline":
        connection = Connection(clock, [(1, raw)])
    else:
        connection = Connection(clock, [(6 if case == "slow_first_reply" else 0.8, raw)])
    monkeypatch.setattr(protocol.socket, "socket", lambda *_: connection)
    timeout = 10 if case == "slow_first_reply" else 1
    client = protocol.TransactionSocketClient(Endpoint(), CONFIG.account, timeout=timeout)
    if case in {"drip", "stages", "at_deadline"}:
        with pytest.raises(TimeoutError):
            client.pending_status()
        assert clock.now <= timeout
    else:
        assert client.pending_status() == {"pending": None}
    assert connection.closed
    assert len(connection.sent) == 1  # No reconnect/retry after a partial reply.


@pytest.mark.parametrize("operation", ["status", "sign"])
@pytest.mark.parametrize("slow", [False, True])
def test_authority_does_not_dispatch_a_frame_after_service_deadline(monkeypatch, operation, slow):
    clock = Clock()
    monkeypatch.setattr(protocol.time, "monotonic", clock.monotonic)
    request = {"schema": protocol.SCHEMA, "account": CONFIG.account}
    if operation == "status":
        request["operation"] = "status"
    else:
        request.update(fields=[8453, 0, 1, 1, 21000, CONFIG.account, 0, "0x", []], context={})
    raw = canonical_json(request) + b"\n"
    delay = 0.4 if slow else 0.1
    connection = Connection(clock, [(delay, raw[:1]), (delay, raw[1:2]), (delay, raw[2:])])
    dispatched = []

    class Server:
        accepted = False

        def settimeout(self, _):
            pass

        def accept(self):
            if self.accepted:
                clock.now = 2
                raise TimeoutError
            self.accepted = True
            return connection, None

    class Authority:
        def status(self):
            dispatched.append("status")
            return {}

        def sign(self, *_):
            dispatched.append("sign")
            return b"synthetic nonbroadcastable result"

    count = protocol.serve_connections(
        Server(), Authority(), account=CONFIG.account, max_signatures=1, ttl_seconds=1
    )
    assert dispatched == ([] if slow else [operation])
    assert count == (1 if not slow and operation == "sign" else 0)
    assert connection.closed


def test_last_signature_still_allows_settlement_but_refuses_another_signature(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(protocol.time, "monotonic", clock.monotonic)
    proposal = canonical_json(
        {
            "schema": protocol.SCHEMA,
            "account": CONFIG.account,
            "fields": [8453, 0, 1, 1, 21000, CONFIG.account, 0, "0x", []],
            "context": {},
        }
    )
    tx_hash = "0x" + "12" * 32
    settle = canonical_json(
        {
            "schema": protocol.SCHEMA,
            "account": CONFIG.account,
            "operation": "settle",
            "transaction_hash": tx_hash,
        }
    )
    connections = [
        Connection(clock, [(0, frame + b"\n")]) for frame in (proposal, settle, proposal)
    ]
    waiting = iter(connections)
    calls = []

    def accept():
        connection = next(waiting, None)
        if connection is None:
            clock.now = 2
            raise TimeoutError
        return connection, None

    def settle_receipt(digest):
        calls.append(digest)
        return "a" * 64

    def sign(*_):
        calls.append("sign")
        return b"fixture"

    count = protocol.serve_connections(
        SimpleNamespace(settimeout=lambda _: None, accept=accept),
        SimpleNamespace(sign=sign),
        account=CONFIG.account,
        max_signatures=1,
        ttl_seconds=1,
        settle=settle_receipt,
    )
    assert count == 1 and calls == ["sign", tx_hash]
    assert b'"settlement_digest"' in connections[1].sent[0]
    assert b'"refused":true' in connections[2].sent[0]


@pytest.mark.parametrize(
    "timeout", [0, -1, True, float("nan"), float("inf"), 241, 10**999, "1", None]
)
def test_client_rejects_unbounded_or_invalid_timeouts(timeout):
    with pytest.raises(ValueError, match="timeout"):
        protocol.TransactionSocketClient(Endpoint(), CONFIG.account, timeout=timeout)


@pytest.mark.parametrize("case", ["slow_status", "slow_lookup", "slow_error", "late_send", "valid"])
def test_keyless_status_does_not_send_after_read_or_delivery_deadline(monkeypatch, case):
    clock = Clock()
    monkeypatch.setattr(protocol.time, "monotonic", clock.monotonic)
    request = {"schema": protocol.SCHEMA, "account": CONFIG.account, "operation": "status"}
    if case == "slow_lookup":
        request["nonce"] = 7
    connection = Connection(
        clock,
        [(0.1, canonical_json(request) + b"\n")],
        send_delay=0.3 if case == "late_send" else 0,
    )
    calls = []

    class Server:
        def settimeout(self, _):
            pass

        def accept(self):
            return connection, None

    def status(nonce=None):
        calls.append(nonce)
        clock.now += 1.1 if case.startswith("slow_") else 0.7
        if case == "slow_error":
            raise ValueError("synthetic journal failure")
        return {"pending": None, "signing_unresolved": False}

    count = protocol.serve_status_connections(
        Server(), status, account=CONFIG.account, max_requests=1, ttl_seconds=1, lookup=status
    )
    assert count == 1 and calls == ([7] if case == "slow_lookup" else [None])
    assert connection.closed
    if case == "valid":
        assert connection.sent == [
            canonical_json(
                {
                    "schema": protocol.SCHEMA,
                    "status": {"pending": None, "signing_unresolved": False},
                }
            )
            + b"\n"
        ]
    else:
        assert connection.sent == []  # Neither a late success nor a late refusal is delivered.
        if case == "late_send":
            assert clock.now == pytest.approx(1.0)
