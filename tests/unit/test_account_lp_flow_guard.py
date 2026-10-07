"""Synthetic raw-wire regressions for missing LP cash-purpose attribution.

Two providers agreeing on balances and Transfer logs do not establish that a
wallet debit is an external capital withdrawal. Owned LP value already enters
NAV, so unresolved principal conversion cannot reduce measured account loss.
"""

import json
from dataclasses import asdict, replace
from hashlib import sha256
from random import Random

import pytest

from oasis_agent.evidence.rpc_validation import restore_exchanges
from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.execution.account_risk_reader import AssetFlowEvidence
from oasis_agent.farming.account_flows import (
    FlowScope,
    TokenFlow,
    TransferLog,
    collect_token_flow_quorum,
    persist_token_flow_evidence,
    reconstruct_movements,
)
from oasis_agent.farming.account_inventory import AccountInventoryCapture, _persist_capture
from oasis_agent.models.common import canonical_json
from oasis_agent.risk.account_risk import Commitment, GlobalRiskJournal, RiskState, reconcile
from scripts.replay_account_risk_gas import run
from tests.unit import test_account_risk_reader as f
from tests.unit.test_account_flows import FlowFixture, transfer_log
from tests.unit.test_account_risk_gas_binding import scenario


def frames(store, *, units, counterparty, currency="usdc", round_trip=False, with_lp=True):
    initial = f.InventoryFixture(positions=(f.PositionSpec(),) if with_lp else ())
    key = f.chain_inventory(initial).key
    opening_proofs = (
        f.observe_position(store, initial, snapshot=f.position_snapshot(liquidity=10**9))
        if with_lp
        else ()
    )
    first = f.reader_for(
        store,
        fixtures=(initial,),
        observations={
            key: f.observation(f.observe_inventory(store, initial), positions=opening_proofs)
        },
    ).reconcile()
    token = next(t for t in initial.tokens if t.asset_id == currency)
    final = f.later(f.retoken(initial, asset_id=currency, units=token.units + units))

    def log(amount, incoming, index):
        return transfer_log(
            from_address=counterparty if incoming else f.ACCOUNT,
            to_address=f.ACCOUNT if incoming else counterparty,
            units=amount,
            block_number=f.BLOCK_TWO,
            log_index=index,
            token=token.address,
        )

    incoming = (log(abs(units), True, 0),) if units >= 0 else ()
    outgoing = (log(abs(units), False, 0),) if units < 0 else ()
    if round_trip:
        incoming = (log(100_000_000, True, 0),)
        outgoing = (log(100_000_000, False, 1),)
    flow = f.observe_flow(
        store,
        final,
        asset_id=currency,
        start_block=f.BLOCK_NUMBER,
        incoming=incoming,
        outgoing=outgoing,
    )
    closing_proofs = (
        f.observe_position(
            store,
            final,
            snapshot=f.position_snapshot(
                block_number=f.BLOCK_TWO,
                block_hash=f.HASH_TWO,
                timestamp=f.TIMESTAMP + 30,
                liquidity=17 * 10**9,
            ),
        )
        if with_lp
        else ()
    )
    current = f.reader_for(
        store,
        fixtures=(final,),
        observations={
            key: f.observation(
                f.observe_inventory(store, final),
                positions=closing_proofs,
                flows=(AssetFlowEvidence(currency, flow),),
            )
        },
        previous=first,
    ).reconcile()
    return first, current, f.risk_policy(f.chain_inventory(final))


@pytest.mark.parametrize("counterparty", [f.POOL_MANAGER, f.POSITION_MANAGER, f.THIRD_PARTY])
@pytest.mark.parametrize("units", [-100_000_000, 100_000_000])
def test_lp_currency_transfer_cannot_be_credited_as_external_capital(tmp_path, counterparty, units):
    with_store = EvidenceStore(tmp_path / "proofs.db")
    try:
        first, current, policy = frames(with_store, units=units, counterparty=counterparty)
        row = current.accounts[0]
        assert row.movements["usdc"] == units
        assert row.unresolved_operations == 1
        assert row.non_trading_flow_usd_micros == 0
        if units < 0:
            # The original reader credited -$100 and suppressed this entire loss.
            assert first.accounts[0].nav - row.nav == 4_292_196
        with pytest.raises(ValueError, match="unresolved or stale account state"):
            reconcile(policy, RiskState(frame=first), current, now=f.TIMESTAMP + 30)
    finally:
        with_store.close()


