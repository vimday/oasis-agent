"""Operator-run isolated digest-signing authority for the LP signer sidecar (WP7).

The custody model forbids signing material inside any research, strategy, server or
agent process, so a live canary needs a signer that is a *separate* OS process
holding one key. This module is that process, started by the operator with
``python -m scripts.run_lp_signer_daemon``: it serves exactly one canonical
32-byte digest request per connection over a private Unix socket, holds the key
only in its own memory, and never discloses key material, request bodies or
provider diagnostics.

It signs digests, never transactions. The sidecar builds the EIP-1559 digest,
independently rechecks policy, nonce, fee caps and capital, then recovers the
signer address from the assembled transaction; a digest authority cannot tell
what it authorized, which is why admission lives there and not here. The
in-repo implementation is a bounded reference: one allowlisted account, a 0700
owner directory with a 0600 endpoint, a hard signature budget, a wall-clock time
to live, one request per connection, a bounded request frame, and an append-only
audit of hashes, addresses and fixed refusal reasons.

An EOA key in a local file is the *least* isolated accepted configuration. The
same socket contract can be served by an HSM, a cloud KMS, or a hardware wallet
with no change to the client: the sidecar only ever asks for a digest signature.
"""

import json
import os
import re
import socket
import stat
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from eth_account import Account

from ..models.common import canonical_json, normalize_address, sha256_hex
from .lp_signer_sidecar import SECP256K1_ORDER
from .socket_key_provider import SCHEMA as DIGEST_SIGNING_SCHEMA

AUDIT_SCHEMA = "oasis.digest-signer-audit.v1"
MAX_REQUEST_BYTES = 1024
_KEY_BYTES = re.compile(r"[0-9a-fA-F]{64}\Z")
_DIGEST = re.compile(r"0x[0-9a-f]{64}\Z")
_REQUEST_FIELDS = {"schema", "account", "digest"}


class SignerRefusal(Exception):
    """A request this authority cannot bind, carrying a fixed audit reason."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def sign_digest(private_key: bytes, digest: bytes) -> tuple[int, int, int]:
    """Return the canonical low-s (r, s, y_parity) over one 32-byte digest."""
    if type(private_key) is not bytes or len(private_key) != 32:
        raise ValueError("exact 32-byte private key required")
    if type(digest) is not bytes or len(digest) != 32:
        raise ValueError("exact 32-byte signing digest required")
    signed = Account.unsafe_sign_hash(digest, private_key)
    parity = signed.v - 27 if signed.v >= 27 else signed.v
    if not 1 <= signed.r < SECP256K1_ORDER or not 1 <= signed.s <= SECP256K1_ORDER // 2:
        raise ValueError("signature is not canonical low-s")
    if parity not in (0, 1):
        raise ValueError("signature parity is invalid")
    return signed.r, signed.s, parity


def address_of(private_key: bytes) -> str:
    """The normalized address this key controls, derived without persisting the key."""
    if type(private_key) is not bytes or len(private_key) != 32:
        raise ValueError("exact 32-byte private key required")
    return normalize_address(Account.from_key(private_key).address)


def _parse_hex_key(text: str) -> bytes:
    if not isinstance(text, str):
        raise ValueError("key material must be text")
    value = text.strip()
    if value[:2] in {"0x", "0X"}:
        value = value[2:]
    if _KEY_BYTES.fullmatch(value) is None:
        raise ValueError("key material must be exactly 32 hex-encoded bytes")
    return bytes.fromhex(value)


def load_private_key(
    *, key_file: Path | None, key_env: str | None, environ: Mapping[str, str]
) -> bytes:
    """Read the canary key inside this process only. Never logged, never returned."""
    if (key_file is None) == (key_env is None):
        raise ValueError("configure exactly one key file or key environment name")
    if key_file is not None:
        if not isinstance(key_file, Path):
            raise TypeError("key file path required")
        if key_file.is_symlink():
            raise ValueError("the key file cannot be a symlink")
        info = key_file.stat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise ValueError("the key file must be an owner-only 0600 regular file")
        return _parse_hex_key(key_file.read_text(encoding="ascii"))
    if not isinstance(key_env, str) or not key_env or key_env.strip() != key_env:
        raise ValueError("key environment name required")
    if key_env not in environ:
        raise ValueError("the configured key environment name is not set")
    return _parse_hex_key(environ[key_env])


def prepare_endpoint(path: Path) -> socket.socket:
    """Bind a fresh owner-only endpoint, replacing only this user's stale socket."""
    if not isinstance(path, Path):
        raise TypeError("signer socket path required")
    if not path.is_absolute() or len(os.fsencode(path)) > 100:
        raise ValueError("short absolute signer socket path required")
    parent = path.parent
    if path.is_symlink() or parent.resolve() != parent:
        raise ValueError("signer socket cannot use symlink paths")
    directory = parent.stat()
    if (
        not stat.S_ISDIR(directory.st_mode)
        or directory.st_uid != os.getuid()
        or stat.S_IMODE(directory.st_mode) != 0o700
    ):
        raise ValueError("signer socket needs a private owner directory")
    if path.exists() or path.is_symlink():
        stale = path.lstat()
        if (
            not stat.S_ISSOCK(stale.st_mode)
            or stale.st_uid != os.getuid()
            or stat.S_IMODE(stale.st_mode) != 0o600
        ):
            raise ValueError("refusing to replace a path this authority does not own")
        path.unlink()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(path))
        path.chmod(0o600)
        server.listen(4)
    except OSError:
        server.close()
        raise
    return server


