"""Decode retained callTracer funding evidence from exact receipt logs and calldata."""

from collections import Counter, defaultdict
from dataclasses import dataclass
from itertools import pairwise
from typing import Any

from eth_hash.auto import keccak

from ..arbitrage.cycle_plan import address, hash32
from ..arbitrage.cycle_protocols import abi, data_bytes
from ..models.chain import LogRecord
from ..models.common import ZERO_ADDRESS
from .public_receipts import ObservedReceipt, quantity
from .research_traces import Frame, TracePath, nested_calls
from .rpc_validation import strict_list, strict_object
from .swap_flow import (
    TRANSFER,
    FundingLink,
    ObservedSwap,
    SwapFundingProfile,
    SwapLeg,
    analyze_swap_funding,
    profile_swap_receipt,
)

_BANCOR_NETWORK = "0x2f9ec37d6ccfff1cab21733bdadede11c823ccb0"
_ETHEREUM_V4 = "0x000000000004444c5dc75cb358380d2e3de08a90"
_BANCOR_PATH = ("address[]", "uint256", "uint256", "address", "address", "uint256")
_BANCOR_CONVERT = ("address", "address", "uint256", "address", "address")
_V2_SWAP = ("uint256", "uint256", "address", "bytes")
_V3_SWAP = ("address", "bool", "int256", "uint160", "bytes")
_V3_EVENT = ("int256", "int256", "uint160", "uint128", "int24")
_V4_KEY = ("address", "address", "uint24", "int24", "address")
_V4_SWAP = ("(" + ",".join(_V4_KEY) + ")", "(bool,int256,uint160)", "bytes")
_V4_EVENT = ("int128", "int128", "uint160", "uint128", "int24", "uint24")
_MAX_LOGS = 16_384


def _calldata(frame: Frame, method: str, types: tuple[str, ...]) -> bytes:
    selector = keccak((method + "(" + ",".join(types) + ")").encode())[:4]
    if frame.kind != "CALL" or frame.data[:4] != selector:
        raise ValueError("missing matching swap CALL")
    return frame.data[4:]


def _indexed_address(value: str) -> str:
    return address(abi(("address",), data_bytes(value))[0])


def _static_then_bytes(types: tuple[str, ...], raw: bytes) -> tuple[Any, ...]:
    # Solidity accepts a final bytes argument ending at the last actual byte.
    # Normalize only missing trailing zero padding, never missing argument data.
    offset = 32 * len(types)
    if (
        len(raw) >= offset + 32
        and int.from_bytes(raw[offset - 32 : offset], "big") == offset
        and int.from_bytes(raw[offset : offset + 32], "big") == len(raw) - offset - 32
    ):
        raw += bytes((-len(raw)) % 32)
    return abi(types, raw)


def _signed128(value: int) -> int:
    return value if value < 1 << 127 else value - (1 << 128)


@dataclass(frozen=True, slots=True)
class _Transfer:
    index: int
    path: TracePath
    token: str
    sender: str
    recipient: str
    amount: int


@dataclass(frozen=True, slots=True)
class TraceSwapProof:
    leg: SwapLeg
    call_path: TracePath
    input_transfer_log: int | None
    output_transfer_log: int | None
    hookless_v4_caller: str | None = None


@dataclass(frozen=True, slots=True)
class TraceSwapFunding:
    profile: SwapFundingProfile
    resolved_swaps: tuple[TraceSwapProof, ...]
    links: tuple[FundingLink, ...]
    unresolved_events: tuple[tuple[int, str], ...]


