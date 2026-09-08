"""WP7: isolated signer sidecar fails closed with no key provider."""

from dataclasses import replace
from pathlib import Path

import pytest
from eth_hash.auto import keccak
from eth_keys import keys
from rlp import decode as rlp_decode

from oasis_agent.execution.lp_signer_policy import (
    LpSignerPolicy,
    LpSigningRequest,
    calldata_hash,
    preflight_evidence_hash,
    simulation_evidence_hash,
)
from oasis_agent.execution.lp_signer_sidecar import (
    LpSidecarConfig,
    LpSidecarRequest,
    LpSignerSidecar,
    NullSigningKeyProvider,
    recover_signer,
    sidecar_config_from_json,
    sidecar_config_to_json,
    sidecar_request_from_json,
    sidecar_request_to_json,
    signed_transaction_bytes,
    transaction_digest,
)
from oasis_agent.models.common import BlockRef, EvidenceRef, normalize_address
from oasis_agent.models.v4 import DYNAMIC_FEE_FLAG, PoolKey
from oasis_agent.uniswap_v4.pool_id import pool_id_from_key
from tests.unit.test_lp_signer_policy import make_calldata
from tests.unit.test_lp_state_machine import make_intent

EXECUTOR = "0x" + "44" * 20
CODE_HASHES = ("0x" + "12" * 32, "0x" + "34" * 32)
CONFIG_HASH = "0x" + "ef" * 32
POOL_KEY = PoolKey("0x" + "11" * 20, "0x" + "22" * 20, DYNAMIC_FEE_FLAG, 60, "0x" + "33" * 20)
MAX_FEE_CAP = 1_000_000_000
PRIORITY_CAP = 100_000_000


class FixtureKeyProvider:
    """Deterministic test provider; signs with the given fixture private key."""

    def __init__(self, private_key: bytes) -> None:
        self._key = keys.PrivateKey(private_key)

    def sign(self, digest: bytes) -> tuple[int, int, int]:
        if len(digest) != 32:
            raise ValueError("provider must sign exactly the 32-byte digest")
        signature = self._key.sign_msg_hash(digest)
        y_parity = signature.v if signature.v in (0, 1) else signature.v - 27
        return signature.r, signature.s, y_parity


def account_for(key: bytes) -> str:
    return keys.PrivateKey(key).public_key.to_address()


def _as_int(field: object) -> int:
    return int.from_bytes(bytes(field), "big")  # type: ignore[arg-type]


def make_request(account: str, *, nonce: int = 91) -> LpSidecarRequest:
    intent = make_intent(
        pool_id=pool_id_from_key(POOL_KEY),
        target=EXECUTOR,
        state_block=100,
        state_hash="0x" + "ab" * 32,
        deadline_timestamp=1_050,
        config_hash=CONFIG_HASH,
        code_hashes=CODE_HASHES,
    )
    calldata = make_calldata(intent)
    intent = replace(intent, calldata_hash=calldata_hash(calldata))
    simulation = BlockRef(4663, 100, "0x" + "ab" * 32)
    request = LpSigningRequest(
        intent=intent,
        account=account,
        target=EXECUTOR,
        calldata=calldata,
        value=0,
        account_nonce=nonce,
        executor_last_nonce=intent.nonce - 1,
        gas_limit=2_000_000,
        current_block=BlockRef(4663, 101, "0x" + "cd" * 32, 1_010),
        simulation_block=simulation,
        simulation_succeeded=True,
        simulation_calldata_hash=calldata_hash(calldata),
        observed_code_hashes=CODE_HASHES,
        risk_approved=True,
        risk_decision_hash="0x" + "67" * 32,
        evidence=(),
    )
    simulation_references = tuple(
        EvidenceRef(
            "pinned-simulation",
            simulation_evidence_hash(request),
            simulation,
            source,
            "2026-09-07T00:00:00+00:00",
            "2" * 64,
            raw,
            "4" * 64,
        )
        for source, raw in (("rpc-a", "3" * 64), ("rpc-b", "5" * 64))
    )
    preflight_references = tuple(
        EvidenceRef(
            "execution-preflight",
            preflight_evidence_hash(request),
            request.current_block,
            source,
            "2026-09-07T00:00:00+00:00",
            "a" * 64,
            raw,
            "b" * 64,
        )
        for source, raw in (("rpc-a", "c" * 64), ("rpc-b", "d" * 64))
    )
    references = (
        simulation_references
        + preflight_references
        + (
            EvidenceRef(
                "risk-decision",
                "67" * 32,
                simulation,
                "risk-engine",
                "2026-09-07T00:00:00+00:00",
                "6" * 64,
                "7" * 64,
                "8" * 64,
            ),
        )
    )
    request = replace(request, evidence=references)
    return LpSidecarRequest(
        signing=request,
        max_fee_per_gas=500_000_000,
        max_priority_fee_per_gas=50_000_000,
    )


