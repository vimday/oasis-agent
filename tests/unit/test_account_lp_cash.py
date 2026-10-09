"""Constructed raw RPC joins only; these tests are not historical/live LP evidence."""

import json
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest
from eth_abi.abi import decode, encode
from pydantic import TypeAdapter

from oasis_agent.evidence.ingestion import persist_and_verify_block_range
from oasis_agent.evidence.rpc_validation import restore_exchanges
from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.execution.account_risk_reader import (
    AccountObservation,
    AssetFlowEvidence,
    EvidenceReplayAccountRiskReader,
    PositionEvidence,
)
from oasis_agent.farming.account_flows import (
    FlowScope,
    TokenFlowCapture,
    _expected,
)
from oasis_agent.farming.account_flows import (
    _persist_capture as persist_flow,
)
from oasis_agent.farming.account_gas_coverage import (
    AccountGasBlockInput,
    AccountGasCapture,
    AccountGasPlan,
    AccountGasRequest,
    replay_account_gas_block,
    retain_account_gas_block,
)
from oasis_agent.farming.account_inventory import (
    AccountInventoryCapture,
)
from oasis_agent.farming.account_inventory import (
    _expected as inventory_requests,
)
from oasis_agent.farming.account_inventory import (
    _persist_capture as persist_inventory,
)
from oasis_agent.farming.account_lp_cash import AccountLpCashEvidence, replay_account_lp_cash
from oasis_agent.farming.cash_window import CashWindowRequest, replay_cash_window
from oasis_agent.farming.owner_payments import OwnerBalanceCapture, persist_owner_payment
from oasis_agent.farming.position_capture import persist_position_snapshot_evidence
from oasis_agent.farming.position_settlement import replay_position_settlement_evidence
from oasis_agent.farming.price_evidence import persist_price_observation_evidence
from oasis_agent.farming.transaction_costs import TransactionCostCapture, persist_transaction_cost
from oasis_agent.models.common import ZERO_ADDRESS, EvidenceRef, canonical_json
from oasis_agent.risk.account_risk import RiskState, reconcile
from oasis_agent.risk.portfolio_manifest import (
    ChainInventory,
    ManifestAsset,
    ManifestFeed,
    ManifestPosition,
    PortfolioManifest,
)
from oasis_agent.shadow.pool_state_capture import (
    Slot0WindowCapture,
    _expected_calls,
    persist_slot0_capture,
)
from oasis_agent.uniswap_v4.state_rpc import Slot0State
from scripts.replay_account_risk_gas import Request, run
from tests.rpc_evidence_helpers import range_exchanges
from tests.unit.test_account_gas_coverage import exchange
from tests.unit.test_account_risk_reader import risk_policy, runtime_hash
from tests.unit.test_owner_payments import fixture as payment_fixture
from tests.unit.test_position_capture import VIEW, snapshot_capture
from tests.unit.test_position_dataset import CORE, OWNER, POSM, scenario
from tests.unit.test_price_evidence import price
from tests.unit.test_transaction_costs import fixture as cost_fixture

SOURCES = ("rpc-a", "rpc-b")


def result(x):
    return json.loads(x.response_payload)["result"]


def change_result(x, value):
    return replace(
        x, response_payload=canonical_json({"jsonrpc": "2.0", "id": x.request_id, "result": value})
    )


def wires(requests, values):
    return tuple(
        exchange(m, p, value, i)
        for i, ((m, p), value) in enumerate(zip(requests, values, strict=True), 1)
    )