def _receipt_positions(
    tree: object, frames: dict[TracePath, Frame], receipt: ObservedReceipt
) -> dict[int, TracePath]:
    if len(receipt.logs) > _MAX_LOGS:
        raise ValueError("receipt exceeds trace log budget")
    root = frames[()]
    if (root.sender, root.recipient, root.error is None) != (
        receipt.sender,
        receipt.recipient,
        receipt.status == 1,
    ):
        raise ValueError("callTracer root and receipt roles or status differ")
    if quantity(strict_object(tree, "callTracer root").get("gasUsed"), 64) != receipt.gas_used:
        raise ValueError("callTracer gas differs from receipt")
    matched: dict[int, TracePath] = {}
    expected = iter(receipt.logs)

    def check(item: object, path: TracePath) -> None:
        row = strict_object(item, "nested callTracer frame")
        if frames[path].error is not None:
            return
        children = strict_list(row.get("calls", []), "callTracer children")
        by_position: dict[int, list[dict[str, object]]] = defaultdict(list)
        previous = 0
        for item in strict_list(row.get("logs", []), "callTracer logs"):
            log = strict_object(item, "callTracer log")
            pos = quantity(log.get("position"), 32)
            if not previous <= pos <= len(children):
                raise ValueError("invalid callTracer log position")
            by_position[pos].append(log)
            previous = pos
        for pos in range(len(children) + 1):
            for trace_log in by_position.get(pos, []):
                log_row = next(expected, None)
                topics = strict_list(trace_log.get("topics"), "callTracer topics")
                if log_row is None or (
                    address(trace_log.get("address")),
                    tuple(hash32(t) for t in topics),
                    data_bytes(trace_log.get("data")),
                ) != (log_row.address, log_row.topics, data_bytes(log_row.data)):
                    raise ValueError("callTracer logs differ from receipt")
                if log_row.log_index in matched:
                    raise ValueError("duplicate receipt log index")
                matched[log_row.log_index] = path
            if pos < len(children):
                check(children[pos], (*path, pos))

    check(tree, ())
    if next(expected, None) is not None:
        raise ValueError("callTracer logs do not cover receipt")
    return matched


def _transfers(receipt: ObservedReceipt, paths: dict[int, TracePath]) -> tuple[_Transfer, ...]:
    result = []
    for log in receipt.logs:
        if len(log.topics) != 3 or log.topics[0] != TRANSFER:
            continue
        try:
            (amount,) = abi(("uint256",), data_bytes(log.data))
            sender, recipient = map(_indexed_address, log.topics[1:])
        except ValueError:
            continue
        if amount:
            result.append(
                _Transfer(
                    log.log_index, paths[log.log_index], log.address, sender, recipient, amount
                )
            )
    return tuple(result)


def _unique(rows: list[_Transfer]) -> _Transfer:
    if len(rows) != 1:
        raise ValueError("missing or ambiguous exact transfer")
    return rows[0]


def _bancor(
    receipt: ObservedReceipt,
    event: ObservedSwap,
    log: LogRecord,
    path: TracePath,
    frames: dict[TracePath, Frame],
    transfers: tuple[_Transfer, ...],
) -> TraceSwapProof:
    frame = frames[path]
    if receipt.block.chain_id != 1 or not path or frame.sender != _BANCOR_NETWORK:
        raise ValueError("unsupported Bancor converter network")
    parent = frames[path[:-1]]
    if parent.recipient != _BANCOR_NETWORK or frame.value or parent.value:
        raise ValueError("unsupported Bancor converter parent or native asset")
    token_in, token_out, quantity_in, trader, beneficiary = abi(
        _BANCOR_CONVERT, _calldata(frame, "convert", _BANCOR_CONVERT)
    )
    if (
        tuple(map(_indexed_address, log.topics[1:])) != (token_in, token_out, trader)
        or ZERO_ADDRESS in (token_in, token_out, trader, beneficiary)
        or parent.sender != trader
        or frame.output is None
        or parent.output is None
    ):
        raise ValueError("Bancor call and event roles differ")
    amount_in, amount_out, _ = abi(("uint256", "uint256", "int256"), data_bytes(log.data))
    route, supplied, minimum, destination, _, _ = abi(
        _BANCOR_PATH, _calldata(parent, "convertByPath", _BANCOR_PATH)
    )
    if (
        len(route) != 3
        or (route[0], route[-1]) != (token_in, token_out)
        or supplied != quantity_in
        or amount_in != quantity_in
        or (trader if destination == ZERO_ADDRESS else destination) != beneficiary
        or amount_out < minimum
        or abi(("uint256",), frame.output) != (amount_out,)
        or abi(("uint256",), parent.output) != (amount_out,)
    ):
        raise ValueError("Bancor path, event and return values disagree")
    inputs = [
        t
        for t in transfers
        if t.path[:-1] == path[:-1]
        and t.path[-1] < path[-1]
        and (t.token, t.sender, t.recipient, t.amount) == (token_in, trader, log.address, amount_in)
    ]
    outputs = [
        t
        for t in transfers
        if t.path[: len(path)] == path
        and (t.token, t.sender, t.recipient, t.amount)
        == (token_out, log.address, beneficiary, amount_out)
    ]
    return TraceSwapProof(
        SwapLeg(event, token_in, token_out, amount_in, amount_out),
        path,
        _unique(inputs).index,
        _unique(outputs).index,
    )


