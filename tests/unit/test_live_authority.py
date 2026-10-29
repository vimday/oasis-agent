"""Full policy admission over synthetic raw RPC, with ephemeral test signing only."""

import json
import tempfile
import threading
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest
from eth_account import Account
from eth_hash.auto import keccak

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.execution.lp_signer_daemon import prepare_endpoint
from oasis_agent.live import authority as authority_module
from oasis_agent.live import runner as runner_module
from oasis_agent.live.authority import TransactionAuthority
from oasis_agent.live.authority_socket import (
    SCHEMA,
    TransactionSocketClient,
    fields_json,
    parse_request,
    serve_connections,
)
from oasis_agent.live.chain import ChainError, Rpc, _envelope
from oasis_agent.live.intent import SubmittedTransaction
from oasis_agent.live.receipts import RetainedReceiptCostReader
from oasis_agent.live.runner import LiveLoop
from oasis_agent.live.state import StateStore
from oasis_agent.live.v4lp import erc20_approve_call
from oasis_agent.models.common import canonical_json
from oasis_agent.reporting import live_release as release_module
from oasis_agent.reporting.live_release import require_live_action
from tests.unit import test_live_receipt_costs as cost_fixtures
from tests.unit import test_live_revert_cash as wallet_fixtures
from tests.unit.test_live_valuation import NavFixture, synthetic_feed_pin  # noqa: F401
from tests.unit.test_lp_receipts import PRIMARY, SECONDARY, sessions


class Key:
    def __init__(self) -> None:
        self._key = Account.create()
        self.account = self._key.address.lower()
        self.calls = 0
        self.fail = False

    def sign(self, digest: bytes) -> tuple[int, int, int]:
        self.calls += 1
        if self.fail:
            raise RuntimeError("synthetic lost key-provider response")
        signed = Account.unsafe_sign_hash(digest, self._key.key)
        return signed.r, signed.s, signed.v - 27


@dataclass
class AuthorityFixture(NavFixture):
    bad_simulation: bool = False
    stale_nonce: bool = False
    l1_fee: int = 0

    def post(self, payload: bytes) -> bytes:
        req = json.loads(payload)
        method, params = req["method"], req["params"]
        result = None
        if method == "eth_estimateGas":
            result = hex(80_000)
        elif method == "eth_call" and "from" in params[0]:
            result = "0xdead" if self.bad_simulation else "0x" + f"{1:064x}"
        elif (
            method == "eth_call" and params[0]["to"] == "0x420000000000000000000000000000000000000f"
        ):
            result = "0x" + f"{self.l1_fee:064x}"
        elif (
            method == "eth_getTransactionCount" and isinstance(params[-1], str) and self.stale_nonce
        ):
            result = hex(self.nonce_after + 1)
        if result is not None:
            return canonical_json({"jsonrpc": "2.0", "id": req["id"], "result": result})
        return super().post(payload)