def build(store, *, extra_flow=False, decrease=False):
    _, _, core_events, nft_events = scenario()
    opening_header, closing_header = core_events.blocks
    payment = payment_fixture()
    direction = -1 if decrease else 1
    if decrease:
        c = payment.core
        xs = list(c.exchanges)
        receipt = result(xs[2])
        obs = replace(c.entry.observation, liquidity_delta=-20)
        entry = replace(c.entry, observation=obs)
        modification = (
            "0x"
            + encode(
                ["int24", "int24", "int256", "bytes32"], [-120, 120, -20, (7).to_bytes(32, "big")]
            ).hex()
        )
        receipt["logs"][0]["data"] = modification
        core_events = replace(core_events, logs=(replace(core_events.logs[0], data=modification),))
        for log in receipt["logs"][1:]:
            log["topics"][1:3] = reversed(log["topics"][1:3])
        root = result(xs[3])
        unlocked, deadline = decode(["bytes", "uint256"], bytes.fromhex(root["input"][10:]))
        _, params = decode(["bytes", "bytes[]"], unlocked)
        root["input"] = (
            root["input"][:10]
            + encode(
                ["bytes", "uint256"],
                [encode(["bytes", "bytes[]"], [b"\x01\x12\x12", params]), deadline],
            ).hex()
        )
        frame = root["calls"][0]
        key, modification_args, hook = decode(
            ["(address,address,uint24,int24,address)", "(int24,int24,int256,bytes32)", "bytes"],
            bytes.fromhex(frame["input"][10:]),
        )
        frame["input"] = (
            frame["input"][:10]
            + encode(
                ["(address,address,uint24,int24,address)", "(int24,int24,int256,bytes32)", "bytes"],
                [key, (*modification_args[:2], -20, modification_args[3]), hook],
            ).hex()
        )
        frame["output"] = (
            "0x" + b"".join(x.to_bytes(16, "big", signed=True) for x in (100, 200, 0, 0)).hex()
        )
        for frame, amount in zip(root["calls"][1:], (100, 200), strict=True):
            frame.update(
                {
                    "from": CORE,
                    "input": "0xa9059cbb" + encode(["address", "uint256"], [OWNER, amount]).hex(),
                }
            )
        root["calls"][0]["calls"] = root["calls"][1:]
        root["calls"] = root["calls"][:1]
        xs[2], xs[3] = change_result(xs[2], receipt), change_result(xs[3], root)
        c = replace(c, entry=entry, exchanges=tuple(xs))
        balance_xs = list(payment.exchanges)
        for i, amount in enumerate((100, 200)):
            balance_xs[7 + 5 * i] = change_result(
                balance_xs[7 + 5 * i], "0x" + encode(["uint256"], [1000 + amount]).hex()
            )
        balance_xs[-2] = change_result(balance_xs[-2], receipt["logs"][1:])
        payment = replace(payment, core=c, exchanges=tuple(balance_xs))
    xs = list(payment.core.exchanges)
    receipt = result(xs[2])
    receipt.update(type="0x2", gasUsedForL1="0x14")
    xs[2] = change_result(xs[2], receipt)
    core = replace(payment.core, exchanges=tuple(xs))
    balance_xs = list(payment.exchanges)
    for index in (5, 10):
        balance_xs[index] = change_result(balance_xs[index], "0x" + encode(["uint8"], [0]).hex())
    payment = replace(payment, core=core, exchanges=tuple(balance_xs))
    snapshot_refs = []
    snapshots = []
    for closing in (False, True):
        c = snapshot_capture(closing=closing)
        snapshot = replace(
            c.snapshot, nft_owner=OWNER, liquidity=10000 + (20 * direction if closing else 0)
        )
        xs = list(c.exchanges)
        for i, x in enumerate(xs):
            if x.method != "eth_call":
                continue
            request = json.loads(x.request_payload)
            data = request["params"][0]["data"]
            if data.startswith("0x6352211e"):
                xs[i] = change_result(x, "0x" + encode(["address"], [OWNER]).hex())
            elif len(bytes.fromhex(result(x)[2:])) == 32:
                xs[i] = change_result(x, "0x" + encode(["uint128"], [snapshot.liquidity]).hex())
            elif len(bytes.fromhex(result(x)[2:])) == 96:
                xs[i] = change_result(
                    x,
                    "0x"
                    + encode(["uint128", "uint256", "uint256"], [snapshot.liquidity, 0, 0]).hex(),
                )
        c = replace(c, snapshot=snapshot, exchanges=tuple(xs))
        snapshots.append(snapshot)
        snapshot_refs.append(
            tuple(persist_position_snapshot_evidence(store, replace(c, source=s)) for s in SOURCES)
        )

    def ranges(dataset):
        return tuple(
            persist_and_verify_block_range(
                store,
                replace(dataset, source=s),
                range_exchanges(replace(dataset, source=s)),
                research_single_provider=True,
            ).evidence
            for s in SOURCES
        )

    payment_refs = tuple(
        persist_owner_payment(
            store, replace(payment, source=s, core=replace(core, source=s))
        ).settlement_evidence
        for s in SOURCES
    )
    window = CashWindowRequest(
        CORE,
        *snapshot_refs,
        ranges(core_events),
        ranges(replace(nft_events, logs=())),
        (payment_refs,),
    )
    cost = cost_fixture(4663)
    cost_xs = list(cost.exchanges)
    cost_xs[2] = change_result(cost_xs[2], receipt)
    cost = TransactionCostCapture(
        core.header, core.entry.transaction_hash, SOURCES[0], tuple(cost_xs)
    )
    native = price(cost, ZERO_ADDRESS, 2000 * 10**8)
    plan = AccountGasPlan(core.header, OWNER, SOURCES, 2)
    hdr = result(core.exchanges[1])
    tx = dict(
        hash=core.entry.transaction_hash,
        blockHash=hdr["hash"],
        blockNumber=hdr["number"],
        transactionIndex="0x0",
        **{
            "from": OWNER,
            "to": POSM,
            "type": "0x2",
            "input": result(core.exchanges[3])["input"],
            "value": "0x0",
            "gas": "0x10000",
            "nonce": "0x0",
        },
    )
    gas_capture = AccountGasCapture(
        plan,
        SOURCES[0],
        wires(
            plan.requests(),
            [hex(4663), {**hdr, "transactions": [tx]}, {**hdr, "transactions": [tx["hash"]]}],
        ),
    )
    gas = AccountGasRequest(
        opening_header,
        closing_header,
        OWNER,
        SOURCES,
        native.policy,
        (
            AccountGasBlockInput(
                plan,
                tuple(
                    retain_account_gas_block(store, replace(gas_capture, source=s)) for s in SOURCES
                ),
                (tuple(persist_transaction_cost(store, replace(cost, source=s)) for s in SOURCES),),
                tuple(
                    persist_price_observation_evidence(store, replace(native, source=s))
                    for s in SOURCES
                ),
            ),
        ),
    )
    feed = ManifestFeed(
        address=native.policy.address,
        description=native.policy.description,
        runtime_keccak=runtime_hash("0x6000"),
        max_age_seconds=60,
    )
    currencies = (snapshots[0].pool_key.currency0, snapshots[0].pool_key.currency1)
    assets = (
        ManifestAsset(
            asset_id="native",
            address=ZERO_ADDRESS,
            decimals=18,
            runtime_keccak=None,
            role="cash",
            feed=feed,
        ),
        *tuple(
            ManifestAsset(
                asset_id=f"token{i}",
                address=t,
                decimals=0,
                runtime_keccak=runtime_hash("0x6000"),
                role="cash",
                feed=feed.model_copy(
                    update={"address": "0x" + f"{90 + i:02x}" * 20, "description": "TOKEN / USD"}
                ),
            )
            for i, t in enumerate(currencies)
        ),
    )
    position = ManifestPosition(
        asset_id="lp",
        position_manager=POSM,
        position_manager_runtime_keccak=runtime_hash("0x6001"),
        token_id=7,
        pool_id=snapshots[0].pool_id,
        state_view=VIEW,
        state_view_runtime_keccak=runtime_hash("0x6002"),
        pool_manager=CORE,
        pool_manager_runtime_keccak=runtime_hash("0x6000"),
        currency0_id="token0",
        currency1_id="token1",
    )
    entry = ChainInventory(
        chain_id=4663,
        account=OWNER,
        primary_source=SOURCES[0],
        secondary_source=SOURCES[1],
        assets=assets,
        positions=(position,),
    )
    manifest = PortfolioManifest(policy=risk_policy(entry), chains=(entry,))
    observations = []
    for closing, header in enumerate((opening_header, closing_header)):
        h = {
            "number": hex(header.ref.number),
            "hash": header.ref.block_hash,
            "parentHash": header.parent_hash,
            "timestamp": hex(header.ref.timestamp),
        }
        units = (
            10**15 - 200 * closing,
            1000 - 100 * closing * direction,
            1000 - 200 * closing * direction,
        )
        values = [hex(4663), h, hex(units[0])]
        for amount in units[1:]:
            values += [
                "0x6000",
                "0x" + encode(["uint8"], [0]).hex(),
                "0x" + encode(["uint256"], [amount]).hex(),
            ]
        values += ["0x6001", "0x" + encode(["address"], [OWNER]).hex()]
        for asset in assets:
            answer = 2000 * 10**8 if asset.is_native else 10**6
            values += [
                "0x6000",
                "0x" + encode(["uint8"], [8]).hex(),
                "0x" + encode(["string"], [asset.feed.description]).hex(),
                "0x"
                + encode(
                    ["uint80", "int256", "uint256", "uint256", "uint80"],
                    [2, answer, header.ref.timestamp - 100, header.ref.timestamp, 2],
                ).hex(),
            ]
        values.append(h)
        inv = AccountInventoryCapture(
            SOURCES[0], header, entry, wires(inventory_requests(entry, header), values)
        )
        inventory = tuple(
            persist_inventory(store, replace(inv, source=s), retrieved_at=None) for s in SOURCES
        )
        state = Slot0State(2**96, 0, 0, 3000)
        fields = {
            "source": SOURCES[0],
            "headers": (header,),
            "state_view": VIEW,
            "pool_manager": CORE,
            "pool_id": position.pool_id,
            "state_view_runtime_keccak": position.state_view_runtime_keccak,
            "pool_manager_runtime_keccak": position.pool_manager_runtime_keccak,
            "slot0": (state,),
        }
        sv = [
            hex(4663),
            h,
            "0x6002",
            "0x6000",
            "0x" + encode(["address"], [CORE]).hex(),
            "0x" + encode(["uint160", "int24", "uint24", "uint24"], [2**96, 0, 0, 3000]).hex(),
            h,
        ]
        slot = Slot0WindowCapture(
            **fields, exchanges=wires(_expected_calls(SimpleNamespace(**fields)), sv)
        )
        pool_state = tuple(
            persist_slot0_capture(
                store, replace(slot, source=s), retrieved_at="2026-10-08T00:00:00+00:00"
            )
            for s in SOURCES
        )
        observations.append(
            AccountObservation(
                inventory, positions=(PositionEvidence("lp", snapshot_refs[closing], pool_state),)
            )
        )
    flows = []
    headers = [result(core.exchanges[1])]
    old_hdr = dict(
        headers[0],
        number=hex(10),
        hash=opening_header.ref.block_hash,
        parentHash=opening_header.parent_hash,
        timestamp=hex(1000),
    )
    for i, token in enumerate(currencies):
        scope = FlowScope(
            4663,
            OWNER,
            f"token{i}",
            token,
            0,
            10,
            11,
            start_block_hash=opening_header.ref.block_hash,
            end_block_hash=closing_header.ref.block_hash,
        )
        logs = [r for r in receipt["logs"] if r["address"] == token]
        incoming, logs = (logs, []) if decrease else ([], logs)
        if extra_flow and i == 0:
            # Same net balance with extra unknown incoming/outgoing activity.
            incoming = [
                {
                    **logs[0],
                    "topics": [logs[0]["topics"][0], logs[0]["topics"][2], logs[0]["topics"][1]],
                    "logIndex": "0x7",
                }
            ]
            logs = [*logs, {**logs[0], "logIndex": "0x8"}]
        capture = TokenFlowCapture(
            SOURCES[0],
            scope,
            wires(
                _expected(scope, core_events.blocks),
                [hex(4663), old_hdr, headers[0], incoming, logs, headers[0]],
            ),
        )
        flows.append(
            AssetFlowEvidence(
                scope.asset_id,
                tuple(
                    persist_flow(store, replace(capture, source=s), retrieved_at=None)
                    for s in SOURCES
                ),
            )
        )
    observations[1] = replace(
        observations[1],
        flows=tuple(flows),
        native_gas_evidence=gas,
        lp_cash=(AccountLpCashEvidence("lp", window),),
    )
    return manifest, {entry.key: observations[0]}, {entry.key: observations[1]}


