"""Isolated digest signer: the key stays in its own process, the protocol is bounded.

These tests drive the operator-run authority directly: canonical low-s
signatures that recover to the configured account, refusals with fixed reasons
that never echo client input, an owner-only endpoint, a hard signature budget and
time to live, and an end-to-end signing where a real signer sidecar obtains its
signature over the socket. Every key here is generated inside the test process
and never written to the repository.
"""

import errno
import json
import shutil
import socket
import stat
import tempfile
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

import pytest
from eth_hash.auto import keccak
from eth_keys import keys

from oasis_agent.execution import lp_signer_daemon as daemon_module
from oasis_agent.execution.lp_signer_daemon import (
    AUDIT_SCHEMA,
    DIGEST_SIGNING_SCHEMA,
    SignerLimits,
    SigningAudit,
    address_of,
    load_private_key,
    prepare_endpoint,
    serve,
    sign_digest,
)
from oasis_agent.execution.lp_signer_sidecar import recover_signer
from oasis_agent.execution.socket_key_provider import SocketKeyProvider
from oasis_agent.models.common import canonical_json, normalize_address
from tests.unit.test_lp_signer_sidecar import ACCOUNT, KEY, make_request, make_sidecar

SECP256K1_HALF_ORDER = 0x7FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF5D576E7357A4501DDFE92F46681B20A0
OTHER_KEY = bytes(range(33, 65))
DEFAULT_LIMITS = SignerLimits(4, 5)


def short_private_directory() -> Path:
    """A 0700 directory whose parent resolves cleanly inside the client's path bound."""
    for candidate in (Path("/private/tmp"), Path("/tmp")):
        if candidate.is_dir() and candidate.resolve() == candidate:
            return Path(tempfile.mkdtemp(prefix="oasis-signer-", dir=candidate))
    return Path(tempfile.mkdtemp(prefix="oasis-signer-"))


def read_audit(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="ascii").splitlines()]


@contextmanager
def running_daemon(
    tmp_path: Path,
    *,
    limits: SignerLimits = DEFAULT_LIMITS,
    key: bytes = KEY,
    account: str = ACCOUNT,
) -> Iterator[tuple[Path, Path, list[int], list[Exception]]]:
    """Start the authority on a real socket and stop it with the context.

    The socket lives in a short-lived private directory that is removed on exit;
    the audit journal lives in ``tmp_path`` so callers can read it afterwards.
    """
    directory = short_private_directory()
    endpoint, audit_path = directory / "signer.sock", tmp_path / "signer-audit.jsonl"
    audit = SigningAudit(audit_path)
    signed: list[int] = []
    errors: list[Exception] = []

    def run() -> None:
        try:
            signed.append(
                serve(
                    endpoint=endpoint,
                    account=account,
                    private_key=key,
                    limits=limits,
                    audit=audit,
                )
            )
        except Exception as error:  # noqa: BLE001 - surfaced to the assertions below
            errors.append(error)

    # The daemon binds, restricts the endpoint to 0600 and only then listens, so an
    # existing socket file can still refuse connections. Signal readiness after listen().
    listening = threading.Event()
    prepare = daemon_module.prepare_endpoint

    def prepare_and_signal(path: Path) -> socket.socket:
        server = prepare(path)
        listening.set()
        return server

    daemon_module.prepare_endpoint = prepare_and_signal  # type: ignore[assignment]
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    listening.wait(timeout=5)
    try:
        yield endpoint, audit_path, signed, errors
    finally:
        thread.join(timeout=10)
        daemon_module.prepare_endpoint = prepare  # type: ignore[assignment]
        shutil.rmtree(directory, ignore_errors=True)


def raw_request(endpoint: Path, payload: bytes) -> bytes:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(5)
        client.connect(str(endpoint))
        client.sendall(payload)
        try:
            client.shutdown(socket.SHUT_WR)
        except OSError as exc:
            # The daemon may close a refused connection before this half-close; on macOS
            # that surfaces as ENOTCONN. It means the same thing: no reply was sent.
            if exc.errno != errno.ENOTCONN:
                raise
            return b""
        return client.recv(4096)


def request_frame(**overrides: object) -> bytes:
    payload: dict[str, object] = {
        "schema": DIGEST_SIGNING_SCHEMA,
        "account": normalize_address(ACCOUNT),
        "digest": "0x" + keccak(b"oasis").hex(),
    }
    payload.update(overrides)
    return canonical_json(payload) + b"\n"


def test_sign_digest_is_canonical_low_s_and_recovers_to_the_key_owner() -> None:
    digest = keccak(b"oasis-daemon")
    r, s, y_parity = sign_digest(KEY, digest)
    assert 1 <= r < 2**256
    assert 1 <= s <= SECP256K1_HALF_ORDER
    assert y_parity in (0, 1)
    recovered = keys.Signature(vrs=(y_parity, r, s)).recover_public_key_from_msg_hash(digest)
    assert normalize_address("0x" + keccak(recovered.to_bytes())[12:].hex()) == normalize_address(
        ACCOUNT
    )
    assert sign_digest(KEY, digest) == (r, s, y_parity)


