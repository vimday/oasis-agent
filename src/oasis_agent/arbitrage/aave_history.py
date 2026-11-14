"""Interpret selected Aave V3 ABI events; event flows are not balances, debt or profit."""

from collections import defaultdict
from dataclasses import asdict, dataclass
from typing import Annotated, Any, Literal, Self

from pydantic import Field, model_validator

from ..evidence.public_receipts import ObservedReceipt
from ..models.chain import LogRecord
from ..models.common import ZERO_ADDRESS
from .cycle_plan import Address, Frozen
from .cycle_protocols import abi, data_bytes, topic

PROTOCOL_SOURCE = "aave-v3-origin:8305565ae342f1773c42cd2e4593f175fe5968a0:IPool.sol"
SIGNATURES = {
    "supply": "Supply(address,address,address,uint256,uint16)",
    "withdraw": "Withdraw(address,address,address,uint256)",
    "borrow": "Borrow(address,address,address,uint256,uint8,uint256,uint16)",
    "repay": "Repay(address,address,address,uint256,bool)",
    "liquidation": "LiquidationCall(address,address,address,uint256,uint256,address,bool)",
    "reserve_update": "ReserveDataUpdated(address,uint256,uint256,uint256,uint256,uint256)",
}
TOPICS = {topic(sig): name for name, sig in SIGNATURES.items()}


class AaveHistoryPlan(Frozen):
    schema_version: Literal["oasis-aave-receipt-analysis-v1"] = Field(alias="schema")
    chain_id: Annotated[int, Field(gt=0, lt=1 << 64)]
    account: Address
    pools: Annotated[tuple[Address, ...], Field(min_length=1, max_length=4)]

    @model_validator(mode="after")
    def unique(self) -> Self:
        if ZERO_ADDRESS in (self.account, *self.pools) or len(set(self.pools)) != len(self.pools):
            raise ValueError("nonzero_account_and_unique_pools")
        return self


@dataclass(frozen=True, slots=True)
class EventFlow:
    reserve: str
    role: str
    amount_raw: int


@dataclass(frozen=True, slots=True)
class AaveEvent:
    event: str
    pool: str
    transaction_hash: str
    log_index: int
    account: str | None
    actor: str | None
    recipient: str | None
    flows: tuple[EventFlow, ...]
    details: dict[str, Any]


def decode_event(log: LogRecord) -> AaveEvent | None:
    kind = TOPICS.get(log.topics[0]) if log.topics else None
    if kind is None:
        return None
    types = (
        ("address",)
        if kind == "reserve_update"
        else ("address", "address", "uint16" if kind in {"supply", "borrow"} else "address")
    )
    if len(log.topics) != len(types) + 1:
        raise ValueError("aave_indexed_topic_count")
    fields = tuple(abi((t,), data_bytes(v))[0] for t, v in zip(types, log.topics[1:], strict=True))
    if any(v == ZERO_ADDRESS for v, t in zip(fields, types, strict=True) if t == "address"):
        raise ValueError("zero_aave_role")
    reserve = fields[0]
    raw = data_bytes(log.data)
    details: dict[str, Any] = {}
    flows: tuple[EventFlow, ...]
    if kind == "supply":
        actor, amount = abi(("address", "uint256"), raw)
        account, recipient = fields[1], fields[1]
        details["referral_code"] = fields[2]
        flows = (EventFlow(reserve, "supply", amount),)
    elif kind == "borrow":
        actor, amount, mode, rate = abi(("address", "uint256", "uint8", "uint256"), raw)
        if mode not in (1, 2):
            raise ValueError("unsupported_borrow_rate_mode")
        account, recipient = fields[1], actor
        details.update(rate_mode=mode, borrow_rate_ray=rate, referral_code=fields[2])
        flows = (EventFlow(reserve, "borrow", amount),)
    elif kind == "withdraw":
        (amount,) = abi(("uint256",), raw)
        account, actor, recipient = fields[1], fields[1], fields[2]
        flows = (EventFlow(reserve, "withdraw", amount),)
    elif kind == "repay":
        amount, use_atokens = abi(("uint256", "bool"), raw)
        account, actor, recipient = fields[1], fields[2], None
        details["use_atokens"] = use_atokens
        flows = (
            EventFlow(reserve, "repay_atokens" if use_atokens else "repay_underlying", amount),
        )
    elif kind == "liquidation":
        debt, collateral, actor, receive_atoken = abi(
            ("uint256", "uint256", "address", "bool"), raw
        )
        account, recipient = fields[2], actor
        details["receive_atoken"] = receive_atoken
        flows = (
            EventFlow(fields[1], "liquidated_debt", debt),
            EventFlow(reserve, "liquidated_collateral", collateral),
        )
    else:
        values = abi(("uint256",) * 5, raw)
        if not values[3] or not values[4]:
            raise ValueError("missing_reserve_index")
        details = dict(
            zip(
                (
                    "liquidity_rate_ray",
                    "stable_rate_ray",
                    "variable_rate_ray",
                    "liquidity_index_ray",
                    "variable_borrow_index_ray",
                ),
                values,
                strict=True,
            )
        )
        details["reserve"] = reserve
        account = actor = recipient = None
        flows = ()
    if actor == ZERO_ADDRESS:
        raise ValueError("zero_aave_actor")
    return AaveEvent(
        kind,
        log.address,
        log.transaction_hash,
        log.log_index,
        account,
        actor,
        recipient,
        flows,
        details,
    )


