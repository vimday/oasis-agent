"""Reconcile an Ethereum history candidate and replay a registered serial witness."""

import argparse
import json
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from itertools import pairwise
from pathlib import Path

from eth_hash.auto import keccak

from oasis_agent.arbitrage.cycle_plan import address, hash32
from oasis_agent.arbitrage.cycle_protocols import abi, data_bytes
from oasis_agent.evidence.public_receipts import ObservedReceipt, decode_receipt
from oasis_agent.evidence.research_traces import first_failure, nested_calls
from oasis_agent.evidence.rpc_validation import (
    strict_integer,
    strict_json_object,
    strict_list,
    strict_object,
)
from oasis_agent.evidence.state_trie import prague_header
from oasis_agent.evidence.swap_flow import profile_swap_receipt
from oasis_agent.evidence.swap_flow_trace import (
    _receipt_positions,
    _Transfer,
    _transfers,
    replay_trace_funding,
)
from oasis_agent.evidence.weth_principal import WETH
from oasis_agent.models.chain import LogRecord
from oasis_agent.models.common import ZERO_ADDRESS
from oasis_agent.rpc.protocol import response_envelope

from .replay_swap_flow import read
from .replay_weth_principal import analyze as analyze_principal


def topic(signature: str) -> str:
    return "0x" + keccak(signature.encode()).hex()


_CURVE = topic("TokenExchange(address,uint256,uint256,uint256,uint256,uint256,uint256)")
_WRAP = "0xeb5580a0908e96b78bdcb1a3c5638793b491a6073c3ff56061a069cb20581773"
_UNWRAP = "0x12d6424519838e57637c6db9df31af32d7926ff0a53dd37007c191d0fe302818"
_DEPOSIT = topic("Deposit(address,uint256)")
_WITHDRAWAL = topic("Withdrawal(address,uint256)")


@dataclass(frozen=True)
class WitnessLeg:
    event: int
    input_log: int
    output_log: int


# The custom ABIs and path selection below describe this retained observation.
# Replay of any other transaction or call tree requires independent asset and code evidence.
_REGISTERED_BLOCK = "0xad23bf0cc2cb32d029eba806ab6b3fe3c802aacf755179e1735d512cb6ada9ce"
_REGISTERED_TX = "0x17886d908ef8eb9f864cd6776991952c15ac79a5a364037318d0ef53efe19c89"
_REGISTERED_TRACE = "13681fb2d862b1bb1b6d73b13856732f81e38c72380a7af8f1113576e5fcc9b9"
_REGISTERED_PATH = (
    WitnessLeg(839, 838, 837),
    WitnessLeg(842, 840, 841),
    WitnessLeg(845, 843, 844),
    WitnessLeg(848, 846, 847),
    WitnessLeg(851, 850, 849),
    WitnessLeg(854, 853, 852),
    WitnessLeg(857, 856, 855),
    WitnessLeg(860, 859, 858),
    WitnessLeg(863, 861, 862),
    WitnessLeg(867, 864, 865),
    WitnessLeg(870, 868, 869),
)