def frames(store, manifest, opening, closing):
    first = EvidenceReplayAccountRiskReader(store, manifest, opening).reconcile()
    last = EvidenceReplayAccountRiskReader(store, manifest, closing, previous=first).reconcile()
    return first, last


def test_wallet_lp_transfer_does_not_hide_loss_and_cli_replays_raw(tmp_path):
    path = tmp_path / "evidence.db"
    store = EvidenceStore(path)
    manifest, opening, closing = build(store)
    first, last = frames(store, manifest, opening, closing)
    assert last.accounts[0].unresolved_operations == 0
    assert last.accounts[0].non_trading_flow_usd_micros == 0
    assert last.accounts[0].movements["token0"] == -100
    assert last.accounts[0].movements["token1"] == -200
    state = reconcile(manifest.policy, RiskState(frame=first), last, now=1001)
    assert state.cumulative_loss > 0
    carried = reconcile(
        manifest.policy, RiskState(frame=first, cumulative_loss=42_000_000), last, now=1001
    )
    assert carried.cumulative_loss == 42_000_000 + state.cumulative_loss
    store.close()
    request = {
        "schema": "oasis.account-risk-gas-input.v1",
        "manifest": manifest.model_dump(mode="json"),
        "opening": {k: asdict(v) for k, v in opening.items()},
        "closing": {k: asdict(v) for k, v in closing.items()},
    }
    p = tmp_path / "input.json"
    p.write_bytes(canonical_json(request))
    before = path.read_bytes()
    run(p, path, tmp_path / "out.json")
    assert path.read_bytes() == before
    assert json.loads((tmp_path / "out.json").read_bytes())["execution_eligible"] is False


