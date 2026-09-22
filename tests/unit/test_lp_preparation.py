"""Synthetic full-wire preparation; never a release or profitability assertion."""

import json
import sqlite3
import subprocess
from dataclasses import asdict, replace
from decimal import Decimal

import pytest

from oasis_agent.errors import RpcError
from oasis_agent.execution.lp_preparation import LpPlanBounds, build_lp_simulation_plan
from oasis_agent.execution.lp_signer_policy import EXECUTE_TYPE, LpSignerPolicy
from oasis_agent.execution.lp_state_machine import RuntimePin, RuntimeRole, TickRange
from oasis_agent.execution.range_controller import LpActionType
from oasis_agent.farming.position_quorum import PositionQuorum
from oasis_agent.models.common import canonical_json
from oasis_agent.models.position_scope import LpPositionScope
from oasis_agent.shadow.journal import ShadowJournal
from oasis_agent.shadow.service import LpShadowService, serializable
from scripts import prepare_lp_simulation

from .test_lp_shadow_service import setup
from .test_position_capture import snapshot_capture
from .test_shadow_lp_runner import band_strategy


def preparation(tmp_path, mode="recenter", same_range=False):
    store, journal, _, cycle = setup(tmp_path)
    capture = snapshot_capture()
    snapshot = capture.snapshot
    journal.close()
    # Use one explicit scope and one block across all inputs.

    journal = ShadowJournal(
        tmp_path / "scoped.db", chain_id=snapshot.block.chain_id, max_age_seconds=60
    )
    header_ref = store.put(
        "header",
        canonical_json(asdict(capture.header)),
        snapshot.block,
        "fixture",
        request={},
        raw_response={},
        interpretation={},
    )
    scope = LpPositionScope(
        snapshot.block.chain_id,
        snapshot.position_manager,
        snapshot.token_id,
        snapshot.pool_id,
        snapshot.nft_owner,
    )
    cycle = replace(
        cycle,
        header=capture.header,
        header_evidence=header_ref,
        state=replace(
            cycle.state, block=snapshot.block, observed_at_timestamp=1000, oracle_timestamp=1000
        ),
        position_scope=scope,
    )
    inputs, selected_range = next(iter(cycle.candidates.values()))
    if same_range:
        selected_range = TickRange(
            snapshot.tick_lower, snapshot.tick_upper, snapshot.pool_key.tick_spacing
        )
    cycle = replace(cycle, candidates={LpActionType(mode): (inputs, selected_range)})
    ref = store.put(
        "lp_inputs",
        cycle.input_payload,
        snapshot.block,
        "fixture",
        request={},
        raw_response={},
        interpretation={},
    )
    cycle = replace(cycle, input_evidence=ref)
    service = LpShadowService(
        store, journal, band_strategy(), min_benefit=Decimal(0), canary_limits=None
    )
    service.consume_once(cycle, now=1000)
    quorum = PositionQuorum(capture, replace(capture, source="rpc-b"))
    executor = "0x" + "88" * 20
    policy = LpSignerPolicy(
        snapshot.block.chain_id,
        snapshot.nft_owner,
        executor,
        snapshot.pool_id,
        "0x" + "99" * 32,
        (
            RuntimePin(RuntimeRole.EXECUTOR, executor, "0x" + "11" * 32),
            RuntimePin(
                RuntimeRole.POSITION_MANAGER,
                snapshot.position_manager,
                "0x" + "22" * 32,
            ),
            RuntimePin(RuntimeRole.POOL_MANAGER, "0x" + "77" * 20, "0x" + "33" * 32),
        ),
        1000,
        1000,
        1000,
        500000,
        1000000,
        10,
        60,
    )
    bounds = LpPlanBounds(1, 1050, 100 if mode == "recenter" else 0, 1, 1, 1000, 1000, 400000, 60)
    return store, journal, cycle, quorum, policy, bounds


def test_end_to_end_plan_is_deterministic_and_not_authorized(tmp_path):
    store, journal, cycle, quorum, policy, bounds = preparation(tmp_path)
    try:
        assert type(cycle).from_json(canonical_json(serializable(cycle))) == cycle
        plan = build_lp_simulation_plan(store, journal, cycle, quorum, policy, bounds, now=1001)
        assert json.loads(plan.payload)["schema"] == "oasis.lp-simulation-plan.v2"
        assert plan.execution_eligible is False
        assert plan.intent.position_token_id == quorum.left.snapshot.token_id
        assert plan.intent.max_swap_input == 0
        assert (
            build_lp_simulation_plan(store, journal, cycle, quorum, policy, bounds, now=1002).intent
            == plan.intent
        )
        journal.fault(now=1002)
        with pytest.raises(ValueError, match=r"latest|halt"):
            build_lp_simulation_plan(store, journal, cycle, quorum, policy, bounds, now=1003)
    finally:
        journal.close()
        store.close()


