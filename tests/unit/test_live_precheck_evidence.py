"""Operational CLI diagnostics replay retained proofs without altering their store."""

import fcntl
import json
import sqlite3
from contextlib import closing
from dataclasses import asdict, replace

import pytest

from oasis_agent.evidence.store import EvidenceStore, ReadOnlyEvidenceStore
from oasis_agent.live.precheck_evidence import PrecheckEvidenceStore, precheck_evidence
from oasis_agent.live.runner import LiveLoop
from oasis_agent.live.state import LoopState, StateStore, day_of
from oasis_agent.live.valuation import capture_nav
from oasis_agent.models.common import BlockRef, canonical_json, sha256_hex
from oasis_agent.rpc.budget import RpcBudget
from scripts import run_live_lp as cli
from tests.unit.test_live_lp import market
from tests.unit.test_live_precheck import setup_budgeted_cli
from tests.unit.test_live_receipt_costs import position_config
from tests.unit.test_live_valuation import NavFixture, synthetic_feed_pin  # noqa: F401
from tests.unit.test_lp_receipts import sessions

SOURCES = ("live_primary", "live_secondary")


def capture(store, config, fixture):
    return capture_nav(
        store,
        config,
        BlockRef(config.chain_id, fixture.block_number, fixture.block_hash, fixture.timestamp),
        None,
        sources=SOURCES,
        sessions=sessions(dict(zip(SOURCES, (fixture, replace(fixture)), strict=True))),
    )


