"""Synthetic raw-proof integration, never real LP/NAV or economic acceptance."""

import json
import sqlite3
from dataclasses import asdict, replace
from hashlib import sha256

import pytest
from eth_abi.abi import encode

from oasis_agent.evidence.rpc_validation import restore_exchanges
from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.execution.account_risk_reader import (
    AccountObservation,
    EvidenceReplayAccountRiskReader,
)
from oasis_agent.farming.account_flows import reconstruct_movements
from oasis_agent.farming.account_gas_coverage import replay_account_gas
from oasis_agent.farming.account_inventory import (
    AccountInventoryCapture,
    _persist_capture,
    collect_account_inventory_quorum,
    persist_account_inventory_evidence,
)
from oasis_agent.models.chain import BlockSnapshot
from oasis_agent.models.common import canonical_json
from oasis_agent.risk.account_risk import RiskState, reconcile
from scripts.replay_account_risk_gas import ReplayStore, run
from tests.unit.test_account_gas_coverage import inputs
from tests.unit.test_account_risk_reader import (
    USDC_SPEC,
    WETH_SPEC,
    FeedSpec,
    InventoryFixture,
    PositionSpec,
    chain_inventory,
    inventory_session,
    manifest,
    observe_position,
    position_snapshot,
)


class GasInventory(InventoryFixture):
    def _feed(self, spec, data):
        if data.endswith("feaf968c"):
            return (
                "0x"
                + encode(
                    ["uint80", "int256", "uint256", "uint256", "uint80"],
                    [2, spec.answer, self.timestamp - 100, self.timestamp, 2],
                ).hex()
            )
        return super()._feed(spec, data)


def scenario(store, *, empty=False, unchanged=False, with_position=False):
    gas, _ = inputs(
        store, chain=4663 if with_position else 1, empty=empty, providers=("rpc_a", "rpc_b")
    )
    policy = gas.native_policy
    spec = FeedSpec(policy.address, policy.description, 2000 * 10**8, "0x6000")
    fixture = GasInventory(
        chain_id=gas.opening.ref.chain_id,
        account=gas.account,
        block_number=gas.opening.ref.number,
        block_hash=gas.opening.ref.block_hash,
        parent_hash=gas.opening.parent_hash,
        timestamp=gas.opening.ref.timestamp,
        native_units=10**15,
        native_feed=spec,
        tokens=(replace(USDC_SPEC, units=1), replace(WETH_SPEC, units=1)) if with_position else (),
        positions=(PositionSpec(owner=gas.account),) if with_position else (),
    )
    entry = chain_inventory(fixture)
    # Bind the manifest staleness policy to the original complete gas proof.
    entry = entry.model_copy(
        update={
            "assets": (
                entry.native.model_copy(
                    update={
                        "feed": entry.native.feed.model_copy(
                            update={"max_age_seconds": policy.max_age_seconds}
                        )
                    }
                ),
                *entry.erc20_assets,
            )
        }
    )
    m = manifest(fixture).model_copy(update={"chains": (entry,)})
    # The capture helper re-derives its entry; same feed age policy must be used there too.

    def observe(f):
        return persist_account_inventory_evidence(
            store,
            collect_account_inventory_quorum(
                inventory_session("rpc_a", f),
                inventory_session("rpc_b", replace(f, methods=[], block_calls=0)),
                entry=entry,
                block_number=f.block_number,
            ),
        )

    def positions(f):
        if not with_position:
            return ()
        return observe_position(
            store,
            f,
            snapshot=position_snapshot(
                block_number=f.block_number,
                block_hash=f.block_hash,
                timestamp=f.timestamp,
                owner=f.account,
            ),
        )

    opening = {entry.key: AccountObservation(observe(fixture), positions=positions(fixture))}
    current = replace(
        fixture,
        block_number=gas.closing.ref.number,
        block_hash=gas.closing.ref.block_hash,
        parent_hash=gas.closing.parent_hash,
        timestamp=gas.closing.ref.timestamp,
        native_units=fixture.native_units - (0 if empty or unchanged else 400),
        methods=[],
        block_calls=0,
    )
    closing = {
        entry.key: AccountObservation(
            observe(current), positions=positions(current), native_gas_evidence=gas
        )
    }
    return m, opening, closing, gas


def frames(store, m, opening, closing):
    first = EvidenceReplayAccountRiskReader(store, m, opening).reconcile()
    last = EvidenceReplayAccountRiskReader(store, m, closing, previous=first).reconcile()
    return first, last


