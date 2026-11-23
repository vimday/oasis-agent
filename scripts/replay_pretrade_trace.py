"""Audit retained transaction-index simulations against a canonical receipt. Offline only."""

import argparse
import json
from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path

from eth_abi import decode, encode
from eth_abi.exceptions import DecodingError

from oasis_agent.arbitrage.cycle_evidence import header
from oasis_agent.arbitrage.cycle_plan import address, hash32
from oasis_agent.arbitrage.cycle_protocols import data_bytes, topic
from oasis_agent.evidence.public_receipts import (
    ObservedReceipt,
    decode_receipt,
    quantity,
    receipt_header,
    transaction_for_receipt,
)
from oasis_agent.evidence.rpc_validation import (
    strict_integer,
    strict_json_object,
    strict_list,
    strict_object,
    strict_string,
)
from oasis_agent.evidence.swap_flow import profile_swap_receipt
from oasis_agent.rpc.protocol import response_envelope

from .replay_swap_flow import read

MAX_TRACE_FRAMES = 16_384
MAX_TRACE_LOGS = 4096
TX_FIELDS = ("gas", "maxFeePerGas", "maxPriorityFeePerGas", "value")
OPTIONAL_TX_FIELDS = ("accessList", "authorizationList", "blobVersionedHashes", "maxFeePerBlobGas")
FAILED_OP = bytes.fromhex(topic("FailedOp(uint256,string)")[2:10])
ENTRYPOINT_V06 = "0x5ff137d4b0fdcd49dca30c7cf57e578a026d2789"
Log = tuple[str, tuple[str, ...], bytes]


@dataclass(frozen=True, slots=True)
class CallTrace:
    gas_used: int
    success: bool
    entrypoint_nonce_rejected: bool
    frames: int
    ordered_logs: tuple[Log, ...]


@dataclass(frozen=True, slots=True)
class PretradeContext:
    receipt: ObservedReceipt
    transaction: dict[str, object]
    parent_hash: str
    next_hash: str


def result(raw: bytes) -> dict[str, object]:
    response = strict_json_object(raw)
    response = response_envelope(raw, strict_integer(response.get("id"), "response ID"))
    return strict_object(response.get("result"), "RPC result")


def inclusion(
    chain_id: int, block_raw: bytes, parent_raw: bytes, next_raw: bytes, receipt_raw: bytes
) -> PretradeContext:
    if type(chain_id) is not int or not 0 < chain_id < 1 << 64:
        raise ValueError("invalid replay chain ID")
    block, observed = result(block_raw), result(receipt_raw)
    number = quantity(observed.get("blockNumber"), 64)
    tx_hash = hash32(observed.get("transactionHash"))
    transactions = [
        strict_object(value, "block transaction")
        for value in strict_list(block.get("transactions"), "block transactions")
    ]
    hashes = [hash32(tx.get("hash")) for tx in transactions]
    bound = receipt_header({**block, "transactions": hashes}, chain_id, number, (tx_hash,))
    index = bound.receipt_positions[0][1]
    receipt = decode_receipt(observed, bound.ref, tx_hash, index)
    tx = transactions[index]
    transaction_for_receipt(tx, receipt)
    if receipt.recipient is None:
        raise ValueError("call replay requires a transaction recipient")
    parent = header(result(parent_raw), chain_id, number - 1)
    child = header(result(next_raw), chain_id, number + 1)
    if parent.ref.block_hash != bound.parent_hash or child.parent_hash != bound.ref.block_hash:
        raise ValueError("nonconsecutive replay block hashes")
    return PretradeContext(receipt, tx, parent.ref.block_hash, child.ref.block_hash)


def _log(value: object) -> Log:
    row = strict_object(value, "trace log")
    return (
        address(row.get("address")),
        tuple(hash32(v) for v in strict_list(row.get("topics"), "log topics")),
        data_bytes(row.get("data")),
    )