def _v2(
    event: ObservedSwap,
    log: LogRecord,
    path: TracePath,
    frame: Frame,
    transfers: tuple[_Transfer, ...],
) -> TraceSwapProof:
    i0, i1, o0, o1 = abi(("uint256",) * 4, data_bytes(log.data))
    out0, out1, beneficiary, _ = abi(_V2_SWAP, _calldata(frame, "swap", _V2_SWAP))
    if (
        bool(i0) == bool(i1)
        or bool(o0) == bool(o1)
        or (out0, out1) != (o0, o1)
        or (_indexed_address(log.topics[1]), _indexed_address(log.topics[2]))
        != (frame.sender, beneficiary)
    ):
        raise ValueError("unsupported or ambiguous V2 settlement")
    input_transfer = _unique(
        [
            t
            for t in transfers
            if t.recipient == log.address and t.amount == i0 + i1 and t.index < log.log_index
        ]
    )
    output_transfer = _unique(
        [
            t
            for t in transfers
            if (t.sender, t.recipient, t.amount) == (log.address, beneficiary, o0 + o1)
            and t.path[: len(path)] == path
        ]
    )
    return TraceSwapProof(
        SwapLeg(event, input_transfer.token, output_transfer.token, i0 + i1, o0 + o1),
        path,
        input_transfer.index,
        output_transfer.index,
    )


def _v3(
    event: ObservedSwap,
    log: LogRecord,
    path: TracePath,
    frame: Frame,
    transfers: tuple[_Transfer, ...],
) -> TraceSwapProof:
    recipient, zero_for_one, amount_specified, _, _ = _static_then_bytes(
        _V3_SWAP, _calldata(frame, "swap", _V3_SWAP)
    )
    layout = (*_V3_EVENT, "uint128", "uint128") if event.shape == "pancake_v3" else _V3_EVENT
    delta0, delta1, *_ = abi(layout, data_bytes(log.data))
    amount_in, amount_out = (delta0, -delta1) if zero_for_one else (delta1, -delta0)
    if (
        frame.output is None
        or abi(("int256", "int256"), frame.output) != (delta0, delta1)
        or (_indexed_address(log.topics[1]), _indexed_address(log.topics[2]))
        != (frame.sender, recipient)
        or not amount_specified
        or amount_in <= 0
        or amount_out <= 0
    ):
        raise ValueError("V3 return or settlement roles differ")
    local = [t for t in transfers if t.path[: len(path)] == path]
    paid = _unique([t for t in local if t.recipient == log.address and t.amount == amount_in])
    delivered = _unique(
        [
            t
            for t in local
            if (t.sender, t.recipient, t.amount) == (log.address, recipient, amount_out)
        ]
    )
    return TraceSwapProof(
        SwapLeg(event, paid.token, delivered.token, amount_in, amount_out),
        path,
        paid.index,
        delivered.index,
    )