class DirectRpc(Rpc):
    def __init__(self, fixture: NavFixture) -> None:
        self.fixture = fixture
        self.bad_simulation = False
        self.stale_nonce = False

    def call(self, method: str, params: list[Any]) -> Any:
        if method == "eth_getBlockByNumber" and params[0] == "latest":
            params = [hex(self.fixture.block_number), False]
        if method == "eth_call" and "from" in params[0]:
            return "0x" if self.bad_simulation else "0x" + f"{1:064x}"
        if method == "eth_estimateGas":
            return hex(80_000)
        if method == "eth_getTransactionCount" and self.stale_nonce:
            return hex(self.fixture.nonce_after + 1)
        if method == "eth_blockNumber":
            return hex(self.fixture.block_number + 10)
        raw = self.fixture.post(
            canonical_json({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        )
        return json.loads(raw)["result"]


def setup_authority(
    tmp_path: Path,
    store: EvidenceStore,
    monkeypatch: pytest.MonkeyPatch,
    *,
    feed_ages: tuple[int, int] = (10, 10),
):
    # Synthetic admission exercises later policy checks without enabling production.
    monkeypatch.setattr(authority_module, "require_live_action", lambda *_: None)
    key = Key()
    base = cost_fixtures.position_config()
    pins = {
        name: (value if name == "usdc_implementation" else "0x" + keccak(b"\x60\x00").hex())
        for name, value in base.expected_pins.items()
    }
    config = replace(base, account=key.account, expected_pins=pins)
    monkeypatch.setattr(cost_fixtures, "CONFIG", config)
    monkeypatch.setattr(wallet_fixtures, "CONFIG", config)
    f = AuthorityFixture(
        position_enabled=False,
        payer=key.account,
        position_owner=key.account,
        native_after=2 * 10**17,
        token_after=0,
        usdc_price=10**8,
        eth_age=feed_ages[0],
        usdc_age=feed_ages[1],
    )
    # Test clock is pinned to synthetic raw headers, never a production clock override.
    monkeypatch.setattr(authority_module.time, "time", lambda: f.timestamp)
    loop = LiveLoop.recovery(config, DirectRpc(f), StateStore(tmp_path / "authority.json"), "live")
    peer = replace(f)
    loop.secondary = DirectRpc(peer)
    loop.receipt_costs = RetainedReceiptCostReader(
        store,
        chain_id=config.chain_id,
        account=config.account,
        native_decimals=18,
        sources=(PRIMARY, SECONDARY),
        sessions=sessions({PRIMARY: f, SECONDARY: peer}),
    )
    authority = TransactionAuthority(loop, key)
    fields = _envelope(
        config.chain_id,
        4,
        config.priority_fee_wei,
        config.max_fee_per_gas_wei,
        100_000,
        config.pool.currency1,
        0,
        erc20_approve_call(config.permit2, config.approval_usdc),
    )
    context = {
        "kind": "approve_usdc",
        "intent": {"amount": config.approval_usdc},
        "block": f.block_number,
        "block_hash": f.block_hash,
    }
    return authority, key, fields, context


def close_release(monkeypatch):
    """Restore the real gate with no acceptance permit."""
    monkeypatch.setattr(release_module, "ACCEPTANCE_PERMIT", None)
    monkeypatch.setattr(authority_module, "require_live_action", require_live_action)


def test_authority_enforces_real_release_before_state_or_rpc(tmp_path, monkeypatch):
    with closing(EvidenceStore(tmp_path / "evidence.db")) as store:
        authority, key, fields, context = setup_authority(tmp_path, store, monkeypatch)
        close_release(monkeypatch)

        def forbidden(*args, **kwargs):
            pytest.fail("closed release accessed state or RPC")

        monkeypatch.setattr(authority.loop.store, "load", forbidden)
        monkeypatch.setattr(authority.loop.store, "save", forbidden)
        monkeypatch.setattr(authority.loop.rpc, "call", forbidden)
        with pytest.raises(ValueError, match="live release blocked"):
            authority.sign(fields, context)
        assert key.calls == 0


def test_authority_rechecks_release_after_reservation_before_key(tmp_path, monkeypatch):
    with closing(EvidenceStore(tmp_path / "evidence.db")) as store:
        authority, key, fields, context = setup_authority(tmp_path, store, monkeypatch)
        state_store = authority.loop.store
        save = state_store.save

        def save_then_close_release(state):
            save(state)
            if state.signing_attempt is not None:
                close_release(monkeypatch)

        monkeypatch.setattr(state_store, "save", save_then_close_release)
        with pytest.raises(ValueError, match="live release blocked"):
            authority.sign(fields, context)
        assert key.calls == 0
        saved = state_store.load("live")
        assert saved.signing_attempt is not None and saved.pending is None
        # Read-only recovery is still available while new signing is refused.
        assert authority.status() is not None


def test_authority_refuses_unreported_position_before_key_or_reservation(tmp_path, monkeypatch):
    with closing(EvidenceStore(tmp_path / "evidence.db")) as store:
        authority, key, fields, context = setup_authority(tmp_path, store, monkeypatch)
        authority.loop.secondary.fixture.nft_count = 1
        with pytest.raises(ChainError, match="position inventory"):
            authority.sign(fields, context)
        assert key.calls == 0
        assert authority.loop.state.signing_attempt is None


@pytest.mark.parametrize("feed", [0, 1])
def test_observation_refuses_price_expired_during_capture(tmp_path, monkeypatch, feed):
    c = cost_fixtures.CONFIG
    ages = (c.oracle_max_age_seconds, 10) if feed == 0 else (10, c.usdc_oracle_max_age_seconds)
    with closing(EvidenceStore(tmp_path / "evidence.db")) as store:
        authority, key, _, _ = setup_authority(tmp_path, store, monkeypatch, feed_ages=ages)
        capture = runner_module.capture_nav
        timestamp = authority.loop.rpc.fixture.timestamp

        def capture_then_advance(*args, **kwargs):
            result = capture(*args, **kwargs)
            monkeypatch.setattr(authority_module.time, "time", lambda: timestamp + 1)
            return result

        monkeypatch.setattr(runner_module, "capture_nav", capture_then_advance)
        with pytest.raises(ChainError, match="expired"):
            authority.loop.observe(
                authority.loop.rpc.call("eth_getBlockByNumber", ["latest", False])
            )
        assert key.calls == 0


@pytest.mark.parametrize("feed", [0, 1])
@pytest.mark.parametrize("advance", [1, 2])
def test_authority_checks_each_feed_at_key_use(tmp_path, monkeypatch, feed, advance):
    c = cost_fixtures.CONFIG
    ages = (
        (c.oracle_max_age_seconds - 1, 10) if feed == 0 else (10, c.usdc_oracle_max_age_seconds - 1)
    )
    with closing(EvidenceStore(tmp_path / "evidence.db")) as store:
        authority, key, fields, context = setup_authority(
            tmp_path, store, monkeypatch, feed_ages=ages
        )
        state_store = authority.loop.store
        save = state_store.save
        timestamp = authority.loop.rpc.fixture.timestamp

        def save_then_advance(state):
            save(state)
            if state.signing_attempt is not None:
                monkeypatch.setattr(authority_module.time, "time", lambda: timestamp + advance)

        monkeypatch.setattr(state_store, "save", save_then_advance)
        if advance == 1:
            authority.sign(fields, context)
            assert key.calls == 1
            assert state_store.load("live").pending is not None
        else:
            with pytest.raises(ValueError, match="authority expired before key use"):
                authority.sign(fields, context)
            assert key.calls == 0
            saved = state_store.load("live")
            assert saved.signing_attempt is not None and saved.pending is None


@pytest.mark.parametrize("permit_nonce", [4, 3])
def test_acceptance_permit_signs_only_at_its_captured_nonce(tmp_path, monkeypatch, permit_nonce):
    with closing(EvidenceStore(tmp_path / "evidence.db")) as store:
        authority, key, fields, context = setup_authority(tmp_path, store, monkeypatch)
        # Real gate; the fixture account is at nonce 4.
        monkeypatch.setattr(authority_module, "require_live_action", require_live_action)
        monkeypatch.setattr(
            release_module,
            "ACCEPTANCE_PERMIT",
            release_module.AcceptancePermit(
                kind="approve_usdc", account_nonce=permit_nonce, authorized_by="test"
            ),
        )
        if permit_nonce == 4:
            tx = SubmittedTransaction.from_signed(authority.sign(fields, context))
            assert tx.nonce == 4 and key.calls == 1
        else:
            with pytest.raises(ValueError, match="live release blocked"):
                authority.sign(fields, context)
            assert key.calls == 0
            assert authority.loop.store.load("live").signing_attempt is None


def test_authority_rederives_action_and_journals_actual_signature_before_return(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with closing(EvidenceStore(tmp_path / "evidence.db")) as store:
        authority, key, fields, context = setup_authority(tmp_path, store, monkeypatch)
        raw = authority.sign(fields, context)
        tx = SubmittedTransaction.from_signed(raw)
        assert tx.account == key.account and tx.nonce == 4 and key.calls == 1
        saved = StateStore(authority.loop.store.path).load("live")
        assert saved.signing_attempt is None and saved.pending.transaction == tx
        assert len(saved.pending.wallet_evidence) == 2
        with pytest.raises((ValueError, RuntimeError)):
            authority.sign(fields, context)
        assert key.calls == 1


@pytest.mark.parametrize(
    "fault",
    [
        "chain",
        "nonce",
        "fees",
        "gas",
        "value",
        "target",
        "calldata",
        "recipient",
        "amount",
        "kind",
        "access",
        "bool",
        "simulation",
        "pending_nonce",
        "loss",
        "halt",
        "unknown",
        "empty_unknown",
        "expired",
        "l1_fee",
        "l1_disagreement",
    ],
)
def test_malicious_or_stale_proposals_never_reach_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    with closing(EvidenceStore(tmp_path / "evidence.db")) as store:
        authority, key, fields, context = setup_authority(tmp_path, store, monkeypatch)
        if fault == "chain":
            fields[0] = 1
        if fault == "nonce":
            fields[1] = 5
        if fault == "fees":
            fields[3] += 1
        if fault == "gas":
            fields[4] = 2_000_000
        if fault == "value":
            fields[6] = 1
        if fault == "target":
            fields[5] = b"\x11" * 20
        if fault == "calldata":
            fields[7] += b"\x00"
        if fault == "recipient":
            fields[7] = erc20_approve_call("0x" + "11" * 20, context["intent"]["amount"])
        if fault == "amount":
            context["intent"]["amount"] -= 1
        if fault == "kind":
            context["kind"] = "swap"
        if fault == "access":
            fields[8] = [b"unexpected"]
        if fault == "bool":
            fields[1] = True
        if fault == "simulation":
            authority.loop.secondary.fixture.bad_simulation = True
        if fault == "pending_nonce":
            authority.loop.secondary.fixture.stale_nonce = True
        if fault == "expired":
            authority.expires = 0
        if fault == "l1_fee":
            authority.loop.rpc.fixture.l1_fee = authority.loop.c.gas_reserve_wei
            authority.loop.secondary.fixture.l1_fee = authority.loop.c.gas_reserve_wei
        if fault == "l1_disagreement":
            authority.loop.secondary.fixture.l1_fee = 1
        if fault in {"loss", "halt", "unknown", "empty_unknown"}:
            s = authority.loop.state
            if fault == "loss":
                s.integer_risk().observe(day="2026-10-15", at=0, nav=2_000_000_000)
            if fault == "halt":
                s.halted = "operator halt"
            if fault == "unknown":
                s.signing_attempt = {"nonce": 4}
            if fault == "empty_unknown":
                s.signing_attempt = {}
            authority.loop.store.save(s)
        with pytest.raises((ValueError, RuntimeError)):
            authority.sign(fields, context)
        assert key.calls == 0
        if fault in {"unknown", "empty_unknown"}:
            saved = authority.loop.store.load("live")
            assert saved.signing_attempt == ({} if fault == "empty_unknown" else {"nonce": 4})
            assert saved.pending is None


@pytest.mark.parametrize("fault", ["future", "stale", "expired", "stop"])
def test_authority_rechecks_before_key_after_durable_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    with closing(EvidenceStore(tmp_path / "evidence.db")) as store:
        authority, key, fields, context = setup_authority(tmp_path, store, monkeypatch)
        state_store = authority.loop.store
        save = state_store.save
        timestamp = authority.loop.rpc.fixture.timestamp
        authority.loop.stop_file = tmp_path / "STOP"

        def save_then_change_conditions(state):
            save(state)
            if state.signing_attempt is None:
                return
            if fault in {"future", "stale"}:
                now = timestamp + (-1 if fault == "future" else 121)
                monkeypatch.setattr(authority_module.time, "time", lambda: now)
            elif fault == "expired":
                authority.expires = 0
            else:
                authority.loop.stop_file.touch()

        monkeypatch.setattr(state_store, "save", save_then_change_conditions)
        with pytest.raises(ValueError, match="authority expired before key use"):
            authority.sign(fields, context)
        assert key.calls == 0
        reserved = state_store.load("live")
        assert reserved.pending is None
        assert reserved.signing_attempt is not None
        assert len(reserved.signing_attempt["simulation_evidence"]) == 2
        # Restoring normal conditions must not erase an unresolved reservation.
        monkeypatch.setattr(authority_module.time, "time", lambda: timestamp)
        authority.expires = authority_module.time.monotonic() + 60
        authority.loop.stop_file.unlink(missing_ok=True)
        monkeypatch.setattr(state_store, "save", save)
        with pytest.raises(ValueError, match="signing outcome unresolved"):
            authority.sign(fields, context)
        assert key.calls == 0
        assert state_store.load("live").signing_attempt == reserved.signing_attempt


@pytest.mark.parametrize("age", [0, 120])
def test_authority_accepts_final_freshness_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, age: int
) -> None:
    with closing(EvidenceStore(tmp_path / "evidence.db")) as store:
        authority, key, fields, context = setup_authority(tmp_path, store, monkeypatch)
        state_store = authority.loop.store
        save = state_store.save
        timestamp = authority.loop.rpc.fixture.timestamp

        def save_then_advance_clock(state):
            save(state)
            if state.signing_attempt is not None:
                monkeypatch.setattr(authority_module.time, "time", lambda: timestamp + age)

        monkeypatch.setattr(state_store, "save", save_then_advance_clock)
        tx = SubmittedTransaction.from_signed(authority.sign(fields, context))
        assert key.calls == 1 and tx.nonce == fields[1]
        saved = state_store.load("live")
        assert saved.pending.transaction == tx and saved.signing_attempt is None


def test_key_response_loss_retains_reservation_and_cannot_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with closing(EvidenceStore(tmp_path / "evidence.db")) as store:
        authority, key, fields, context = setup_authority(tmp_path, store, monkeypatch)
        key.fail = True
        with pytest.raises(RuntimeError):
            authority.sign(fields, context)
        assert StateStore(authority.loop.store.path).load("live").signing_attempt is not None
        with pytest.raises(ValueError):
            authority.sign(fields, context)
        assert key.calls == 1


def test_socket_protocol_requires_canonical_full_transaction_not_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with closing(EvidenceStore(tmp_path / "evidence.db")) as store:
        _, key, fields, context = setup_authority(tmp_path, store, monkeypatch)
        raw = canonical_json(
            {
                "schema": SCHEMA,
                "account": key.account,
                "fields": fields_json(fields),
                "context": context,
            }
        )
        assert parse_request(raw) == (key.account, fields, context)
        with pytest.raises(ValueError):
            parse_request(raw + b" ")
        with pytest.raises(ValueError):
            parse_request(canonical_json({"schema": SCHEMA, "digest": "0x" + "00" * 32}))
        with pytest.raises(ValueError):
            parse_request(b"x" * 32769)


def test_full_transaction_authority_over_private_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with closing(EvidenceStore(tmp_path / "evidence.db")) as store:
        authority, key, fields, context = setup_authority(tmp_path, store, monkeypatch)
        with tempfile.TemporaryDirectory(prefix="oa-auth-", dir=Path("/tmp").resolve()) as name:
            directory = Path(name)
            directory.chmod(0o700)
            endpoint = directory / "a.sock"
            received = []
            failures = []

            def client():
                try:
                    socket_client = TransactionSocketClient(endpoint, key.account, timeout=10)
                    status = socket_client.pending_status()
                    assert status["pending"] is None and not status["signing_unresolved"]
                    assert key.calls == 0
                    received.append(socket_client.sign_envelope(fields, context))
                except (OSError, ValueError, RuntimeError) as exc:
                    failures.append(type(exc).__name__)

            with prepare_endpoint(endpoint) as server:
                worker = threading.Thread(target=client)
                worker.start()
                count = serve_connections(
                    server, authority, account=key.account, max_signatures=1, ttl_seconds=10
                )
                worker.join(timeout=10)
            assert count == 1 and not failures and not worker.is_alive()
            assert len(received) == 1
            assert SubmittedTransaction.from_signed(received[0]).account == key.account
            assert StateStore(authority.loop.store.path).load("live").pending is not None
