"""Observe-only reconciliation CLI: declared-provider binding and chainable state.

The observer must refuse to label a session with a provider the manifest never
declared, must refuse a partial pin set, and must chain only a state that
describes exactly this manifest. These tests cover the pure binding rules; the
RPC capture and replay paths are covered by the inventory/flow/reader suites.
"""

import json
from dataclasses import replace

import pytest
from pydantic import ValidationError

from oasis_agent.evidence.exact_capture import validate_session_pair
from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.models.common import canonical_json
from oasis_agent.risk.account_risk import RiskState
from scripts.run_account_risk_reconcile import (
    _bind_blocks,
    _bind_endpoints,
    _bind_state,
    _budget_policies,
    _environment,
    _native_declarations,
    _pairs,
    _prior_state,
    _rpc_map,
    _sessions,
)
from tests.unit.test_account_risk_reader import (
    ACCOUNT,
    SECOND_ACCOUNT,
    InventoryFixture,
    baseline,
    chain_inventory,
    manifest,
)

OTHER_CHAIN = 8453


def test_rpc_pairs_must_be_the_declared_manifest_providers() -> None:
    portfolio = manifest(InventoryFixture())
    declared = {"4663": ("rpc_a", "rpc_b")}
    assert _bind_endpoints(portfolio, declared) == declared
    with pytest.raises(ValueError, match="one RPC pair per manifest chain"):
        _bind_endpoints(portfolio, {})
    with pytest.raises(ValueError, match="one RPC pair per manifest chain"):
        _bind_endpoints(portfolio, declared | {str(OTHER_CHAIN): ("rpc_a", "rpc_b")})
    with pytest.raises(ValueError, match="declared manifest providers"):
        _bind_endpoints(portfolio, {"4663": ("rpc_a", "rpc_c")})
    with pytest.raises(ValueError, match="declared manifest providers"):
        _bind_endpoints(portfolio, {"4663": ("rpc_b", "rpc_a")})


def test_pinned_blocks_must_cover_exactly_the_manifest_chains() -> None:
    portfolio = manifest(InventoryFixture(), replace(InventoryFixture(), chain_id=OTHER_CHAIN))
    assert _bind_blocks(portfolio, {4663: 10, OTHER_CHAIN: 20}) == {4663: 10, OTHER_CHAIN: 20}
    with pytest.raises(ValueError, match="one pinned block per manifest chain"):
        _bind_blocks(portfolio, {4663: 10})


def test_rpc_and_environment_entries_are_strictly_parsed(tmp_path) -> None:
    assert _rpc_map(["4663:rpc_a:rpc_b"]) == {"4663": ("rpc_a", "rpc_b")}
    assert _pairs(["4663=10"], "block") == {"4663": "10"}
    with pytest.raises(ValueError, match="two distinct RPC environment names"):
        _rpc_map(["4663:rpc_a:rpc_a"])
    with pytest.raises(ValueError, match="two distinct RPC environment names"):
        _rpc_map(["4663:rpc_a:rpc_b", "4663:rpc_a:rpc_b"])
    with pytest.raises(ValueError, match="invalid block entry"):
        _pairs(["4663"], "block")

    env = tmp_path / ".env"
    env.write_text("export RPC_A=https://a.invalid\nRPC_B=https://b.invalid\n")
    assert _environment(env, {"RPC_A", "RPC_B"}) == {
        "RPC_A": "https://a.invalid",
        "RPC_B": "https://b.invalid",
    }
    assert _environment(env, {"RPC_A"}) == {"RPC_A": "https://a.invalid"}
    with pytest.raises(ValueError, match="two configured RPC endpoints"):
        _environment(env, {"RPC_A", "RPC_C"})
    env.write_text("RPC_A=https://a.invalid\nRPC_C=https://a.invalid\n")
    with pytest.raises(ValueError, match="two configured RPC endpoints"):
        _environment(env, {"RPC_A", "RPC_C"})
    env.write_text("RPC_A=https://a.invalid\nRPC_A=https://b.invalid\n")
    with pytest.raises(ValueError, match="duplicate RPC configuration"):
        _environment(env, {"RPC_A", "RPC_C"})