def _v4(
    receipt: ObservedReceipt,
    event: ObservedSwap,
    log: LogRecord,
    path: TracePath,
    frame: Frame,
    transfers: tuple[_Transfer, ...],
) -> TraceSwapProof:
    if (
        receipt.block.chain_id != 1
        or log.address != _ETHEREUM_V4
        or _indexed_address(log.topics[2]) != frame.sender
        or frame.output is None
    ):
        raise ValueError("unsupported V4 manager or call")
    raw = _calldata(frame, "swap", _V4_SWAP)
    key, params, _ = abi(_V4_SWAP, raw)
    if "0x" + keccak(raw[: 32 * len(_V4_KEY)]).hex() != log.topics[1]:
        raise ValueError("V4 PoolKey differs from event")
    delta0, delta1, *_ = abi(_V4_EVENT, data_bytes(log.data))
    (packed,) = abi(("uint256",), frame.output)
    if (delta0, delta1) != tuple(map(_signed128, divmod(packed, 1 << 128))):
        raise ValueError("V4 returned delta differs from event")
    i = 0 if params[0] else 1
    amount_in, amount_out = -(delta0, delta1)[i], (delta0, delta1)[1 - i]
    leg = SwapLeg(event, key[i], key[1 - i], amount_in, amount_out)
    # Settlement can be internal to a singleton unlock. Only ERC-20 payments
    # in the same immediate callback subtree produce automatic links here.
    callback = path[:-1]
    incoming = [
        t
        for t in transfers
        if (t.token, t.recipient, t.amount) == (leg.token_in, log.address, amount_in)
        and t.path[: len(callback)] == callback
    ]
    outgoing = [
        t
        for t in transfers
        if (t.token, t.sender, t.amount) == (leg.token_out, log.address, amount_out)
        and t.path[: len(callback)] == callback
    ]
    return TraceSwapProof(
        leg,
        path,
        incoming[0].index if len(incoming) == 1 else None,
        outgoing[0].index if len(outgoing) == 1 else None,
        frame.sender if key[4] == ZERO_ADDRESS else None,
    )


def _v4_links(
    proofs: tuple[TraceSwapProof, ...],
    transfers: tuple[_Transfer, ...],
    frames: dict[TracePath, Frame],
) -> tuple[FundingLink, ...]:
    """Recognize exact opposite credits of adjacent hookless legs in one unlock."""
    by_path = {p.call_path: p for p in proofs if p.hookless_v4_caller is not None}
    links = []
    for path, first in by_path.items():
        if len(path) < 2 or first.output_transfer_log is not None:
            continue
        second = by_path.get((*path[:-1], path[-1] + 1))
        asset, amount = first.leg.token_out, first.leg.amount_out
        if (
            second is None
            or second.hookless_v4_caller != first.hookless_v4_caller
            or second.input_transfer_log is not None
            or first.leg.event.pool == second.leg.event.pool
            or asset == ZERO_ADDRESS
            or (asset, amount) != (second.leg.token_in, second.leg.amount_in)
        ):
            continue
        callback, unlock = frames[path[:-1]], frames[path[:-2]]
        if (
            callback.sender != _ETHEREUM_V4
            or callback.recipient != first.hookless_v4_caller
            or unlock.recipient != _ETHEREUM_V4
            or unlock.sender != callback.recipient
            or any(
                t.token == asset
                and _ETHEREUM_V4 in (t.sender, t.recipient)
                and t.path[: len(path) - 1] == path[:-1]
                for t in transfers
            )
        ):
            continue
        try:
            unlocking = _static_then_bytes(("bytes",), _calldata(unlock, "unlock", ("bytes",)))
            callback_args = _static_then_bytes(
                ("bytes",), _calldata(callback, "unlockCallback", ("bytes",))
            )
        except ValueError:
            continue
        if unlocking == callback_args:
            src, dst = first.leg.event.log_index, second.leg.event.log_index
            links.append(FundingLink(src, dst, asset, amount, f"unlock:{src},{dst}"))
    return tuple(links)


