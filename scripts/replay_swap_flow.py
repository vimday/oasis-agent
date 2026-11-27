"""Profile one retained block-bound receipt and optional resolved funding allocations."""

import argparse
import json
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path

from oasis_agent.arbitrage.cycle_plan import address, hash32
from oasis_agent.arbitrage.cycle_protocols import data_bytes
from oasis_agent.evidence.public_receipts import decode_receipt, quantity, receipt_header
from oasis_agent.evidence.rpc_validation import (
    strict_integer,
    strict_json_object,
    strict_list,
    strict_object,
    strict_string,
)
from oasis_agent.evidence.swap_flow import (
    FundingLink,
    SwapLeg,
    analyze_swap_funding,
    direct_pool_funding,
    profile_swap_call_bundles,
    profile_swap_receipt,
)
from oasis_agent.evidence.swap_flow_trace import replay_trace_funding
from oasis_agent.evidence.swap_transfer_splits import replay_with_transfer_splits
from oasis_agent.rpc.protocol import response_envelope

MAX_BYTES = 16_000_000


def read(path: Path) -> bytes:
    with path.open("rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("retained payload too large")
    return raw


def analyze(
    *,
    chain_id: int,
    block_raw: bytes,
    receipt_raw: bytes,
    funding_raw: bytes | None,
    trace_raw: bytes | None = None,
    call_trace_request: bytes | None = None,
    call_trace_raw: bytes | None = None,
    transfer_splits: bool = False,
) -> dict[str, object]:
    if transfer_splits and call_trace_raw is None:
        raise ValueError("split transfers require a retained callTracer request and response")
    if chain_id <= 0:
        raise ValueError("invalid chain ID")
    bh = strict_object(strict_json_object(block_raw).get("result"), "header")
    rc = strict_object(strict_json_object(receipt_raw).get("result"), "receipt")
    tx_hash = hash32(rc.get("transactionHash"))
    block = receipt_header(bh, chain_id, quantity(rc.get("blockNumber"), 64), (tx_hash,))
    tx_index = block.receipt_positions[0][1]
    receipt = decode_receipt(rc, block.ref, tx_hash, tx_index)
    profile = profile_swap_receipt(receipt)
    digest = sha256(receipt_raw).hexdigest()
    result: dict[str, object] = {
        "schema": "oasis-swap-funding-replay-v1",
        "chain_id": chain_id,
        "block": block.ref.number,
        "block_hash": block.ref.block_hash,
        "transaction_hash": tx_hash,
        "transaction_index": tx_index,
        "block_sha256": sha256(block_raw).hexdigest(),
        "receipt_sha256": digest,
        "gas_used": receipt.gas_used,
        "gas_paid_native": receipt.gas_used * receipt.effective_gas_price,
        "l1_fee_native": quantity(rc["l1Fee"]) if "l1Fee" in rc else None,
        "receiver": receipt.recipient,
        "fee_payer": receipt.sender,
        "receipt_swap_events": len(profile.events),
        "receipt_profile": asdict(profile),
        "direct_funding": None,
        "provided_funding": None,
    }
    if trace_raw is not None:
        observed = strict_json_object(trace_raw)
        h = strict_object(observed.get("header"), "trace header")
        t = strict_object(observed.get("transaction"), "trace transaction")
        if (
            hash32(h.get("hash")) != block.ref.block_hash
            or strict_integer(h.get("number"), "trace block number") != block.ref.number
            or hash32(t.get("hash")) != tx_hash
            or strict_integer(t.get("transactionIndex"), "trace transaction index") != tx_index
            or address(t.get("from")) != receipt.sender
            or receipt.recipient is None
            or address(t.get("to")) != receipt.recipient
        ):
            raise ValueError("trace and receipt inclusion differ")
        frames = []
        for raw in strict_list(observed.get("traces"), "trace frames"):
            row = strict_object(raw, "trace frame")
            if (
                strict_integer(row.get("transactionIndex"), "frame transaction index") != tx_index
                or row.get("type") != "call"
            ):
                raise ValueError("unsupported or unrelated trace frame")
            action = strict_object(row.get("action"), "trace action")
            seq = tuple(
                strict_integer(i, "trace index")
                for i in strict_list(row.get("traceAddress"), "trace address")
            )
            selector = action.get("sighash") if action.get("type") == "call" else None
            if not seq and (
                address(action.get("from")) != receipt.sender
                or address(action.get("to")) != receipt.recipient
            ):
                raise ValueError("trace root and receipt roles differ")
            if selector is not None and "input" in action:
                supplied = data_bytes(action["input"])
                expected = "0x" + supplied[:4].hex()
                if selector != expected:
                    raise ValueError("trace selector and calldata differ")
            frames.append(
                (
                    seq,
                    strict_string(action.get("to"), "trace recipient"),
                    strict_string(selector, "selector") if selector is not None else "",
                    row.get("error") is not None,
                )
            )
        result["call_bundles"] = asdict(profile_swap_call_bundles(receipt, frames))
        result["trace_sha256"] = sha256(trace_raw).hexdigest()
    if (call_trace_request is None) != (call_trace_raw is None):
        raise ValueError("callTracer replay needs both request and response")
    if call_trace_request is not None and call_trace_raw is not None:
        request = strict_json_object(call_trace_request)
        params = strict_list(request.get("params"), "callTracer request params")
        request_id = strict_integer(request.get("id"), "callTracer request ID")
        if (
            request.get("jsonrpc") != "2.0"
            or request.get("method") != "debug_traceTransaction"
            or request_id < 1
            or len(params) != 2
            or hash32(params[0]) != tx_hash
        ):
            raise ValueError("callTracer request does not identify the retained receipt")
        config = strict_object(params[1], "callTracer request config")
        if (
            config.get("tracer") != "callTracer"
            or strict_object(config.get("tracerConfig"), "callTracer tracer config").get("withLog")
            is not True
        ):
            raise ValueError("callTracer request must include logs")
        envelope = response_envelope(call_trace_raw, request_id)
        if "result" not in envelope:
            raise ValueError("callTracer request failed")
        if transfer_splits:
            funding, splits = replay_with_transfer_splits(receipt, envelope["result"])
            result["token_retained_funding"] = asdict(splits)
        else:
            funding = replay_trace_funding(receipt, envelope["result"])
        result["trace_funding"] = asdict(funding)
        result["call_trace_request_sha256"] = sha256(call_trace_request).hexdigest()
        result["call_trace_response_sha256"] = sha256(call_trace_raw).hexdigest()
    if funding_raw is None:
        return result
    resolved = strict_json_object(funding_raw)
    if resolved.get("receipt_sha256") != digest:
        raise ValueError("funding allocation file does not match original receipt")
    events = {row.log_index: row for row in profile.events}
    legs = []
    for raw in strict_list(resolved.get("legs"), "resolved swap legs"):
        row = strict_object(raw, "resolved swap leg")
        event_index = strict_integer(row.get("log_index"), "swap log index")
        legs.append(
            SwapLeg(
                events[event_index],
                strict_string(row.get("token_in"), "swap input token"),
                strict_string(row.get("token_out"), "swap output token"),
                quantity(row.get("amount_in")),
                quantity(row.get("amount_out")),
            )
        )
    links = []
    for raw in strict_list(resolved.get("funding", []), "funding allocations"):
        row = strict_object(raw, "funding allocation")
        links.append(
            FundingLink(
                strict_integer(row.get("source"), "source swap log index"),
                strict_integer(row.get("target"), "target swap log index"),
                strict_string(row.get("token"), "funding asset"),
                quantity(row.get("amount")),
                strict_string(row.get("evidence_id"), "external evidence reference"),
            )
        )
    direct = direct_pool_funding(receipt, legs)
    result["direct_funding"] = asdict(analyze_swap_funding(legs, direct))
    if links:
        result["provided_funding"] = asdict(analyze_swap_funding(legs, links))
        result["provided_funding_sha256"] = sha256(funding_raw).hexdigest()
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chain", type=int, required=True)
    parser.add_argument("--block", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--funding", type=Path)
    parser.add_argument("--trace", type=Path)
    parser.add_argument("--call-trace-request", type=Path)
    parser.add_argument("--call-trace", type=Path, help="retained callTracer JSON-RPC response")
    parser.add_argument(
        "--transfer-splits",
        action="store_true",
        help="Verify balance-backed transfers split between a pool and the token contract",
    )
    args = parser.parse_args()
    try:
        result = analyze(
            chain_id=args.chain,
            transfer_splits=args.transfer_splits,
            block_raw=read(args.block),
            receipt_raw=read(args.receipt),
            funding_raw=read(args.funding) if args.funding is not None else None,
            trace_raw=read(args.trace) if args.trace is not None else None,
            call_trace_request=read(args.call_trace_request) if args.call_trace_request else None,
            call_trace_raw=read(args.call_trace) if args.call_trace else None,
        )
    except (ValueError, KeyError, OSError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
