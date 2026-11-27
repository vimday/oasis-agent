"""Verify a token-retained amount between two swaps in a committed call trace."""

from collections import defaultdict
from dataclasses import dataclass
from itertools import pairwise

from eth_hash.auto import keccak

from ..arbitrage.cycle_protocols import data_bytes, topic
from ..models.chain import LogRecord
from .public_receipts import ObservedReceipt
from .research_traces import Frame, TracePath, first_failure, nested_calls
from .swap_flow import TRANSFER, FundingLink, SwapFundingProfile, analyze_swap_funding
from .swap_flow_trace import (
    TraceSwapFunding,
    TraceSwapProof,
    _receipt_positions,
    _Transfer,
    _transfers,
    replay_trace_funding,
)

_TRANSFER = keccak(b"transfer(address,uint256)")[:4]
_BALANCE = keccak(b"balanceOf(address)")[:4]
_RESERVES = keccak(b"getReserves()")[:4]
_SYNC = topic("Sync(uint112,uint112)")
_TRUE = (1).to_bytes(32)


@dataclass(frozen=True, slots=True)
class TokenRetainedSplit:
    source_swap_log: int
    target_swap_log: int
    token: str
    holder: str
    pool: str
    gross: int
    credited: int
    retained_by_token: int
    source_transfer_log: int
    pool_credit_log: int
    token_credit_log: int
    transfer_call: TracePath
    balance_reads: tuple[TracePath, TracePath, TracePath]
    reserve_reads: tuple[TracePath, TracePath]
    sync_logs: tuple[int, int]


@dataclass(frozen=True, slots=True)
class SplitFunding:
    splits: tuple[TokenRetainedSplit, ...]
    profile_with_splits: SwapFundingProfile


def _v2_reserve_witness(
    proof: TraceSwapProof, frames: dict[TracePath, Frame], pool_logs: list[LogRecord]
) -> tuple[TracePath, int] | None:
    """Bind both currencies and amounts to reserve reads, the pair's own balances and Sync."""
    leg, path = proof.leg, proof.call_path
    if leg.event.shape != "v2" or not path:
        return None
    pool = leg.event.pool[0]
    prior = None
    for i in reversed(range(path[-1])):
        p = (*path[:-1], i)
        read = frames[p]
        if read.recipient != pool:
            continue
        if (
            read.kind == "STATICCALL"
            and read.data == _RESERVES
            and read.error is None
            and read.output is not None
            and len(read.output) == 96
        ):
            prior = p, read.output
        break
    if prior is None:
        return None
    reserve_path, raw = prior
    old = [int.from_bytes(raw[i : i + 32]) for i in (0, 32)]
    if max(old) >= 1 << 112 or int.from_bytes(raw[64:]) >= 1 << 32:
        return None
    logs = [log for log in pool_logs if log.address == pool]
    if len(logs) != 2 or logs[0].topics != (_SYNC,) or logs[1].log_index != leg.event.log_index:
        return None
    sync, swap = (data_bytes(log.data) for log in logs)
    if len(sync) != 64 or len(swap) != 128:
        return None
    new = [int.from_bytes(sync[i : i + 32]) for i in (0, 32)]
    if max(new) >= 1 << 112:
        return None
    amount0_in, amount1_in, amount0_out, amount1_out = (
        int.from_bytes(swap[i : i + 32]) for i in range(0, 128, 32)
    )
    if new != [old[0] + amount0_in - amount0_out, old[1] + amount1_in - amount1_out]:
        return None
    tokens = [leg.token_in, leg.token_out] if amount0_in else [leg.token_out, leg.token_in]
    pool_balances = []
    i = 0
    while (child_path := (*path, i)) in frames:
        child = frames[child_path]
        if child.data[:4] == _BALANCE:
            if (
                child.kind != "STATICCALL"
                or child.sender != pool
                or child.data != _BALANCE + bytes(12) + data_bytes(pool)
                or child.error is not None
                or child.output is None
                or len(child.output) != 32
            ):
                return None
            pool_balances.append((child.recipient, int.from_bytes(child.output)))
        i += 1
    if pool_balances != list(zip(tokens, new, strict=True)):
        return None
    return reserve_path, logs[0].log_index


def _transfer_call(frames: dict[TracePath, Frame], movement: _Transfer) -> TracePath | None:
    path = movement.path
    frame = frames[path]
    if frame.kind == "DELEGATECALL" and path:
        delegated = frame
        path = path[:-1]
        frame = frames[path]
        if (
            delegated.sender != movement.token
            or delegated.data != frame.data
            or delegated.output != frame.output
        ):
            return None
    if (
        frame.kind == "CALL"
        and frame.sender == movement.sender
        and frame.recipient == movement.token
        and frame.output == _TRUE
        and not frame.value
    ):
        return path
    return None