def test_budget_policies_are_one_immutable_set_per_ledger() -> None:
    policies = _budget_policies({"rpc_b", "rpc_a"}, max_requests=8, interval_ms=3000)
    assert [policy.source for policy in policies] == ["rpc_a", "rpc_b"]
    assert {policy.max_requests for policy in policies} == {8}
    # Every session for the shared ledger is built from this identical tuple.
    for cap in (0, -1, 1_000_001, "8"):
        with pytest.raises(ValueError, match="request cap must be"):
            _budget_policies({"rpc_a"}, max_requests=cap, interval_ms=3000)  # type: ignore[arg-type]


def test_sessions_are_fresh_one_shot_sessions_labelled_by_the_manifest(tmp_path) -> None:
    policies = _budget_policies({"rpc_a", "rpc_b"}, max_requests=8, interval_ms=3000)
    urls = {"rpc_a": "https://a.invalid", "rpc_b": "https://b.invalid"}
    budget = tmp_path / "budget.db"
    first = _sessions(sources=("rpc_a", "rpc_b"), urls=urls, policies=policies, budget=budget)
    validate_session_pair(*first)  # the exact-capture contract the CLI depends on
    second = _sessions(sources=("rpc_a", "rpc_b"), urls=urls, policies=policies, budget=budget)
    validate_session_pair(*second)
    assert [session[2] for session in first] == ["rpc_a", "rpc_b"]
    assert first[0][0] is not second[0][0] and first[0][1] is not second[0][1]
    assert all(not session[1].exchanges for session in (*first, *second))


def test_native_declarations_require_exact_coverage_and_bounded_integers(tmp_path) -> None:
    portfolio = manifest(InventoryFixture())
    key = chain_inventory(InventoryFixture()).key
    assert _native_declarations(None, portfolio) == {key: (0, 0)}
    path = tmp_path / "native.json"
    path.write_text(json.dumps({key: {"gas_units": 5, "non_trading_units": 0}}))
    assert _native_declarations(path, portfolio) == {key: (5, 0)}
    for payload in (
        {},
        {key: {"gas_units": 5}},
        {key: {"gas_units": 5, "non_trading_units": 0, "extra": 1}},
        {key: {"gas_units": -1, "non_trading_units": 0}},
        {key: {"gas_units": 1.0, "non_trading_units": 0}},
        {key: {"gas_units": 2**256, "non_trading_units": 0}},
        {f"{OTHER_CHAIN}:{ACCOUNT}": {"gas_units": 1, "non_trading_units": 0}},
    ):
        path.write_text(json.dumps(payload))
        with pytest.raises(ValueError):
            _native_declarations(path, portfolio)


def test_chained_state_must_describe_exactly_the_manifest_accounts(tmp_path) -> None:
    store = EvidenceStore(tmp_path / "state.db")
    fixture = InventoryFixture()
    try:
        frame = baseline(store, fixture)
        assert _prior_state(None) == RiskState()
        path = tmp_path / "state.json"
        path.write_bytes(
            canonical_json(
                RiskState(frame=frame, cumulative_loss=7, halted=True).model_dump(mode="json")
            )
            + b"\n"
        )
        loaded = _prior_state(path)
        assert (loaded.frame, loaded.cumulative_loss, loaded.halted) == (frame, 7, True)
        assert _bind_state(manifest(fixture), loaded) is loaded
        with pytest.raises(ValueError, match="previous state must cover exactly"):
            _bind_state(manifest(replace(fixture, account=SECOND_ACCOUNT)), loaded)
    finally:
        store.close()


def test_an_unreadable_reconciliation_input_is_rejected(tmp_path) -> None:
    path = tmp_path / "state.json"
    path.write_bytes(b'{"halted": "yes"}\n')
    with pytest.raises(ValidationError):
        _prior_state(path)
