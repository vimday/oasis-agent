"""Decode selected lending-market observations and separate them from trade prices."""

from dataclasses import asdict, dataclass
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from ..evidence.public_receipts import ObservedReceipt
from ..models.common import ZERO_ADDRESS, BlockRef
from .aave_cashflows import transfer
from .aave_history import decode_event
from .cycle_plan import Address, Frozen, Hash
from .cycle_protocols import abi, data_bytes, topic

FREEZE = topic("ReserveFrozen(address,bool)")
WITHDRAWAL = topic("Withdrawal(address,uint256)")


class Plan(Frozen):
    schema_version: Literal["oasis-aave-incident-v1"] = Field(alias="schema")
    chain_id: Annotated[int, Field(gt=0, lt=1 << 64)]
    account: Address
    pool: Address
    provider: Address
    configurator: Address
    oracle: Address
    oracle_runtime_hash: Hash
    weth: Address
    weth9_runtime_hash: Hash
    stable: Address
    stable_decimals: Annotated[int, Field(ge=0, le=18)]
    freeze_tx: Hash
    borrow_tx: Hash
    purchase_tx: Hash
    unwrap_tx: Hash
    max_weth_feed_age_seconds: Annotated[int, Field(ge=1, le=3600)]

    @model_validator(mode="after")
    def distinct(self) -> Self:
        addresses = (
            self.account,
            self.pool,
            self.provider,
            self.configurator,
            self.oracle,
            self.weth,
            self.stable,
        )
        transactions = (self.freeze_tx, self.borrow_tx, self.purchase_tx, self.unwrap_tx)
        if ZERO_ADDRESS in addresses or len(set(addresses)) != len(addresses):
            raise ValueError("incident_address_roles")
        if len(set(transactions)) != len(transactions):
            raise ValueError("incident_duplicate_transaction")
        return self


@dataclass(frozen=True, slots=True)
class Reserve:
    raw: int
    decimals: int
    ltv_bps: int
    liquidation_threshold_bps: int
    active: bool
    frozen: bool
    paused: bool
    borrowing_enabled: bool


def reserve(word: object, decimals: int) -> Reserve:
    (raw,) = abi(("uint256",), data_bytes(word))
    ltv, threshold = raw & 65535, raw >> 16 & 65535
    if raw >> 48 & 255 != decimals or not 0 <= ltv <= threshold <= 10_000:
        raise ValueError("reserve_config_units_or_thresholds")
    return Reserve(
        raw,
        decimals,
        ltv,
        threshold,
        bool(raw >> 56 & 1),
        bool(raw >> 57 & 1),
        bool(raw >> 60 & 1),
        bool(raw >> 58 & 1),
    )


def freeze(
    plan: Plan, receipt: ObservedReceipt, before: dict[str, Reserve], after: dict[str, Reserve]
) -> list[dict[str, object]]:
    if receipt.status != 1 or receipt.transaction_hash != plan.freeze_tx:
        raise ValueError("freeze_receipt_identity")
    events = {}
    for log in receipt.logs:
        if log.address != plan.configurator or log.topics[:1] != (FREEZE,):
            continue
        if len(log.topics) != 2:
            raise ValueError("freeze_event_topics")
        (asset,) = abi(("address",), data_bytes(log.topics[1]))
        (frozen,) = abi(("bool",), data_bytes(log.data))
        if asset not in (plan.stable, plan.weth):
            continue
        if not frozen or asset in events:
            raise ValueError("freeze_event_ambiguous")
        events[asset] = log.log_index
    result = []
    for asset in (plan.stable, plan.weth):
        previous, next_state = before[asset], after[asset]
        if asset not in events or previous.frozen or not next_state.frozen:
            raise ValueError("freeze_event_or_boundary_disagreement")
        result.append(
            {
                "asset": asset,
                "log_index": events[asset],
                "parent_block_end": asdict(previous),
                "freeze_block_end": asdict(next_state),
            }
        )
    return result