@pytest.mark.parametrize("change", ["unscoped", "owner", "pool", "manager", "stale", "capital"])
def test_plan_rejects_unbound_or_unsafe_inputs(tmp_path, change):
    store, journal, cycle, quorum, policy, bounds = preparation(tmp_path)
    try:
        if change == "unscoped":
            cycle = replace(cycle, position_scope=None)
        elif change == "owner":
            policy = replace(policy, account="0x" + "66" * 20)
        elif change == "pool":
            policy = replace(policy, pool_id="0x" + "66" * 32)
        elif change == "manager":
            pins = tuple(
                replace(pin, address="0x" + "55" * 20)
                if pin.role is RuntimeRole.POSITION_MANAGER
                else pin
                for pin in policy.runtime_pins
            )
            policy = replace(policy, runtime_pins=pins)
        elif change == "capital":
            bounds = replace(bounds, amount0_max=1001)
        with pytest.raises(ValueError):
            build_lp_simulation_plan(
                store,
                journal,
                cycle,
                quorum,
                policy,
                bounds,
                now=2000 if change == "stale" else 1001,
            )
    finally:
        journal.close()
        store.close()


def test_quorum_rejects_same_source_and_different_blocks():
    capture = snapshot_capture()
    with pytest.raises(ValueError):
        PositionQuorum(capture, capture)
    with pytest.raises(ValueError):
        PositionQuorum(capture, replace(snapshot_capture(closing=True), source="rpc-b"))


@pytest.mark.parametrize("mode", ["recenter", "remove"])
def test_canonical_calldata_matches_independent_foundry_encoder(tmp_path, mode):
    store, journal, cycle, quorum, policy, bounds = preparation(tmp_path, mode)
    try:
        plan = build_lp_simulation_plan(store, journal, cycle, quorum, policy, bounds, now=1001)
        i = plan.intent
        key = quorum.left.snapshot.pool_key
        key_arg = f"({key.currency0},{key.currency1},{key.fee},{key.tick_spacing},{key.hooks})"
        values = [
            i.nonce,
            i.deadline_timestamp,
            0 if mode == "recenter" else 1,
            key_arg,
            i.position_token_id,
            i.new_range.tick_lower,
            i.new_range.tick_upper,
            i.liquidity,
            i.amount0_min,
            i.amount1_min,
            i.amount0_max,
            i.amount1_max,
            0,
            "0x",
        ]
        result = subprocess.run(
            [
                "cast",
                "calldata",
                "execute(" + EXECUTE_TYPE + ")",
                "(" + ",".join(map(str, values)) + ")",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        assert bytes.fromhex(result.stdout.strip()[2:]) == plan.calldata
    finally:
        journal.close()
        store.close()


def test_cli_uses_query_only_journal_and_writes_new_report(tmp_path, monkeypatch, capsys):
    store, journal, cycle, quorum, policy, bounds = preparation(tmp_path)
    try:
        config = {
            "policy": asdict(policy),
            "bounds": asdict(bounds),
            "position_manager": quorum.left.snapshot.position_manager,
            "state_view": quorum.left.state_view,
            "token_id": quorum.left.snapshot.token_id,
            "block_number": quorum.left.header.ref.number,
            "shadow_max_age_seconds": 60,
        }
        config_path, cycle_path, report = (
            tmp_path / name for name in ("config.json", "cycle.json", "report.json")
        )
        config_path.write_bytes(canonical_json(config))
        cycle_path.write_bytes(canonical_json(serializable(cycle)))
        monkeypatch.setenv("OASIS_LP_RPC_PRIMARY", "https://primary.invalid")
        monkeypatch.setenv("OASIS_LP_RPC_SECONDARY", "https://secondary.invalid")
        monkeypatch.setattr(
            prepare_lp_simulation, "collect_position_quorum", lambda *a, **kw: quorum
        )
        argv = [
            "--config",
            str(config_path),
            "--cycle",
            str(cycle_path),
            "--journal",
            str(tmp_path / "scoped.db"),
            "--evidence",
            str(tmp_path / "evidence.db"),
            "--report",
            str(report),
            "--now",
            "1001",
        ]

        def rpc_failure(*args, **kwargs):
            raise RpcError("opaque_provider_error_must_not_escape")

        monkeypatch.setattr(prepare_lp_simulation, "collect_position_quorum", rpc_failure)
        with pytest.raises(SystemExit):
            prepare_lp_simulation.main(argv)
        assert "opaque_provider_error_must_not_escape" not in capsys.readouterr().err
        monkeypatch.setattr(
            prepare_lp_simulation, "collect_position_quorum", lambda *a, **kw: quorum
        )
        assert prepare_lp_simulation.main(argv) == 0
        assert json.loads(report.read_bytes())["execution_eligible"] is False
        with pytest.raises(SystemExit):
            prepare_lp_simulation.main(argv)
        reader = ShadowJournal(
            tmp_path / "scoped.db", chain_id=4663, max_age_seconds=60, read_only=True
        )
        try:
            with pytest.raises(sqlite3.OperationalError):
                reader.fault(now=1001)
        finally:
            reader.close()
    finally:
        journal.close()
        store.close()


def test_noop_recenter_rejected(tmp_path):
    store, journal, cycle, quorum, policy, bounds = preparation(tmp_path, same_range=True)
    try:
        with pytest.raises(ValueError, match="different range"):
            build_lp_simulation_plan(store, journal, cycle, quorum, policy, bounds, now=1001)
    finally:
        journal.close()
        store.close()


@pytest.mark.parametrize(
    "field,value",
    [("executor_nonce", True), ("amount0_max", 2**128), ("liquidity", -1), ("deadline", 2**64)],
)
def test_invalid_wire_bounds_rejected(field, value):
    bounds = LpPlanBounds(1, 1050, 100, 1, 1, 1000, 1000, 400000, 60)
    with pytest.raises(ValueError):
        replace(bounds, **{field: value})
