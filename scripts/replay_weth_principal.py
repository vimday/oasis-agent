"""Replay temporary WETH funding against retained Ethereum block, receipts and callTracer."""

import argparse
import json
from collections import Counter
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path

import rlp

from oasis_agent.arbitrage.cycle_plan import hash32
from oasis_agent.arbitrage.cycle_protocols import data_bytes
from oasis_agent.evidence.nitro_block import indexed_trie_root
from oasis_agent.evidence.public_receipts import decode_receipt, quantity
from oasis_agent.evidence.rpc_validation import (
    strict_integer,
    strict_json_object,
    strict_list,
    strict_object,
)
from oasis_agent.evidence.state_trie import prague_header
from oasis_agent.evidence.weth_principal import MANAGER, WETH, replay_weth_principal
from oasis_agent.rpc.protocol import response_envelope

from .replay_swap_flow import read


def _receipt_leaf(row: dict[str, object]) -> bytes:
    status, typ = quantity(row.get("status"), 8), quantity(row.get("type"), 8)
    bloom = data_bytes(row.get("logsBloom"))
    if status not in (0, 1) or typ not in (0, 1, 2, 3, 4) or len(bloom) != 256:
        raise ValueError("unsupported Ethereum receipt encoding")
    logs = []
    for raw in strict_list(row.get("logs"), "receipt logs"):
        log = strict_object(raw, "receipt log")
        emitter = data_bytes(log.get("address"))
        topics = [data_bytes(t) for t in strict_list(log.get("topics"), "receipt topics")]
        if len(emitter) != 20 or len(topics) > 4 or any(len(topic) != 32 for topic in topics):
            raise ValueError("unsupported Ethereum receipt log encoding")
        logs.append([emitter, topics, data_bytes(log.get("data"))])
    if not status and logs:
        raise ValueError("failed receipt has committed logs")
    leaf = rlp.encode([status, quantity(row.get("cumulativeGasUsed")), bloom, logs])
    return bytes([typ]) + leaf if typ else leaf


def analyze(
    *,
    block_raw: bytes,
    receipts_raw: bytes,
    call_trace_request: bytes,
    call_trace_raw: bytes,
) -> dict[str, object]:
    request = strict_json_object(call_trace_request)
    params = strict_list(request.get("params"), "callTracer request params")
    request_id = strict_integer(request.get("id"), "callTracer request id")
    if (
        request.get("jsonrpc") != "2.0"
        or request.get("method") != "debug_traceTransaction"
        or len(params) != 2
        or request_id < 1
    ):
        raise ValueError("invalid callTracer request")
    tx = hash32(params[0])
    config = strict_object(params[1], "callTracer configuration")
    if (
        config.get("tracer") != "callTracer"
        or strict_object(config.get("tracerConfig"), "callTracer options").get("withLog")
        is not True
    ):
        raise ValueError("callTracer request must include logs")
    trace = response_envelope(call_trace_raw, request_id).get("result")

    header = strict_object(strict_json_object(block_raw).get("result"), "Ethereum block")
    count = quantity(header.get("number"), 64)
    block, _ = prague_header(header, count, hash32(header.get("hash")))
    hashes = [
        hash32(item.get("hash") if isinstance(item, dict) else item)
        for item in strict_list(header.get("transactions"), "block transactions")
    ]
    rows = [
        strict_object(item, "Ethereum receipt")
        for item in strict_list(strict_json_object(receipts_raw).get("result"), "block receipts")
    ]
    if (
        len(rows) != len(hashes)
        or len(hashes) != len(set(hashes))
        or not 1 <= len(rows) <= 16_384
        or tx not in hashes
    ):
        raise ValueError("incomplete block receipts or target transaction missing")
    block_hash, cumulative, log_index = block.ref.block_hash, 0, 0
    for index, (row, tx_hash) in enumerate(zip(rows, hashes, strict=True)):
        if (
            hash32(row.get("transactionHash")) != tx_hash
            or hash32(row.get("blockHash")) != block_hash
            or quantity(row.get("blockNumber"), 64) != count
            or quantity(row.get("transactionIndex"), 32) != index
        ):
            raise ValueError("receipt position or block mismatch")
        cumulative += quantity(row.get("gasUsed"), 64)
        if cumulative != quantity(row.get("cumulativeGasUsed"), 64):
            raise ValueError("receipt gas differs from cumulative commitment")
        for item in strict_list(row.get("logs"), "receipt logs"):
            log = strict_object(item, "receipt log")
            if (
                quantity(log.get("logIndex"), 32) != log_index
                or hash32(log.get("transactionHash")) != tx_hash
                or hash32(log.get("blockHash")) != block_hash
            ):
                raise ValueError("receipt log order or inclusion mismatch")
            log_index += 1
    computed_root = "0x" + indexed_trie_root([_receipt_leaf(row) for row in rows]).hex()
    if computed_root != hash32(header.get("receiptsRoot")) or cumulative != quantity(
        header.get("gasUsed"), 64
    ):
        raise ValueError("Ethereum receipts do not match block commitment")

    index = hashes.index(tx)
    receipt = decode_receipt(rows[index], block.ref, tx, index)
    report = replay_weth_principal(receipt, trace)
    reasons = Counter(u.reason for u in report.unlocks if u.reason is not None)
    return {
        "schema": "oasis-ethereum-weth-principal-v1",
        "chain_id": 1,
        "block": count,
        "block_hash": block_hash,
        "transaction_hash": tx,
        "transaction_index": index,
        "receipts_root": computed_root,
        "receipts_verified": len(rows),
        "matched_call_logs": len(receipt.logs),
        "pool_manager": MANAGER,
        "currency": WETH,
        "observed_unlocks": len(report.unlocks),
        "verified_principal_groups": sum(u.proof is not None for u in report.unlocks),
        "unproved_unlocks_by_reason": dict(reasons),
        "unlocks": [asdict(u) for u in report.unlocks],
        "input_sha256": {
            name: sha256(raw).hexdigest()
            for name, raw in (
                ("header", block_raw),
                ("receipts", receipts_raw),
                ("trace_request", call_trace_request),
                ("trace_response", call_trace_raw),
            )
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--block", type=Path, required=True)
    parser.add_argument("--receipts", type=Path, required=True)
    parser.add_argument("--call-trace-request", type=Path, required=True)
    parser.add_argument("--call-trace", type=Path, required=True)
    args = parser.parse_args()
    result = analyze(
        block_raw=read(args.block),
        receipts_raw=read(args.receipts),
        call_trace_request=read(args.call_trace_request),
        call_trace_raw=read(args.call_trace),
    )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
