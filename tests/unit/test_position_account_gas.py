"""Raw-proof integration tests only; no synthetic result is a real economic sample."""

import hashlib
import json
import sqlite3
from dataclasses import asdict, replace

import pytest

from oasis_agent.evidence.ingestion import replay_block_range_evidence
from oasis_agent.evidence.rpc_validation import decode_exchange, restore_exchanges
from oasis_agent.farming.account_gas_coverage import (
    AccountGasBlockInput,
    AccountGasCapture,
    AccountGasPlan,
    AccountGasRequest,
    retain_account_gas_block,
)
from oasis_agent.farming.position_account_gas import (
    PositionAccountGasRequest,
    replay_position_account_gas,
)
from oasis_agent.farming.position_capture import replay_position_snapshot_evidence
from oasis_agent.farming.position_hold_return import PositionHoldRequest
from oasis_agent.farming.price_evidence import persist_price_observation_evidence
from oasis_agent.farming.transaction_costs import TransactionCostCapture, persist_transaction_cost
from oasis_agent.models.common import ZERO_ADDRESS, canonical_json
from scripts.replay_position_account_gas import ReplayStore, Request, run
from tests.unit.test_account_gas_coverage import exchange
from tests.unit.test_position_hold_return import inputs as hold_inputs
from tests.unit.test_position_return_replay import PEERS, feed_capture
from tests.unit.test_position_return_replay import inputs as action_inputs
from tests.unit.test_transaction_costs import fixture


def gas_input(store, position, *, empty=False, action=True):
    canonical = replay_block_range_evidence(store, position.cash_window.core[0]).blocks
    opening, closing = canonical
    snapshot = replay_position_snapshot_evidence(store, position.cash_window.closing[0])
    native = feed_capture(snapshot, ZERO_ADDRESS, 200000000000, address="0x" + "83" * 20)
    if not isinstance(position, PositionHoldRequest):
        native_prices = next(p.native_prices for p in position.prices if p.block == closing.ref)
    else:
        native_prices = tuple(
            persist_price_observation_evidence(store, replace(native, source=p)) for p in PEERS
        )
    template = fixture(closing.ref.chain_id)
    raw_header = decode_exchange(template.exchanges[1])[1]["result"]
    txs, costs = [], []
    for index, status in enumerate([] if empty else [1, 0, 1]):
        digest = "0x" + f"{70 + index:02x}" * 32
        if index == 0 and action:
            proof = position.costs[0]
            cost_pair = proof.evidence
            record = store.get_record(cost_pair[0])
            xs = restore_exchanges(record.request_payload, record.raw_response_payload)
            receipt = decode_exchange(xs[2])[1]["result"]
            digest = proof.transaction_hash
        else:
            xs = list(template.exchanges)
            receipt = dict(decode_exchange(xs[2])[1]["result"])
            receipt.update(
                transactionHash=digest, transactionIndex=hex(index), status=hex(status), logs=[]
            )
            response = json.loads(xs[2].response_payload)
            response["result"] = receipt
            request = json.loads(xs[2].request_payload)
            request["params"] = [digest]
            xs[2] = replace(
                xs[2],
                request_payload=canonical_json(request),
                response_payload=canonical_json(response),
            )
            cost_pair = tuple(
                persist_transaction_cost(
                    store, TransactionCostCapture(closing, digest, p, tuple(xs))
                )
                for p in PEERS
            )
        costs.append(cost_pair)
        txs.append(
            {
                "hash": digest,
                "blockHash": closing.ref.block_hash,
                "blockNumber": hex(closing.ref.number),
                "transactionIndex": hex(index),
                "from": receipt["from"],
                "to": receipt["to"],
                "type": receipt["type"],
                "input": "0x",
                "value": "0x0",
                "gas": "0x10000",
                "nonce": hex(index),
            }
        )
    plan = AccountGasPlan(closing, snapshot.snapshot.nft_owner, PEERS, 3)
    specs = [
        hex(closing.ref.chain_id),
        {**raw_header, "transactions": txs},
        {**raw_header, "transactions": [t["hash"] for t in txs]},
    ]
    xs = tuple(
        exchange(m, p, r, i)
        for i, ((m, p), r) in enumerate(zip(plan.requests(), specs, strict=True), 1)
    )
    refs = tuple(retain_account_gas_block(store, AccountGasCapture(plan, p, xs)) for p in PEERS)
    block = AccountGasBlockInput(plan, refs, tuple(costs), native_prices)
    return AccountGasRequest(opening, closing, plan.account, PEERS, native.policy, (block,))