@pytest.mark.parametrize("currency", ["usdc", "weth"])
def test_both_lp_currencies_require_cash_purpose_even_if_net_units_are_zero(tmp_path, currency):
    store = EvidenceStore(tmp_path / "round-trip.db")
    try:
        _, current, _ = frames(
            store, units=0, counterparty=f.THIRD_PARTY, currency=currency, round_trip=True
        )
        row = current.accounts[0]
        assert row.movements[currency] == 0
        assert row.unresolved_operations == 1
        assert row.non_trading_flow_usd_micros == 0
    finally:
        store.close()


def test_zero_value_transfer_does_not_invent_an_economic_movement(tmp_path):
    store = EvidenceStore(tmp_path / "zero.db")
    try:
        first, current, policy = frames(store, units=0, counterparty=f.POOL_MANAGER)
        row = current.accounts[0]
        assert (row.unresolved_operations, row.non_trading_flow_usd_micros) == (0, 0)
        assert reconcile(policy, RiskState(frame=first), current, now=f.TIMESTAMP + 30)
    finally:
        store.close()


@pytest.mark.parametrize("units", [-100_000_000, 100_000_000])
def test_accounts_without_declared_lp_keep_the_separate_transfer_path(tmp_path, units):
    store = EvidenceStore(tmp_path / "external.db")
    try:
        first, current, policy = frames(
            store, units=units, counterparty=f.THIRD_PARTY, with_lp=False
        )
        row = current.accounts[0]
        assert row.unresolved_operations == 0
        assert row.non_trading_flow_usd_micros == units
        assert (
            reconcile(policy, RiskState(frame=first), current, now=f.TIMESTAMP + 30).cumulative_loss
            == 0
        )
    finally:
        store.close()


@pytest.mark.parametrize("units", [-(10**14), 0, 10**14])
def test_native_lp_cash_cannot_use_a_non_trading_scalar_override(tmp_path, units):
    store = EvidenceStore(tmp_path / "native.db")
    try:
        initial = f.InventoryFixture(positions=(f.PositionSpec(native_pool=True),))
        first = f.position_frame(store, initial)
        final = f.later(replace(initial, native_units=initial.native_units + units - 200))
        key = f.chain_inventory(final).key
        current = f.reader_for(
            store,
            fixtures=(final,),
            observations={
                key: f.observation(
                    f.observe_inventory(store, final),
                    positions=f.observe_position(
                        store,
                        final,
                        snapshot=f.position_snapshot(
                            native_pool=True,
                            block_number=f.BLOCK_TWO,
                            block_hash=f.HASH_TWO,
                            timestamp=f.TIMESTAMP + 30,
                        ),
                    ),
                    native_gas_units=200,
                    non_trading_native_units=units,
                )
            },
            previous=first,
        ).reconcile()
        row = current.accounts[0]
        assert row.movements["native"] == units - 200
        assert (row.unresolved_operations, row.non_trading_flow_usd_micros) == (bool(units), 0)
        if units:
            with pytest.raises(ValueError, match="unresolved or stale account state"):
                reconcile(
                    f.risk_policy(f.chain_inventory(final)),
                    RiskState(frame=first),
                    current,
                    now=f.TIMESTAMP + 30,
                )
        else:
            assert row.nav < first.accounts[0].nav
            reconcile(
                f.risk_policy(f.chain_inventory(final)),
                RiskState(frame=first),
                current,
                now=f.TIMESTAMP + 30,
            )
    finally:
        store.close()


def test_a_refused_lp_cash_frame_cannot_append_or_admit_a_commitment(tmp_path):
    store = EvidenceStore(tmp_path / "proofs.db")
    try:
        first, current, policy = frames(store, units=-100_000_000, counterparty=f.POOL_MANAGER)
        journal = GlobalRiskJournal(tmp_path / "risk.db", policy)
        try:
            journal.admit(
                first,
                Commitment(request_hash="a" * 64, account_key=first.accounts[0].key, usd_micros=1),
                now=f.TIMESTAMP,
            )
            before = journal.current_state()
            count = journal.db.execute("SELECT count(*) FROM global_events").fetchone()[0]
            with pytest.raises(ValueError, match="unresolved or stale account state"):
                journal.admit(
                    current,
                    Commitment(
                        request_hash="b" * 64, account_key=first.accounts[0].key, usd_micros=1
                    ),
                    now=f.TIMESTAMP + 30,
                )
            assert journal.current_state() == before
            assert journal.db.execute("SELECT count(*) FROM global_events").fetchone()[0] == count
        finally:
            journal.close()
    finally:
        store.close()