@pytest.mark.parametrize("gas,inflow", [(200, 0), (0, 200), (200, 100), (0, -200)])
def test_unchanged_native_balance_does_not_hide_unexplained_offsets(gas, inflow):
    result = reconstruct_movements(
        previous_units={"native": 1000},
        current_units={"native": 1000},
        prices={"native": (1, 1, 0)},
        flows={},
        native_asset_id="native",
        native_gas_units=gas,
        non_trading_native_units=inflow,
    )
    assert result.unresolved_operations == 1
    assert result.non_trading_flow_usd_micros == 0


@pytest.mark.parametrize("empty", [False, True])
def test_complete_gas_reconciles_wallet_debit_once_including_failed_tx(tmp_path, empty):
    with_store = EvidenceStore(tmp_path / "evidence.db")
    try:
        m, opening, closing, gas = scenario(with_store, empty=empty)
        first, last = frames(with_store, m, opening, closing)
        account = last.accounts[0]
        assert account.movements["native"] == (0 if empty else -400)
        assert account.unresolved_operations == 0
        assert account.non_trading_flow_usd_micros == 0
        assert account.assets[0].units == first.accounts[0].assets[0].units - (0 if empty else 400)
        # Risk uses observed wallet units, not a second debit of rounded expense USD.
        state = reconcile(m.policy, RiskState(frame=first), last, now=account.timestamp)
        assert not state.halted
        costs = replay_account_gas(with_store, gas)
        assert [t["status"] for t in costs.transactions] == ([] if empty else [1, 0])
        assert costs.total_native == (0 if empty else 400)
    finally:
        with_store.close()


def test_unexplained_gas_offset_halts_risk_even_with_zero_wallet_delta(tmp_path):
    store = EvidenceStore(tmp_path / "evidence.db")
    try:
        m, opening, closing, _ = scenario(store, unchanged=True)
        first, last = frames(store, m, opening, closing)
        assert last.accounts[0].unresolved_operations == 1
        with pytest.raises(ValueError, match="unresolved or stale"):
            reconcile(m.policy, RiskState(frame=first), last, now=last.accounts[0].timestamp)
    finally:
        store.close()


def test_owned_lp_value_is_not_debited_for_wallet_gas_again(tmp_path):
    store = EvidenceStore(tmp_path / "evidence.db")
    try:
        m, opening, closing, _ = scenario(store, with_position=True)
        first, last = frames(store, m, opening, closing)
        old = {a.asset_id: a for a in first.accounts[0].assets}
        new = {a.asset_id: a for a in last.accounts[0].assets}
        assert new["lp-position"].value == old["lp-position"].value > 0
        assert new["native"].units == old["native"].units - 400
        assert last.accounts[0].unresolved_operations == 0
        assert (
            last.accounts[0].nav - first.accounts[0].nav
            == new["native"].value - old["native"].value
        )
    finally:
        store.close()


