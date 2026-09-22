"""Retained market cursors survive restart without trusting serialized features."""

import sqlite3
from dataclasses import replace

import pytest

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.rpc.budget import RpcBudget
from oasis_agent.shadow.market_checkpoint import MarketCheckpoint, MarketScope
from oasis_agent.shadow.market_window import build_market_window
from scripts.run_pool_state_observer import Config, MarketConfig, open_market_checkpoint
from tests.unit.test_market_window import CHAIN, MANAGER, PIN, POOL, POOL_ID, VIEW, inputs


def retained(tmp_path):
    evidence = tmp_path / "evidence.db"
    store = EvidenceStore(evidence)
    try:
        swaps, prices = inputs(store)
        window = build_market_window(
            store,
            swap_references=swaps,
            price_references=prices,
            pool=POOL,
            pool_manager=MANAGER,
            horizon_seconds=1,
            max_lateness_seconds=1,
            mature_prefix=True,
        )
    finally:
        store.close()
    return evidence, window


def scope():
    return MarketScope(CHAIN, POOL, MANAGER, 1, 1, "a" * 64)


def test_restart_idempotency_and_missing_raw_evidence(tmp_path):
    evidence, window = retained(tmp_path)
    path = tmp_path / "cursor.db"
    cursor = MarketCheckpoint(path, scope())
    cursor.append(evidence, window)
    cursor.close()
    cursor = MarketCheckpoint(path, scope())
    try:
        assert cursor.read() == window
        cursor.append(evidence, window)
        assert cursor.db.execute("SELECT count(*) FROM windows").fetchone() == (1,)
        with pytest.raises(ValueError, match="replay mismatch"):
            cursor.append(evidence, replace(window, deferred_count=999))
        assert cursor.read() == window
    finally:
        cursor.close()
    evidence.rename(tmp_path / "removed.db")
    with pytest.raises(ValueError, match="missing"):
        MarketCheckpoint(path, scope())


def test_configuration_and_running_audit_changes_fail_closed(tmp_path):
    evidence, window = retained(tmp_path)
    path = tmp_path / "cursor.db"
    cursor = MarketCheckpoint(path, scope())
    try:
        cursor.append(evidence, window)
        with pytest.raises(ValueError, match="configuration"):
            MarketCheckpoint(path, replace(scope(), config_hash="b" * 64))
        db = sqlite3.connect(path)
        with db:
            db.execute("UPDATE windows SET digest=?", ("0" * 64,))
        db.close()
        with pytest.raises(ValueError, match="audit chain"):
            cursor.append(evidence, window)
    finally:
        cursor.close()


def test_budget_and_checkpoint_cannot_be_silently_recreated(tmp_path):
    config = Config(
        chain_id=CHAIN,
        pool_id=POOL_ID,
        state_view=VIEW,
        pool_manager=MANAGER,
        state_view_runtime_keccak=PIN,
        pool_manager_runtime_keccak=PIN,
        public_request_cap=27,
        paid_request_cap=25,
        public_interval_ms=1000,
        paid_interval_ms=3000,
        max_age_seconds=300,
        market=MarketConfig(pool=POOL, blocks=3, horizon_seconds=1, max_lateness_seconds=1),
    )
    budget, path = tmp_path / "budget.db", tmp_path / "cursor.db"
    cursor = open_market_checkpoint(config, budget, path)
    evidence, window = retained(tmp_path)
    cursor.append(evidence, window)
    cursor.close()
    with pytest.raises(ValueError, match="lost its RPC budget"):
        open_market_checkpoint(config, budget, path)
    with RpcBudget(budget, config.policies()) as quota:
        assert quota.reserve("public", now_ms=1000) == 0
    path.unlink()
    with pytest.raises(ValueError, match="lost its checkpoint"):
        open_market_checkpoint(config, budget, path)
    assert not path.exists()
