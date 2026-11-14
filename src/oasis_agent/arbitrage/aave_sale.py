"""Cashflows of a selected native sale and Aave collateral replenishment."""

from typing import Literal, Self

from pydantic import Field, model_validator

from ..evidence.public_receipts import ObservedReceipt, ObservedTransaction
from ..models.common import ZERO_ADDRESS
from .aave_cashflows import transfer
from .aave_incident import WITHDRAWAL
from .cycle_plan import Address, Frozen, Hash
from .cycle_protocols import abi, data_bytes, topic

DEPOSIT = topic("Deposit(address,uint256)")


class Plan(Frozen):
    schema_version: Literal["oasis-aave-sale-v1"] = Field(alias="schema")
    sale_entry_point: Address
    sale_executor: Address
    borrow_gateway: Address
    first_borrow_tx: Hash
    first_funding_tx: Hash
    failed_sale_tx: Hash
    second_funding_tx: Hash
    sale_tx: Hash
    resupply_tx: Hash
    subsequent_borrow_tx: Hash

    @model_validator(mode="after")
    def validate_roles(self) -> Self:
        contracts = {self.sale_entry_point, self.sale_executor, self.borrow_gateway}
        transactions = {
            self.first_borrow_tx,
            self.first_funding_tx,
            self.failed_sale_tx,
            self.second_funding_tx,
            self.sale_tx,
            self.resupply_tx,
            self.subsequent_borrow_tx,
        }
        if ZERO_ADDRESS in contracts or len(contracts) != 3 or len(transactions) != 7:
            raise ValueError("sale_roles_or_transactions_not_distinct")
        return self


def native_delta(account: str, tx: ObservedTransaction, receipt: ObservedReceipt) -> int:
    if (tx.sender, tx.recipient) != (receipt.sender, receipt.recipient):
        raise ValueError("native_transaction_receipt_roles")
    delta = tx.value_raw * (int(tx.recipient == account) - int(tx.sender == account))
    paid = receipt.gas_used * receipt.effective_gas_price if tx.sender == account else 0
    return delta * receipt.status - paid


def sale_proceeds(
    plan: Plan,
    *,
    account: str,
    stable: str,
    weth: str,
    tx: ObservedTransaction,
    receipt: ObservedReceipt,
) -> dict[str, int]:
    if (
        receipt.status != 1
        or tx.sender != account
        or tx.recipient != plan.sale_entry_point
        or tx.value_raw <= 0
    ):
        raise ValueError("sale_sender_entry_or_value")
    deposits = []
    proceeds = []
    for log in receipt.logs:
        if log.address == weth and log.topics[:1] == (DEPOSIT,):
            if len(log.topics) != 2:
                raise ValueError("sale_deposit_topics")
            (depositor,) = abi(("address",), data_bytes(log.topics[1]))
            (amount,) = abi(("uint256",), data_bytes(log.data))
            if depositor == plan.sale_executor:
                deposits.append((amount, log.log_index))
        if log.address == stable and (movement := transfer(log)) and account in movement[1:3]:
            proceeds.append((movement, log.log_index))
    if len(deposits) != 1 or len(proceeds) != 1:
        raise ValueError("sale_deposit_or_receipt_proceeds_ambiguous")
    ((deposit_amount, deposit_index),) = deposits
    (((_, sender, recipient, paid), stable_index),) = proceeds
    if (
        deposit_amount != tx.value_raw
        or (sender, recipient) != (plan.sale_executor, account)
        or paid <= 0
        or deposit_index >= stable_index
    ):
        raise ValueError("sale_flow_quantity_order_or_roles")
    return {
        "eth_sent_wei": tx.value_raw,
        "usdc_received_raw": paid,
        "deposit_log_index": deposit_index,
        "proceeds_log_index": stable_index,
    }


def gateway_withdrawal(
    receipt: ObservedReceipt, *, account: str, weth: str, gateway: str, principal: int
) -> int:
    if gateway in (ZERO_ADDRESS, account, weth) or receipt.recipient != gateway:
        raise ValueError("borrow_gateway_roles")
    matches = []
    for log in receipt.logs:
        if log.address != weth or log.topics[:1] != (WITHDRAWAL,):
            continue
        if len(log.topics) != 2:
            raise ValueError("borrow_withdrawal_topics")
        (sender,) = abi(("address",), data_bytes(log.topics[1]))
        (amount,) = abi(("uint256",), data_bytes(log.data))
        if sender == gateway:
            if amount != principal:
                raise ValueError("borrow_withdrawal_amount")
            matches.append(log.log_index)
    if len(matches) != 1:
        raise ValueError("borrow_withdrawal_missing_or_ambiguous")
    return matches[0]
