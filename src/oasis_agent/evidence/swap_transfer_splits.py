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
    _V3_SWAP,
    TraceSwapFunding,
    TraceSwapProof,
    _calldata,
    _receipt_positions,
    _static_then_bytes,
    _Transfer,
    _transfers,
    replay_trace_funding,
)

_TRANSFER = keccak(b"transfer(address,uint256)")[:4]
_BALANCE = keccak(b"balanceOf(address)")[:4]
_RESERVES = keccak(b"getReserves()")[:4]
_SYNC = topic("Sync(uint112,uint112)")
_TRUE = (1).to_bytes(32)
_PANCAKE_V3_CALLBACK = ("int256", "int256", "bytes")


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
    reserve_reads: tuple[TracePath, ...]
    sync_logs: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class PancakeV3PaymentWitness:
    balance_before: TracePath
    callback: TracePath
    transfer_call: TracePath
    balance_after: TracePath


@dataclass(frozen=True, slots=True)
class PancakeV3TokenRetainedSplit(TokenRetainedSplit):
    source_payment: PancakeV3PaymentWitness


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


def _pancake_v3_payment_witness(
    proof: TraceSwapProof,
    frames: dict[TracePath, Frame],
    incoming: _Transfer | None,
    pool_transfers: list[_Transfer],
    holder: str,
    output_call: TracePath,
) -> PancakeV3PaymentWitness | None:
    """Bind the retained exact-input swap to the pool's actual input balance change."""
    leg, path = proof.leg, proof.call_path
    pool = leg.event.pool[0]
    call = frames[path]
    recipient, zero_for_one, specified, _, callback_data = _static_then_bytes(
        _V3_SWAP, _calldata(call, "swap", _V3_SWAP)
    )
    if (
        not path
        or output_call[:-1] != path
        or call.sender != holder
        or call.value
        or recipient != holder
        or specified != leg.amount_in
        or zero_for_one != (leg.token_in < leg.token_out)
    ):
        return None
    # PancakeV3Pool.swap transfers the output, reads the input balance, calls
    # pancakeV3SwapCallback, then checks the input balance again.
    amount0, amount1 = (
        (leg.amount_in, -leg.amount_out) if zero_for_one else (-leg.amount_out, leg.amount_in)
    )
    if incoming is None or incoming.sender != holder:
        return None
    payment = _transfer_call(frames, incoming)
    if not payment or len(payment) != len(path) + 2 or payment[:-2] != path:
        return None
    if frames[payment].data != (
        _TRANSFER + bytes(12) + data_bytes(pool) + leg.amount_in.to_bytes(32)
    ):
        return None
    callback = payment[:-1]
    callback_frame = frames[callback]
    if callback_frame.recipient != holder or callback_frame.value:
        return None
    if _static_then_bytes(
        _PANCAKE_V3_CALLBACK,
        _calldata(callback_frame, "pancakeV3SwapCallback", _PANCAKE_V3_CALLBACK),
    ) != (amount0, amount1, callback_data):
        return None

    query = _BALANCE + bytes(12) + data_bytes(pool)
    before, after = (*path, callback[-1] - 1), (*path, callback[-1] + 1)
    if not output_call < before:
        return None
    balances = []
    for position in (before, after):
        read = frames.get(position)
        if (
            read is None
            or read.kind != "STATICCALL"
            or read.recipient != leg.token_in
            or read.data != query
            or read.error is not None
            or read.output is None
            or len(read.output) != 32
        ):
            return None
        balances.append(int.from_bytes(read.output))
    if balances[1] - balances[0] != leg.amount_in or [
        t.index for t in pool_transfers if before < t.path < after
    ] != [incoming.index]:
        return None
    return PancakeV3PaymentWitness(before, callback, payment, after)


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
    transfers_by_index = {m.index: m for m in transfers}
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
    pool_movements: dict[tuple[str, str], list[_Transfer]] = defaultdict(list)
    for transfer in transfers:
        if holder in (transfer.sender, transfer.recipient):
            movements[transfer.token].append(transfer)
        for account in {transfer.sender, transfer.recipient}:
            pool_movements[transfer.token, account].append(transfer)

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
            target_state = _v2_reserve_witness(
                swaps[target], frames, logs_by_frame[swaps[target].call_path]
            )
            if target_state is None:
                continue
            source_proof = swaps[source]
            source_payment = None
            pool_states: tuple[tuple[TracePath, int], ...]
            if receipt.block.chain_id == 56 and source_proof.leg.event.shape == "pancake_v3":
                input_asset = source_proof.leg.token_in
                input_transfer = source_proof.input_transfer_log
                if input_transfer is None or input_asset in malformed:
                    continue
                try:
                    source_payment = _pancake_v3_payment_witness(
                        source_proof,
                        frames,
                        transfers_by_index.get(input_transfer),
                        pool_movements[input_asset, source_proof.leg.event.pool[0]],
                        holder,
                        ingress,
                    )
                except ValueError:
                    continue
                if source_payment is None:
                    continue
                pool_states = (target_state,)
            else:
                source_state = _v2_reserve_witness(
                    source_proof, frames, logs_by_frame[source_proof.call_path]
                )
                if source_state is None:
                    continue
                pool_states = (source_state, target_state)
            split_args = (
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
                tuple(state[0] for state in pool_states),
                tuple(state[1] for state in pool_states),
            )
            split = (
                TokenRetainedSplit(*split_args)
                if source_payment is None
                else PancakeV3TokenRetainedSplit(*split_args, source_payment)
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
