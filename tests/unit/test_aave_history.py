"""Aave ABI role attribution cannot turn financing events into profits or balances."""

import json
from dataclasses import replace

import pytest
from eth_abi.abi import encode

from oasis_agent.arbitrage.aave_history import SIGNATURES, AaveHistoryPlan, decode_event, summarize
from oasis_agent.arbitrage.cycle_protocols import topic
from oasis_agent.evidence.public_receipts import ObservedReceipt
from oasis_agent.models.chain import LogRecord
from tests.unit.test_public_receipts import BLOCK, TX

POOL, ACCOUNT, ACTOR, RESERVE, COLLATERAL = ("0x" + f"{i:02x}" * 20 for i in range(1, 6))
RAY = 10**27


def plan():
    return AaveHistoryPlan.model_validate_json(
        json.dumps(
            {
                "schema": "oasis-aave-receipt-analysis-v1",
                "chain_id": 1,
                "account": ACCOUNT,
                "pools": [POOL],
            }
        )
    )


def event(kind, index=0, account=ACCOUNT, amount=100, atokens=False):
    indexed = [("address", RESERVE), ("address", account), ("address", ACTOR)]
    if kind in {"supply", "borrow"}:
        indexed[-1] = ("uint16", 7)
    if kind == "supply":
        types, values = ("address", "uint256"), (ACTOR, amount)
    elif kind == "borrow":
        types, values = ("address", "uint256", "uint8", "uint256"), (ACTOR, amount, 2, RAY // 20)
    elif kind == "repay":
        types, values = ("uint256", "bool"), (amount, atokens)
    elif kind == "withdraw":
        types, values = ("uint256",), (amount,)
    elif kind == "liquidation":
        indexed = [("address", COLLATERAL), ("address", RESERVE), ("address", account)]
        types, values = ("uint256", "uint256", "address", "bool"), (amount, 120, ACTOR, atokens)
    else:
        indexed = [("address", RESERVE)]
        types, values = ("uint256",) * 5, (RAY // 40, 0, RAY // 20, RAY + 1, RAY + 2)
    return LogRecord(
        POOL,
        (topic(SIGNATURES[kind]), *("0x" + encode([t], [v]).hex() for t, v in indexed)),
        "0x" + encode(types, values).hex(),
        BLOCK.number,
        TX,
        index,
        BLOCK.block_hash,
        2,
    )


def receipt(*logs):
    return ObservedReceipt(BLOCK, TX, 2, ACTOR, POOL, 1, 100000, 10**9, tuple(logs))


def test_delegated_supply_borrow_and_external_repay_are_account_roles_not_sender_roles():
    r = summarize(plan(), (receipt(event("supply", 0), event("borrow", 1), event("repay", 2)),))
    assert len(r["account_events"]) == 3
    assert r["account_events"][1]["account"] == ACCOUNT
    assert r["account_events"][1]["actor"] == ACTOR
    assert r["account_events"][1]["recipient"] == ACTOR
    assert r["account_events"][1]["details"]["borrow_rate_ray"] == RAY // 20
    assert r["event_flow_totals"][0]["flows_raw"] == {
        "supply": 100,
        "borrow": 100,
        "repay_underlying": 100,
    }
    assert r["receipts"][0]["gas_sender_is_account"] is False
    assert r["net_profit"] is None and not r["initial_and_final_debt_verified"]
    assert not r["execution_eligible"] and not r["protocol_implementation_verified"]


def test_a_token_repayment_is_separate_from_underlying_cash_repayment():
    r = summarize(plan(), (receipt(event("repay", atokens=True)),))
    assert r["event_flow_totals"][0]["flows_raw"] == {"repay_atokens": 100}
    assert not r["token_transfers_reconciled"]


def test_liquidation_has_two_distinct_assets_not_a_single_signed_amount():
    r = summarize(plan(), (receipt(event("liquidation", atokens=True)),))
    flows = {row["reserve"]: row for row in r["event_flow_totals"]}
    assert flows[RESERVE]["flows_raw"] == {"liquidated_debt": 100}
    assert flows[COLLATERAL]["flows_raw"] == {"liquidated_collateral": 120}
    assert all(
        row["decimals"] is None and row["unit"] == "reserve_base_units" for row in flows.values()
    )
    assert r["account_events"][0]["details"]["receive_atoken"] is True


def test_interest_indexes_are_context_and_withdraw_excess_is_not_profit():
    r = summarize(
        plan(),
        (
            receipt(
                event("supply", 0), event("withdraw", 1, amount=110), event("reserve_update", 2)
            ),
        ),
    )
    assert r["event_flow_totals"][0]["flows_raw"] == {"supply": 100, "withdraw": 110}
    assert r["reserve_updates"][0]["details"]["variable_borrow_index_ray"] == RAY + 2
    assert r["net_profit"] is None and not r["complete_lending_history"]


def test_foreign_account_or_pool_cannot_be_attributed_to_user():
    logs = (event("borrow", account=ACTOR), replace(event("supply", 1), address=ACTOR))
    r = summarize(plan(), (receipt(*logs),))
    assert r["account_events"] == [] and r["event_flow_totals"] == []
    assert r["coverage"] == "selected_receipts_only"


def test_duplicate_receipt_and_chain_mismatch_refuse():
    r = receipt(event("borrow"))
    with pytest.raises(ValueError, match="duplicate_receipt"):
        summarize(plan(), (r, r))
    with pytest.raises(ValueError, match="chain_outside"):
        summarize(plan(), (replace(r, block=replace(BLOCK, chain_id=8453)),))


@pytest.mark.parametrize("kind", list(SIGNATURES))
@pytest.mark.parametrize("corruption", ["trailing_data", "missing_topic", "address_padding"])
def test_noncanonical_aave_event_invalidates_interpretation(kind, corruption):
    log = event(kind)
    if corruption == "trailing_data":
        log = replace(log, data=log.data + "00" * 32)
    elif corruption == "missing_topic":
        log = replace(log, topics=log.topics[:-1])
    else:
        log = replace(log, topics=(log.topics[0], "0x" + "ff" * 32, *log.topics[2:]))
    with pytest.raises(ValueError):
        summarize(plan(), (receipt(log),))


def test_unknown_event_has_no_financial_interpretation():
    log = replace(event("borrow"), topics=("0x" + "ee" * 32,))
    assert decode_event(log) is None


@pytest.mark.parametrize("mode", [0, 3])
def test_invalid_borrow_mode_refused(mode):
    log = replace(
        event("borrow"),
        data="0x"
        + encode(["address", "uint256", "uint8", "uint256"], [ACTOR, 100, mode, RAY]).hex(),
    )
    with pytest.raises(ValueError, match="borrow_rate_mode"):
        decode_event(log)


def test_reverted_receipt_remains_a_cost_observation_without_events():
    r = summarize(plan(), (replace(receipt(), status=0),))
    assert r["receipts"][0]["receipt_gas_component_native_raw"] == 10**14
    assert r["account_events"] == [] and r["net_profit"] is None