@pytest.mark.parametrize("fault", [None, "missing", "corrupt"])
def test_cli_replays_retained_nav_before_ephemeral_observation(
    tmp_path, monkeypatch, capsys, fault
):
    profile, http = setup_budgeted_cli(tmp_path, monkeypatch)
    config = position_config()
    monkeypatch.setattr(cli.LiveConfig, "load", lambda _: config)
    path = tmp_path / "live.receipts.db"
    old = NavFixture(
        position_enabled=False, native_after=10**17, token_after=100_000_000, usdc_price=100_000_000
    )
    with closing(EvidenceStore(path)) as evidence:
        value, refs = capture(evidence, config, old)
    state = LoopState("live", nav_evidence=refs)
    state.integer_risk().observe(day=day_of(old.timestamp), at=old.timestamp, nav=value.nav_micros)
    state.last_units = [value.wallet.native, value.wallet.token, 0]
    store = StateStore(tmp_path / "live.state.json")
    store.bind(sha256_hex(canonical_json(asdict(config))))
    store.save(state)
    policies = profile.policies(SOURCES)
    with RpcBudget(tmp_path / "live.receipt-budget.db", policies):
        pass
    if fault == "missing":
        path.unlink()
    elif fault == "corrupt":
        with sqlite3.connect(path) as db:
            db.execute("UPDATE evidence SET raw_response_payload = ?", (b"corrupted",))
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    observed = []

    def observe(loop):
        assert fault is None, "invalid retained evidence must refuse before current RPC"
        newer = replace(
            old,
            timestamp=old.timestamp + 2,
            block_number=old.block_number + 1,
            block_hash="0x" + "66" * 32,
        )
        value, fresh = capture(loop.receipt_costs.store, config, newer)
        observed.extend(fresh)
        return market(
            block=newer.block_number,
            block_hash=newer.block_hash,
            timestamp=newer.timestamp,
            eth=value.wallet.native,
            usdc=value.wallet.token,
            valuation=value,
            nav_evidence=fresh,
        )

    class NoNetworkNonce:
        def nonce(self, *_):
            assert fault is None, "invalid retained evidence must refuse before nonce RPC"
            return 4

    monkeypatch.setattr(cli, "BudgetedRpc", lambda *_args, **_kwargs: NoNetworkNonce())
    monkeypatch.setattr(LiveLoop, "observe", observe)
    monkeypatch.setattr(LiveLoop, "runtime_pins", lambda *_: config.expected_pins)
    if fault:
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 3
    else:
        cli.main()
    result = json.loads(capsys.readouterr().out)
    assert result["operational_checks_passed"] is (fault is None)
    assert not result["execution_eligible"] and not result["signer_contacted"]
    assert bool(observed) is (fault is None)
    assert not http.calls
    assert before == {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    if fault is None:
        assert result["observation"]["nav_evidence_ephemeral"]
        with closing(ReadOnlyEvidenceStore(path)) as evidence:
            for ref in observed:
                with pytest.raises(KeyError):
                    evidence.get_record(ref)


@pytest.mark.parametrize("fault", [None, "payload", "raw_response_payload"])
def test_original_precedes_temporary_and_cannot_be_written(tmp_path, fault):
    original, temporary = tmp_path / "original.db", tmp_path / "temporary.db"
    args = ("test", b"payload", None, "fixture")
    kwargs = {"retrieved_at": "2026-11-04T00:00:00Z"}
    with closing(EvidenceStore(original)) as retained:
        ref = retained.put(*args, **kwargs)
        if fault:
            retained._connection.execute(f"UPDATE evidence SET {fault} = ?", (b"corrupted",))
            retained._connection.commit()
    before = original.read_bytes()
    with closing(PrecheckEvidenceStore(temporary, original)) as evidence:
        duplicate = evidence.put(*args, **kwargs)
        assert duplicate == ref
        fresh = evidence.put("new", b"new proof", None, "fixture")
        assert evidence.get_record(fresh).payload == b"new proof"
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            evidence.retained._connection.execute("DELETE FROM evidence")
        if fault:
            with pytest.raises(ValueError, match="hash mismatch"):
                evidence.get_record(ref)
            if fault == "payload":
                with pytest.raises(ValueError, match="hash mismatch"):
                    evidence.get(ref)
        else:
            assert evidence.get_record(ref).payload == evidence.get(ref) == b"payload"
    assert original.read_bytes() == before
    with closing(ReadOnlyEvidenceStore(original)) as retained, pytest.raises(KeyError):
        retained.get_record(fresh)


def test_missing_original_is_not_created_and_existing_temp_is_refused(tmp_path):
    original, temporary = tmp_path / "absent.db", tmp_path / "temporary.db"
    with closing(PrecheckEvidenceStore(temporary, original)) as evidence:
        ref = evidence.put("new", b"proof", None, "fixture")
        assert evidence.get(ref) == b"proof"
    assert not original.exists()
    before = temporary.read_bytes()
    with pytest.raises(ValueError, match="new separate"):
        PrecheckEvidenceStore(temporary, original)
    assert temporary.read_bytes() == before and not original.exists()


def test_active_strategy_refuses_before_budget_evidence_or_rpc(tmp_path, monkeypatch, capsys):
    _, http = setup_budgeted_cli(tmp_path, monkeypatch)
    lock_path = tmp_path / "live.lock"
    lock_path.write_bytes(b"existing lock")

    def forbidden(*_args, **_kwargs):
        pytest.fail("active strategy must refuse before evidence or quota access")

    monkeypatch.setattr(cli, "receipt_cost_reader", forbidden)
    with lock_path.open("rb") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 3
    assert not http.calls
    assert json.loads(capsys.readouterr().out)["blockers"] == ["precheck_inputs_unresolved"]
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == {"live.lock": b"existing lock"}


def test_readonly_diagnostic_lock_blocks_restart_without_changing_lock(tmp_path):
    lock_path = tmp_path / "live.lock"
    lock_path.write_bytes(b"existing lock")
    with (
        precheck_evidence(tmp_path / "temporary.db", tmp_path / "missing.db", lock_path),
        lock_path.open("rb") as writer,
        pytest.raises(BlockingIOError),
    ):
        fcntl.flock(writer, fcntl.LOCK_EX | fcntl.LOCK_NB)
    with lock_path.open("rb") as writer:
        fcntl.flock(writer, fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert lock_path.read_bytes() == b"existing lock" and not (tmp_path / "missing.db").exists()
