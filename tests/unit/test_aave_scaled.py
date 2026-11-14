"""Aave v3.5 rounding, debt accrual, transfer coverage and closing residuals."""

from dataclasses import replace

import pytest
from eth_abi.abi import encode

from oasis_agent.arbitrage import aave_scaled as scaled
from oasis_agent.models.common import ZERO_ADDRESS
from tests.unit.test_aave_cashflows import ATOKEN, binding, transfer
from tests.unit.test_aave_history import ACCOUNT, ACTOR, event, plan, receipt

DEBT = "0x" + "61" * 20
RAY = 10**27


def words(*values):
    return "0x" + encode(["uint256"] * len(values), list(values)).hex()


def token_event(token, signature, first, second, values, index):
    return replace(
        event("supply", index),
        address=token,
        topics=(
            signature,
            *("0x" + encode(["address"], [value]).hex() for value in (first, second)),
        ),
        data=words(*values),
    )


def update(index, liq, debt):
    return replace(event("reserve_update", index), data=words(0, 0, 0, liq, debt))


def later(r):
    return replace(r, block=replace(r.block, number=r.block.number + 1))


def test_real_rounding_boundary_and_nominal_transfer_are_distinct():
    index = 3 * RAY // 2
    # 4 underlying produces 2 scaled shares and a nominal mint of 3.
    supplied = receipt(
        update(0, index, RAY),
        transfer(ATOKEN, ZERO_ADDRESS, ACCOUNT, 3, 1),
        token_event(ATOKEN, scaled.MINT, ACTOR, ACCOUNT, (3, 0, index), 2),
        event("supply", 3, amount=4),
    )
    sent = later(
        receipt(
            transfer(ATOKEN, ACCOUNT, ACTOR, 1, 0),
            token_event(ATOKEN, scaled.BALANCE_TRANSFER, ACCOUNT, ACTOR, (1, index), 1),
        )
    )
    result = scaled.replay(plan(), binding(), (supplied, sent), {ATOKEN: 0}, {ATOKEN: 1})
    assert result["status"] == "selected_scaled_positions_match"
    assert result["computed_closing_scaled_raw"] == {ATOKEN: 1}
    assert result["steps"][1]["transfer_log_indices"] == [0]
    assert scaled.replay(plan(), binding(), (supplied, sent), {ATOKEN: 0}, {ATOKEN: 0})[
        "residuals_raw"
    ] == {ATOKEN: 1}


@pytest.mark.parametrize("bad", [None, "index", "accrual", "missing_transfer", "nonzero_opening"])
def test_debt_upward_rounding_and_full_repay(bad):
    index = 3 * RAY // 2
    borrowed = receipt(
        transfer(DEBT, ZERO_ADDRESS, ACCOUNT, 5, 0),
        token_event(DEBT, scaled.MINT, ACTOR, ACCOUNT, (5, 0, index), 1),
        update(2, RAY, index + (bad == "index")),
        event("borrow", 3, amount=4),
    )
    # Three scaled units accrue to 6 at the new index. The burn reports 5
    # against the previous booking, with the remaining unit as accrued interest.
    logs = [
        transfer(DEBT, ACCOUNT, ZERO_ADDRESS, 5, 0),
        token_event(
            DEBT, scaled.BURN, ACCOUNT, ZERO_ADDRESS, (5, 0 if bad == "accrual" else 1, 2 * RAY), 1
        ),
        update(2, RAY, 2 * RAY),
        event("repay", 3, amount=6),
    ]
    repaid = later(receipt(*(logs[1:] if bad == "missing_transfer" else logs)))
    bindings = binding()
    bindings = bindings.model_copy(
        update={
            "reserves": (bindings.reserves[0].model_copy(update={"variable_debt_token": DEBT}),)
        }
    )
    start = {ATOKEN: 0, DEBT: 1 if bad == "nonzero_opening" else 0}
    end = {ATOKEN: 0, DEBT: 0}
    if bad:
        with pytest.raises(ValueError):
            scaled.replay(plan(), bindings, (borrowed, repaid), start, end)
    else:
        report = scaled.replay(plan(), bindings, (borrowed, repaid), start, end)
        assert [row["scaled_after_raw"] for row in report["steps"]] == [3, 0]
        assert [row["accrual_raw"] for row in report["steps"]] == [0, 1]


def test_unattributed_transfer_cannot_disappear():
    r = receipt(transfer(ATOKEN, ACCOUNT, ACTOR, 100, 1))
    with pytest.raises(ValueError, match="unattributed"):
        scaled.replay(plan(), binding(), (r,), {ATOKEN: 0}, {ATOKEN: 0})
