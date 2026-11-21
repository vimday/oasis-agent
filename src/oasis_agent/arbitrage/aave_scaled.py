"""Replay the pinned Aave v3.5 scaled-token arithmetic over selected receipts."""

from dataclasses import dataclass
from typing import Any

from ..evidence.public_receipts import ObservedReceipt
from ..models.chain import LogRecord
from ..models.common import ZERO_ADDRESS
from .aave_cashflows import Plan, transfer
from .aave_history import PROTOCOL_SOURCE, AaveEvent, AaveHistoryPlan, decode_event
from .cycle_protocols import abi, data_bytes, topic

SOURCE_REVISION = PROTOCOL_SOURCE.split(":")[1]
RAY = 10**27
MINT = topic("Mint(address,address,uint256,uint256,uint256)")
BURN = topic("Burn(address,address,uint256,uint256,uint256)")
BALANCE_TRANSFER = topic("BalanceTransfer(address,address,uint256,uint256)")


def ceil_div(value: int, divisor: int) -> int:
    return (value + divisor - 1) // divisor


@dataclass(frozen=True, slots=True)
class Position:
    debt: bool
    scaled: int = 0
    index: int = 0

    def nominal(self, index: int) -> int:
        product = self.scaled * index
        return ceil_div(product, RAY) if self.debt else product // RAY

    def advance(self, delta: int, index: int) -> Position:
        scaled = self.scaled + delta
        if not max(RAY, self.index) <= index < 1 << 128 or not 0 <= scaled < 1 << 120:
            raise ValueError("scaled_position_index_or_balance")
        return Position(self.debt, scaled, index)

    def event_delta(self, kind: str, amount: int, index: int) -> int:
        if not 0 < amount <= ((1 << 256) - 1) // RAY or not RAY <= index < 1 << 128:
            raise ValueError("scaled_event_amount_or_index_bounds")
        if kind == "supply":
            return amount * RAY // index
        if kind == "borrow":
            return ceil_div(amount * RAY, index)
        if kind == "withdraw":
            return -ceil_div(amount * RAY, index)
        if kind != "repay":
            raise ValueError("unsupported_scaled_operation")
        if amount > self.nominal(index):
            raise ValueError("scaled_repay_exceeds_current_debt")
        return -(amount * RAY // index)


@dataclass(frozen=True, slots=True)
class TokenEvent:
    log: LogRecord
    first: str
    second: str
    amount: int
    index: int
    increase: int | None

    @property
    def kind(self) -> str:
        return self.log.topics[0]


def token_events(receipt: ObservedReceipt, token: str, account: str) -> list[TokenEvent]:
    result = []
    for log in receipt.logs:
        if (
            log.address != token
            or not log.topics
            or log.topics[0]
            not in {
                MINT,
                BURN,
                BALANCE_TRANSFER,
            }
        ):
            continue
        if len(log.topics) != 3:
            raise ValueError("invalid_scaled_event_topics")
        first, second = (abi(("address",), data_bytes(value))[0] for value in log.topics[1:])
        kind = log.topics[0]
        if (kind == MINT and second != account) or (kind == BURN and first != account):
            continue
        if kind == BALANCE_TRANSFER and account not in (first, second):
            continue
        size = 2 if kind == BALANCE_TRANSFER else 3
        values = abi(("uint256",) * size, data_bytes(log.data))
        result.append(
            TokenEvent(log, first, second, values[0], values[-1], values[1] if size == 3 else None)
        )
    return result


def require_transfer(
    receipt: ObservedReceipt, event: TokenEvent, sender: str, recipient: str, amount: int
) -> int:
    key = event.log.address, sender, recipient, amount
    matches = [
        log.log_index
        for log in receipt.logs
        if log.address == event.log.address and transfer(log) == key
    ]
    if len(matches) != 1:
        raise ValueError("scaled_event_transfer_not_unique")
    return matches[0]


def apply_operation(
    receipt: ObservedReceipt,
    account: str,
    before: Position,
    event: TokenEvent,
    operation: AaveEvent,
) -> tuple[Position, int, int]:
    delta = before.event_delta(operation.event, operation.flows[0].amount_raw, event.index)
    if not delta:
        raise ValueError("scaled_event_zero_delta")
    after = before.advance(delta, event.index)
    increase = before.nominal(event.index) - before.nominal(before.index)
    change = after.nominal(event.index) - before.nominal(before.index)
    if event.increase != increase or event.amount != abs(change):
        raise ValueError("scaled_nominal_or_accrual_mismatch")
    if change > 0:
        actor = operation.actor if operation.event in {"supply", "borrow"} else account
        if (event.kind, event.first, event.second) != (MINT, actor, account):
            raise ValueError("scaled_mint_role_mismatch")
        matched = require_transfer(receipt, event, ZERO_ADDRESS, account, event.amount)
    else:
        target = ZERO_ADDRESS if before.debt else operation.recipient
        if (event.kind, event.first, event.second) != (BURN, account, target):
            raise ValueError("scaled_burn_role_mismatch")
        matched = require_transfer(receipt, event, account, ZERO_ADDRESS, event.amount)
    return after, matched, increase


def apply_balance_transfer(
    receipt: ObservedReceipt, account: str, before: Position, events: list[TokenEvent]
) -> tuple[Position, set[int], int]:
    movements = [e for e in events if e.kind == BALANCE_TRANSFER]
    if before.debt or len(movements) != 1:
        raise ValueError("unsupported_scaled_transfer_set")
    event = movements[0]
    if event.first == event.second or ZERO_ADDRESS in (event.first, event.second):
        raise ValueError("unsupported_scaled_transfer_roles")
    delta = -event.amount if event.first == account else event.amount
    after = before.advance(delta, event.index)
    claimed = [
        (log.log_index, row[3])
        for log in receipt.logs
        if log.address == event.log.address
        and (row := transfer(log))
        and row[1:3] == (event.first, event.second)
    ]
    if len(claimed) != 1 or ceil_div(claimed[0][1] * RAY, event.index) != event.amount:
        raise ValueError("scaled_balance_transfer_amount_mismatch")
    consumed = {claimed[0][0]}
    increase = before.nominal(event.index) - before.nominal(before.index)
    accruals = [e for e in events if e is not event]
    if increase:
        if len(accruals) != 1:
            raise ValueError("scaled_transfer_accrual_missing")
        accrual = accruals[0]
        if (accrual.kind, accrual.amount, accrual.increase, accrual.index) != (
            MINT,
            increase,
            increase,
            event.index,
        ):
            raise ValueError("scaled_transfer_accrual_mismatch")
        consumed.add(require_transfer(receipt, accrual, ZERO_ADDRESS, account, increase))
    elif accruals:
        raise ValueError("unexpected_scaled_transfer_accrual")
    return after, consumed, increase


def replay(
    analysis: AaveHistoryPlan,
    bindings: Plan,
    receipts: tuple[ObservedReceipt, ...],
    opening: dict[str, int],
    closing: dict[str, int],
) -> dict[str, Any]:
    if not receipts or {reserve.pool for reserve in bindings.reserves} - set(analysis.pools):
        raise ValueError("scaled_receipts_or_reserves_outside_plan")
    ordering = [(r.block.number, r.transaction_index) for r in receipts]
    if ordering != sorted(set(ordering)) or any(
        r.block.chain_id != analysis.chain_id for r in receipts
    ):
        raise ValueError("scaled_receipt_order_or_chain")
    roles = {}
    for reserve in bindings.reserves:
        roles[reserve.pool, reserve.underlying, False] = reserve.a_token
        if reserve.variable_debt_token:
            roles[reserve.pool, reserve.underlying, True] = reserve.variable_debt_token
    if (
        len(set(roles.values())) != len(roles)
        or set(opening) != set(roles.values())
        or set(closing) != set(opening)
    ):
        raise ValueError("scaled_position_binding_mismatch")
    if any(type(value) is not int or value != 0 for value in opening.values()):
        raise ValueError("scaled_replay_requires_zero_opening")
    if any(type(value) is not int or not 0 <= value < 1 << 120 for value in closing.values()):
        raise ValueError("scaled_closing_balance_bounds")
    positions = {token: Position(debt) for (_, _, debt), token in roles.items()}
    rows = []
    for receipt in receipts:
        operations: dict[str, AaveEvent] = {}
        indices: dict[tuple[str, str], AaveEvent] = {}
        for log in receipt.logs:
            if log.address not in analysis.pools or (ev := decode_event(log)) is None:
                continue
            if ev.event == "reserve_update":
                key = ev.pool, ev.details["reserve"]
                if key in indices:
                    raise ValueError("scaled_reserve_update_ambiguous")
                indices[key] = ev
            if ev.account != analysis.account:
                continue
            if (
                ev.event not in {"supply", "withdraw", "borrow", "repay"}
                or ev.details.get("use_atokens")
                or ev.details.get("rate_mode", 2) != 2
            ):
                raise ValueError("unsupported_scaled_pool_operation")
            token = roles.get((ev.pool, ev.flows[0].reserve, ev.event in {"borrow", "repay"}))
            if token is None or token in operations:
                raise ValueError("scaled_pool_operation_scope_or_ambiguity")
            operations[token] = ev
        for token, before in positions.items():
            events = token_events(receipt, token, analysis.account)
            operation = operations.get(token)
            consumed: set[int] = set()
            if operation is not None:
                if len(events) != 1:
                    raise ValueError("scaled_operation_token_event_not_unique")
                update = indices.get((operation.pool, operation.flows[0].reserve))
                name = "variable_borrow_index_ray" if before.debt else "liquidity_index_ray"
                if update is None or update.details[name] != events[0].index:
                    raise ValueError("scaled_reserve_index_mismatch")
                after, matched, increase = apply_operation(
                    receipt, analysis.account, before, events[0], operation
                )
                consumed.add(matched)
            elif events:
                after, consumed, increase = apply_balance_transfer(
                    receipt, analysis.account, before, events
                )
            else:
                after, increase = before, 0
            transfers = {
                log.log_index
                for log in receipt.logs
                if log.address == token and (row := transfer(log)) and analysis.account in row[1:3]
            }
            if transfers != consumed:
                raise ValueError("unattributed_scaled_token_transfer")
            if events:
                rows.append(
                    {
                        "block_number": receipt.block.number,
                        "block_hash": receipt.block.block_hash,
                        "transaction_hash": receipt.transaction_hash,
                        "token": token,
                        "event": operation.event if operation else "balance_transfer",
                        "pool_event_log_index": operation.log_index if operation else None,
                        "token_event_log_indices": [e.log.log_index for e in events],
                        "transfer_log_indices": sorted(consumed),
                        "index_ray": after.index,
                        "scaled_before_raw": before.scaled,
                        "scaled_after_raw": after.scaled,
                        "accrual_raw": increase,
                    }
                )
                positions[token] = after
    computed = {token: state.scaled for token, state in positions.items()}
    residuals = {token: n - closing[token] for token, n in computed.items()}
    return {
        "formula_source_revision": SOURCE_REVISION,
        "status": "residual" if any(residuals.values()) else "selected_scaled_positions_match",
        "observed_opening_scaled_raw": opening,
        "computed_closing_scaled_raw": computed,
        "observed_closing_scaled_raw": closing,
        "residuals_raw": residuals,
        "steps": rows,
        "implementation_source_equivalence_verified": False,
        "complete_history_verified": False,
    }
