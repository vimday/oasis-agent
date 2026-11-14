"""Reconcile selected Aave pool events to exact underlying Transfer logs."""

from collections import Counter
from typing import Annotated, Any, Literal, Self

from eth_abi.abi import encode
from pydantic import Field, model_validator

from ..evidence.public_receipts import ObservedReceipt
from ..models.chain import LogRecord
from ..models.common import ZERO_ADDRESS
from .aave_history import AaveEvent, AaveHistoryPlan, decode_event
from .cycle_plan import Address, Frozen
from .cycle_protocols import abi, data_bytes, topic

TRANSFER = topic("Transfer(address,address,uint256)")


class Reserve(Frozen):
    pool: Address
    underlying: Address
    a_token: Address
    decimals: Annotated[int, Field(ge=0, le=255)]
    variable_debt_token: Address | None = None

    @model_validator(mode="after")
    def distinct(self) -> Self:
        roles = [self.pool, self.underlying, self.a_token]
        if self.variable_debt_token is not None:
            roles.append(self.variable_debt_token)
        if len(set(roles)) != len(roles) or ZERO_ADDRESS in roles:
            raise ValueError("invalid_reserve_transfer_roles")
        return self


class Plan(Frozen):
    schema_version: Literal["oasis-aave-cashflow-plan-v1"] = Field(alias="schema")
    reserves: Annotated[tuple[Reserve, ...], Field(min_length=1, max_length=32)]

    @model_validator(mode="after")
    def unique(self) -> Self:
        if len({(r.pool, r.underlying) for r in self.reserves}) != len(self.reserves):
            raise ValueError("duplicate_reserve_transfer_binding")
        return self


def endpoints(event: AaveEvent, reserve: Reserve) -> tuple[str, str] | None:
    if event.event in {"supply", "repay"} and not event.details.get("use_atokens") and event.actor:
        return event.actor, reserve.a_token
    if event.event in {"borrow", "withdraw"} and event.recipient:
        return reserve.a_token, event.recipient
    return None


def transfer(log: LogRecord) -> tuple[str, str, str, int] | None:
    if not log.topics or log.topics[0] != TRANSFER:
        return None
    if len(log.topics) != 3:
        raise ValueError("underlying_transfer_topic_count")
    sender, recipient = (abi(("address",), data_bytes(key))[0] for key in log.topics[1:])
    (amount,) = abi(("uint256",), data_bytes(log.data))
    return log.address, sender, recipient, amount


def reconcile(
    analysis: AaveHistoryPlan, bindings: Plan, receipts: tuple[ObservedReceipt, ...]
) -> dict[str, Any]:
    reserves = {(item.pool, item.underlying): item for item in bindings.reserves}
    if {r.pool for r in bindings.reserves} - set(analysis.pools):
        raise ValueError("reserve_binding_outside_receipt_plan")
    assets = {r.underlying for r in bindings.reserves}
    totals: Counter[tuple[str, str, str, int]] = Counter()
    rows = []
    for receipt in receipts:
        transfers: dict[tuple[str, str, str, int], list[int]] = {}
        for log in receipt.logs:
            if log.address in assets and (key := transfer(log)) is not None:
                transfers.setdefault(key, []).append(log.log_index)
        pending = []
        for log in receipt.logs:
            if log.address not in analysis.pools:
                continue
            event = decode_event(log)
            if event is None or event.account != analysis.account:
                continue
            key = None
            reserve = reserves.get((event.pool, event.flows[0].reserve))
            if len(event.flows) == 1 and reserve is not None:
                edge = endpoints(event, reserve)
                if edge is not None:
                    key = (reserve.underlying, *edge, event.flows[0].amount_raw)
            pending.append((event, reserve, key))
        needs = Counter(key for _, _, key in pending if key is not None)
        for event, reserve, key in pending:
            indices = transfers.get(key, []) if key is not None else []
            status = (
                "unmodelled_flow"
                if key is None
                else "transfer_missing"
                if not indices
                else "transfer_ambiguous"
                if len(indices) != 1 or needs[key] != 1
                else "matched"
            )
            matched = status == "matched"
            if matched and key is not None and reserve is not None:
                totals[
                    (reserve.pool, reserve.underlying, event.flows[0].role, reserve.decimals)
                ] += key[3]
            rows.append(
                {
                    "block_number": receipt.block.number,
                    "block_hash": receipt.block.block_hash,
                    "transaction_hash": event.transaction_hash,
                    "transaction_index": receipt.transaction_index,
                    "event": event.event,
                    "event_log_index": event.log_index,
                    "status": status,
                    "matched_transfer_log_index": indices[0] if matched else None,
                    "expected_underlying_transfer": None
                    if key is None
                    else {
                        "token": key[0],
                        "from": key[1],
                        "to": key[2],
                        "amount_raw": key[3],
                        "decimals": reserve.decimals if reserve is not None else None,
                    },
                }
            )
    return {
        "basis": "selected_receipt_erc20_logs_with_declared_reserve_bindings",
        "matches": rows,
        "status_counts": dict(Counter(row["status"] for row in rows)),
        "matched_totals": [
            {"pool": pool, "token": asset, "role": role, "decimals": decimals, "amount_raw": n}
            for (pool, asset, role, decimals), n in sorted(totals.items())
        ],
        "bindings_independently_verified": False,
        "account_balance_reconciled": False,
    }


def position_snapshot(
    analysis: AaveHistoryPlan, bindings: Plan, calls: dict[tuple[str, str], object]
) -> dict[str, Any]:
    """Decode exact account calls from a pinned view capture."""
    account_arg = encode(("address",), (analysis.account,)).hex()

    def read(target: str, signature: str, count: int = 1) -> tuple[int, ...]:
        key = target, topic(signature)[:10] + account_arg
        if key not in calls:
            raise ValueError("boundary_missing_account_state_call")
        values: tuple[int, ...] = abi(("uint256",) * count, data_bytes(calls[key]))
        return values

    reserves = []
    for reserve in bindings.reserves:
        roles = {"underlying": reserve.underlying, "a_token": reserve.a_token}
        if reserve.variable_debt_token is not None:
            roles["variable_debt"] = reserve.variable_debt_token
        for role, token in roles.items():
            reserves.append(
                {
                    "pool": reserve.pool,
                    "reserve": reserve.underlying,
                    "role": role,
                    "token": token,
                    "decimals": reserve.decimals,
                    "balance_raw": read(token, "balanceOf(address)")[0],
                    "scaled_balance_raw": None
                    if role == "underlying"
                    else read(token, "scaledBalanceOf(address)")[0],
                }
            )
    fields = (
        "total_collateral_base",
        "total_debt_base",
        "available_borrows_base",
        "liquidation_threshold_bps",
        "ltv_bps",
        "health_factor_wad",
    )
    pools = []
    for pool in analysis.pools:
        state = dict(
            zip(fields, read(pool, "getUserAccountData(address)", len(fields)), strict=True)
        )
        if not 0 <= state["ltv_bps"] <= state["liquidation_threshold_bps"] <= 10_000:
            raise ValueError("boundary_collateral_threshold_out_of_range")
        pools.append({"pool": pool, **state})
    return {"reserves": reserves, "pool_account_data": pools}