def _entrypoint_nonce_error(root: dict[str, object]) -> bool:
    if (
        address(root.get("to")) != ENTRYPOINT_V06
        or root.get("error") is None
        or "output" not in root
    ):
        return False
    raw = data_bytes(root["output"])
    if raw[:4] != FAILED_OP:
        return False
    try:
        params = decode(["uint256", "string"], raw[4:])
    except DecodingError, UnicodeError:
        return False
    return encode(["uint256", "string"], params) == raw[4:] and params[1] == (
        "AA25 invalid account nonce"
    )


def call_trace(raw: object, context: PretradeContext) -> CallTrace:
    root = strict_object(raw, "call trace")
    receipt, tx = context.receipt, context.transaction
    if (
        root.get("type") != "CALL"
        or address(root.get("from")) != receipt.sender
        or address(root.get("to")) != receipt.recipient
        or data_bytes(root.get("input")) != data_bytes(tx.get("input"))
        or quantity(root.get("value")) != quantity(tx.get("value"))
    ):
        raise ValueError("trace root differs from the published call")
    ordered: list[Log] = []
    frames = 0

    def visit(frame: dict[str, object], depth: int, committed: bool) -> None:
        nonlocal frames
        frames += 1
        if frames > MAX_TRACE_FRAMES or depth > 64:
            raise ValueError("call trace exceeds frame budget")
        failed = frame.get("error") is not None
        if failed:
            strict_string(frame["error"], "frame error")
        committed = committed and not failed
        children = strict_list(frame.get("calls", []), "child calls")
        local_logs = strict_list(frame.get("logs", []), "trace logs")
        by_position: dict[int, list[Log]] = {}
        if committed:
            last = 0
            for value in local_logs:
                row = strict_object(value, "positioned trace log")
                position = quantity(row.get("position"), 32)
                if not last <= position <= len(children):
                    raise ValueError("trace log position outside frame")
                by_position.setdefault(position, []).append(_log(row))
                last = position
        for i, child in enumerate(children):
            ordered.extend(by_position.get(i, []))
            if len(ordered) > MAX_TRACE_LOGS:
                raise ValueError("call trace exceeds log budget")
            visit(strict_object(child, "child call"), depth + 1, committed)
        ordered.extend(by_position.get(len(children), []))
        if len(ordered) > MAX_TRACE_LOGS:
            raise ValueError("call trace exceeds log budget")

    visit(root, 0, True)
    return CallTrace(
        quantity(root.get("gasUsed"), 64),
        root.get("error") is None,
        _entrypoint_nonce_error(root),
        frames,
        tuple(ordered),
    )


def _check_call_args(value: object, context: PretradeContext) -> None:
    call = strict_object(value, "simulated call")
    tx = context.transaction
    if (
        set(call) - {"to", "from", "input", "data", "gasPrice", *TX_FIELDS, *OPTIONAL_TX_FIELDS}
        or ("input" in call) == ("data" in call)
        or address(call.get("from")) != context.receipt.sender
        or address(call.get("to")) != context.receipt.recipient
        or data_bytes(call.get("input", call.get("data"))) != data_bytes(tx.get("input"))
    ):
        raise ValueError("simulated call differs from published transaction")
    fee_fields = ("gas", "value", "gasPrice")
    if tx.get("maxFeePerGas") is not None:
        fee_fields = TX_FIELDS
        if "gasPrice" in call:
            raise ValueError("ambiguous simulated effective gas price")
    if any(quantity(call.get(k)) != quantity(tx[k]) for k in fee_fields):
        raise ValueError("simulated call gas or value differs")
    if any(call.get(k, []) != tx.get(k, []) for k in OPTIONAL_TX_FIELDS):
        raise ValueError("simulated call access or authorization fields differ")


