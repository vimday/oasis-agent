from dataclasses import replace

import pytest

from oasis_agent.arbitrage import aave_sale
from oasis_agent.evidence.public_receipts import ObservedTransaction
from tests.unit.test_aave_cashflows import transfer
from tests.unit.test_aave_history import ACCOUNT, ACTOR, receipt
from tests.unit.test_aave_incident import STABLE, WETH, singleton

EXECUTOR = "0x" + "52" * 20


def test_failed_value_is_not_spent_but_receipt_gas_is():
    failed = replace(
        receipt(), sender=ACCOUNT, recipient=ACTOR, status=0, gas_used=7, effective_gas_price=3
    )
    tx = ObservedTransaction(ACCOUNT, ACTOR, 10**18, None)
    assert aave_sale.native_delta(ACCOUNT, tx, failed) == -21
    assert aave_sale.native_delta(ACTOR, tx, failed) == 0
    success = replace(failed, status=1)
    assert aave_sale.native_delta(ACCOUNT, tx, success) == -(10**18) - 21
    assert aave_sale.native_delta(ACTOR, tx, success) == 10**18


def test_native_sale_keeps_receiver_and_exact_input_bound():
    plan = aave_sale.Plan(
        schema="oasis-aave-sale-v1",
        sale_entry_point=ACTOR,
        sale_executor=EXECUTOR,
        borrow_gateway="0x" + "53" * 20,
        first_borrow_tx="0x" + "51" * 32,
        first_funding_tx="0x" + "52" * 32,
        failed_sale_tx="0x" + "53" * 32,
        second_funding_tx="0x" + "54" * 32,
        sale_tx="0x" + "55" * 32,
        resupply_tx="0x" + "56" * 32,
        subsequent_borrow_tx="0x" + "57" * 32,
    )
    deposit = singleton(WETH, aave_sale.DEPOSIT, EXECUTOR, ("uint256",), (50,))
    stable = transfer(STABLE, EXECUTOR, ACCOUNT, 240, 2)
    observation = replace(receipt(deposit, stable), sender=ACCOUNT, recipient=ACTOR)
    tx = ObservedTransaction(ACCOUNT, ACTOR, 50, None)

    def sale(r=observation, t=tx):
        return aave_sale.sale_proceeds(
            plan, account=ACCOUNT, stable=STABLE, weth=WETH, tx=t, receipt=r
        )

    assert sale() == {
        "eth_sent_wei": 50,
        "usdc_received_raw": 240,
        "deposit_log_index": 1,
        "proceeds_log_index": 2,
    }
    with pytest.raises(ValueError, match="quantity"):
        sale(t=replace(tx, value_raw=51))
    with pytest.raises(ValueError, match="ambiguous"):
        sale(r=replace(observation, logs=(*observation.logs, stable)))
