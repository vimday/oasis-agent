"""WP7/WP8: the operator broadcast gate holds no key and sends nothing unverified.

The gate is attacked rather than the transport: a raw transaction that is not the
one the sidecar signed, an undeclared chain or target, a signer from another
account, value/gas/fee caps, a swapped decision, and an endpoint on the wrong
chain must each refuse without a submission. An unknown submission result must be
journaled as unresolved and never retried. Tampering with the audit chain, or
changing the policy behind it, must fail at open.
"""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from eth_hash.auto import keccak
from rlp import decode as rlp_decode
from rlp import encode as rlp_encode

from oasis_agent.errors import RpcError
from oasis_agent.execution import lp_broadcast
from oasis_agent.execution.lp_broadcast import (
    BroadcastAuthorization,
    BroadcastPolicy,
    HttpBroadcastRpc,
    LpBroadcaster,
    LpBroadcastJournal,
    authorize_broadcast,
    broadcast_policy_from_json,
    broadcast_policy_hash,
    broadcast_policy_to_json,
    decode_signed_transaction,
    expectation_from_sidecar,
    raw_bytes_from_decision,
)
from oasis_agent.execution.lp_signer_sidecar import (
    SECP256K1_ORDER,
    SIDE_CAR_DECISION_SCHEMA,
    LpSidecarDecision,
    LpSidecarRequest,
    sidecar_request_hash,
    sidecar_request_to_json,
    signed_transaction_bytes,
    transaction_digest,
)
from oasis_agent.models.common import canonical_json
from tests.unit.test_lp_signer_sidecar import (
    ACCOUNT,
    EXECUTOR,
    KEY,
    MAX_FEE_CAP,
    OTHER_KEY,
    FixtureKeyProvider,
    account_for,
    make_request,
)

CHAIN_ID = 4663
OTHER_CHAIN = 8453
OTHER_ACCOUNT = account_for(OTHER_KEY)
OTHER_HASH = "0x" + "5a" * 32
STRANGER = "0x" + "77" * 20
REQUEST_NONCE = 91
REQUEST_GAS = 2_000_000
REQUEST_MAX_FEE = 500_000_000
REQUEST_PRIORITY_FEE = 50_000_000
MAX_TOTAL_GAS_COST = 2 * 10**15
ENDPOINT_ENV = "OASIS_TEST_BROADCAST_RPC"


def policy(**overrides: object) -> BroadcastPolicy:
    options: dict[str, object] = {
        "chain_ids": frozenset({CHAIN_ID}),
        "account": ACCOUNT,
        "targets": frozenset({EXECUTOR}),
        "max_value_wei": 0,
        "max_gas_limit": 3_000_000,
        "max_fee_per_gas": MAX_FEE_CAP,
        "max_total_gas_cost_wei": MAX_TOTAL_GAS_COST,
    }
    options.update(overrides)
    return BroadcastPolicy(**options)


def signed_request(*, nonce: int = REQUEST_NONCE) -> LpSidecarRequest:
    return make_request(ACCOUNT, nonce=nonce)


def signed_decision(request: LpSidecarRequest, *, key: bytes = KEY) -> LpSidecarDecision:
    """The decision the sidecar would have produced for this exact request."""
    digest = transaction_digest(request)
    r, s, y_parity = FixtureKeyProvider(key).sign(digest)
    raw = signed_transaction_bytes(request, r=r, s=s, y_parity=y_parity)
    return LpSidecarDecision(
        "signed",
        (),
        sidecar_request_hash(request),
        digest="0x" + digest.hex(),
        signature_hash="0x" + "11" * 32,
        transaction_hash="0x" + keccak(raw).hex(),
        raw_transaction_hex=raw.hex(),
    )


def _envelope(
    *,
    chain_id: object = CHAIN_ID,
    nonce: object = REQUEST_NONCE,
    priority_fee: object = REQUEST_PRIORITY_FEE,
    max_fee: object = REQUEST_MAX_FEE,
    gas_limit: object = REQUEST_GAS,
    target: object = None,
    value: object = 0,
    calldata: object = b"\x01\x02\x03\x04",
    access_list: object = None,
    y_parity: object = 1,
    r: object = 1,
    s: object = 1,
) -> bytes:
    """A type-2 envelope with the signed request's fields, unless overridden."""
    body = [
        chain_id,
        nonce,
        priority_fee,
        max_fee,
        gas_limit,
        bytes.fromhex(EXECUTOR[2:]) if target is None else target,
        value,
        calldata,
        [] if access_list is None else access_list,
        y_parity,
        r,
        s,
    ]
    return b"\x02" + rlp_encode(body)