def borrow_amount(plan: Plan, receipt: ObservedReceipt) -> tuple[int, int]:
    if receipt.status != 1 or receipt.transaction_hash != plan.borrow_tx:
        raise ValueError("borrow_receipt_identity")
    events = [
        event
        for log in receipt.logs
        if log.address == plan.pool
        and (event := decode_event(log)) is not None
        and event.event == "borrow"
        and event.account == plan.account
    ]
    if len(events) != 1 or events[0].flows[0].reserve != plan.weth:
        raise ValueError("incident_borrow_not_unique")
    event = events[0]
    amount = event.flows[0].amount_raw
    if amount <= 0:
        raise ValueError("incident_borrow_amount")
    return amount, event.log_index


def purchase(plan: Plan, receipt: ObservedReceipt) -> dict[str, int]:
    if receipt.status != 1 or receipt.transaction_hash != plan.purchase_tx:
        raise ValueError("purchase_receipt_identity")
    relevant = [
        (log.log_index, move)
        for log in receipt.logs
        if log.address in (plan.stable, plan.weth)
        and (move := transfer(log)) is not None
        and plan.account in move[1:3]
    ]
    if len(relevant) != 2:
        raise ValueError("purchase_transfer_ambiguous")
    (paid_index, paid), (bought_index, bought) = relevant
    if (
        paid[0] != plan.stable
        or paid[1] != plan.account
        or paid[2] in (ZERO_ADDRESS, plan.account)
        or paid[3] == 0
        or bought[0] != plan.weth
        or bought[2] != plan.account
        or bought[1] in (ZERO_ADDRESS, plan.account)
        or bought[3] == 0
    ):
        raise ValueError("purchase_direction_or_amount")
    return {
        "stable_paid_raw": paid[3],
        "weth_received_raw": bought[3],
        "stable_transfer_log_index": paid_index,
        "weth_transfer_log_index": bought_index,
    }


def unwrap(plan: Plan, receipt: ObservedReceipt, expected: int) -> dict[str, int]:
    # A direct successful WETH9 withdrawal burns via Withdrawal, without an ERC20 Transfer.
    if (
        receipt.status != 1
        or receipt.transaction_hash != plan.unwrap_tx
        or (receipt.sender, receipt.recipient) != (plan.account, plan.weth)
        or len(receipt.logs) != 1
    ):
        raise ValueError("unwrap_receipt_roles_or_logs")
    log = receipt.logs[0]
    if log.address != plan.weth or log.topics[:1] != (WITHDRAWAL,) or len(log.topics) != 2:
        raise ValueError("unwrap_event_shape")
    (issuer,) = abi(("address",), data_bytes(log.topics[1]))
    (amount,) = abi(("uint256",), data_bytes(log.data))
    if issuer != plan.account or not amount or amount != expected:
        raise ValueError("unwrap_event_account_or_quantity")
    return {
        "weth_withdrawn_raw": amount,
        "withdrawal_log_index": log.log_index,
        "gas_paid_native_raw": receipt.gas_used * receipt.effective_gas_price,
    }


def weth_round(
    wire: object, *, observed: BlockRef, market_price: int, age_limit: int
) -> dict[str, int]:
    round_id, answer, started, updated, answered = abi(
        ("uint80", "int256", "uint256", "uint256", "uint80"), data_bytes(wire)
    )
    if (
        observed.timestamp is None
        or not round_id
        or answered < round_id
        or not 0 < started <= updated <= observed.timestamp
        or answer <= 0
        or answer != market_price
        or observed.timestamp - updated > age_limit
    ):
        raise ValueError("weth_round_disagreement_or_stale")
    return {
        "round_id": round_id,
        "answer_usd_e8": answer,
        "started_at": started,
        "updated_at": updated,
        "answered_in_round": answered,
        "age_at_parent_seconds": observed.timestamp - updated,
    }


def fill_comparison(
    *, paid: int, received: int, stable_usd_e8: int, weth_usd_e8: int, stable_decimals: int
) -> dict[str, int]:
    # Compare the receipt's token ratio to the parent-block marks before rounding.
    if min(paid, received, stable_usd_e8, weth_usd_e8) <= 0 or not 0 <= stable_decimals <= 18:
        raise ValueError("incident_fill_comparison_units")
    numerator = paid * 10**18
    denominator = received * 10**stable_decimals
    marked = numerator * stable_usd_e8
    base = denominator * weth_usd_e8
    return {
        "stable_per_weth_e8_floor": numerator * 10**8 // denominator,
        "marked_usd_per_weth_e8_floor": marked // denominator,
        "marked_difference_bps_floor": (marked - base) * 10_000 // base,
    }