def joined_proofs(tmp_path, mode):
    generator = (action_inputs if mode == "action" else hold_inputs).__wrapped__(tmp_path)
    store, position, *_ = next(generator)
    try:
        gas = gas_input(store, position, action=mode == "action")
        yield store, PositionAccountGasRequest(position, gas)
    finally:
        generator.close()


@pytest.fixture(params=["action", "hold"])
def joined(tmp_path, request):
    yield from joined_proofs(tmp_path, request.param)


@pytest.fixture
def active_joined(tmp_path):
    yield from joined_proofs(tmp_path, "action")


@pytest.fixture
def hold_joined(tmp_path):
    yield from joined_proofs(tmp_path, "hold")


def test_rebuilds_complete_gas_and_deducts_once(joined):
    store, request = joined
    result = replay_position_account_gas(store, request)
    assert result.account_gas.total_usd_micros == 3
    assert result.account_gas.reverted_native == 200
    assert (
        result.marked_position_less_whole_account_gas_usd_micros
        == result.marked_position_before_gas_usd_micros - 3
    )
    if isinstance(request.position, PositionHoldRequest):
        assert result.overlapping_action_transactions == ()
        assert result.other_account_gas_usd_micros == 3
    else:
        assert len(result.overlapping_action_transactions) == 1
        assert result.overlapping_action_gas_usd_micros == 1
        assert result.other_account_gas_usd_micros == 2
        assert (
            result.marked_position_less_whole_account_gas_usd_micros
            == result.position.marked_pnl_after_action_gas_usd_micros - 2
        )
    assert result.position_expense_allocation == "unresolved"
    assert (
        result.net_pnl is None
        and not result.execution_eligible
        and not result.out_of_sample_accepted
    )
    assert not result.canary_accepted


@pytest.mark.parametrize(
    "bad", ["opening", "closing", "providers", "payer", "policy", "missing_receipt"]
)
def test_foreign_or_incomplete_account_component_refuses(joined, bad):
    store, request = joined
    gas = request.account_gas
    if bad == "opening":
        gas = replace(
            gas, opening=replace(gas.opening, ref=replace(gas.opening.ref, timestamp=999))
        )
    elif bad == "closing":
        gas = replace(gas, closing=gas.opening)
    elif bad == "providers":
        gas = replace(gas, providers=tuple(reversed(gas.providers)))
    elif bad == "payer":
        gas = replace(gas, account="0x" + "44" * 20)
    elif bad == "policy":
        gas = replace(gas, native_policy=replace(gas.native_policy, max_age_seconds=1))
    else:
        gas = replace(gas, blocks=(replace(gas.blocks[0], costs=gas.blocks[0].costs[:-1]),))
    with pytest.raises(ValueError):
        replay_position_account_gas(store, replace(request, account_gas=gas))


def test_cli_rebuilds_both_inputs_and_never_overwrites(joined, tmp_path):
    _store, request = joined
    path, out = tmp_path / "input.json", tmp_path / "report.json"
    path.write_bytes(
        canonical_json(
            {"schema": "oasis.position-account-gas-input.v1", "request": asdict(request)}
        )
    )
    digest = hashlib.sha256((tmp_path / "cash.db").read_bytes()).hexdigest()
    run(path, tmp_path / "cash.db", out)
    assert hashlib.sha256((tmp_path / "cash.db").read_bytes()).hexdigest() == digest
    data = json.loads(out.read_bytes())
    assert data["net_pnl"] is None and not data["target_admitted"]
    assert data["account_gas"]["total_usd_micros"] == 3
    assert data["other_account_gas_usd_micros"] in (2, 3)
    with pytest.raises(ValueError, match="exclusive"):
        run(path, tmp_path / "cash.db", out)
    parsed = json.loads(path.read_bytes())
    parsed["request"]["net_pnl"] = 0
    with pytest.raises(ValueError):
        Request.from_json(canonical_json(parsed))