def _short_envelope() -> bytes:
    body = rlp_decode(_envelope()[1:])
    assert isinstance(body, list)
    return b"\x02" + rlp_encode(body[:-1])


class FakeRpc:
    """Endpoint double that records every submission and can fail on demand."""

    def __init__(
        self,
        *,
        chain: int | None = CHAIN_ID,
        result: object = None,
        error: Exception | None = None,
    ) -> None:
        self._chain = chain
        self._result = result
        self._error = error
        self.sent: list[str] = []

    def chain_id(self) -> int:
        if self._chain is None:
            raise RpcError("chain unreadable")
        return self._chain

    def send_raw_transaction(self, raw_hex: str) -> str:
        self.sent.append(raw_hex)
        if self._error is not None:
            raise self._error
        return str(self._result)


def authorize(
    request: LpSidecarRequest,
    decision: LpSidecarDecision,
    *,
    broadcast_policy: BroadcastPolicy | None = None,
) -> BroadcastAuthorization:
    transaction = decode_signed_transaction(raw_bytes_from_decision(decision))
    return authorize_broadcast(
        broadcast_policy if broadcast_policy is not None else policy(),
        expectation_from_sidecar(request, decision),
        transaction,
    )


def test_a_signed_sidecar_transaction_is_decoded_and_broadcast(tmp_path: Path) -> None:
    request = signed_request()
    decision = signed_decision(request)
    expectation = expectation_from_sidecar(request, decision)
    transaction = decode_signed_transaction(raw_bytes_from_decision(decision))
    assert transaction.signer == ACCOUNT
    assert transaction.chain_id == CHAIN_ID
    assert transaction.nonce == REQUEST_NONCE
    assert transaction.target == EXECUTOR
    assert transaction.value == 0
    assert transaction.gas_limit == REQUEST_GAS
    assert transaction.max_fee_per_gas == REQUEST_MAX_FEE
    assert transaction.max_priority_fee_per_gas == REQUEST_PRIORITY_FEE
    assert transaction.transaction_hash == expectation.transaction_hash
    assert authorize(request, decision) == BroadcastAuthorization(True, ())

    rpc = FakeRpc(result=expectation.transaction_hash)
    broadcaster = LpBroadcaster(policy(), tmp_path / "broadcast.db", rpc=rpc)
    result = broadcaster.broadcast(transaction.raw, expectation=expectation)
    broadcaster.journal.close()
    assert result.outcome == "broadcast"
    assert result.reasons == ()
    assert result.signer == ACCOUNT
    assert result.transaction_hash == expectation.transaction_hash
    assert rpc.sent == ["0x" + transaction.raw.hex()]

    reopened = LpBroadcastJournal(tmp_path / "broadcast.db", policy=policy())
    try:
        assert reopened.open_transactions() == (expectation.transaction_hash,)
        assert reopened.seen_transactions() == frozenset({expectation.transaction_hash})
    finally:
        reopened.close()


def test_raw_bytes_that_are_not_the_signed_transaction_are_refused(tmp_path: Path) -> None:
    request = signed_request()
    decision = signed_decision(request)
    expectation = expectation_from_sidecar(request, decision)
    stranger = raw_bytes_from_decision(signed_decision(signed_request(nonce=REQUEST_NONCE + 1)))
    rpc = FakeRpc(result=expectation.transaction_hash)
    broadcaster = LpBroadcaster(policy(), tmp_path / "swapped.db", rpc=rpc)
    try:
        result = broadcaster.broadcast(stranger, expectation=expectation)
    finally:
        broadcaster.journal.close()
    assert result.outcome == "denied"
    assert result.reasons == ("raw transaction is not the one the sidecar produced",)
    assert rpc.sent == []