def _links(
    proofs: tuple[TraceSwapProof, ...],
    transfers: tuple[_Transfer, ...],
    receiver: str | None,
    internal: tuple[FundingLink, ...],
) -> tuple[FundingLink, ...]:
    observed = {t.index: t for t in transfers}
    by_log: dict[int, list[TraceSwapProof]] = defaultdict(list)
    movements: dict[str, list[_Transfer]] = defaultdict(list)
    for proof in proofs:
        if proof.input_transfer_log is not None:
            by_log[proof.input_transfer_log].append(proof)
    for t in transfers:
        if receiver in (t.sender, t.recipient):
            movements[t.token].append(t)
    next_touch = {
        previous.index: following
        for history in movements.values()
        for previous, following in pairwise(history)
    }
    candidates: list[tuple[FundingLink, tuple[int, ...]]] = [(link, ()) for link in internal]
    for swap in proofs:
        if swap.output_transfer_log is None:
            continue
        paid = observed[swap.output_transfer_log]
        paths: list[tuple[TraceSwapProof, tuple[int, ...]]] = [
            (candidate, (paid.index,)) for candidate in by_log.get(paid.index, [])
        ]
        transfer = next_touch.get(paid.index)
        if (
            paid.recipient == receiver
            and transfer is not None
            and transfer.sender == receiver
            and transfer.amount == paid.amount
            and transfer.index > paid.index
        ):
            paths.extend(
                (candidate, (paid.index, transfer.index))
                for candidate in by_log.get(transfer.index, [])
            )
        for target, path in paths:
            a, b = swap.leg, target.leg
            if a.event.pool != b.event.pool and (a.token_out, a.amount_out) == (
                b.token_in,
                b.amount_in,
            ):
                evidence_id = "logs:" + ",".join(str(i) for i in path)
                candidates.append(
                    (
                        FundingLink(
                            a.event.log_index,
                            b.event.log_index,
                            paid.token,
                            paid.amount,
                            evidence_id,
                        ),
                        path,
                    )
                )
    used_transfers = Counter(index for _, path in candidates for index in path)
    used_inputs = Counter(link.target for link, _ in candidates)
    used_outputs = Counter(link.source for link, _ in candidates)
    return tuple(
        link
        for link, path in candidates
        if used_inputs[link.target] == used_outputs[link.source] == 1
        and all(used_transfers[index] == 1 for index in path)
    )


def replay_trace_funding(receipt: ObservedReceipt, trace: object) -> TraceSwapFunding:
    """Conservatively resolve supported swaps and full-amount transfer links."""
    frames = nested_calls(trace)
    positions = _receipt_positions(trace, frames, receipt)
    transfers = _transfers(receipt, positions)
    logs = {log.log_index: log for log in receipt.logs}
    resolved = []
    unresolved = []
    for event in profile_swap_receipt(receipt).events:
        idx = event.log_index
        log = logs[idx]
        path = positions[idx]
        frame = frames[path]
        try:
            if frame.recipient != log.address:
                parent = frames.get(path[:-1])
                if (
                    frame.kind != "DELEGATECALL"
                    or parent is None
                    or parent.recipient != log.address
                    or (parent.data, parent.output) != (frame.data, frame.output)
                ):
                    raise ValueError("unsupported logging call context")
                path, frame = path[:-1], parent
            if event.shape == "bancor_conversion":
                proof = _bancor(receipt, event, log, path, frames, transfers)
            elif event.shape == "v2":
                proof = _v2(event, log, path, frame, transfers)
            elif event.shape in {"v3", "pancake_v3"}:
                proof = _v3(event, log, path, frame, transfers)
            elif event.shape == "v4":
                proof = _v4(receipt, event, log, path, frame, transfers)
            else:
                raise ValueError("unsupported swap shape for exact funding")
            resolved.append(proof)
        except ValueError as exc:
            unresolved.append((idx, str(exc)))
    counts = Counter(swap.call_path for swap in resolved)
    unresolved.extend(
        (swap.leg.event.log_index, "ambiguous multi-event swap CALL")
        for swap in resolved
        if counts[swap.call_path] > 1
    )
    proofs = tuple(swap for swap in resolved if counts[swap.call_path] == 1)
    links = _links(proofs, transfers, receipt.recipient, _v4_links(proofs, transfers, frames))
    return TraceSwapFunding(
        analyze_swap_funding((item.leg for item in proofs), links),
        proofs,
        links,
        tuple(sorted(unresolved)),
    )