def summarize(plan: AaveHistoryPlan, receipts: tuple[ObservedReceipt, ...]) -> dict[str, Any]:
    seen = set()
    events = []
    reserves = []
    summaries = []
    totals: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for receipt in receipts:
        if receipt.block.chain_id != plan.chain_id:
            raise ValueError("receipt_chain_outside_plan")
        if receipt.transaction_hash in seen:
            raise ValueError("duplicate_receipt_cannot_double_count")
        seen.add(receipt.transaction_hash)
        selected_count = 0
        for log in receipt.logs:
            if log.address not in plan.pools:
                continue
            event = decode_event(log)
            if event is None:
                continue
            row = {
                **asdict(event),
                "block": asdict(receipt.block),
                "transaction_index": receipt.transaction_index,
            }
            if event.event == "reserve_update":
                reserves.append(row)
            elif event.account == plan.account:
                events.append(row)
                selected_count += 1
                for flow in event.flows:
                    totals[flow.reserve][flow.role] += flow.amount_raw
        summaries.append(
            {
                "block": asdict(receipt.block),
                "transaction_hash": receipt.transaction_hash,
                "transaction_index": receipt.transaction_index,
                "status": receipt.status,
                "sender": receipt.sender,
                "recipient": receipt.recipient,
                "receipt_gas_component_native_raw": receipt.gas_used * receipt.effective_gas_price,
                "gas_sender_is_account": receipt.sender == plan.account,
                "log_addresses": sorted({log.address for log in receipt.logs}),
                "account_event_count": selected_count,
            }
        )
    return {
        "schema": "oasis-aave-receipt-report-v1",
        "chain_id": plan.chain_id,
        "account": plan.account,
        "protocol_abi_source": PROTOCOL_SOURCE,
        "evidence_class": "selected_receipt_abi_interpretation",
        "receipts": summaries,
        "account_events": events,
        "reserve_updates": reserves,
        "event_flow_totals": [
            {
                "chain_id": plan.chain_id,
                "account": plan.account,
                "reserve": reserve,
                "decimals": None,
                "unit": "reserve_base_units",
                "flows_raw": dict(flows),
            }
            for reserve, flows in sorted(totals.items())
        ],
        "coverage": "selected_receipts_only",
        "protocol_implementation_verified": False,
        "complete_lending_history": False,
        "token_transfers_reconciled": False,
        "native_cashflows_reconciled": False,
        "initial_and_final_debt_verified": False,
        "independent_providers_verified": False,
        "execution_eligible": False,
        "net_profit": None,
    }