@pytest.mark.parametrize(
    "bad",
    [
        "account",
        "providers",
        "opening_height",
        "opening_hash",
        "opening_time",
        "closing_parent",
        "policy",
        "missing_cost",
        "single_source",
        "gap",
        "round",
    ],
)
def test_foreign_partial_or_disagreeing_gas_cannot_explain_nav(tmp_path, bad):
    store = EvidenceStore(tmp_path / "evidence.db")
    try:
        m, opening, closing, gas = scenario(store)
        if bad == "account":
            gas = replace(gas, account="0x" + "ff" * 20)
        elif bad == "providers":
            gas = replace(gas, providers=tuple(reversed(gas.providers)))
        elif bad.startswith("opening_"):
            changes = {
                "opening_height": {"number": gas.opening.ref.number - 1},
                "opening_hash": {"block_hash": "0x" + "ff" * 32},
                "opening_time": {"timestamp": gas.opening.ref.timestamp - 1},
            }
            gas = replace(
                gas, opening=replace(gas.opening, ref=replace(gas.opening.ref, **changes[bad]))
            )
        elif bad == "closing_parent":
            gas = replace(gas, closing=replace(gas.closing, parent_hash="0x" + "ff" * 32))
        elif bad == "policy":
            gas = replace(gas, native_policy=replace(gas.native_policy, max_age_seconds=61))
        elif bad in ("missing_cost", "single_source", "gap"):
            block = gas.blocks[0]
            if bad == "missing_cost":
                block = replace(block, costs=block.costs[:1])
            elif bad == "single_source":
                block = replace(block, blocks=block.blocks[:1])
            else:
                block = replace(
                    block,
                    plan=replace(
                        block.plan,
                        header=replace(
                            block.plan.header,
                            ref=replace(
                                block.plan.header.ref, number=block.plan.header.ref.number + 1
                            ),
                        ),
                    ),
                )
            gas = replace(gas, blocks=(block,))
        else:
            # Keep internally agreed gas prices intact; only inventory's round disagrees.
            obs = closing[m.keys[0]]
            refs = []
            for ref in obs.inventory:
                record = store.get_record(ref)
                xs = list(restore_exchanges(record.request_payload, record.raw_response_payload))
                for index, x in enumerate(xs):
                    response = json.loads(x.response_payload)
                    if x.method == "eth_call" and json.loads(x.request_payload)["params"][0][
                        "data"
                    ].endswith("feaf968c"):
                        response["result"] = (
                            "0x"
                            + encode(
                                ["uint80", "int256", "uint256", "uint256", "uint80"],
                                [
                                    3,
                                    2000 * 10**8,
                                    ref.block.timestamp - 100,
                                    ref.block.timestamp,
                                    3,
                                ],
                            ).hex()
                        )
                        xs[index] = replace(x, response_payload=canonical_json(response))
                refs.append(
                    _persist_capture(
                        store,
                        AccountInventoryCapture(
                            ref.source,
                            BlockSnapshot(ref.block, gas.closing.parent_hash),
                            m.chains[0],
                            tuple(xs),
                        ),
                        retrieved_at=ref.retrieved_at,
                    )
                )
            closing[m.keys[0]] = replace(obs, inventory=tuple(refs))
        closing[m.keys[0]] = replace(closing[m.keys[0]], native_gas_evidence=gas)
        with pytest.raises((ValueError, TypeError)):
            frames(store, m, opening, closing)
    finally:
        store.close()


def test_no_scalar_override_or_inception_expense_proof(tmp_path):
    store = EvidenceStore(tmp_path / "evidence.db")
    try:
        m, _opening, closing, _gas = scenario(store)
        with pytest.raises(ValueError, match="also carry declared"):
            replace(closing[m.keys[0]], native_gas_units=1)
        with pytest.raises(TypeError, match="typed complete"):
            replace(closing[m.keys[0]], native_gas_evidence={})
        with pytest.raises(ValueError, match="inception baseline"):
            EvidenceReplayAccountRiskReader(store, m, closing).reconcile()
    finally:
        store.close()


@pytest.mark.parametrize("bad", [None, "missing", "residual", "native_override", "flag", "output"])
def test_offline_cli_requires_covered_frames_refuses_without_write(tmp_path, bad):
    db = tmp_path / "evidence.db"
    store = EvidenceStore(db)
    try:
        m, opening, closing, _ = scenario(store, unchanged=bad == "residual")
        if bad == "missing":
            closing[m.keys[0]] = replace(closing[m.keys[0]], native_gas_evidence=None)
        elif bad == "native_override":
            closing[m.keys[0]] = replace(closing[m.keys[0]], non_trading_native_units=400)
        value = {
            "schema": "oasis.account-risk-gas-input.v1",
            "manifest": m.model_dump(mode="json"),
            "opening": {k: asdict(v) for k, v in opening.items()},
            "closing": {k: asdict(v) for k, v in closing.items()},
        }
        if bad == "flag":
            value["execution_eligible"] = True
        path = tmp_path / "input.json"
        path.write_bytes(canonical_json(value))
    finally:
        store.close()
    before = sha256(db.read_bytes()).hexdigest()
    output = tmp_path / "report.json"
    if bad == "output":
        output.write_text("original")
    if bad:
        with pytest.raises((ValueError, TypeError)):
            run(path, db, output)
        assert output.read_text() == "original" if bad == "output" else not output.exists()
    else:
        run(path, db, output)
        result = json.loads(output.read_bytes())
        assert result["net_pnl"] is None and result["execution_eligible"] is False
        assert result["closing"]["accounts"][0]["movements"]["native"] == -400
    assert sha256(db.read_bytes()).hexdigest() == before
    ro = ReplayStore(db)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            ro._connection.execute("CREATE TABLE forbidden(x)")
    finally:
        ro.close()
