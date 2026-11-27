"""Reconcile WETH cash across directly bracketed executor self-calls in a landed receipt."""

from dataclasses import dataclass

from ..arbitrage.cycle_protocols import abi, calldata, data_bytes, topic
from ..models.common import ZERO_ADDRESS
from .public_receipts import ObservedReceipt, quantity
from .research_traces import nested_calls
from .rpc_validation import strict_list, strict_object
from .swap_flow import TRANSFER
from .swap_flow_trace import _receipt_positions
from .weth_principal import WETH

_DEPOSIT = topic("Deposit(address,uint256)")
_WITHDRAW = topic("Withdrawal(address,uint256)")


@dataclass(frozen=True, slots=True)
class SelfCallCash:
    root_child: int
    status: str
    frame_gas: int
    before_weth_wei: int | None = None
    after_weth_wei: int | None = None
    weth_delta_wei: int | None = None
    cash_logs: tuple[int, ...] = ()


def replay_weth_self_calls(receipt: ObservedReceipt, tree: object) -> tuple[SelfCallCash, ...]:
    """Report component cash movements and frame gas before allocating any shared costs."""
    owner = receipt.recipient
    if receipt.block.chain_id != 1 or owner in (None, ZERO_ADDRESS, WETH):
        raise ValueError("self-call cash requires an Ethereum executor contract")
    frames = nested_calls(tree)
    positions = _receipt_positions(tree, frames, receipt)
    raw_children = strict_list(strict_object(tree, "call trace").get("calls", []), "root children")
    query = data_bytes(calldata("balanceOf(address)", ("address",), (owner,)))
    logs_by_child: dict[int, list[tuple[int, int]]] = {}
    for log in receipt.logs:
        p = positions[log.log_index]
        if not p or log.address != WETH or not log.topics:
            continue
        sig = log.topics[0]
        if sig not in (TRANSFER, _DEPOSIT, _WITHDRAW):
            continue
        expected = 3 if sig == TRANSFER else 2
        if len(log.topics) != expected:
            raise ValueError("malformed canonical WETH event")
        (amount,) = abi(("uint256",), data_bytes(log.data))
        who = tuple(abi(("address",), data_bytes(t))[0] for t in log.topics[1:])
        if owner not in who:
            continue
        if sig == TRANSFER:
            delta = amount * (int(who[1] == owner) - int(who[0] == owner))
        else:
            delta = amount if sig == _DEPOSIT else -amount
        logs_by_child.setdefault(p[0], []).append((log.log_index, delta))

    results = []
    for i, child in enumerate(raw_children):
        call = frames[(i,)]
        if call.kind != "CALL" or call.sender != owner or call.recipient != owner:
            continue
        node = strict_object(child, "self-call")
        gas = quantity(node.get("gasUsed"), 64)
        if not receipt.status or call.error:
            status = "reverted" if call.error else "rolled_back_by_root"
            results.append(SelfCallCash(i, status, gas, weth_delta_wei=0))
            continue
        children = strict_list(node.get("calls", []), "self-call children")
        reads = []
        if len(children) >= 2:
            for j in (0, len(children) - 1):
                read = frames[(i, j)]
                if (
                    read.kind == "STATICCALL"
                    and read.sender == owner
                    and read.recipient == WETH
                    and read.data == query
                    and read.error is None
                    and read.output is not None
                    and len(read.output) == 32
                ):
                    reads.append(int.from_bytes(read.output))
        if len(reads) != 2:
            results.append(SelfCallCash(i, "missing_boundary_reads", gas))
            continue
        before, after = reads
        cash = logs_by_child.get(i, [])
        delta = sum(value for _, value in cash)
        if after - before != delta:
            raise ValueError("WETH balance change disagrees with committed self-call transfers")
        results.append(
            SelfCallCash(
                i, "reconciled", gas, before, after, delta, tuple(index for index, _ in cash)
            )
        )
    return tuple(results)
