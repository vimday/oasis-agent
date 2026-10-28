"""Bounded full-envelope Unix socket protocol; no digest signing endpoint."""

import json
import os
import socket
import stat
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any, Protocol

from eth_hash.auto import keccak

from ..models.common import canonical_json, normalize_address
from .intent import SubmittedTransaction

SCHEMA = "oasis.live-transaction-authority.v1"
MAX_FRAME = 32_768


def status_nonce(raw: bytes, account: str) -> int | None:
    request = json.loads(raw)
    expected: dict[str, Any] = {"schema": SCHEMA, "account": account, "operation": "status"}
    nonce: int | None = None
    if isinstance(request, dict) and "nonce" in request:
        nonce = request["nonce"]
        if type(nonce) is not int or not 0 <= nonce < 2**64:
            raise ValueError("canonical status nonce required")
        expected["nonce"] = nonce
    if canonical_json(expected) != raw:
        raise ValueError("canonical account-bound status request required")
    return nonce


def fields_json(fields: list[object]) -> list[object]:
    if len(fields) != 9 or not isinstance(fields[5], bytes) or not isinstance(fields[7], bytes):
        raise ValueError("unsigned transaction envelope required")
    return [*fields[:5], "0x" + fields[5].hex(), fields[6], "0x" + fields[7].hex(), fields[8]]


def parse_request(raw: bytes) -> tuple[str, list[object], dict[str, Any]]:
    if len(raw) > MAX_FRAME:
        raise ValueError("authority frame exceeds bound")
    d = json.loads(raw)
    if (
        not isinstance(d, dict)
        or set(d) != {"schema", "account", "fields", "context"}
        or d["schema"] != SCHEMA
        or canonical_json(d) != raw
        or not isinstance(d["context"], dict)
    ):
        raise ValueError("canonical authority request required")
    f = d["fields"]
    if not isinstance(f, list) or len(f) != 9:
        raise ValueError("unsigned envelope fields required")
    for i in (5, 7):
        if not isinstance(f[i], str) or not f[i].startswith("0x"):
            raise ValueError("canonical envelope bytes required")
        value = bytes.fromhex(f[i][2:])
        if "0x" + value.hex() != f[i]:
            raise ValueError("noncanonical envelope hex")
        f[i] = value
    return normalize_address(d["account"]), f, d["context"]


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("authority exchange deadline expired")
    return remaining


def read_frame(
    connection: socket.socket, *, deadline: float | None = None, idle_timeout: float = 5
) -> bytes:
    result = bytearray()
    while len(result) <= MAX_FRAME:
        if deadline is not None:
            connection.settimeout(min(idle_timeout, _remaining(deadline)))
        chunk = connection.recv(min(4096, MAX_FRAME + 1 - len(result)))
        if deadline is not None:
            _remaining(deadline)  # A complete frame arriving at expiry is still too late.
        if not chunk:
            raise ValueError("incomplete authority frame")
        result.extend(chunk)
        if b"\n" in result:
            if result[-1:] != b"\n" or result.count(b"\n") != 1:
                raise ValueError("one frame per connection required")
            return bytes(result[:-1])
    raise ValueError("authority frame exceeds bound")


class TransactionSocketClient:
    def __init__(self, endpoint: Path, account: str, timeout: float = 240) -> None:
        if type(timeout) not in (int, float) or not 0 < timeout <= 240:
            raise ValueError("authority timeout must be finite and within 240 seconds")
        self.endpoint, self.account, self.timeout = endpoint, normalize_address(account), timeout

    def _exchange(self, request: bytes) -> dict[str, Any]:
        path = self.endpoint
        if path != path.resolve() or not path.is_absolute():
            raise ValueError("authority endpoint cannot traverse a symlink")
        parent, info = path.parent.stat(), path.lstat()
        if (
            parent.st_uid != os.getuid()
            or stat.S_IMODE(parent.st_mode) != 0o700
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or not stat.S_ISSOCK(info.st_mode)
        ):
            raise ValueError("authority socket must be private and owned")
        if len(request) >= MAX_FRAME:
            raise ValueError("authority request exceeds bound")
        deadline = time.monotonic() + self.timeout
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(_remaining(deadline))
            conn.connect(str(path))
            conn.settimeout(_remaining(deadline))
            conn.sendall(request + b"\n")
            reply = read_frame(conn, deadline=deadline, idle_timeout=self.timeout)
        d = json.loads(reply)
        if not isinstance(d, dict) or canonical_json(d) != reply or d.get("schema") != SCHEMA:
            raise ValueError("authority reply is malformed")
        return d

    def pending_status(self, nonce: int | None = None) -> dict[str, Any]:
        request: dict[str, Any] = {"schema": SCHEMA, "account": self.account, "operation": "status"}
        if nonce is not None:
            request["nonce"] = nonce
        raw = canonical_json(request)
        status_nonce(raw, self.account)  # Reject invalid nonce before socket access.
        d = self._exchange(raw)
        if set(d) != {"schema", "status"} or not isinstance(d["status"], dict):
            raise ValueError("authority status unavailable")
        return d["status"]

    def sign_envelope(self, fields: list[object], context: dict[str, Any]) -> bytes:
        request = canonical_json(
            {
                "schema": SCHEMA,
                "account": self.account,
                "fields": fields_json(fields),
                "context": context,
            }
        )
        d = self._exchange(request)
        if set(d) != {"schema", "signed"} or not isinstance(d["signed"], str):
            raise ValueError("authority refused or returned malformed response")
        raw = bytes.fromhex(d["signed"])
        tx = SubmittedTransaction.from_signed(raw)
        if (
            tx.account != self.account
            or [
                tx.chain_id,
                tx.nonce,
                tx.max_priority_fee_per_gas,
                tx.max_fee_per_gas,
                tx.gas_limit,
                bytes.fromhex(tx.target[2:]),
                tx.value,
            ]
            != fields[:7]
            or not isinstance(fields[7], bytes)
            or tx.calldata_hash != "0x" + keccak(fields[7]).hex()
        ):
            raise ValueError("authority signed a different transaction")
        return raw