class SigningAudit:
    """Append-only decisions: addresses, digests, hashes and fixed reasons only."""

    def __init__(self, path: Path) -> None:
        if not isinstance(path, Path):
            raise TypeError("audit path required")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._stream = path.open("a", encoding="ascii")
        self._seq = 0

    def append(
        self,
        *,
        address: str,
        digest: bytes | None,
        outcome: str,
        reason: str | None,
        signature_hash: str | None,
    ) -> None:
        if outcome not in {"signed", "refused"}:
            raise ValueError("audit outcome must be signed or refused")
        if (outcome == "signed") != (reason is None):
            raise ValueError("only a refusal carries a reason")
        self._seq += 1
        record = {
            "schema": AUDIT_SCHEMA,
            "seq": self._seq,
            "timestamp": int(time.time()),
            "address": address,
            "digest": None if digest is None else "0x" + digest.hex(),
            "outcome": outcome,
            "reason": reason,
            "signature_hash": signature_hash,
        }
        self._stream.write(canonical_json(record).decode() + "\n")
        self._stream.flush()


def signature_hash(r: int, s: int, y_parity: int) -> str:
    """Same binding the sidecar journals, so an operator can correlate the two."""
    return "0x" + sha256_hex(canonical_json({"r": r, "s": s, "y_parity": y_parity}))


@dataclass(frozen=True, slots=True)
class SignerLimits:
    """Hard bounds so a stray authority cannot sign indefinitely."""

    max_signatures: int
    ttl_seconds: int

    def __post_init__(self) -> None:
        if type(self.max_signatures) is not int or not 1 <= self.max_signatures <= 1000:
            raise ValueError("signature budget must be an integer in 1..1000")
        if type(self.ttl_seconds) is not int or not 1 <= self.ttl_seconds <= 86400:
            raise ValueError("time to live must be an integer in 1..86400 seconds")


def _read_frame(connection: socket.socket) -> bytes | None:
    """One newline-terminated canonical frame within `MAX_REQUEST_BYTES`."""
    frame = bytearray()
    while True:
        chunk = connection.recv(512)
        if not chunk:
            return None
        frame.extend(chunk)
        if len(frame) > MAX_REQUEST_BYTES:
            return None
        if b"\n" in frame:
            if frame.count(b"\n") != 1 or frame[-1:] != b"\n":
                return None
            return bytes(frame[:-1])


