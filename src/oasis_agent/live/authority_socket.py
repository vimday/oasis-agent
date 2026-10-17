"""Bounded full-envelope Unix socket protocol; no digest signing endpoint."""

import json
import os
import socket
import stat
import time
from contextlib import suppress
from pathlib import Path
from typing import Any, Protocol

from eth_hash.auto import keccak

from ..models.common import canonical_json, normalize_address
from .intent import SubmittedTransaction

SCHEMA = "oasis.live-transaction-authority.v1"
MAX_FRAME = 32_768


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


def read_frame(connection: socket.socket) -> bytes:
    result = bytearray()
    while len(result) <= MAX_FRAME:
        chunk = connection.recv(min(4096, MAX_FRAME + 1 - len(result)))
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
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(self.timeout)
            conn.connect(str(path))
            conn.sendall(request + b"\n")
            reply = read_frame(conn)
        d = json.loads(reply)
        if not isinstance(d, dict) or canonical_json(d) != reply or d.get("schema") != SCHEMA:
            raise ValueError("authority reply is malformed")
        return d

    def pending_status(self) -> dict[str, Any]:
        d = self._exchange(
            canonical_json({"schema": SCHEMA, "account": self.account, "operation": "status"})
        )
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

    def sign(self, fields: list[object], context: dict[str, Any]) -> bytes: ...


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
            connection.settimeout(min(240, max(1, deadline - time.monotonic())))
            try:
                frame = read_frame(connection)
                parsed = json.loads(frame)
                if (
                    isinstance(parsed, dict)
                    and set(parsed) == {"schema", "account", "operation"}
                    and parsed == {"schema": SCHEMA, "account": account, "operation": "status"}
                    and canonical_json(parsed) == frame
                ):
                    reply: dict[str, object] = {"schema": SCHEMA, "status": authority.status()}
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
                connection.sendall(canonical_json(reply) + b"\n")
    return signed