def make_policy(account: str) -> LpSignerPolicy:
    intent = make_request(account).signing.intent
    return LpSignerPolicy(
        4663,
        account,
        EXECUTOR,
        intent.pool_id,
        CONFIG_HASH,
        CODE_HASHES,
        1_000_000,
        2_000_000,
        3_000_000,
        3_000_000,
        50_000,
        3,
        60,
    )


def make_sidecar(tmp_path: Path, account: str, *, provider=None) -> LpSignerSidecar:
    config = LpSidecarConfig(
        policy=make_policy(account),
        max_fee_per_gas=MAX_FEE_CAP,
        max_priority_fee_per_gas=PRIORITY_CAP,
        journal_path=tmp_path / "sidecar.db",
        provider=provider,
    )
    return LpSignerSidecar(config)


KEY = bytes(range(1, 33))
OTHER_KEY = bytes(range(33, 65))
ACCOUNT = account_for(KEY)


def _events(db_path: Path) -> list[tuple]:
    import sqlite3

    connection = sqlite3.connect(db_path)
    try:
        return connection.execute("SELECT seq, payload, digest FROM events ORDER BY seq").fetchall()
    finally:
        connection.close()


def test_no_provider_denies_an_otherwise_valid_request(tmp_path):
    sidecar = make_sidecar(tmp_path, ACCOUNT)
    with sidecar:
        decision = sidecar.handle(make_request(ACCOUNT))
    assert decision.outcome == "denied"
    assert "no signing key provider is configured" in decision.reasons
    assert decision.raw_transaction_hex is None
    assert decision.transaction_hash is None
    rows = _events(tmp_path / "sidecar.db")
    assert len(rows) == 1
    assert b'"denied"' in rows[0][1]


def test_null_provider_fails_closed(tmp_path):
    sidecar = make_sidecar(tmp_path, ACCOUNT, provider=NullSigningKeyProvider())
    with sidecar:
        decision = sidecar.handle(make_request(ACCOUNT))
    assert decision.outcome == "denied"
    assert "signing provider failed" in decision.reasons


def test_valid_request_with_provider_signs_and_recovers(tmp_path):
    sidecar = make_sidecar(tmp_path, ACCOUNT, provider=FixtureKeyProvider(KEY))
    request = make_request(ACCOUNT)
    with sidecar:
        decision = sidecar.handle(request)
    assert decision.outcome == "signed"
    assert not decision.reasons
    assert decision.raw_transaction_hex is not None
    raw = bytes.fromhex(decision.raw_transaction_hex)
    assert normalize_address(recover_signer(raw)) == ACCOUNT
    assert decision.transaction_hash == "0x" + keccak(raw).hex()
    assert decision.digest == "0x" + transaction_digest(request).hex()
    assert decision.signature_hash is not None and len(decision.signature_hash) == 66
    fields = rlp_decode(raw[1:])
    assert len(fields) == 12
    assert _as_int(fields[0]) == 4663
    assert _as_int(fields[1]) == request.signing.account_nonce
    assert _as_int(fields[2]) == request.max_priority_fee_per_gas
    assert _as_int(fields[3]) == request.max_fee_per_gas
    assert _as_int(fields[4]) == request.signing.gas_limit
    assert bytes(fields[5]).hex() == request.signing.target[2:].lower()
    assert _as_int(fields[6]) == 0
    assert bytes(fields[7]) == request.signing.calldata
    assert fields[8] == []