def _parse_request(raw: bytes) -> tuple[str, bytes]:
    try:
        data = json.loads(raw, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, ValueError) as exc:
        raise SignerRefusal("request is not valid JSON") from exc
    if not isinstance(data, dict) or set(data) != _REQUEST_FIELDS:
        raise SignerRefusal("request has invalid fields")
    if data["schema"] != DIGEST_SIGNING_SCHEMA:
        raise SignerRefusal("request schema is unsupported")
    if canonical_json(data) != raw:
        raise SignerRefusal("request is not canonical JSON")
    if not isinstance(data["digest"], str) or _DIGEST.fullmatch(data["digest"]) is None:
        raise SignerRefusal("request digest is not 32 canonical bytes")
    if not isinstance(data["account"], str):
        raise SignerRefusal("request account is not an address")
    try:
        account = normalize_address(data["account"])
    except ValueError as exc:  # never echo the supplied address
        raise SignerRefusal("request account is not an address") from exc
    return account, bytes.fromhex(data["digest"][2:])


def _handle(
    connection: socket.socket, *, address: str, private_key: bytes, audit: SigningAudit
) -> bool:
    """Serve one connection. Returns True only when the key was used."""
    raw = _read_frame(connection)
    if raw is None:
        audit.append(
            address=address,
            digest=None,
            outcome="refused",
            reason="request frame is malformed or oversized",
            signature_hash=None,
        )
        return False
    try:
        requested, digest = _parse_request(raw)
        if requested != address:
            raise SignerRefusal("request account is not the configured account")
    except SignerRefusal as refusal:
        audit.append(
            address=address,
            digest=None,
            outcome="refused",
            reason=refusal.reason,
            signature_hash=None,
        )
        return False
    r, s, parity = sign_digest(private_key, digest)
    audit.append(
        address=address,
        digest=digest,
        outcome="signed",
        reason=None,
        signature_hash=signature_hash(r, s, parity),
    )
    reply = canonical_json(
        {
            "schema": DIGEST_SIGNING_SCHEMA,
            "account": address,
            "digest": "0x" + digest.hex(),
            "r": f"0x{r:064x}",
            "s": f"0x{s:064x}",
            "y_parity": parity,
        }
    )
    try:
        connection.sendall(reply + b"\n")
    except OSError:
        # The key was used even though the caller never received the signature, so
        # the audit keeps the record and the caller's own reservation stays
        # unresolved. Nothing is retried here.
        return True
    return True


def _unlink_own_endpoint(path: Path) -> None:
    try:
        info = path.lstat()
    except OSError:
        return
    if stat.S_ISSOCK(info.st_mode) and info.st_uid == os.getuid():
        path.unlink(missing_ok=True)


def serve(
    *,
    endpoint: Path,
    account: str,
    private_key: bytes,
    limits: SignerLimits,
    audit: SigningAudit,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """Serve digest requests until the signature budget or the time to live ends.

    Returns the number of signatures produced (refusals are audited, not counted).
    """
    address = normalize_address(account)
    if address_of(private_key) != address:
        raise ValueError("the configured key does not control the configured account")
    server = prepare_endpoint(endpoint)
    deadline = clock() + limits.ttl_seconds
    signed = 0
    try:
        server.settimeout(1.0)
        while signed < limits.max_signatures:
            remaining = deadline - clock()
            if remaining <= 0:
                break
            try:
                connection, _ = server.accept()
            except TimeoutError:
                continue
            with connection:
                connection.settimeout(min(3.0, max(0.1, remaining)))
                try:
                    if _handle(connection, address=address, private_key=private_key, audit=audit):
                        signed += 1
                except OSError:
                    # A stalled or vanished client is not fatal: its reservation
                    # stays unresolved in the sidecar, which blocks a later sign.
                    continue
    finally:
        server.close()
        _unlink_own_endpoint(endpoint)
    return signed


__all__ = [
    "AUDIT_SCHEMA",
    "DIGEST_SIGNING_SCHEMA",
    "MAX_REQUEST_BYTES",
    "SignerLimits",
    "SignerRefusal",
    "SigningAudit",
    "address_of",
    "load_private_key",
    "prepare_endpoint",
    "serve",
    "sign_digest",
    "signature_hash",
]