class Authority(Protocol):
    def status(self) -> dict[str, Any]: ...

    def status_for_nonce(self, nonce: int) -> dict[str, Any]: ...

    def sign(self, fields: list[object], context: dict[str, Any]) -> bytes: ...


def serve_status_connections(
    server: socket.socket,
    status: Callable[[], dict[str, Any]],
    *,
    account: str,
    max_requests: int,
    ttl_seconds: int,
    lookup: Callable[[int], dict[str, Any]] | None = None,
) -> int:
    """Keyless recovery endpoint: every connection spends a finite request slot.

    Signing frames are rejected before parsing an envelope. This server accepts
    only a status reader, so there is no signing provider or signing callback.
    """
    if (
        type(max_requests) is not int
        or not 1 <= max_requests <= 1000
        or type(ttl_seconds) is not int
        or not 1 <= ttl_seconds <= 86400
    ):
        raise ValueError("status lifetime/request budget invalid")
    deadline, requests = time.monotonic() + ttl_seconds, 0
    while requests < max_requests and time.monotonic() < deadline:
        server.settimeout(min(1, max(0.001, deadline - time.monotonic())))
        try:
            connection, _ = server.accept()
        except TimeoutError:
            continue
        requests += 1
        with connection:
            connection.settimeout(min(5, max(0.001, deadline - time.monotonic())))
            try:
                nonce = status_nonce(read_frame(connection, deadline=deadline), account)
                if time.monotonic() >= deadline:
                    raise ValueError("status request refused")
                if nonce is None:
                    public = status()
                elif lookup is None:
                    raise ValueError("historical status unavailable")
                else:
                    public = lookup(nonce)
                reply = canonical_json({"schema": SCHEMA, "status": public})
                if len(reply) >= MAX_FRAME:
                    raise ValueError("status reply exceeds frame bound")
            except Exception:  # noqa: BLE001 - no journal/request details in a refusal
                reply = canonical_json({"schema": SCHEMA, "refused": True})
            with suppress(OSError):
                # Journal reads and reply encoding consume the same lifetime as
                # request reception. Do not send even a refusal after expiry.
                connection.settimeout(min(5, _remaining(deadline)))
                connection.sendall(reply + b"\n")
    return requests


def serve_connections(
    server: socket.socket,
    authority: Authority,
    *,
    account: str,
    max_signatures: int,
    ttl_seconds: int,
) -> int:
    if not 1 <= max_signatures <= 1000 or not 1 <= ttl_seconds <= 86400:
        raise ValueError("authority lifetime/signature budget invalid")
    server.settimeout(1)
    deadline, signed = time.monotonic() + ttl_seconds, 0
    while signed < max_signatures and time.monotonic() < deadline:
        try:
            connection, _ = server.accept()
        except TimeoutError:
            continue
        with connection:
            try:
                frame = read_frame(
                    connection, deadline=min(deadline, time.monotonic() + 240), idle_timeout=240
                )
                parsed = json.loads(frame)
                _remaining(deadline)
                if isinstance(parsed, dict) and parsed.get("operation") == "status":
                    nonce = status_nonce(frame, account)
                    public = (
                        authority.status() if nonce is None else authority.status_for_nonce(nonce)
                    )
                    reply: dict[str, object] = {"schema": SCHEMA, "status": public}
                else:
                    requested, fields, context = parse_request(frame)
                    if requested != account or time.monotonic() >= deadline:
                        raise ValueError("account/lifetime mismatch")
                    raw = authority.sign(fields, context)
                    signed += 1  # Counts even when reply delivery fails.
                    reply = {"schema": SCHEMA, "signed": raw.hex()}
            except Exception:  # noqa: BLE001 - never expose request/provider/key errors
                reply = {"schema": SCHEMA, "refused": True}
            with suppress(OSError):
                connection.settimeout(min(5, _remaining(deadline)))
                connection.sendall(canonical_json(reply) + b"\n")
    return signed