def test_extra_offsetting_transfer_stays_unresolved(tmp_path):
    store = EvidenceStore(tmp_path / "evidence.db")
    try:
        m, opening, closing = build(store, extra_flow=True)
        _, last = frames(store, m, opening, closing)
        assert last.accounts[0].non_trading_flow_usd_micros == 0
        assert last.accounts[0].unresolved_operations == 1
    finally:
        store.close()


def test_decrease_cash_stays_internal_and_does_not_duplicate_gas(tmp_path):
    store = EvidenceStore(tmp_path / "evidence.db")
    try:
        m, opening, closing = build(store, decrease=True)
        first, last = frames(store, m, opening, closing)
        assert last.accounts[0].unresolved_operations == 0
        assert last.accounts[0].non_trading_flow_usd_micros == 0
        assert last.accounts[0].movements["token0"] == 100
        assert last.accounts[0].movements["token1"] == 200
        assert last.accounts[0].movements["native"] == -200
        assert last.accounts[0].assets[0].units == first.accounts[0].assets[0].units - 200
    finally:
        store.close()


@pytest.mark.parametrize(
    "case",
    [
        "no_bridge",
        "missing_payment",
        "duplicate_payment",
        "wrong_pool",
        "wrong_position",
        "reversed_providers",
        "prior_snapshot",
        "missing_flow",
    ],
)
def test_incomplete_or_foreign_cash_cannot_clear_guard(tmp_path, case):
    store = EvidenceStore(tmp_path / "evidence.db")
    try:
        m, opening, closing = build(store)
        first, original = frames(store, m, opening, closing)
        assert original.accounts[0].unresolved_operations == 0
        key = m.keys[0]
        observation = closing[key]
        proof = observation.lp_cash[0]
        window = proof.window
        if case == "no_bridge":
            last = EvidenceReplayAccountRiskReader(
                store, m, {key: replace(observation, lp_cash=())}, previous=first
            ).reconcile()
            assert last.accounts[0].unresolved_operations == 2
            assert last.accounts[0].non_trading_flow_usd_micros == 0
            return
        if case == "missing_payment":
            window = replace(window, payments=())
        elif case == "duplicate_payment":
            window = replace(window, payments=window.payments * 2)
        elif case == "wrong_pool":
            window = replace(window, pool_manager=POSM)
        elif case == "wrong_position":
            proof = replace(proof, asset_id="undeclared")
        elif case == "reversed_providers":
            window = replace(window, opening=tuple(reversed(window.opening)))
        elif case == "prior_snapshot":
            account = first.accounts[0]
            assets = tuple(
                a.model_copy(update={"evidence_hash": "0x" + "ef" * 32})
                if a.asset_id == "lp"
                else a
                for a in account.assets
            )
            first = first.model_copy(
                update={"accounts": (account.model_copy(update={"assets": assets}),)}
            )
        elif case == "missing_flow":
            observation = replace(observation, flows=observation.flows[:1])
        closing = {key: replace(observation, lp_cash=(replace(proof, window=window),))}
        with pytest.raises(ValueError):
            EvidenceReplayAccountRiskReader(store, m, closing, previous=first).reconcile()
    finally:
        store.close()