def test_provider_receives_only_the_bound_digest(tmp_path):
    seen = []

    class CapturingProvider:
        def sign(self, digest: bytes) -> tuple[int, int, int]:
            seen.append(digest)
            return FixtureKeyProvider(KEY).sign(digest)

    sidecar = make_sidecar(tmp_path, ACCOUNT, provider=CapturingProvider())
    request = make_request(ACCOUNT)
    with sidecar:
        decision = sidecar.handle(request)
    assert decision.outcome == "signed"
    assert len(seen) == 1
    assert seen[0] == transaction_digest(request)


def test_wrong_provider_key_is_rejected(tmp_path):
    sidecar = make_sidecar(tmp_path, ACCOUNT, provider=FixtureKeyProvider(OTHER_KEY))
    with sidecar:
        decision = sidecar.handle(make_request(ACCOUNT))
    assert decision.outcome == "denied"
    assert any("does not recover" in reason for reason in decision.reasons)
    assert decision.raw_transaction_hex is None
    rows = _events(tmp_path / "sidecar.db")
    assert len(rows) == 1
    assert b'"denied"' in rows[0][1]


def test_high_s_signature_is_rejected(tmp_path):
    order = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141

    class HighSProvider:
        def sign(self, digest: bytes) -> tuple[int, int, int]:
            r, s, y = FixtureKeyProvider(KEY).sign(digest)
            return r, order - s, y  # flip to high-s

    sidecar = make_sidecar(tmp_path, ACCOUNT, provider=HighSProvider())
    with sidecar:
        decision = sidecar.handle(make_request(ACCOUNT))
    assert decision.outcome == "denied"
    assert any("high-s" in reason for reason in decision.reasons)


def test_stale_or_replayed_account_nonce_is_denied(tmp_path):
    sidecar = make_sidecar(tmp_path, ACCOUNT, provider=FixtureKeyProvider(KEY))
    request = make_request(ACCOUNT)
    with sidecar:
        first = sidecar.handle(request)
        replay = sidecar.handle(request)
    assert first.outcome == "signed"
    assert replay.outcome == "denied"
    assert any("already signed" in reason for reason in replay.reasons)
    assert len(_events(tmp_path / "sidecar.db")) == 2


def test_concurrent_handles_never_double_sign_the_same_nonce(tmp_path):
    import threading

    sidecar = make_sidecar(tmp_path, ACCOUNT, provider=FixtureKeyProvider(KEY))
    request = make_request(ACCOUNT)  # every worker offers the same account nonce
    outcomes: list = []
    errors: list = []

    def worker() -> None:
        try:
            outcomes.append(sidecar.handle(request))
        # The point of this test is that worker failures are collected and
        # asserted, so the catch must stay maximally broad.
        except Exception as exc:  # noqa: BLE001 - worker failures must surface
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    with sidecar:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    assert not errors
    signed = [item for item in outcomes if item.outcome == "signed"]
    denied = [item for item in outcomes if item.outcome == "denied"]
    assert len(signed) == 1
    assert len(denied) == 7
    assert all(any("already signed" in reason for reason in item.reasons) for item in denied)
    assert all(item.raw_transaction_hex is None for item in denied)
    assert len(_events(tmp_path / "sidecar.db")) == 8


