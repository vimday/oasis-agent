"""Attribute initial temporary WETH funding within canonical Ethereum v4 unlocks."""

from collections.abc import Mapping
from dataclasses import dataclass

from eth_hash.auto import keccak

from ..arbitrage.cycle_protocols import abi, topic
from ..models.chain import LogRecord
from .public_receipts import ObservedReceipt
from .research_traces import Frame, TracePath, first_failure, nested_calls
from .swap_flow import TRANSFER
from .swap_flow_trace import _ETHEREUM_V4, _indexed_address, _receipt_positions, _static_then_bytes

WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
MANAGER = _ETHEREUM_V4
_UNLOCK, _CALLBACK, _TAKE, _SYNC, _SETTLE, _SETTLE_FOR, _TRANSFER, _BALANCE = (
    keccak(signature.encode())[:4]
    for signature in (
        "unlock(bytes)",
        "unlockCallback(bytes)",
        "take(address,address,uint256)",
        "sync(address)",
        "settle()",
        "settleFor(address)",
        "transfer(address,uint256)",
        "balanceOf(address)",
    )
)
_WRAPS = {topic("Deposit(address,uint256)"), topic("Withdrawal(address,uint256)")}
_BALANCE_OF_MANAGER = _BALANCE + bytes(12) + bytes.fromhex(MANAGER[2:])


@dataclass(frozen=True, slots=True)
class WethPrincipalProof:
    amount_wei: int
    take_path: TracePath
    take_log: int
    sync_path: TracePath
    repayment_log: int
    repayment_path: TracePath
    settle_path: TracePath
    balance_at_sync_wei: int
    balance_at_settle_wei: int


@dataclass(frozen=True, slots=True)
class WethUnlockFunding:
    unlock_path: TracePath
    borrower: str
    reason: str | None
    proof: WethPrincipalProof | None
    other_outbound_logs: tuple[int, ...] = ()
    other_inbound_logs: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class WethPrincipalReport:
    unlocks: tuple[WethUnlockFunding, ...]


@dataclass(frozen=True, slots=True)
class _WethMovement:
    path: TracePath
    index: int
    sender: str | None
    recipient: str | None
    amount: int


def _within(path: TracePath, parent: TracePath) -> bool:
    return path[: len(parent)] == parent


def _manager_movement(log: LogRecord, path: TracePath) -> _WethMovement | None:
    if log.address != WETH or not log.topics:
        return None
    if log.topics[0] == TRANSFER:
        if len(log.topics) != 3 or len(log.data) != 66:
            raise ValueError("invalid canonical WETH Transfer")
        sender, recipient = map(_indexed_address, log.topics[1:])
        if MANAGER in (sender, recipient):
            return _WethMovement(path, log.log_index, sender, recipient, int(log.data, 16))
    if log.topics[0] in _WRAPS:
        if len(log.topics) != 2 or len(log.data) != 66:
            raise ValueError("invalid canonical WETH wrap event")
        if _indexed_address(log.topics[1]) == MANAGER:
            wrap_sender, wrap_recipient = (
                (None, MANAGER)
                if log.topics[0] == topic("Deposit(address,uint256)")
                else (MANAGER, None)
            )
            return _WethMovement(
                path, log.log_index, wrap_sender, wrap_recipient, int(log.data, 16)
            )
    return None


def _direct_transfer(frame: Frame, movement: _WethMovement) -> bool:
    return (
        frame.kind == "CALL"
        and frame.recipient == WETH
        and frame.sender == movement.sender
        and frame.data[:4] == _TRANSFER
        and frame.value == 0
        and frame.output == bytes(31) + b"\x01"
        and abi(("address", "uint256"), frame.data[4:]) == (movement.recipient, movement.amount)
    )


def _balance_read(
    frames: Mapping[TracePath, Frame],
    path: TracePath,
    children: Mapping[TracePath, tuple[TracePath, ...]],
) -> int:
    if children.get(path) == ((*path, 0),):
        read = frames[(*path, 0)]
        if (
            read.kind == "STATICCALL"
            and read.sender == MANAGER
            and read.recipient == WETH
            and read.data == _BALANCE_OF_MANAGER
            and read.error is None
            and read.output is not None
            and len(read.output) == 32
        ):
            return int.from_bytes(read.output)
    raise ValueError("missing_exact_manager_balance_read")


def _initial_take(
    calls: tuple[tuple[TracePath, Frame], ...],
    borrower: str,
) -> tuple[TracePath, int] | None:
    # Only the first delta-changing manager CALL can premise a new WETH debt.
    for path, call in calls:
        if call.data[:4] == _SYNC:
            abi(("address",), call.data[4:])
            continue
        if call.data[:4] != _TAKE:
            return None
        asset, recipient, amount = abi(("address", "address", "uint256"), call.data[4:])
        if (
            asset == WETH
            and call.sender == recipient == borrower
            and 0 < amount < 1 << 127
            and call.value == 0
            and call.output == b""
        ):
            return path, amount
        return None
    return None