def test_sign_digest_rejects_material_of_the_wrong_shape() -> None:
    digest = keccak(b"oasis-daemon")
    with pytest.raises(ValueError, match="exact 32-byte private key"):
        sign_digest(KEY[:31], digest)
    with pytest.raises(ValueError, match="exact 32-byte private key"):
        sign_digest(KEY.hex(), digest)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="exact 32-byte signing digest"):
        sign_digest(KEY, digest[:31])
    with pytest.raises(ValueError, match="exact 32-byte signing digest"):
        sign_digest(KEY, bytearray(digest))  # type: ignore[arg-type]


def test_the_key_is_read_from_one_owner_only_source(tmp_path) -> None:
    key_file = tmp_path / "canary.key"
    key_file.write_text("0x" + KEY.hex() + "\n", encoding="ascii")
    key_file.chmod(0o600)
    loaded = load_private_key(key_file=key_file, key_env=None, environ={})
    assert loaded == KEY
    assert address_of(loaded) == normalize_address(ACCOUNT)

    key_file.chmod(0o644)
    with pytest.raises(ValueError, match="owner-only 0600 regular file"):
        load_private_key(key_file=key_file, key_env=None, environ={})
    key_file.chmod(0o600)
    link = tmp_path / "link.key"
    link.symlink_to(key_file)
    with pytest.raises(ValueError, match="cannot be a symlink"):
        load_private_key(key_file=link, key_env=None, environ={})
    with pytest.raises(ValueError, match="owner-only 0600 regular file"):
        load_private_key(key_file=tmp_path, key_env=None, environ={})

    environ = {"OASIS_CANARY_KEY": KEY.hex().upper()}
    assert load_private_key(key_file=None, key_env="OASIS_CANARY_KEY", environ=environ) == KEY
    with pytest.raises(ValueError, match="exactly one key file or key environment name"):
        load_private_key(key_file=key_file, key_env="OASIS_CANARY_KEY", environ=environ)
    with pytest.raises(ValueError, match="exactly one key file or key environment name"):
        load_private_key(key_file=None, key_env=None, environ=environ)
    with pytest.raises(ValueError, match="key environment name is not set"):
        load_private_key(key_file=None, key_env="OASIS_MISSING", environ=environ)
    with pytest.raises(ValueError, match="key environment name required"):
        load_private_key(key_file=None, key_env=" OASIS_CANARY_KEY", environ=environ)
    with pytest.raises(ValueError, match="exactly 32 hex-encoded bytes"):
        load_private_key(
            key_file=None, key_env="OASIS_CANARY_KEY", environ={"OASIS_CANARY_KEY": ""}
        )
    with pytest.raises(ValueError, match="exactly 32 hex-encoded bytes"):
        load_private_key(
            key_file=None, key_env="OASIS_CANARY_KEY", environ={"OASIS_CANARY_KEY": "zz" * 32}
        )
    with pytest.raises(ValueError, match="exactly 32 hex-encoded bytes"):
        load_private_key(
            key_file=None,
            key_env="OASIS_CANARY_KEY",
            environ={"OASIS_CANARY_KEY": KEY.hex() + KEY.hex()},
        )


def test_the_authority_refuses_a_key_that_is_not_the_configured_account() -> None:
    directory = short_private_directory()
    try:
        endpoint = directory / "signer.sock"
        with pytest.raises(ValueError, match="does not control the configured account"):
            serve(
                endpoint=endpoint,
                account=address_of(OTHER_KEY),
                private_key=KEY,
                limits=SignerLimits(1, 5),
                audit=SigningAudit(directory / "audit.jsonl"),
            )
        assert not endpoint.exists()
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_the_endpoint_needs_a_private_short_owner_directory() -> None:
    directory = short_private_directory()
    try:
        with pytest.raises(ValueError, match="short absolute signer socket path"):
            prepare_endpoint(Path("signer.sock"))
        with pytest.raises(ValueError, match="short absolute signer socket path"):
            prepare_endpoint(directory / Path("x" * 120))
        directory.chmod(0o755)
        with pytest.raises(ValueError, match="private owner directory"):
            prepare_endpoint(directory / "signer.sock")
        directory.chmod(0o700)
        endpoint = directory / "signer.sock"
        endpoint.write_text("not a socket")
        with pytest.raises(ValueError, match="refusing to replace a path this authority"):
            prepare_endpoint(endpoint)
        endpoint.unlink()

        server = prepare_endpoint(endpoint)
        try:
            info = endpoint.lstat()
            assert stat.S_ISSOCK(info.st_mode)
            assert stat.S_IMODE(info.st_mode) == 0o600
        finally:
            server.close()
        assert prepare_endpoint(endpoint).close() is None
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_a_sidecar_request_is_signed_through_the_isolated_authority(tmp_path) -> None:
    with running_daemon(tmp_path, limits=SignerLimits(1, 5)) as (
        endpoint,
        audit_path,
        signed,
        errors,
    ):
        sidecar = make_sidecar(tmp_path, ACCOUNT, provider=SocketKeyProvider(endpoint, ACCOUNT))
        decision = sidecar.handle(make_request(ACCOUNT))
        assert errors == []
    assert decision.outcome == "signed"
    assert decision.raw_transaction_hex is not None
    assert normalize_address(
        recover_signer(bytes.fromhex(decision.raw_transaction_hex))
    ) == normalize_address(ACCOUNT)
    assert signed == [1]
    records = read_audit(audit_path)
    assert [record["outcome"] for record in records] == ["signed"]
    assert records[0]["signature_hash"] == decision.signature_hash
    assert records[0]["digest"] == decision.digest
    assert records[0]["schema"] == AUDIT_SCHEMA