def test_two_sidecar_instances_reserve_before_calling_provider(tmp_path):
    import threading

    started = threading.Event()
    release = threading.Event()
    first_outcome = []

    class BlockingProvider:
        def sign(self, digest: bytes) -> tuple[int, int, int]:
            started.set()
            assert release.wait(timeout=5)
            return FixtureKeyProvider(KEY).sign(digest)

    class UnexpectedProvider:
        calls = 0

        def sign(self, digest: bytes) -> tuple[int, int, int]:
            self.calls += 1
            return FixtureKeyProvider(KEY).sign(digest)

    first = make_sidecar(tmp_path, ACCOUNT, provider=BlockingProvider())
    unexpected = UnexpectedProvider()
    second = make_sidecar(tmp_path, ACCOUNT, provider=unexpected)
    request = make_request(ACCOUNT)
    worker = threading.Thread(target=lambda: first_outcome.append(first.handle(request)))
    try:
        worker.start()
        assert started.wait(timeout=5)
        second_outcome = second.handle(request)
        release.set()
        worker.join(timeout=5)
    finally:
        release.set()
        worker.join(timeout=5)
        first.journal.close()
        second.journal.close()
    assert len(first_outcome) == 1 and first_outcome[0].outcome == "signed"
    assert second_outcome.outcome == "denied"
    assert unexpected.calls == 0
    assert any("unresolved signing attempt" in item for item in second_outcome.reasons)


def test_provider_failure_latches_nonce_and_blocks_later_signing(tmp_path):
    sidecar = make_sidecar(tmp_path, ACCOUNT, provider=NullSigningKeyProvider())
    with sidecar:
        failed = sidecar.handle(make_request(ACCOUNT, nonce=91))
        later = sidecar.handle(make_request(ACCOUNT, nonce=92))
    assert failed.outcome == "denied"
    assert "signing provider failed" in failed.reasons
    assert later.outcome == "denied"
    assert any("unresolved signing attempt" in item for item in later.reasons)


def test_sequential_distinct_nonces_each_sign_once(tmp_path):
    sidecar = make_sidecar(tmp_path, ACCOUNT, provider=FixtureKeyProvider(KEY))
    with sidecar:
        first = sidecar.handle(make_request(ACCOUNT, nonce=91))
        second = sidecar.handle(make_request(ACCOUNT, nonce=92))
        third = sidecar.handle(make_request(ACCOUNT, nonce=93))
    assert (first.outcome, second.outcome, third.outcome) == ("signed", "signed", "signed")
    assert len(_events(tmp_path / "sidecar.db")) == 3


def test_policy_violation_denied_even_with_provider(tmp_path):
    sidecar = make_sidecar(tmp_path, ACCOUNT, provider=FixtureKeyProvider(KEY))
    request = replace(
        make_request(ACCOUNT), signing=replace(make_request(ACCOUNT).signing, value=1)
    )
    with sidecar:
        decision = sidecar.handle(request)
    assert decision.outcome == "denied"
    assert any("value" in reason for reason in decision.reasons)
    assert decision.raw_transaction_hex is None


def test_fee_cap_violation_denied(tmp_path):
    sidecar = make_sidecar(tmp_path, ACCOUNT, provider=FixtureKeyProvider(KEY))
    request = replace(make_request(ACCOUNT), max_fee_per_gas=MAX_FEE_CAP + 1)
    with sidecar:
        decision = sidecar.handle(request)
    assert decision.outcome == "denied"
    assert any("exceeds the sidecar cap" in reason for reason in decision.reasons)


def test_journal_binds_policy_and_config(tmp_path):
    sidecar = make_sidecar(tmp_path, ACCOUNT, provider=FixtureKeyProvider(KEY))
    with sidecar:
        sidecar.handle(make_request(ACCOUNT))
    changed = LpSidecarConfig(
        policy=make_policy(ACCOUNT),
        max_fee_per_gas=MAX_FEE_CAP * 2,
        max_priority_fee_per_gas=PRIORITY_CAP,
        journal_path=tmp_path / "sidecar.db",
        provider=FixtureKeyProvider(KEY),
    )
    with pytest.raises(ValueError, match="policy cannot change"):
        LpSignerSidecar(changed)