def witness_leg(value: str) -> WitnessLeg:
    try:
        event, input_log, output_log = map(int, value.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected event,input_log,output_log") from exc
    if min(event, input_log, output_log) < 0:
        raise argparse.ArgumentTypeError("log indices must be nonnegative")
    return WitnessLeg(event, input_log, output_log)


def _archive_match(raw: bytes, receipt: ObservedReceipt) -> None:
    rows = []
    stamped = datetime.fromtimestamp(receipt.block.timestamp or 0, UTC).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    for item in strict_list(json.loads(raw), "archive log rows"):
        row = strict_object(item, "archive log")
        if row.get("transaction_hash") != receipt.transaction_hash:
            continue
        if (
            row.get("block_hash"),
            row.get("block_number"),
            row.get("transaction_index"),
            row.get("block_timestamp"),
        ) != (
            receipt.block.block_hash,
            receipt.block.number,
            receipt.transaction_index,
            stamped,
        ):
            raise ValueError("archive log inclusion or timestamp differs")
        rows.append((row.get("log_index"), row.get("address"), row.get("topics"), row.get("data")))
    if rows != [(log.log_index, log.address, list(log.topics), log.data) for log in receipt.logs]:
        raise ValueError("archive logs differ from rooted receipts")


def _serial(
    receipt: ObservedReceipt,
    trace_raw: bytes,
    trace: object,
    legs: tuple[WitnessLeg, ...],
) -> dict[str, object]:
    if (
        receipt.block.chain_id != 1
        or receipt.block.number != 26_478_290
        or receipt.block.block_hash != _REGISTERED_BLOCK
        or receipt.transaction_hash != _REGISTERED_TX
        or receipt.transaction_index != 5
        or legs != _REGISTERED_PATH
        or sha256(trace_raw).hexdigest() != _REGISTERED_TRACE
    ):
        return {
            "status": "unresolved",
            "reason": "serial witness requires the registered block, transaction, trace and path",
        }
    executor = receipt.recipient
    if executor is None:
        raise ValueError("registered witness has no executor")
    frames = nested_calls(trace)
    positions = _receipt_positions(trace, frames, receipt)
    transfers = {t.index: t for t in _transfers(receipt, positions)}
    proofs = {p.leg.event.log_index: p for p in replay_trace_funding(receipt, trace).resolved_swaps}
    events = {log.log_index: log for log in receipt.logs}
    used: set[int] = set()
    pools: set[tuple[str, str | None]] = set()
    route = []
    for leg in legs:
        indices = {leg.event, leg.input_log, leg.output_log}
        if len(indices) != 3 or used & indices:
            raise ValueError("reused path evidence")
        used.update(indices)
        event = events[leg.event]
        source, target = transfers[leg.input_log], transfers[leg.output_log]
        proof = proofs.get(leg.event)
        path = proof.call_path if proof is not None else positions[leg.event]
        frame = frames[path]
        if (
            not path
            or target.path[: len(path)] != path
            or frame.kind != "CALL"
            or first_failure(frames, path) is not None
            or source.sender != executor
            or target.recipient != executor
            or source.token == target.token
            or source.amount <= 0
            or target.amount <= 0
        ):
            raise ValueError("path leg lacks one committed executor transfer in each direction")
        if proof is not None:
            if (proof.input_transfer_log, proof.output_transfer_log) != (
                source.index,
                target.index,
            ):
                raise ValueError("generic decoder disagrees with assigned transfer logs")
            pool = proof.leg.event.pool
            kind = proof.leg.event.shape
        else:
            if source.path[: len(path)] != path or frame.recipient != event.address:
                raise ValueError("custom conversion transfers lack a common execution frame")
            pool = (event.address, None)
            kind = _conversion(frame.data, frame.output, executor, event, source, target)
        if kind not in {"wrap", "unwrap"}:
            if pool in pools:
                raise ValueError("serial witness reuses a pool")
            pools.add(pool)
        route.append(
            {
                "type": kind,
                "contract": event.address,
                "pool_id": pool[1],
                "call_path": path,
                "event": leg.event,
                "token_in": source.token,
                "token_out": target.token,
                "amount_in": source.amount,
                "amount_out": target.amount,
                "input_log": source.index,
                "output_log": target.index,
            }
        )
    for before, after in pairwise(legs):
        output, spend = transfers[before.output_log], transfers[after.input_log]
        if (
            max(before.input_log, output.index, before.event)
            >= min(spend.index, after.output_log, after.event)
            or (output.token, output.amount) != (spend.token, spend.amount)
            or any(
                output.index < t.index < spend.index
                and t.token == output.token
                and executor in (t.sender, t.recipient)
                for t in transfers.values()
            )
        ):
            raise ValueError("leg output does not form an uninterrupted exact funding link")
    start, end = transfers[legs[0].input_log], transfers[legs[-1].output_log]
    return {
        "distinct_serial_pools": len(pools),
        "serial_actions": len(legs),
        "closed_currency": start.token if start.token == end.token else None,
        "gross_raw": end.amount - start.amount if start.token == end.token else None,
        "legs": route,
    }


def _conversion(
    call: bytes,
    output: bytes | None,
    executor: str,
    event: LogRecord,
    source: _Transfer,
    target: _Transfer,
) -> str:
    kind = event.topics[0]
    if kind == _CURVE:
        types = ("uint256", "uint256", "uint256", "uint256", "address")
        if call[:4] != keccak(b"exchange(uint256,uint256,uint256,uint256,address)")[:4]:
            raise ValueError("unsupported Curve exchange call")
        sold, bought, amount, minimum, receiver = abi(types, call[4:])
        (amount_out,) = abi(("uint256",), output or b"")
        event_values = abi(("uint256",) * 6, data_bytes(event.data))
        if (
            len(event.topics) == 2
            and abi(("address",), data_bytes(event.topics[1]))[0] == executor
            and receiver == executor
            and source.recipient == target.sender == event.address
            and (sold, amount, bought, amount_out) == event_values[:4]
            and sold != bought
            and source.amount == amount
            and target.amount == amount_out >= minimum
        ):
            return "curve_crypto_exchange"
    if kind in {_WRAP, _UNWRAP}:
        method = "wrap" if kind == _WRAP else "unwrap"
        transfers_match = (
            (source.recipient, target.token, target.sender)
            if method == "wrap"
            else (target.sender, source.token, source.recipient)
        ) == (event.address, event.address, ZERO_ADDRESS)
        if (
            call[:4] == keccak(f"{method}(uint256)".encode())[:4]
            and transfers_match
            and len(event.topics) == 3
            and all(abi(("address",), data_bytes(item))[0] == executor for item in event.topics[1:])
            and abi(("uint256",), call[4:])
            == abi(("uint256",), output or b"")
            == abi(("uint256",), data_bytes(event.data))
            == (source.amount,)
            == (target.amount,)
        ):
            return method
    raise ValueError("unresolved witness conversion")


def _settlement(receipt: ObservedReceipt, trace: object, fee_receiver: str) -> dict[str, object]:
    execution = receipt.recipient
    actors = {receipt.sender, execution} if execution is not None else {receipt.sender}
    frames = nested_calls(trace)
    positions = _receipt_positions(trace, frames, receipt)
    balances: Counter[tuple[str, str]] = Counter()
    direct_fee = 0
    native = []
    for path, call in sorted(frames.items()):
        if call.kind != "CALL" or not call.value or first_failure(frames, path) is not None:
            continue
        if not actors.intersection((call.sender, call.recipient)):
            continue
        balances[call.sender, "ETH"] -= call.value
        balances[call.recipient, "ETH"] += call.value
        if call.recipient == fee_receiver and call.sender in actors:
            direct_fee += call.value
        native.append(
            {"path": path, "from": call.sender, "to": call.recipient, "amount": call.value}
        )
    for transfer in _transfers(receipt, positions):
        balances[transfer.sender, transfer.token] -= transfer.amount
        balances[transfer.recipient, transfer.token] += transfer.amount
    for log in receipt.logs:
        if log.address != WETH or len(log.topics) != 2:
            continue
        if log.topics[0] in {_DEPOSIT, _WITHDRAWAL}:
            account = abi(("address",), data_bytes(log.topics[1]))[0]
            (amount,) = abi(("uint256",), data_bytes(log.data))
            balances[account, WETH] += amount if log.topics[0] == _DEPOSIT else -amount
    gas = receipt.gas_used * receipt.effective_gas_price
    balances[receipt.sender, "ETH"] -= gas
    role_deltas = [
        (account, asset, delta)
        for (account, asset), delta in sorted(balances.items())
        if account in actors and delta
    ]
    observed = [
        {"account": account, "asset": asset, "delta_after_receipt_gas": delta}
        for account, asset, delta in role_deltas
    ]
    return {
        "fee_payer": receipt.sender,
        "transaction_recipient": execution,
        "fee_receiver": fee_receiver,
        "gas_used": receipt.gas_used,
        "effective_gas_price": receipt.effective_gas_price,
        "paid_gas_wei": gas,
        "direct_eth_to_header_fee_receiver_wei": direct_fee,
        "native_calls_touching_roles": native,
        "visible_role_deltas": observed,
        "conditional_common_role_eth_weth_delta_wei": (
            sum(delta for _, _, delta in role_deltas)
            if all(asset in {"ETH", WETH} for _, asset, _ in role_deltas)
            else None
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("block", "receipts", "call-trace-request", "call-trace", "archive-logs"):
        parser.add_argument(f"--{name}", required=True, type=Path)
    parser.add_argument(
        "--leg",
        action="append",
        type=witness_leg,
        default=[],
        help="registered path event,input_log,output_log; repeat for each leg",
    )
    args = parser.parse_args()
    block_raw = read(args.block)
    receipts_raw = read(args.receipts)
    trace_req = read(args.call_trace_request)
    trace_raw = read(args.call_trace)
    archive = read(args.archive_logs)
    result = analyze_principal(
        block_raw=block_raw,
        receipts_raw=receipts_raw,
        call_trace_request=trace_req,
        call_trace_raw=trace_raw,
    )
    tx = hash32(result["transaction_hash"])
    index = strict_integer(result["transaction_index"], "transaction index")
    height = strict_integer(result["block"], "block height")
    header = strict_object(strict_json_object(block_raw).get("result"), "Ethereum header")
    block, _ = prague_header(header, height, hash32(result["block_hash"]))
    rows = strict_list(strict_json_object(receipts_raw).get("result"), "Ethereum receipts")
    receipt = decode_receipt(rows[index], block.ref, tx, index)
    _archive_match(archive, receipt)
    request_id = strict_integer(strict_json_object(trace_req)["id"], "callTracer request ID")
    trace = response_envelope(trace_raw, request_id).get("result")
    funding = replay_trace_funding(receipt, trace)
    profile = profile_swap_receipt(receipt)
    report = {
        "schema": "oasis-ethereum-tail-case-v1",
        "archive_rows_matched": len(receipt.logs),
        "archive_sha256": sha256(archive).hexdigest(),
        "principal": result,
        "generic_funding": {
            "recognized_receipt_swaps": len(profile.events),
            "distinct_pools": profile.distinct_pools,
            "resolved_swaps": len(funding.resolved_swaps),
            "exact_links": len(funding.links),
            "unresolved": funding.unresolved_events,
            "depth": funding.profile.witnessed_depth,
            "path": funding.profile.longest_witnessed_path,
        },
        "settlement": _settlement(receipt, trace, address(header.get("miner"))),
        "serial_witness": _serial(receipt, trace_raw, trace, tuple(args.leg)) if args.leg else None,
    }
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