@pytest.mark.parametrize(
    "overrides,reason",
    [
        ({"chain_ids": frozenset({OTHER_CHAIN})}, "chain is not in the operator allowlist"),
        ({"targets": frozenset({STRANGER})}, "target is not in the operator allowlist"),
        ({"account": OTHER_ACCOUNT}, "signed transaction signer is not the operator account"),
        ({"max_gas_limit": 1_000_000}, "gas limit exceeds the operator cap"),
        ({"max_fee_per_gas": 1}, "max fee per gas exceeds the operator cap"),
        ({"max_total_gas_cost_wei": 1}, "worst-case gas cost exceeds the operator cap"),
    ],
)
def test_the_operator_policy_refuses_an_out_of_policy_transaction(
    tmp_path: Path, overrides: dict[str, object], reason: str
) -> None:
    request = signed_request()
    decision = signed_decision(request)
    expectation = expectation_from_sidecar(request, decision)
    transaction = decode_signed_transaction(raw_bytes_from_decision(decision))
    authorization = authorize(request, decision, broadcast_policy=policy(**overrides))
    assert not authorization.authorized
    assert reason in authorization.reasons

    rpc = FakeRpc(result=expectation.transaction_hash)
    broadcaster = LpBroadcaster(policy(**overrides), tmp_path / "policy.db", rpc=rpc)
    try:
        result = broadcaster.broadcast(transaction.raw, expectation=expectation)
    finally:
        broadcaster.journal.close()
    assert result.outcome == "denied"
    assert reason in result.reasons
    assert rpc.sent == []


def test_a_denied_sidecar_decision_cannot_be_broadcast(tmp_path: Path) -> None:
    request = signed_request()
    denied = LpSidecarDecision("denied", ("no signing key provider is configured",), "0" * 64)
    with pytest.raises(ValueError, match="only a signed sidecar decision carries a transaction"):
        expectation_from_sidecar(request, denied)
    with pytest.raises(ValueError, match="only a signed sidecar decision carries a transaction"):
        raw_bytes_from_decision(denied)


def test_a_decision_from_another_request_is_refused() -> None:
    request = signed_request()
    stranger = signed_decision(signed_request(nonce=REQUEST_NONCE + 5))
    with pytest.raises(ValueError, match="does not belong to this request"):
        expectation_from_sidecar(request, stranger)


def test_a_decision_whose_raw_bytes_do_not_match_its_hash_is_refused() -> None:
    request = signed_request()
    decision = signed_decision(request)
    tampered = LpSidecarDecision(
        "signed",
        (),
        decision.request_hash,
        digest=decision.digest,
        signature_hash=decision.signature_hash,
        transaction_hash=OTHER_HASH,
        raw_transaction_hex=decision.raw_transaction_hex,
    )
    with pytest.raises(ValueError, match="does not match its hash"):
        raw_bytes_from_decision(tampered)