def test_journal_detects_audit_chain_tampering(tmp_path):
    import sqlite3

    sidecar = make_sidecar(tmp_path, ACCOUNT, provider=FixtureKeyProvider(KEY))
    with sidecar:
        sidecar.handle(make_request(ACCOUNT))
    connection = sqlite3.connect(tmp_path / "sidecar.db")
    payload, _ = connection.execute("SELECT payload, digest FROM events WHERE seq = 1").fetchone()
    tampered = payload.replace(b'"signed"', b'"denied"')
    connection.execute("UPDATE events SET payload = ? WHERE seq = 1", (tampered,))
    connection.commit()
    connection.close()
    with pytest.raises(ValueError, match="audit chain corrupt"):
        make_sidecar(tmp_path, ACCOUNT, provider=FixtureKeyProvider(KEY))


def test_signed_journal_restarts_with_the_same_policy(tmp_path):
    sidecar = make_sidecar(tmp_path, ACCOUNT, provider=FixtureKeyProvider(KEY))
    with sidecar:
        assert sidecar.handle(make_request(ACCOUNT)).outcome == "signed"
    restarted = make_sidecar(tmp_path, ACCOUNT, provider=FixtureKeyProvider(KEY))
    with restarted:
        assert restarted.journal.max_signed_nonce == 91
        assert restarted.handle(make_request(ACCOUNT)).outcome == "denied"


def test_json_round_trip_request_and_config(tmp_path):
    request = make_request(ACCOUNT)
    decoded = sidecar_request_from_json(sidecar_request_to_json(request))
    assert decoded == request
    assert decoded.signing.intent == request.signing.intent
    config = LpSidecarConfig(
        policy=make_policy(ACCOUNT),
        max_fee_per_gas=MAX_FEE_CAP,
        max_priority_fee_per_gas=PRIORITY_CAP,
        journal_path=tmp_path / "sidecar.db",
    )
    restored = sidecar_config_from_json(
        sidecar_config_to_json(config), journal_path=tmp_path / "sidecar.db"
    )
    assert restored == config


def test_json_decoders_reject_duplicate_and_unknown_fields(tmp_path):
    with pytest.raises(ValueError, match="duplicate JSON key"):
        sidecar_config_from_json(
            b'{"policy":{},"policy":{},"max_fee_per_gas":1,"max_priority_fee_per_gas":1}',
            journal_path=tmp_path / "x.db",
        )
    payload = sidecar_request_to_json(make_request(ACCOUNT))
    changed = payload[:-1] + b',"unexpected":true}'
    with pytest.raises(ValueError, match="invalid fields"):
        sidecar_request_from_json(changed)


def test_signed_transaction_rejects_out_of_range_signature():
    request = make_request(ACCOUNT)
    with pytest.raises(ValueError, match="outside the secp256k1 range"):
        signed_transaction_bytes(request, r=0, s=1, y_parity=0)
    with pytest.raises(ValueError, match="y_parity"):
        signed_transaction_bytes(request, r=1, s=1, y_parity=2)


def test_cli_demonstrates_default_deny_without_provider(tmp_path):
    import subprocess
    import sys

    request = make_request(ACCOUNT)
    config = LpSidecarConfig(
        policy=make_policy(ACCOUNT),
        max_fee_per_gas=MAX_FEE_CAP,
        max_priority_fee_per_gas=PRIORITY_CAP,
        journal_path=tmp_path / "cli-sidecar.db",
    )
    config_path = tmp_path / "config.json"
    request_path = tmp_path / "request.json"
    out_path = tmp_path / "decision.json"
    config_path.write_bytes(sidecar_config_to_json(config))
    request_path.write_bytes(sidecar_request_to_json(request))
    command = [
        sys.executable,
        "-m",
        "scripts.run_lp_signer_sidecar",
        "--config",
        str(config_path),
        "--request",
        str(request_path),
        "--out",
        str(out_path),
    ]
    root = Path(__file__).parents[2]
    result = subprocess.run(
        command, cwd=root, capture_output=True, text=True, timeout=20, check=False
    )
    assert result.returncode == 0, result.stderr
    import json

    decision = json.loads(out_path.read_text())
    assert decision["outcome"] == "denied"
    assert decision["raw_transaction_hex"] is None
    assert "no signing key provider is configured" in decision["reasons"]
