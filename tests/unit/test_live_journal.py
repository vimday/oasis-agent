"""Crash boundaries, competing writers, projection rollback and reorg admission."""

import json
import sqlite3
from dataclasses import asdict
from pathlib import Path

import pytest

from oasis_agent.live.chain import ChainError
from oasis_agent.live.journal import JournalConflict, LiveJournal
from oasis_agent.live.state import LoopState, StateStore
from oasis_agent.models.common import canonical_json, sha256_hex
from tests.unit.test_live_lp import CONFIG, ScriptedRpc, live_loop

CONFIG_HASH = sha256_hex(canonical_json(asdict(CONFIG)))


def bound(tmp_path: Path) -> StateStore:
    store = StateStore(tmp_path / "live.json")
    store.bind(CONFIG_HASH)
    return store


@pytest.mark.parametrize("fault", ["missing", "json_only", "empty", "foreign"])
def test_recovery_binding_never_initializes_or_migrates_journal(tmp_path, fault):
    store = StateStore(tmp_path / "live.json")
    if fault == "json_only":
        store.save(LoopState("live"))
    elif fault == "empty":
        LiveJournal(store.journal_path, CONFIG_HASH)
    elif fault == "foreign":
        store.bind("f" * 64)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    with pytest.raises(ValueError):
        StateStore(store.path).bind(CONFIG_HASH, require_existing=True)
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before


def test_existing_binding_recovers_missing_projection_without_resetting_losses(tmp_path):
    store = bound(tmp_path)
    state = store.load("live")
    state.integer_risk().observe(day="2026-10-22", at=1000, nav=500_000_000)
    state.integer_risk().observe(day="2026-10-22", at=1001, nav=290_000_000)
    state.halted = "loss cap"
    store.save(state)
    before = store.journal_path.read_bytes()
    store.path.unlink()
    recovered = StateStore(store.path)
    recovered.bind(CONFIG_HASH, require_existing=True)
    assert recovered.load("live").integer_risk().losses(290_000_000) == (210_000_000, 210_000_000)
    assert recovered.load("live").halted == "loss cap"
    assert not store.path.exists() and store.journal_path.read_bytes() == before


def test_open_journal_does_not_recreate_file_deleted_before_next_read(tmp_path):
    store = bound(tmp_path)
    journal = LiveJournal(store.journal_path, CONFIG_HASH, require_existing=True)
    journal.load()
    store.journal_path.unlink()
    with pytest.raises(sqlite3.OperationalError):
        journal.load()
    assert not store.journal_path.exists()


def test_crash_after_journal_commit_recovers_without_resetting_losses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = bound(tmp_path)
    state = store.load("live")
    state.integer_risk().observe(day="2026-10-15", at=1000, nav=500_000_000)
    store.save(state)
    before = store.path.read_bytes()
    state.integer_risk().observe(day="2026-10-15", at=1001, nav=290_000_000)
    state.halted = "loss cap"

    def crash(*_):
        raise OSError("synthetic power loss before projection replace")

    with monkeypatch.context() as m:
        m.setattr(Path, "replace", crash)
        with pytest.raises(OSError):
            store.save(state)
    assert store.path.read_bytes() == before
    recovered = StateStore(store.path).load("live")
    assert recovered.halted == "loss cap"
    assert recovered.integer_risk().losses(290_000_000) == (210_000_000, 210_000_000)


def test_stale_writer_cannot_erase_other_writer_reservation(tmp_path: Path) -> None:
    first = bound(tmp_path)
    second = StateStore(first.path)
    a, b = first.load("live"), second.load("live")
    a.signing_attempt = {"nonce": 3, "request_hash": "a" * 64}
    first.save(a)
    b.halted = "stale"
    with pytest.raises(JournalConflict):
        second.save(b)
    assert StateStore(first.path).load("live").signing_attempt == a.signing_attempt


def test_unknown_signature_survives_restart_and_blocks_all_calls(tmp_path: Path) -> None:
    store = bound(tmp_path)
    state = store.load("live")
    state.signing_attempt = {"nonce": 3}
    store.save(state)
    loop = live_loop(tmp_path, ScriptedRpc())
    loop.store, loop.state = store, StateStore(store.path).load("live")
    assert "signing outcome unknown" in loop.tick()
    assert StateStore(store.path).load("live").signing_attempt == {"nonce": 3}


@pytest.mark.parametrize("fault", ["config", "projection", "missing", "truncate", "corrupt"])
def test_journal_tampering_or_policy_change_fails_closed(tmp_path: Path, fault: str) -> None:
    store = bound(tmp_path)
    state = store.load("live")
    state.halted = "retained stop"
    store.save(state)
    if fault == "config":
        with pytest.raises(ValueError):
            store.bind("f" * 64)
        return
    if fault == "projection":
        raw = json.loads(store.path.read_bytes())
        raw["halted"] = None
        raw["gas_spent_wei"] = 12345
        store.path.write_text(json.dumps(raw))
    if fault == "missing":
        store.journal_path.unlink()
    if fault in {"truncate", "corrupt"}:
        with sqlite3.connect(store.journal_path) as db:
            db.execute("DROP TRIGGER no_delete")
            db.execute("DROP TRIGGER no_update")
            if fault == "truncate":
                db.execute("DELETE FROM events WHERE seq = (SELECT MAX(seq) FROM events)")
            else:
                db.execute("UPDATE events SET parent = ? WHERE seq = 2", ("e" * 64,))
    with pytest.raises(ValueError):
        StateStore(store.path).load("live")


def test_settlement_reorg_blocks_before_new_observation(tmp_path: Path) -> None:
    anchor = {"number": 100, "hash": "0x" + "11" * 32, "transaction_hash": "0x" + "22" * 32}
    loop = live_loop(tmp_path, ScriptedRpc(), settled_blocks=[anchor])
    assert "reorged" in loop.tick()
    assert loop.state.halted is not None


def test_settlement_receipt_disappearance_is_not_considered_final(tmp_path: Path) -> None:
    anchor = {"number": 100, "hash": "0x" + "11" * 32, "transaction_hash": "0x" + "22" * 32}
    rpc = ScriptedRpc(canonical=anchor["hash"])
    loop = live_loop(tmp_path, rpc, settled_blocks=[anchor])
    with pytest.raises(ChainError, match="receipt changed"):
        loop._check_settlements()


def test_failed_mint_interpretation_does_not_consume_settlement_anchor(tmp_path: Path) -> None:
    loop = live_loop(tmp_path, ScriptedRpc())
    receipt = {
        "blockNumber": "0x1",
        "blockHash": "0x" + "11" * 32,
        "transactionHash": "0x" + "22" * 32,
        "logs": [],
    }
    before = asdict(loop.state)
    # Other proofs have already passed upstream; a late receipt interpretation
    # refusal must leave the journalable state unchanged and recoverable.
    with pytest.raises(ChainError):
        loop._apply(
            "mint",
            {"lower": -10, "upper": 10, "liquidity": 1},
            receipt,
            native_cost=1,
            cost_evidence=(),
            wallet=object(),
            cash_evidence=(object(), object()),
        )
    assert asdict(loop.state) == before