def replay_stage(context: PretradeContext, request_raw: bytes, response_raw: bytes) -> dict:
    req = strict_json_object(request_raw)
    if req.get("jsonrpc") != "2.0":
        raise ValueError("invalid replay request version")
    reply = response_envelope(response_raw, strict_integer(req.get("id"), "request ID"))
    params = strict_list(req.get("params"), "request params")
    receipt = context.receipt
    block_hash = receipt.block.block_hash
    method = req.get("method")
    state_index = None
    if method == "debug_traceTransaction":
        tx_hash, config = params
        if hash32(tx_hash) != receipt.transaction_hash:
            raise ValueError("trace transaction differs from receipt")
        position = "canonical_transaction"
    elif method == "debug_traceCall":
        call, block_id, config = params
        _check_call_args(call, context)
        pin = strict_object(block_id, "trace call block pin")
        if set(pin) != {"blockHash", "requireCanonical"} or pin["requireCanonical"] is not True:
            raise ValueError("trace call needs a canonical block pin")
        target = hash32(pin["blockHash"])
        opts = strict_object(config, "trace config")
        if "txIndex" in opts:
            state_index = quantity(opts["txIndex"], 32)
        if target == block_hash:
            position = "inclusion_block_end"
            if state_index is not None:
                position = "inclusion_block_indexed"
        elif state_index is None and target in (context.parent_hash, context.next_hash):
            position = "parent_end" if target == context.parent_hash else "next_block_end"
        else:
            raise ValueError("trace call outside the bound replay blocks")
        block_hash = target
    else:
        raise ValueError("unsupported retained replay method")
    opts = strict_object(config, "trace config")
    if (
        set(opts) - {"tracer", "tracerConfig", "timeout", "txIndex"}
        or (method == "debug_traceTransaction" and "txIndex" in opts)
        or opts.get("tracer") != "callTracer"
        or opts.get("tracerConfig") != {"withLog": True}
    ):
        raise ValueError("replay requires full callTracer logs without overrides")
    out = {
        "position": position,
        "block_hash": block_hash,
        "index_parameter": state_index,
        "request_sha256": sha256(request_raw).hexdigest(),
        "response_sha256": sha256(response_raw).hexdigest(),
    }
    if "error" in reply:
        error = strict_object(reply["error"], "RPC error")
        return {**out, "rpc_error_code": error["code"], "matches_receipt": None}
    trace = call_trace(reply["result"], context)
    observed = tuple(
        (log.address, log.topics, data_bytes(log.data))
        for log in sorted(receipt.logs, key=lambda row: row.log_index)
    )
    logs_match = trace.ordered_logs == observed
    metrics = asdict(trace)
    del metrics["ordered_logs"]
    return {
        **out,
        **metrics,
        "logs": len(trace.ordered_logs),
        "ordered_logs_match_receipt": logs_match,
        "matches_receipt": (
            trace.success == bool(receipt.status)
            and trace.gas_used == receipt.gas_used
            and logs_match
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chain", type=int, required=True)
    parser.add_argument(
        "--headers", type=Path, required=True, help="Folder with block/parent/next.res"
    )
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument(
        "--trace", nargs=2, metavar=("REQUEST", "RESPONSE"), type=Path, action="append"
    )
    args = parser.parse_args()
    try:
        block, parent, next_block = (
            read(args.headers / f"{name}.res") for name in ("block", "parent", "next")
        )
        receipt = read(args.receipt)
        context = inclusion(args.chain, block, parent, next_block, receipt)
        profile = profile_swap_receipt(context.receipt)
        stages = [replay_stage(context, read(req), read(res)) for req, res in (args.trace or ())]
    except (ValueError, KeyError, TypeError, OSError) as exc:
        parser.error(str(exc))
    print(
        json.dumps(
            {
                "schema": "oasis-pretrade-trace-replay-v1",
                "chain_id": args.chain,
                "transaction_hash": context.receipt.transaction_hash,
                "transaction_index": context.receipt.transaction_index,
                "receipt_sha256": sha256(receipt).hexdigest(),
                "header_sha256": [sha256(raw).hexdigest() for raw in (block, parent, next_block)],
                "receipt_swap_events": len(profile.events),
                "receipt_distinct_pools": profile.distinct_pools,
                "stages": stages,
                "original_discovery_and_completion_clock": None,
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
