"""Reconcile Ethereum Morpho Blue flash-loan receipts inside an observed callback."""

from collections import defaultdict
from dataclasses import dataclass

from ..arbitrage.cycle_protocols import abi, data_bytes, topic
from ..models.chain import LogRecord
from ..models.common import ZERO_ADDRESS
from .public_receipts import ObservedReceipt
from .research_traces import Frame, TracePath, nested_calls
from .swap_flow import TRANSFER, FundingLink, SwapFundingProfile, analyze_swap_funding
from .swap_flow_trace import (
    TraceSwapFunding,
    TraceSwapProof,
    _calldata,
    _indexed_address,
    _links,
    _receipt_positions,
    _static_then_bytes,
    _Transfer,
    _transfers,
)

MORPHO_ETHEREUM = "0xbbbbbbbbbb9cc5e90e3b3af64bdaf62c37eeffcb"
FLASH_LOAN = topic("FlashLoan(address,address,uint256)")
_APPROVAL = topic("Approval(address,address,uint256)")
_TRUE = (1).to_bytes(32, "big")
_LOAN_TYPES = ("address", "uint256", "bytes")
_CALLBACK_TYPES = ("uint256", "bytes")


@dataclass(frozen=True, slots=True)
class BorrowerCycle:
    swap_logs: tuple[int, ...]
    returned_raw: int
    surplus_raw: int


@dataclass(frozen=True, slots=True)
class MorphoCallback:
    loan_log: int
    callback: TracePath
    borrower: str
    asset: str
    principal_raw: int
    repaid_raw: int
    credit_log: int
    repayment_log: int
    callback_links: tuple[FundingLink, ...]
    callback_profile: SwapFundingProfile
    principal_cycle: BorrowerCycle | None


@dataclass(frozen=True, slots=True)
class CallbackResearch:
    loans: tuple[MorphoCallback, ...]
    unresolved_loan_logs: tuple[tuple[int, str], ...]


