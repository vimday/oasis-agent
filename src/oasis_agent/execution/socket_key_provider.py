"""Bounded local transport to a separately operated signing authority.

Only the policy sidecar may use this adapter. The remote authority and OS access
isolation must be provisioned separately; this module never loads key material.
"""

import json
import os
import re
import socket
import stat
import time
from pathlib import Path

from ..models.common import canonical_json, normalize_address
from .lp_signer_sidecar import SECP256K1_ORDER

SCHEMA = "oasis.digest-signing.v1"
MAX_REPLY = 2048


class SocketKeyProvider:
    def __init__(self, path: Path, account: str, *, timeout_ms: int = 3000):
        if not path.is_absolute() or len(os.fsencode(path)) > 100:
            raise ValueError("short absolute signer socket path required")
        if type(timeout_ms) is not int or not 1 <= timeout_ms <= 10000:
            raise ValueError("signer timeout must be 1..10000 milliseconds")
        self.path, self.account, self.timeout_ms = path, normalize_address(account), timeout_ms

    def _identity(self) -> tuple[int, int]:
        parent = self.path.parent
        if parent.resolve() != parent or self.path.is_symlink():
            raise ValueError("signer socket cannot use symlink paths")
        directory, endpoint = parent.stat(), self.path.lstat()
        if (
            not stat.S_ISDIR(directory.st_mode)
            or directory.st_uid != os.getuid()
            or stat.S_IMODE(directory.st_mode) != 0o700
            or not stat.S_ISSOCK(endpoint.st_mode)
            or endpoint.st_uid != os.getuid()
            or stat.S_IMODE(endpoint.st_mode) != 0o600
        ):
            raise ValueError("signer socket needs a private owner directory and endpoint")
        return endpoint.st_dev, endpoint.st_ino

    def _exchange(self, request: bytes) -> bytes:
        identity = self._identity()
        deadline = time.monotonic() + self.timeout_ms / 1000
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(self.timeout_ms / 1000)
            client.connect(str(self.path))
            if self._identity() != identity:
                raise ValueError("signer endpoint changed during connection")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("signer deadline exhausted")
            client.settimeout(remaining)
            client.sendall(request + b"\n")
            reply = bytearray()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("signer deadline exhausted")
                client.settimeout(remaining)
                chunk = client.recv(min(512, MAX_REPLY + 1 - len(reply)))
                if not chunk:
                    raise ValueError("signer response incomplete")
                reply.extend(chunk)
                if len(reply) > MAX_REPLY:
                    raise ValueError("signer response exceeds limit")
                if b"\n" in reply:
                    if reply.count(b"\n") != 1 or reply[-1:] != b"\n":
                        raise ValueError("signer response framing invalid")
                    return bytes(reply[:-1])

    def sign(self, digest: bytes) -> tuple[int, int, int]:
        if type(digest) is not bytes or len(digest) != 32:
            raise ValueError("exact 32-byte signing digest required")
        request = {"schema": SCHEMA, "account": self.account, "digest": "0x" + digest.hex()}
        try:
            raw = self._exchange(canonical_json(request))
            data = json.loads(raw)
            if (
                not isinstance(data, dict)
                or set(data) != {*request, "r", "s", "y_parity"}
                or canonical_json(data) != raw
                or any(data[key] != value for key, value in request.items())
            ):
                raise ValueError("signer response binding invalid")
            for name in ("r", "s"):
                if (
                    not isinstance(data[name], str)
                    or re.fullmatch(r"0x[0-9a-f]{64}", data[name]) is None
                ):
                    raise ValueError("canonical signature scalar required")
            r, s, parity = int(data["r"], 16), int(data["s"], 16), data["y_parity"]
            if (
                not 1 <= r < SECP256K1_ORDER
                or not 1 <= s <= SECP256K1_ORDER // 2
                or type(parity) is not int
                or parity not in (0, 1)
            ):
                raise ValueError("signature scalar or parity out of bounds")
            return r, s, parity
        except Exception:  # noqa: BLE001 - never expose remote diagnostics
            # A timeout may mean a signature exists remotely. Sidecar reservations
            # remain unresolved; never retry here or echo remote strings.
            raise RuntimeError("external signing authority failed") from None
