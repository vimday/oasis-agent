import json
import time
from queue import Empty, Queue
from threading import Event

import pytest
from eth_hash.auto import keccak
from rlp import encode

from oasis_agent.evidence.eth_child_witness import (
    CHAIN_REQUEST,
    SUB_REQUEST,
    EthereumChildSession,
    WitnessHead,
    inspect_witness,
)
from oasis_agent.evidence.state_trie import PRAGUE_FIELDS
from oasis_agent.models.common import canonical_json

SCOPE = "f" * 64
TOKEN = "0xab"


def header(height: int, parent: str) -> dict:
    fields = {
        name: (
            hex(height)
            if name == "number"
            else hex(int(time.time()))
            if name == "timestamp"
            else "0x0"
        )
        if width < 0
        else parent
        if name == "parentHash"
        else "0x" + "00" * width
        for name, width in PRAGUE_FIELDS
    }
    parts = [
        int(fields[name], 16) if width < 0 else bytes.fromhex(fields[name][2:])
        for name, width in PRAGUE_FIELDS
    ]
    return {**fields, "hash": "0x" + keccak(encode(parts)).hex()}


def notification(head: dict) -> bytes:
    return canonical_json(
        {
            "jsonrpc": "2.0",
            "method": "eth_subscription",
            "params": {"subscription": TOKEN, "result": head},
        }
    )


def binding(raw: dict) -> WitnessHead:
    return WitnessHead(int(raw["number"], 16), raw["hash"], raw["parentHash"])


class Socket:
    def __init__(self) -> None:
        self.frames: Queue[bytes | OSError] = Queue()
        self.sent: list[bytes] = []

    def __enter__(self) -> Socket:
        return self

    def __exit__(self, *_: object) -> None:
        pass

    def send(self, message: bytes, *, text: bool) -> None:
        assert text
        self.sent.append(message)
        index = len(self.sent)
        self.frames.put(
            canonical_json(
                {"jsonrpc": "2.0", "id": index, "result": "0x1" if index == 1 else TOKEN}
            )
        )

    def recv(self, *, timeout: float, decode: bool) -> bytes:
        assert not decode
        try:
            row = self.frames.get(timeout=timeout)
        except Empty:
            raise TimeoutError from None
        if isinstance(row, OSError):
            raise row
        return row


def test_independent_child_reuses_first_identical_h_and_verifies_raw_prefix(tmp_path):
    directory = tmp_path / "head_feed"
    transport = Socket()
    session = EthereumChildSession(directory, SCOPE, Event(), socket_factory=lambda: transport)
    session.start()
    query_ns = time.monotonic_ns()
    h = header(10, "0x" + "12" * 32)
    c = header(11, h["hash"])
    for head in (h, h, c):
        transport.frames.put(notification(head))
    session.close(binding(c))
    assert transport.sent == [CHAIN_REQUEST, SUB_REQUEST]

    params = {"discovery": binding(h), "discovery_request_ns": query_ns, "child": binding(c)}
    witness, terminal = inspect_witness(directory, SCOPE, **params)
    first = json.loads((directory / "head-01.json").read_bytes())
    duplicate = json.loads((directory / "head-02.json").read_bytes())
    assert witness.status == "verified" and terminal["frames"] == 3
    assert witness.discovery_monotonic_ns == first["monotonic_ns"] < duplicate["monotonic_ns"]
    assert witness.child_monotonic_ns > witness.discovery_monotonic_ns

    late, _ = inspect_witness(directory, SCOPE, **(params | {"discovery_request_ns": 1}))
    assert (
        late.child_monotonic_ns is None and late.status == "subscription_not_ready_before_h_request"
    )
    wrong = binding(header(11, "0x" + "34" * 32))
    mismatch, _ = inspect_witness(directory, SCOPE, **(params | {"child": wrong}))
    assert mismatch.child_monotonic_ns is None and mismatch.status == "h_or_c_diverges_from_http"
    (directory / "head-02.raw").write_bytes(b"{}")
    with pytest.raises(ValueError, match="raw frame identity"):
        inspect_witness(directory, SCOPE, **params)


@pytest.mark.parametrize(
    ("fault", "reason"),
    [
        ("gap", "Ethereum newHeads height or parent continuity failed"),
        ("same_height", "Ethereum newHeads height or parent continuity failed"),
        ("wrong_parent", "Ethereum newHeads height or parent continuity failed"),
        ("bad_hash", "ethereum_header_commitment_mismatch"),
        ("disconnect", "OSError"),
    ],
)
def test_independent_witness_stops_on_invalid_continuity_or_disconnect(tmp_path, fault, reason):
    directory = tmp_path / "head_feed"
    transport = Socket()
    abort = Event()
    session = EthereumChildSession(directory, SCOPE, abort, socket_factory=lambda: transport)
    session.start()
    query_ns = time.monotonic_ns()
    h = header(10, "0x" + "12" * 32)
    c = header(11, h["hash"])
    transport.frames.put(notification(h))
    if fault == "disconnect":
        transport.frames.put(OSError("mock link dropped"))
    elif fault == "gap":
        transport.frames.put(notification(header(12, h["hash"])))
    elif fault == "same_height":
        transport.frames.put(notification(header(10, "0x" + "56" * 32)))
    elif fault == "wrong_parent":
        transport.frames.put(notification(header(11, "0x" + "34" * 32)))
    else:
        transport.frames.put(notification({**c, "hash": "0x" + "ab" * 32}))
    assert abort.wait(2)
    session.close(binding(c))
    witness, terminal = inspect_witness(
        directory, SCOPE, discovery=binding(h), discovery_request_ns=query_ns, child=binding(c)
    )
    assert witness.child_monotonic_ns is None and terminal["failure"] == witness.status == reason