@pytest.mark.parametrize(
    "raw,match",
    [
        (b"", "outside the accepted bounds"),
        (b"\x02\x00", "not a complete type-2 list"),
        (b"\x02\xf9\x01", "not decodable RLP"),
        (rlp_encode([CHAIN_ID, 91, 500_000_000, 2_000_000, bytes(20), 0, b"", 1, 1, 1]), None),
        (_short_envelope(), "not a complete type-2 list"),
        (_envelope(access_list=[[b"\x11" * 20, []]]), "access list is refused"),
        (_envelope(target=b"\x11" * 19), "must be exactly one address"),
        (_envelope(chain_id=0), "chain ID must be nonzero"),
        (_envelope(gas_limit=0), "gas limit must be positive"),
        (_envelope(s=SECP256K1_ORDER // 2 + 1), "high-s signatures are rejected"),
        (_envelope(r=0), "outside the secp256k1 range"),
        (_envelope(s=SECP256K1_ORDER), "outside the accepted range"),
        (_envelope(y_parity=2), "y parity is outside the accepted range"),
    ],
)
def test_a_transaction_the_gate_cannot_fully_decode_is_refused(
    raw: bytes, match: str | None
) -> None:
    expected = match or "only EIP-1559 \\(type 2\\) transactions may be broadcast"
    with pytest.raises(ValueError, match=expected):
        decode_signed_transaction(raw)


def test_a_truncated_envelope_is_refused_at_decode_and_never_sent(tmp_path: Path) -> None:
    request = signed_request()
    decision = signed_decision(request)
    expectation = expectation_from_sidecar(request, decision)
    rpc = FakeRpc(result=expectation.transaction_hash)
    broadcaster = LpBroadcaster(policy(), tmp_path / "tampered.db", rpc=rpc)
    try:
        result = broadcaster.broadcast(_short_envelope(), expectation=expectation)
    finally:
        broadcaster.journal.close()
    assert result.outcome == "denied"
    assert result.reasons == (
        "raw transaction is refused: signed transaction envelope is not a complete type-2 list",
    )
    assert rpc.sent == []


def test_an_endpoint_on_another_chain_never_receives_the_transaction(tmp_path: Path) -> None:
    request = signed_request()
    decision = signed_decision(request)
    expectation = expectation_from_sidecar(request, decision)
    transaction = decode_signed_transaction(raw_bytes_from_decision(decision))
    rpc = FakeRpc(chain=OTHER_CHAIN, result=expectation.transaction_hash)
    broadcaster = LpBroadcaster(policy(), tmp_path / "chain.db", rpc=rpc)
    try:
        result = broadcaster.broadcast(transaction.raw, expectation=expectation)
    finally:
        broadcaster.journal.close()
    assert result.outcome == "denied"
    assert result.reasons == (
        "broadcast endpoint is on a different chain than the signed transaction",
    )
    assert rpc.sent == []


def test_an_unreadable_endpoint_chain_refuses_before_sending(tmp_path: Path) -> None:
    request = signed_request()
    decision = signed_decision(request)
    expectation = expectation_from_sidecar(request, decision)
    transaction = decode_signed_transaction(raw_bytes_from_decision(decision))
    rpc = FakeRpc(chain=None)
    broadcaster = LpBroadcaster(policy(), tmp_path / "unreadable.db", rpc=rpc)
    try:
        result = broadcaster.broadcast(transaction.raw, expectation=expectation)
    finally:
        broadcaster.journal.close()
    assert result.outcome == "denied"
    assert result.reasons == ("broadcast endpoint chain is unreadable",)
    assert rpc.sent == []


def test_an_unknown_outcome_is_unresolved_and_never_retried(tmp_path: Path) -> None:
    request = signed_request()
    decision = signed_decision(request)
    expectation = expectation_from_sidecar(request, decision)
    transaction = decode_signed_transaction(raw_bytes_from_decision(decision))
    journal_path = tmp_path / "unknown.db"
    rpc = FakeRpc(error=RpcError("timeout"))
    broadcaster = LpBroadcaster(policy(), journal_path, rpc=rpc)
    try:
        result = broadcaster.broadcast(transaction.raw, expectation=expectation)
        assert result.outcome == "unresolved"
        assert result.reasons == ("broadcast outcome is unknown",)
        assert len(rpc.sent) == 1
        # A second attempt is refused by the journal, not re-sent.
        again = broadcaster.broadcast(transaction.raw, expectation=expectation)
        assert again.outcome == "denied"
        assert again.reasons == ("this transaction is already recorded in the broadcast journal",)
        assert len(rpc.sent) == 1
    finally:
        broadcaster.journal.close()

    journal = LpBroadcastJournal(journal_path, policy=policy())
    try:
        assert journal.open_transactions() == (expectation.transaction_hash,)
        with pytest.raises(ValueError, match="must be 0 or 1"):
            journal.resolve(expectation.transaction_hash, receipt_status=2, block_number=7)
        journal.resolve(expectation.transaction_hash, receipt_status=1, block_number=7)
        assert journal.open_transactions() == ()
        with pytest.raises(ValueError, match="does not name a broadcast"):
            journal.resolve(expectation.transaction_hash, receipt_status=1, block_number=8)
        with pytest.raises(ValueError, match="does not name a broadcast"):
            journal.resolve(OTHER_HASH, receipt_status=1, block_number=8)
    finally:
        journal.close()


def test_a_submission_returning_another_hash_is_unresolved(tmp_path: Path) -> None:
    request = signed_request()
    decision = signed_decision(request)
    expectation = expectation_from_sidecar(request, decision)
    transaction = decode_signed_transaction(raw_bytes_from_decision(decision))
    journal_path = tmp_path / "mismatch.db"
    broadcaster = LpBroadcaster(policy(), journal_path, rpc=FakeRpc(result=OTHER_HASH))
    try:
        result = broadcaster.broadcast(transaction.raw, expectation=expectation)
    finally:
        broadcaster.journal.close()
    assert result.outcome == "unresolved"
    assert result.reasons == ("broadcast returned a different transaction hash",)
    journal = LpBroadcastJournal(journal_path, policy=policy())
    try:
        assert journal.open_transactions() == (expectation.transaction_hash,)
    finally:
        journal.close()


def test_a_malformed_submission_hash_is_unresolved(tmp_path: Path) -> None:
    request = signed_request()
    decision = signed_decision(request)
    expectation = expectation_from_sidecar(request, decision)
    transaction = decode_signed_transaction(raw_bytes_from_decision(decision))
    broadcaster = LpBroadcaster(policy(), tmp_path / "malformed.db", rpc=FakeRpc(result="0x02"))
    try:
        result = broadcaster.broadcast(transaction.raw, expectation=expectation)
    finally:
        broadcaster.journal.close()
    assert result.outcome == "unresolved"
    assert result.reasons == ("broadcast returned a malformed transaction hash",)


def test_the_journal_refuses_a_changed_policy(tmp_path: Path) -> None:
    path = tmp_path / "bound.db"
    LpBroadcastJournal(path, policy=policy()).close()
    with pytest.raises(ValueError, match="policy cannot change"):
        LpBroadcastJournal(path, policy=policy(max_gas_limit=4_000_000))


def test_the_journal_detects_a_tampered_record(tmp_path: Path) -> None:
    request = signed_request()
    decision = signed_decision(request)
    expectation = expectation_from_sidecar(request, decision)
    transaction = decode_signed_transaction(raw_bytes_from_decision(decision))
    path = tmp_path / "tamper.db"
    broadcaster = LpBroadcaster(policy(), path, rpc=FakeRpc(result=expectation.transaction_hash))
    try:
        broadcaster.broadcast(transaction.raw, expectation=expectation)
    finally:
        broadcaster.journal.close()

    connection = sqlite3.connect(path)
    try:
        with connection:
            connection.execute("UPDATE events SET digest='0' || substr(digest, 2)")
    finally:
        connection.close()
    with pytest.raises(ValueError, match="audit chain corrupt"):
        LpBroadcastJournal(path, policy=policy())


def test_a_broadcast_policy_round_trips_and_refuses_incomplete_allowlists() -> None:
    original = policy()
    assert broadcast_policy_from_json(broadcast_policy_to_json(original)) == original
    assert broadcast_policy_hash(original) == broadcast_policy_hash(policy())
    with pytest.raises(ValueError, match="non-empty chain allowlist"):
        policy(chain_ids=frozenset())
    with pytest.raises(ValueError, match="non-empty target allowlist"):
        policy(targets=frozenset())
    with pytest.raises(ValueError, match="invalid fields"):
        broadcast_policy_from_json(broadcast_policy_to_json(original)[:-1] + b',"x":1}')
    with pytest.raises(ValueError, match="positive cap"):
        policy(max_gas_limit=0)


def test_the_http_endpoint_submits_once_without_retrying() -> None:
    calls: list[dict[str, object]] = []
    submitted = "0x" + "ab" * 32

    class Poster:
        def post(self, payload: bytes) -> bytes:
            request = json.loads(payload)
            calls.append(request)
            result = hex(CHAIN_ID) if request["method"] == "eth_chainId" else submitted
            return canonical_json({"jsonrpc": "2.0", "id": request["id"], "result": result})

    rpc = HttpBroadcastRpc("https://fixture.invalid", http=Poster())
    assert rpc.chain_id() == CHAIN_ID
    assert rpc.send_raw_transaction("0x02") == submitted
    assert [call["method"] for call in calls] == ["eth_chainId", "eth_sendRawTransaction"]
    assert calls[1]["params"] == ["0x02"]

    class Broken:
        def post(self, payload: bytes) -> bytes:
            raise OSError("connection refused")

    with pytest.raises(RpcError):
        HttpBroadcastRpc("https://fixture.invalid", http=Broken()).chain_id()


def test_the_broadcast_module_holds_no_signing_capability() -> None:
    source = Path(lp_broadcast.__file__).read_text(encoding="utf-8")
    for forbidden in ("PrivateKey", "sign_transaction", "add_private_key", "environ", "mnemonic"):
        assert forbidden not in source, forbidden
    assert not [name for name in dir(LpBroadcaster) if "sign" in name.lower()]


def _cli_policy_files(
    tmp_path: Path, request: LpSidecarRequest, decision: LpSidecarDecision
) -> tuple[Path, Path, Path]:
    policy_path = tmp_path / "policy.json"
    request_path = tmp_path / "request.json"
    decision_path = tmp_path / "decision.json"
    policy_path.write_bytes(broadcast_policy_to_json(policy()))
    request_path.write_bytes(sidecar_request_to_json(request))
    decision_path.write_bytes(
        canonical_json(
            {
                "schema": SIDE_CAR_DECISION_SCHEMA,
                "outcome": decision.outcome,
                "reasons": list(decision.reasons),
                "request_hash": decision.request_hash,
                "digest": decision.digest,
                "signature_hash": decision.signature_hash,
                "transaction_hash": decision.transaction_hash,
                "raw_transaction_hex": decision.raw_transaction_hex,
            }
        )
        + b"\n"
    )
    return policy_path, request_path, decision_path


def _run_cli(
    command: list[str], *, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        cwd=Path(__file__).parents[2],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
        env=env,
    )


def test_the_cli_verifies_without_an_endpoint_or_a_journal(tmp_path: Path) -> None:
    request = signed_request()
    decision = signed_decision(request)
    policy_path, request_path, decision_path = _cli_policy_files(tmp_path, request, decision)
    out_path = tmp_path / "report.json"
    journal_path = tmp_path / "report.db"
    env = {name: value for name, value in os.environ.items() if name != ENDPOINT_ENV}
    result = _run_cli(
        [
            sys.executable,
            "-m",
            "scripts.run_lp_broadcast",
            "--policy",
            str(policy_path),
            "--request",
            str(request_path),
            "--decision",
            str(decision_path),
            "--journal",
            str(journal_path),
            "--out",
            str(out_path),
        ],
        env=env,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(out_path.read_text())
    assert report["mode"] == "verify"
    assert report["outcome"] == "authorized"
    assert report["reasons"] == []
    assert report["transaction_hash"] == decision.transaction_hash
    assert report["signer"] == ACCOUNT
    assert not journal_path.exists()


def test_the_cli_broadcast_path_refuses_without_a_configured_endpoint(tmp_path: Path) -> None:
    request = signed_request()
    decision = signed_decision(request)
    policy_path, request_path, decision_path = _cli_policy_files(tmp_path, request, decision)
    out_path = tmp_path / "report.json"
    env = {name: value for name, value in os.environ.items() if name != ENDPOINT_ENV}
    result = _run_cli(
        [
            sys.executable,
            "-m",
            "scripts.run_lp_broadcast",
            "--policy",
            str(policy_path),
            "--request",
            str(request_path),
            "--decision",
            str(decision_path),
            "--endpoint-env",
            ENDPOINT_ENV,
            "--broadcast",
            "--out",
            str(out_path),
        ],
        env=env,
    )
    assert result.returncode == 2
    report = json.loads(out_path.read_text())
    assert report["mode"] == "broadcast"
    assert report["outcome"] == "denied"
    assert f"{ENDPOINT_ENV} is not set" in report["reasons"][0]
    assert not out_path.with_suffix(".db").exists()


def test_the_cli_broadcast_path_fails_closed_on_an_unreachable_endpoint(tmp_path: Path) -> None:
    request = signed_request()
    decision = signed_decision(request)
    policy_path, request_path, decision_path = _cli_policy_files(tmp_path, request, decision)
    out_path = tmp_path / "report.json"
    env = {**os.environ, ENDPOINT_ENV: "http://127.0.0.1:1"}
    result = _run_cli(
        [
            sys.executable,
            "-m",
            "scripts.run_lp_broadcast",
            "--policy",
            str(policy_path),
            "--request",
            str(request_path),
            "--decision",
            str(decision_path),
            "--endpoint-env",
            ENDPOINT_ENV,
            "--journal",
            str(tmp_path / "broadcast.db"),
            "--broadcast",
            "--out",
            str(out_path),
        ],
        env=env,
    )
    assert result.returncode == 2
    report = json.loads(out_path.read_text())
    assert report["outcome"] == "denied"
    assert report["reasons"] == ["broadcast endpoint chain is unreadable"]


def test_the_cli_refuses_a_decision_that_was_never_signed(tmp_path: Path) -> None:
    request = signed_request()
    denied = LpSidecarDecision("denied", ("no signing key provider is configured",), "0" * 64)
    policy_path, request_path, decision_path = _cli_policy_files(tmp_path, request, denied)
    out_path = tmp_path / "report.json"
    result = _run_cli(
        [
            sys.executable,
            "-m",
            "scripts.run_lp_broadcast",
            "--policy",
            str(policy_path),
            "--request",
            str(request_path),
            "--decision",
            str(decision_path),
            "--out",
            str(out_path),
        ]
    )
    assert result.returncode == 2
    report = json.loads(out_path.read_text())
    assert report["outcome"] == "denied"
    assert "not broadcastable" in report["reasons"][0]