def test_claimed_complete_account_cannot_omit_observed_lp_transaction(active_joined):
    store, request = active_joined
    gas = gas_input(store, request.position, action=False)
    with pytest.raises(ValueError, match="each successful LP action"):
        replay_position_account_gas(store, replace(request, account_gas=gas))


@pytest.mark.parametrize("which", ["cost", "price"])
def test_equal_amounts_cannot_replace_overlapping_raw_references(active_joined, which):
    store, request = active_joined
    block = request.account_gas.blocks[0]
    refs = block.costs[0] if which == "cost" else block.native_prices
    alternate = []
    for ref in refs:
        record = store.get_record(ref)
        alternate.append(
            store.put(
                ref.kind,
                record.payload,
                ref.block,
                ref.source,
                request=record.request_payload,
                raw_response=record.raw_response_payload,
                interpretation=json.loads(record.interpretation_payload),
                schema_version=ref.schema_version,
                retrieved_at="2026-10-03T00:00:00Z",
            )
        )
    assert tuple(alternate) != refs
    if which == "cost":
        block = replace(block, costs=(tuple(alternate), *block.costs[1:]))
    else:
        block = replace(block, native_prices=tuple(alternate))
    gas = replace(request.account_gas, blocks=(block,))
    with pytest.raises(ValueError, match="references differ"):
        replay_position_account_gas(store, replace(request, account_gas=gas))


def test_zero_account_gas_requires_full_block_pair_and_native_price(hold_joined):
    store, request = hold_joined
    gas = gas_input(store, request.position, empty=True, action=False)
    result = replay_position_account_gas(store, replace(request, account_gas=gas))
    assert result.account_gas.total_usd_micros == 0
    assert (
        result.marked_position_less_whole_account_gas_usd_micros
        == result.position.marked_lp_pnl_usd_micros
    )
    gas = replace(gas, blocks=(replace(gas.blocks[0], blocks=gas.blocks[0].blocks[:1]),))
    with pytest.raises(ValueError, match="two-provider"):
        replay_position_account_gas(store, replace(request, account_gas=gas))


def test_cli_refusal_produces_no_report(joined, tmp_path):
    _store, request = joined
    gas = request.account_gas
    gas = replace(gas, blocks=(replace(gas.blocks[0], costs=()),))
    payload = {
        "schema": "oasis.position-account-gas-input.v1",
        "request": asdict(replace(request, account_gas=gas)),
    }
    path, out = tmp_path / "bad.json", tmp_path / "refused.json"
    path.write_bytes(canonical_json(payload))
    with pytest.raises(ValueError, match="every account transaction"):
        run(path, tmp_path / "cash.db", out)
    assert not out.exists()


def test_valid_foreign_account_gas_is_not_lp_owner_expense(joined):
    store, request = joined
    gas = request.account_gas
    foreign = "0x" + "44" * 20
    block = gas.blocks[0]
    # The existing full blocks contain only the original owner transactions.
    # Selecting a different real account correctly gives zero transactions,
    # but must not make its zero expense valid for the LP owner.
    record = store.get_record(block.blocks[0])
    xs = restore_exchanges(record.request_payload, record.raw_response_payload)
    plan = replace(block.plan, account=foreign)
    refs = tuple(retain_account_gas_block(store, AccountGasCapture(plan, p, xs)) for p in PEERS)
    block = replace(block, plan=plan, blocks=refs, costs=())
    gas = replace(gas, account=foreign, blocks=(block,))
    with pytest.raises(ValueError, match="position owner differs"):
        replay_position_account_gas(store, replace(request, account_gas=gas))


def test_equal_endpoint_ref_does_not_replace_position_header(joined):
    store, request = joined
    gas = request.account_gas
    gas = replace(gas, opening=replace(gas.opening, parent_hash="0x" + "fe" * 32))
    with pytest.raises(ValueError, match="header ranges differ"):
        replay_position_account_gas(store, replace(request, account_gas=gas))


def test_offline_store_cannot_migrate_or_write(tmp_path):
    path = tmp_path / "older.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE retained (id INTEGER)")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    store = ReplayStore(path)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            store._connection.execute("CREATE TABLE unintended (id INTEGER)")
    finally:
        store.close()
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