def cli_input(store):
    """Full synthetic account gas plus an owned LP currency debit in the same window."""
    manifest, opening, closing, gas = scenario(store, with_position=True)
    key = manifest.keys[0]
    entry = manifest.chains[0]
    token = entry.asset("usdc")
    observation = closing[key]
    inventories = []
    for ref in observation.inventory:
        record = store.get_record(ref)
        exchanges = []
        for exchange in restore_exchanges(record.request_payload, record.raw_response_payload):
            retained = exchange
            request = json.loads(exchange.request_payload)
            if (
                exchange.method == "eth_call"
                and request["params"][0]["to"] == token.address
                and request["params"][0]["data"] == f.balance_of_data(entry.account)
            ):
                response = json.loads(exchange.response_payload)
                response["result"] = "0x" + "00" * 32
                retained = replace(exchange, response_payload=canonical_json(response))
            exchanges.append(retained)
        inventories.append(
            _persist_capture(
                store,
                AccountInventoryCapture(ref.source, gas.closing, entry, tuple(exchanges)),
                retrieved_at=ref.retrieved_at,
            )
        )
    scope = FlowScope(
        entry.chain_id,
        entry.account,
        token.asset_id,
        token.address,
        token.decimals,
        gas.opening.ref.number,
        gas.closing.ref.number,
        start_block_hash=gas.opening.ref.block_hash,
        end_block_hash=gas.closing.ref.block_hash,
    )
    headers = {
        h.ref.number: {
            "number": hex(h.ref.number),
            "hash": h.ref.block_hash,
            "parentHash": h.parent_hash,
            "timestamp": hex(h.ref.timestamp),
        }
        for h in (gas.opening, *(block.plan.header for block in gas.blocks))
    }
    fixture = FlowFixture(
        chain_id=entry.chain_id,
        outgoing=[
            transfer_log(
                from_address=entry.account,
                to_address=entry.positions[0].pool_manager,
                token=token.address,
                units=1,
                block_number=gas.closing.ref.number,
            )
        ],
        headers=headers,
        bind_hashes=True,
    )
    flows = persist_token_flow_evidence(
        store,
        collect_token_flow_quorum(
            f.flow_session(entry.primary_source, fixture),
            f.flow_session(entry.secondary_source, replace(fixture, methods=[])),
            scope=scope,
        ),
    )
    closing[key] = replace(
        observation, inventory=tuple(inventories), flows=(AssetFlowEvidence("usdc", flows),)
    )
    return {
        "schema": "oasis.account-risk-gas-input.v1",
        "manifest": manifest.model_dump(mode="json"),
        "opening": {key: asdict(opening[key])},
        "closing": {key: asdict(closing[key])},
    }


def test_complete_gas_does_not_make_missing_lp_cash_purpose_an_accepted_cli_input(tmp_path):
    db = tmp_path / "proofs.db"
    store = EvidenceStore(db)
    try:
        value = cli_input(store)
    finally:
        store.close()
    path = tmp_path / "input.json"
    path.write_bytes(canonical_json(value))
    output = tmp_path / "report.json"
    before = sha256(db.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="unexplained movement refuses"):
        run(path, db, output)
    assert not output.exists()
    assert sha256(db.read_bytes()).hexdigest() == before


@pytest.mark.parametrize("decimals", [0, 6, 18, 36])
def test_unclassified_lp_cash_never_reduces_loss_at_any_integer_scale(decimals):
    """Fixed-seed conservation invariant over sizes, signs and offsetting legs."""
    random = Random(20261004)
    for _ in range(32):
        amount = random.randrange(1, 2**128)
        sign = random.choice((-1, 0, 1))
        delta = sign * amount
        outgoing = TransferLog(
            f.USDC_SPEC.address,
            f.ACCOUNT,
            f.POOL_MANAGER,
            amount,
            f.BLOCK_TWO,
            1,
            "0x" + "7a" * 32,
        )
        incoming = replace(outgoing, from_address=f.POOL_MANAGER, to_address=f.ACCOUNT, log_index=0)
        logs = (outgoing,) if sign < 0 else (incoming,) if sign > 0 else (incoming, outgoing)
        flow = TokenFlow(
            "lp-currency",
            decimals,
            delta,
            delta,
            amount if sign >= 0 else 0,
            amount if sign <= 0 else 0,
            logs,
        )
        result = reconstruct_movements(
            previous_units={"native": 1000, "lp-currency": amount},
            current_units={"native": 800, "lp-currency": amount + delta},
            prices={
                "native": (1, 1, 0),
                "lp-currency": (random.randrange(1, 10**20), 10**8, decimals),
            },
            flows={"lp-currency": flow},
            native_asset_id="native",
            native_gas_units=200,
            unclassified_assets=frozenset({"lp-currency"}),
        )
        assert result.movements == {"native": -200, "lp-currency": delta}
        assert result.non_trading_flow_usd_micros == 0
        assert result.unresolved_operations == 1