def _loan_arguments(loan: Frame) -> tuple[str, int, bytes]:
    raw = _calldata(loan, "flashLoan", _LOAN_TYPES)
    offset = 32 * len(_LOAN_TYPES)
    if len(raw) >= offset + 32 and int.from_bytes(raw[offset - 32 : offset], "big") == offset:
        size = int.from_bytes(raw[offset : offset + 32], "big")
        end = offset + 32 + ((size + 31) // 32) * 32
        # A settled Morpho loan carries one excess zero word outside its declared
        # bytes argument. The callback still has to reproduce the exact payload.
        if len(raw) == end + 32 and raw[end:] == bytes(32):
            raw = raw[:end]
    token, amount, payload = _static_then_bytes(_LOAN_TYPES, raw)
    return token, amount, payload


def _bound_transfer(
    frame: Frame,
    payments: list[_Transfer],
    method: str,
    sender: str,
    recipient: str,
    token: str,
    amount: int,
) -> _Transfer:
    types = ("address", "uint256") if method == "transfer" else ("address", "address", "uint256")
    expected = (recipient, amount) if method == "transfer" else (sender, recipient, amount)
    if (
        frame.sender != MORPHO_ETHEREUM
        or frame.recipient != token
        or frame.value
        or frame.error
        or frame.output not in (b"", _TRUE)
        or abi(types, _calldata(frame, method, types)) != expected
        or len(payments) != 1
    ):
        raise ValueError("missing unique loan token payment")
    pay = payments[0]
    if (pay.sender, pay.recipient, pay.token, pay.amount) != (sender, recipient, token, amount):
        raise ValueError("loan token payment and transfer differ")
    return pay


def _cycle(
    profile: SwapFundingProfile,
    proofs: tuple[TraceSwapProof, ...],
    transfers: tuple[_Transfer, ...],
    credit: _Transfer,
    repay: _Transfer,
    opaque_assets: set[str],
) -> BorrowerCycle | None:
    path = profile.longest_witnessed_path
    if len(path) < 2 or len(path) != len(proofs) or profile.links != len(path) - 1:
        return None
    by_log = {p.leg.event.log_index: p for p in proofs}
    first, last = by_log[path[0]], by_log[path[-1]]
    if (
        any(p.leg.token_in in opaque_assets or p.leg.token_out in opaque_assets for p in proofs)
        or first.leg.token_in != last.leg.token_out
        or first.leg.token_in != credit.token
        or first.leg.amount_in != credit.amount
        or last.leg.amount_out < repay.amount
    ):
        return None
    # Exact principal is received, paid once, recovered once, then repaid. No
    # other visible movement of that token through the borrower in this interval.
    movements = [
        t
        for t in transfers
        if t.token == credit.token and credit.recipient in (t.sender, t.recipient)
    ]
    if (
        len(movements) != 2
        or (movements[0].index, movements[1].index)
        != (first.input_transfer_log, last.output_transfer_log)
        or movements[0].sender != credit.recipient
        or movements[1].recipient != credit.recipient
        or not credit.index < movements[0].index < movements[1].index < repay.index
    ):
        return None
    return BorrowerCycle(path, last.leg.amount_out, last.leg.amount_out - repay.amount)


def _authenticate(
    log: LogRecord,
    path: TracePath,
    frames: dict[TracePath, Frame],
    by_call: dict[TracePath, list[_Transfer]],
) -> tuple[str, str, int, _Transfer, _Transfer]:
    loan = frames[path]
    borrower, token = map(_indexed_address, log.topics[1:])
    (amount,) = abi(("uint256",), data_bytes(log.data))
    arg_token, arg_amount, payload = _loan_arguments(loan)
    if (*path, 3) in frames or (*path, 0, 0) in frames or (*path, 2, 0) in frames:
        raise ValueError("loan needs direct unambiguous token transfers")
    cb = (*path, 1)
    callback = frames[cb]
    if (
        not amount
        or (arg_token, arg_amount) != (token, amount)
        or loan.sender != borrower
        or loan.recipient != MORPHO_ETHEREUM
        or loan.output != b""
        or callback.error is not None
        or loan.value
        or borrower in (token, MORPHO_ETHEREUM, ZERO_ADDRESS)
        or token in (MORPHO_ETHEREUM, ZERO_ADDRESS)
        or (callback.sender, callback.recipient, callback.value, callback.output)
        != (MORPHO_ETHEREUM, borrower, 0, b"")
        or _static_then_bytes(
            _CALLBACK_TYPES, _calldata(callback, "onMorphoFlashLoan", _CALLBACK_TYPES)
        )
        != (amount, payload)
    ):
        raise ValueError("loan event, callback and calldata differ")
    p0, p2 = (*path, 0), (*path, 2)
    credit = _bound_transfer(
        frames[p0], by_call[p0], "transfer", MORPHO_ETHEREUM, borrower, token, amount
    )
    repay = _bound_transfer(
        frames[p2], by_call[p2], "transferFrom", borrower, MORPHO_ETHEREUM, token, amount
    )
    if not log.log_index < credit.index < repay.index:
        raise ValueError("loan event, credit and repayment order differ")
    return borrower, token, amount, credit, repay


def morpho_callbacks(
    receipt: ObservedReceipt, tree: object, funding: TraceSwapFunding
) -> CallbackResearch:
    """Check committed, three-child loan calls and attribute only their own market legs."""
    events = [
        log
        for log in receipt.logs
        if receipt.block.chain_id == 1
        and log.address == MORPHO_ETHEREUM
        and log.topics[:1] == (FLASH_LOAN,)
    ]
    if not events:
        return CallbackResearch((), ())
    frames = nested_calls(tree)
    positions = _receipt_positions(tree, frames, receipt)
    transfers = _transfers(receipt, positions)
    by_call: dict[TracePath, list[_Transfer]] = defaultdict(list)
    for t in transfers:
        by_call[t.path].append(t)
    by_path: dict[TracePath, list[LogRecord]] = defaultdict(list)
    for log in events:
        by_path[positions[log.log_index]].append(log)
    loans = []
    rejected: list[tuple[int, str]] = []
    for path, emitted in by_path.items():
        try:
            if len(emitted) != 1:
                raise ValueError("ambiguous loan frame")
            log = emitted[0]
            if len(log.topics) != 3:
                raise ValueError("malformed loan event")
            borrower, token, amount, credit, repay = _authenticate(log, path, frames, by_call)
            cb = (*path, 1)
            inside = tuple(t for t in transfers if t.path[: len(cb)] == cb)
            indices = {t.index for t in inside}
            legs = tuple(
                p
                for p in funding.resolved_swaps
                if p.call_path[: len(cb)] == cb
                and all(
                    i is None or i in indices for i in (p.input_transfer_log, p.output_transfer_log)
                )
            )
            opaque_assets = {
                e.address
                for e in receipt.logs
                if credit.index < e.log_index < repay.index
                and e.topics[:1] != (_APPROVAL,)
                and (e.topics[:1] != (TRANSFER,) or e.log_index not in indices)
            }
            nested = any(p[: len(cb)] == cb for p in by_path)
            # An outer loan can share an asset and borrower with a nested one.
            # Keep its authenticated repayment but leave pooled principal unallocated.
            scoped = () if nested else legs
            links = _links(scoped, inside, borrower, ())
            profile = analyze_swap_funding((p.leg for p in scoped), links)
            cycle = _cycle(profile, scoped, inside, credit, repay, opaque_assets)
            loans.append(
                MorphoCallback(
                    log.log_index,
                    cb,
                    borrower,
                    token,
                    amount,
                    repay.amount,
                    credit.index,
                    repay.index,
                    links,
                    profile,
                    cycle,
                )
            )
        except (KeyError, ValueError) as exc:
            rejected.extend((e.log_index, str(exc)) for e in emitted)
    return CallbackResearch(tuple(loans), tuple(rejected))