def test_a_foreign_request_is_refused_without_a_reply_and_the_authority_keeps_serving(
    tmp_path,
) -> None:
    hostile = [
        canonical_json(
            {
                "schema": DIGEST_SIGNING_SCHEMA,
                "account": normalize_address(ACCOUNT),
                "digest": "0x" + keccak(b"oasis").hex(),
            }
        ).replace(b'{"account"', b'{ "account"')
        + b"\n",
        request_frame(schema="oasis.digest-signing.v2"),
        request_frame(account=normalize_address(address_of(OTHER_KEY))),
        request_frame(digest="0x" + "zz" * 32),
        b'{"schema":"x","schema":"x","account":"y","digest":"z"}\n',
        b"{not json}\n",
        b'{"schema":"x","account":"y","digest":"z"}',
        b"{" + b'"pad":"' + b"a" * 4000 + b'"}\n',
        canonical_json(
            {
                "schema": DIGEST_SIGNING_SCHEMA,
                "account": "0xnothex",
                "digest": "0x" + keccak(b"oasis").hex(),
            }
        )
        + b"\n",
    ]
    with running_daemon(tmp_path, limits=SignerLimits(2, 8)) as (
        endpoint,
        audit_path,
        signed,
        errors,
    ):
        for payload in hostile:
            assert raw_request(endpoint, payload) == b""
        digest = keccak(b"after-hostile")
        assert SocketKeyProvider(endpoint, ACCOUNT).sign(digest) == sign_digest(KEY, digest)
    assert errors == []
    assert signed == [1]
    records = read_audit(audit_path)
    assert [record["outcome"] for record in records] == ["refused"] * len(hostile) + ["signed"]
    assert [record["reason"] for record in records[: len(hostile)]] == [
        "request is not canonical JSON",
        "request schema is unsupported",
        "request account is not the configured account",
        "request digest is not 32 canonical bytes",
        "request is not valid JSON",
        "request is not valid JSON",
        "request frame is malformed or oversized",
        "request frame is malformed or oversized",
        "request account is not an address",
    ]
    assert all(record["digest"] is None for record in records[: len(hostile)])
    assert all(record["signature_hash"] is None for record in records[: len(hostile)])
    assert set(records[0]) == {
        "schema",
        "seq",
        "timestamp",
        "address",
        "digest",
        "outcome",
        "reason",
        "signature_hash",
    }
    assert [record["seq"] for record in records] == list(range(1, len(records) + 1))


def test_the_budget_and_time_to_live_stop_the_authority_and_remove_its_endpoint(tmp_path) -> None:
    started = time.monotonic()
    with running_daemon(tmp_path, limits=SignerLimits(1, 1)) as (
        endpoint,
        audit_path,
        signed,
        errors,
    ):
        pass
    assert errors == []
    assert signed == [0]
    assert not endpoint.exists()
    assert time.monotonic() - started < 10
    assert read_audit(audit_path) == []


def test_the_audit_never_contains_key_material(tmp_path) -> None:
    with running_daemon(tmp_path, limits=SignerLimits(2, 8)) as (
        endpoint,
        audit_path,
        signed,
        errors,
    ):
        SocketKeyProvider(endpoint, ACCOUNT).sign(keccak(b"one"))
        foreign = request_frame(account=normalize_address(address_of(OTHER_KEY)))
        assert raw_request(endpoint, foreign) == b""
    assert errors == []
    assert signed == [1]
    text = audit_path.read_text(encoding="ascii")
    assert KEY.hex() not in text
    assert KEY.hex().upper() not in text
    assert "0x" + KEY.hex() not in text
    outcomes: Sequence[str] = [record["outcome"] for record in read_audit(audit_path)]
    assert outcomes == ["signed", "refused"]