def replay_with_transfer_splits(
    receipt: ObservedReceipt, trace: object
) -> tuple[TraceSwapFunding, SplitFunding]:
    """Resolve only a zero-inventory holder with a witnessed gross→pool+token debit."""
    funding = replay_trace_funding(receipt, trace)
    if receipt.recipient is None:
        return funding, SplitFunding((), funding.profile)
    frames = nested_calls(trace)
    paths = _receipt_positions(trace, frames, receipt)
    transfers = _transfers(receipt, paths)
    logs_by_frame: dict[TracePath, list[LogRecord]] = defaultdict(list)
    for log in receipt.logs:
        logs_by_frame[paths[log.log_index]].append(log)
    swaps = {p.leg.event.log_index: p for p in funding.resolved_swaps}
    holder = receipt.recipient
    inputs: dict[int | None, list[int]] = defaultdict(list)
    outputs: dict[int | None, list[int]] = defaultdict(list)
    for proof in funding.resolved_swaps:
        outputs[proof.output_transfer_log].append(proof.leg.event.log_index)
        inputs[proof.input_transfer_log].append(proof.leg.event.log_index)
    used_out = {link.source for link in funding.links}
    used_in = {link.target for link in funding.links}
    movements: dict[str, list[_Transfer]] = defaultdict(list)
    for transfer in transfers:
        if holder in (transfer.sender, transfer.recipient):
            movements[transfer.token].append(transfer)

    reads: dict[tuple[TracePath, str], list[tuple[TracePath, int]]] = defaultdict(list)
    query = _BALANCE + bytes(12) + data_bytes(holder)
    for p, frame in sorted(frames.items()):
        if (
            frame.kind == "STATICCALL"
            and frame.sender == holder
            and frame.data == query
            and frame.output is not None
            and len(frame.output) == 32
            and first_failure(frames, p) is None
        ):
            reads[p[:-1], frame.recipient].append((p, int.from_bytes(frame.output)))

    additions = []
    proofs = []
    malformed = {
        log.address
        for log in receipt.logs
        if log.topics
        and log.topics[0] == TRANSFER
        and (
            len(log.topics) != 3
            or len(log.data) != 66
            or any(not t.startswith("0x" + "0" * 24) for t in log.topics[1:])
        )
    }
    for token, touches in movements.items():
        if token in malformed:
            continue
        for i, (paid, other) in enumerate(pairwise(touches)):
            if i == 0 or paid.path != other.path or paid.sender != holder or other.sender != holder:
                continue
            fee, credit = (paid, other) if paid.recipient == token else (other, paid)
            gross = touches[i - 1]
            if (
                fee.recipient != token
                or credit.recipient in (token, holder)
                or gross.recipient != holder
                or gross.sender == holder
                or gross.amount != fee.amount + credit.amount
                or len(outputs[gross.index]) != 1
                or len(inputs[credit.index]) != 1
            ):
                continue
            source, target = outputs[gross.index][0], inputs[credit.index][0]
            if source == target or source in used_out or target in used_in:
                continue
            call = _transfer_call(frames, credit)
            ingress = _transfer_call(frames, gross)
            if not call or not ingress or ingress[: len(call) - 1] != call[:-1]:
                continue
            credit_args = bytes(12) + data_bytes(credit.recipient) + gross.amount.to_bytes(32)
            gross_args = bytes(12) + data_bytes(holder) + gross.amount.to_bytes(32)
            if (
                frames[call].data != _TRANSFER + credit_args
                or frames[ingress].data != _TRANSFER + gross_args
            ):
                continue
            observed = reads[call[:-1], token]
            before = [p for p, amount in observed if p < ingress and amount == 0]
            available = [
                p for p, amount in observed if ingress < p < call and amount == gross.amount
            ]
            after = [p for p, amount in observed if p > call and amount == 0]
            if not before or not available or not after:
                continue
            bounds = (before[-1], available[-1], after[0])
            # A confirmed zero→gross→zero round trip must not consume unrelated inventory.
            if [m.index for m in touches if bounds[0] < m.path < bounds[2]] != [
                gross.index,
                paid.index,
                other.index,
            ]:
                continue
            pool_states = [
                _v2_reserve_witness(swaps[idx], frames, logs_by_frame[swaps[idx].call_path])
                for idx in (source, target)
            ]
            if pool_states[0] is None or pool_states[1] is None:
                continue
            split = TokenRetainedSplit(
                source,
                target,
                token,
                holder,
                credit.recipient,
                gross.amount,
                credit.amount,
                fee.amount,
                gross.index,
                credit.index,
                fee.index,
                call,
                bounds,
                (pool_states[0][0], pool_states[1][0]),
                (pool_states[0][1], pool_states[1][1]),
            )
            proofs.append(split)
            additions.append(
                FundingLink(
                    source,
                    target,
                    token,
                    credit.amount,
                    f"split:{gross.index},{credit.index},{fee.index}",
                )
            )
            used_out.add(source)
            used_in.add(target)
    return funding, SplitFunding(
        tuple(proofs),
        analyze_swap_funding(
            (proof.leg for proof in funding.resolved_swaps), (*funding.links, *additions)
        ),
    )