@pytest.mark.parametrize(
    "case", ["input", "receipt", "token_code", "protocol_code", "decimals", "log_index", "balances"]
)
def test_valid_components_with_different_raw_values_cannot_be_joined(tmp_path, case):

    store = EvidenceStore(tmp_path / "evidence.db")
    try:
        m, opening, closing = build(store)
        first, last = frames(store, m, opening, closing)
        assert last.accounts[0].unresolved_operations == 0
        key = m.keys[0]
        o = closing[key]
        gas = o.native_gas_evidence
        block = gas.blocks[0]
        if case == "input":
            refs = []
            for ref in block.blocks:
                record = store.get_record(ref)
                xs = list(restore_exchanges(record.request_payload, record.raw_response_payload))
                value = result(xs[1])
                value["transactions"][0]["input"] = "0x"
                xs[1] = change_result(xs[1], value)
                altered = retain_account_gas_block(
                    store, AccountGasCapture(block.plan, ref.source, tuple(xs))
                )
                replay_account_gas_block(store, altered, block.plan)
                refs.append(altered)
            o = replace(
                o, native_gas_evidence=replace(gas, blocks=(replace(block, blocks=tuple(refs)),))
            )
        elif case == "receipt":
            refs = []
            for ref in block.costs[0]:
                record = store.get_record(ref)
                xs = list(restore_exchanges(record.request_payload, record.raw_response_payload))
                value = result(xs[2])
                value["effectiveGasPrice"] = "0x3"
                xs[2] = change_result(xs[2], value)
                c = TransactionCostCapture(
                    gas.closing, value["transactionHash"], ref.source, tuple(xs)
                )
                refs.append(persist_transaction_cost(store, c))
            o = replace(
                o, native_gas_evidence=replace(gas, blocks=(replace(block, costs=(tuple(refs),)),))
            )
        elif case == "log_index":
            flow = o.flows[0]
            refs = []
            for ref in flow.references:
                record = store.get_record(ref)
                payload = json.loads(record.payload)
                scope = FlowScope(**payload["scope"])
                xs = list(restore_exchanges(record.request_payload, record.raw_response_payload))
                logs = result(xs[4])
                logs[0]["logIndex"] = "0x9"
                xs[4] = change_result(xs[4], logs)
                capture = TokenFlowCapture(ref.source, scope, tuple(xs))
                refs.append(persist_flow(store, capture, retrieved_at=None))
            o = replace(o, flows=(replace(flow, references=tuple(refs)), o.flows[1]))
        else:
            proof = o.lp_cash[0]
            refs = []
            for ref in proof.window.payments[0]:
                record = store.get_record(ref)
                payload = json.loads(record.payload)
                core_ref = TypeAdapter(EvidenceRef).validate_python(payload["core_evidence"])

                payment = replay_cash_window(store, proof.window).payments[0]
                core = replay_position_settlement_evidence(
                    store,
                    core_ref,
                    header=gas.closing,
                    pool_manager=CORE,
                    pool_key=scenario()[0].pool_key,
                    entry=payment.entry,
                )
                xs = list(restore_exchanges(record.request_payload, record.raw_response_payload))
                if case == "protocol_code":
                    core_xs = list(core.exchanges)
                    core_xs[4] = change_result(core_xs[4], "0x6008")
                    core = replace(core, exchanges=tuple(core_xs))
                elif case == "token_code":
                    for index in (3, 4):
                        xs[index] = change_result(xs[index], "0x6008")
                elif case == "balances":
                    for index in (6, 7):
                        xs[index] = change_result(
                            xs[index], f"0x{int(result(xs[index]), 16) + 1:064x}"
                        )
                else:
                    xs[5] = change_result(xs[5], "0x" + encode(["uint8"], [18]).hex())
                capture = OwnerBalanceCapture(core, OWNER, ref.source, tuple(xs))
                refs.append(persist_owner_payment(store, capture).settlement_evidence)
            o = replace(
                o, lp_cash=(replace(proof, window=replace(proof.window, payments=(tuple(refs),))),)
            )
        with pytest.raises(
            ValueError, match="runtime hash mismatch" if case == "token_code" else "LP cash"
        ):
            EvidenceReplayAccountRiskReader(store, m, {key: o}, previous=first).reconcile()
    finally:
        store.close()


