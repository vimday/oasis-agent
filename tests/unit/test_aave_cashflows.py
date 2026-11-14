"""Cashflow corroboration pairs underlying transfers without using aToken amounts."""

from dataclasses import replace

import pytest
from eth_abi.abi import encode

from oasis_agent.arbitrage.aave_cashflows import (
    TRANSFER,
    Plan,
    Reserve,
    position_snapshot,
    reconcile,
)
from oasis_agent.arbitrage.cycle_protocols import topic
from oasis_agent.models.common import ZERO_ADDRESS
from tests.unit.test_aave_history import ACCOUNT, ACTOR, POOL, RESERVE, event, plan, receipt

ATOKEN = "0x" + "42" * 20


def binding():
    return Plan(
        schema="oasis-aave-cashflow-plan-v1",
        reserves=(Reserve(pool=POOL, underlying=RESERVE, a_token=ATOKEN, decimals=6),),
    )


def transfer(token, src, dst, amount, index):
    return replace(
        event("supply", index),
        address=token,
        topics=(TRANSFER, *("0x" + encode(["address"], [value]).hex() for value in (src, dst))),
        data="0x" + encode(["uint256"], [amount]).hex(),
    )


def test_supply_uses_actor_underlying_and_ignores_different_atoken_mint_amount():
    r = receipt(
        event("supply", 3),
        transfer(RESERVE, ACTOR, ATOKEN, 100, 1),
        transfer(ATOKEN, ZERO_ADDRESS, ACCOUNT, 99, 2),
    )
    result = reconcile(plan(), binding(), (r,))
    assert result["status_counts"] == {"matched": 1}
    assert result["matches"][0]["matched_transfer_log_index"] == 1
    assert result["matched_totals"] == [
        {"pool": POOL, "token": RESERVE, "role": "supply", "decimals": 6, "amount_raw": 100}
    ]
    assert not result["account_balance_reconciled"]


@pytest.mark.parametrize(
    "kind,from_addr,to_addr",
    [("borrow", ATOKEN, ACTOR), ("withdraw", ATOKEN, ACTOR), ("repay", ACTOR, ATOKEN)],
)
def test_gateway_flows_match_actor_or_receiver(kind, from_addr, to_addr):
    report = reconcile(
        plan(),
        binding(),
        (receipt(event(kind, 4), transfer(RESERVE, from_addr, to_addr, 100, 3)),),
    )
    assert report["status_counts"] == {"matched": 1}


@pytest.mark.parametrize(
    "kind", ["wrong_amount", "wrong_recipient", "reused", "ambiguous", "atokens"]
)
def test_missing_unsupported_and_ambiguous_do_not_contribute_to_matched_totals(kind):
    logs = [event("repay", 1, atokens=kind == "atokens")]
    logs.append(
        transfer(
            RESERVE,
            ACTOR,
            ACCOUNT if kind == "wrong_recipient" else ATOKEN,
            99 if kind == "wrong_amount" else 100,
            2,
        )
    )
    status = "transfer_missing"
    if kind == "reused":
        logs.append(event("supply", 3))
        status = "transfer_ambiguous"
    elif kind == "ambiguous":
        logs.append(transfer(RESERVE, ACTOR, ATOKEN, 100, 3))
        status = "transfer_ambiguous"
    elif kind == "atokens":
        status = "unmodelled_flow"
    report = reconcile(plan(), binding(), (receipt(*logs),))
    assert report["status_counts"] == {status: 2 if kind == "reused" else 1}
    assert report["matched_totals"] == []


@pytest.mark.parametrize("bad", [None, "wrong_account", "extra_word", "ltv"])
def test_boundary_binds_account_and_canonical_return_shape(bad):
    def key(token, signature, account=ACCOUNT):
        return token, topic(signature)[:10] + encode(["address"], [account]).hex()

    def words(*values):
        return "0x" + encode(["uint256"] * len(values), list(values)).hex()

    calls = {
        key(RESERVE, "balanceOf(address)"): words(21),
        key(ATOKEN, "balanceOf(address)"): words(12),
        key(ATOKEN, "scaledBalanceOf(address)"): words(10),
        key(POOL, "getUserAccountData(address)"): words(12, 0, 0, 7800, 7500, 2**256 - 1),
    }
    if bad == "wrong_account":
        calls[key(ATOKEN, "scaledBalanceOf(address)", ACTOR)] = calls.pop(
            key(ATOKEN, "scaledBalanceOf(address)")
        )
    elif bad == "extra_word":
        calls[key(ATOKEN, "balanceOf(address)")] = words(12, 0)
    elif bad == "ltv":
        calls[key(POOL, "getUserAccountData(address)")] = words(12, 0, 0, 7500, 7800, 2**256 - 1)
    if bad:
        with pytest.raises(ValueError):
            position_snapshot(plan(), binding(), calls)
    else:
        report = position_snapshot(plan(), binding(), calls)
        assert report["reserves"][1]["scaled_balance_raw"] == 10
        assert report["pool_account_data"][0]["total_debt_base"] == 0