def _repayment(
    calls: tuple[tuple[TracePath, Frame], ...],
    frames: Mapping[TracePath, Frame],
    children: Mapping[TracePath, tuple[TracePath, ...]],
    movements: tuple[_WethMovement, ...],
    take: _WethMovement,
    paid: _WethMovement,
    borrower: str,
) -> WethPrincipalProof:
    active: tuple[TracePath, str] | None = None
    matches = []
    for path, call in calls:
        if call.data[:4] == _SYNC:
            (asset,) = abi(("address",), call.data[4:])
            active = path, asset
        elif call.data[:4] in {_SETTLE, _SETTLE_FOR}:
            sync, active = active, None
            if sync is None or sync[1] != WETH or call.sender != borrower or path <= paid.path:
                continue
            sync_path = sync[0]
            if not take.path < sync_path < paid.path or frames[sync_path].sender != borrower:
                continue
            if call.data != _SETTLE or call.value or call.output is None or len(call.output) != 32:
                raise ValueError("unsupported_weth_settlement")
            if int.from_bytes(call.output) != paid.amount:
                continue
            window = [movement for movement in movements if sync_path < movement.path < path]
            if window != [paid] or _within(paid.path, sync_path):
                raise ValueError("non_unique_weth_movement_in_settlement_window")
            before = _balance_read(frames, sync_path, children)
            after = _balance_read(frames, path, children)
            if after - before != paid.amount:
                raise ValueError("weth_reserve_change_does_not_match_return")
            matches.append(
                WethPrincipalProof(
                    paid.amount,
                    take.path[:-1],
                    take.index,
                    sync_path,
                    paid.index,
                    paid.path,
                    path,
                    before,
                    after,
                )
            )
    if len(matches) != 1:
        raise ValueError("missing_unique_weth_sync_settle")
    return matches[0]


def _analyze_unlock(
    path: TracePath,
    frames: Mapping[TracePath, Frame],
    children: Mapping[TracePath, tuple[TracePath, ...]],
    manager_calls: tuple[tuple[TracePath, Frame], ...],
    movements: tuple[_WethMovement, ...],
) -> WethUnlockFunding:
    unlock = frames[path]
    borrower = unlock.sender
    callback_path = (*path, 0)
    outgoing = tuple(m for m in movements if (m.sender, m.recipient) == (MANAGER, borrower))
    incoming = tuple(m for m in movements if (m.sender, m.recipient) == (borrower, MANAGER))
    try:
        if children.get(path) != (callback_path,) or unlock.kind != "CALL" or unlock.value:
            raise ValueError("invalid_unlock_call")
        callback = frames[callback_path]
        if (
            callback.kind != "CALL"
            or callback.value
            or callback.error is not None
            or (callback.sender, callback.recipient) != (MANAGER, borrower)
            or callback.data[:4] != _CALLBACK
            or unlock.output != callback.output
            or _static_then_bytes(("bytes",), unlock.data[4:])
            != _static_then_bytes(("bytes",), callback.data[4:])
        ):
            raise ValueError("invalid_unlock_callback")
        candidate = _initial_take(manager_calls, borrower)
        if candidate is None:
            raise ValueError("no_initial_borrower_weth_take")
        take_path, amount = candidate
        takes = [m for m in outgoing if m.amount == amount]
        pays = [m for m in incoming if m.amount == amount]
        if len(takes) != 1 or len(pays) != 1:
            raise ValueError("missing_or_ambiguous_equal_weth_transfer")
        take, paid = takes[0], pays[0]
        if (
            take.path != (*take_path, 0)
            or take.index >= paid.index
            or not _direct_transfer(frames[take.path], take)
            or not _direct_transfer(frames[paid.path], paid)
        ):
            raise ValueError("noncanonical_weth_transfer_call")
        proof = _repayment(manager_calls, frames, children, movements, take, paid, borrower)
    except ValueError as exc:
        return WethUnlockFunding(
            path,
            borrower,
            str(exc),
            None,
            tuple(m.index for m in outgoing),
            tuple(m.index for m in incoming),
        )
    return WethUnlockFunding(
        path,
        borrower,
        None,
        proof,
        tuple(m.index for m in outgoing if m.index != proof.take_log),
        tuple(m.index for m in incoming if m.index != proof.repayment_log),
    )


def replay_weth_principal(receipt: ObservedReceipt, trace: object) -> WethPrincipalReport:
    """Verify at most one initial same-actor principal group per committed Ethereum unlock.

    Each proof consumes independent withdrawal, repayment and settlement evidence.
    Other movements stay visible. This does not source opening external balances,
    count Swap legs, or infer profitability.
    """
    if receipt.block.chain_id != 1:
        raise ValueError("WETH principal replay supports only canonical Ethereum")
    frames = nested_calls(trace)
    positions = _receipt_positions(trace, frames, receipt)
    movements = tuple(
        m
        for log in receipt.logs
        if (m := _manager_movement(log, positions[log.log_index])) is not None
    )
    children: dict[TracePath, list[TracePath]] = {}
    for path in sorted(frames):
        if path:
            children.setdefault(path[:-1], []).append(path)
    child_paths = {parent: tuple(paths) for parent, paths in children.items()}
    calls = tuple(
        (path, frame) for path, frame in sorted(frames.items()) if frame.recipient == MANAGER
    )
    committed = tuple(
        (path, frame)
        for path, frame in calls
        if frame.kind == "CALL" and first_failure(frames, path) is None
    )
    committed_unlocks = {path for path, frame in committed if frame.data[:4] == _UNLOCK}
    conflicts: set[TracePath] = set()
    for path in committed_unlocks:
        for prefix in range(len(path)):
            ancestor = path[:prefix]
            if ancestor in committed_unlocks:
                conflicts.update((path, ancestor))

    results = []
    for path, frame in calls:
        if frame.data[:4] != _UNLOCK:
            continue
        if path not in committed_unlocks or path in conflicts:
            reason = "nested_committed_unlock" if path in conflicts else "uncommitted_unlock"
            results.append(WethUnlockFunding(path, frame.sender, reason, None))
            continue
        callback = (*path, 0)
        operations = tuple(
            (p, f) for p, f in committed if len(p) > len(callback) and _within(p, callback)
        )
        scoped = tuple(m for m in movements if _within(m.path, callback))
        results.append(_analyze_unlock(path, frames, child_paths, operations, scoped))
    return WethPrincipalReport(tuple(results))