@pytest.mark.parametrize("case", ["missing_gas", "native_override", "duplicate", "unknown_field"])
def test_no_scalar_or_schema_bypass(tmp_path, case):

    store = EvidenceStore(tmp_path / "evidence.db")
    try:
        m, opening, closing = build(store)
        o = closing[m.keys[0]]
        with pytest.raises((TypeError, ValueError)):
            if case == "missing_gas":
                replace(o, native_gas_evidence=None)
            elif case == "native_override":
                replace(o, non_trading_native_units=1)
            elif case == "duplicate":
                replace(o, lp_cash=o.lp_cash * 2)
            else:
                request = {
                    "schema": "oasis.account-risk-gas-input.v1",
                    "manifest": m.model_dump(mode="json"),
                    "opening": {k: asdict(v) for k, v in opening.items()},
                    "closing": {k: asdict(v) for k, v in closing.items()},
                }
                request["closing"][m.keys[0]]["lp_cash"][0]["internal_cash_units"] = -100
                Request.from_json(canonical_json(request))
    finally:
        store.close()


@pytest.mark.parametrize("case", ["native", "multiple_blocks"])
def test_bridge_refuses_unsupported_scope_before_replay(tmp_path, case):
    store = EvidenceStore(tmp_path / "evidence.db")
    try:
        m, opening, closing = build(store)
        first, last = frames(store, m, opening, closing)
        entry = m.chains[0]
        previous = first.accounts[0]
        if case == "native":
            position = entry.positions[0].model_copy(update={"currency0_id": "native"})
            entry = entry.model_copy(update={"positions": (position,)})
        else:
            previous = previous.model_copy(update={"block_number": previous.block_number - 1})
        o = closing[m.keys[0]]
        with pytest.raises(ValueError, match=r"native LP cash|single-block"):
            replay_account_lp_cash(
                store,
                entry=entry,
                previous=previous,
                closing_units={a.asset_id: a.units for a in last.accounts[0].assets},
                closing_snapshots={p.asset_id: p.snapshots for p in o.positions},
                requests=o.lp_cash,
                flows={},
                gas_request=o.native_gas_evidence,
            )
    finally:
        store.close()


def test_unclassified_cash_cli_refuses_without_output_or_database_write(tmp_path):
    path = tmp_path / "evidence.db"
    store = EvidenceStore(path)
    m, opening, closing = build(store, extra_flow=True)
    store.close()
    request = {
        "schema": "oasis.account-risk-gas-input.v1",
        "manifest": m.model_dump(mode="json"),
        "opening": {k: asdict(v) for k, v in opening.items()},
        "closing": {k: asdict(v) for k, v in closing.items()},
    }
    input_path = tmp_path / "input.json"
    input_path.write_bytes(canonical_json(request))
    before = path.read_bytes()
    output = tmp_path / "refused.json"
    with pytest.raises(ValueError, match="unexplained movement"):
        run(input_path, path, output)
    assert not output.exists()
    assert path.read_bytes() == before
