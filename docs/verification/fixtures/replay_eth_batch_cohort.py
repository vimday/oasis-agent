"""Replay the frozen Ethereum financing cohort using only local CSV and RPC responses."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import UTC, datetime
from hashlib import sha256
from itertools import pairwise
from pathlib import Path
from typing import Any

import rlp
from eth_hash.auto import keccak

from oasis_agent.arbitrage.cycle_protocols import abi, data_bytes, topic
from oasis_agent.evidence.nitro_block import indexed_trie_root
from oasis_agent.evidence.public_receipts import decode_receipt, receipt_header
from oasis_agent.evidence.research_traces import first_failure, nested_calls
from oasis_agent.evidence.state_trie import prague_header
from oasis_agent.evidence.swap_flow import analyze_swap_funding
from oasis_agent.evidence.swap_flow_trace import _receipt_positions, replay_trace_funding

type Row = dict[str, Any]
EXECUTOR = "0x96cb0b6dd66cbd3201681b9f9588b7107084f9ea"
BENEFICIARY = "0x610aa9ce9da1360852c3419f93a5f7d424379aa0"
V4 = "0x000000000004444c5dc75cb358380d2e3de08a90"
WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
FINGERPRINT = "0x1a52ee375bcdbe79ec67824cbb2a4d2cea35fd095fe5a93c79626fe02f5874f8"
TRANSFER = topic("Transfer(address,address,uint256)")
SWAP_TOPICS = frozenset(
    topic(signature)
    for signature in (
        "TokenExchange(address,uint256,uint256,uint256,uint256,uint256,uint256)",
        "Swap(address,address,int256,int256,uint160,uint128,int24,uint128,uint128)",
        "Swap(bytes32,address,address,uint256,uint256)",
        "Conversion(address,address,address,uint256,uint256,int256)",
        "Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)",
        "TokenExchange(address,int128,uint256,int128,uint256)",
        "Swap(address,address,int256,int256,uint160,uint128,int24)",
        "Swap(address,uint256,uint256,uint256,uint256,address)",
    )
)
SOURCES = {
    "header": "https://ethereum.publicnode.com",
    "receipts": "https://eth.nodely.dev",
    "trace": "https://eth.drpc.org",
}


def _read_csv(path: Path) -> list[Row]:
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def _quantity(row: Row, key: str) -> int:
    return int(row[key], 16 if str(row[key]).startswith("0x") else 10)


def _bool(row: Row, key: str) -> bool:
    if row[key] not in {"true", "false"}:
        raise ValueError(f"invalid boolean in {key}")
    return row[key] == "true"


def _financing(logs: list[Row], executor: str = EXECUTOR) -> dict[str, Any]:
    event_indices = [_quantity(event, "logIndex") for event in logs]
    if any(a + 1 != b for a, b in pairwise(event_indices)):
        raise ValueError("non-contiguous transaction log positions")
    movements = []
    shape_hits = 0
    erc20_count = 0
    for row in logs:
        topics = row["topics"]
        shape_hits += bool(topics and topics[0] in SWAP_TOPICS)
        if len(topics) != 3 or topics[0] != TRANSFER or len(row["data"]) != 66:
            continue
        erc20_count += 1
        if row["address"] != WETH:
            continue
        sender, recipient = (abi(("address",), data_bytes(value))[0] for value in topics[1:])
        amount = int(row["data"], 16)
        if amount and executor in (sender, recipient):
            movements.append((_quantity(row, "logIndex"), sender, recipient, amount))
    starts = [move for move in movements if move[1:3] == (V4, executor)]
    groups = []
    ambiguous = []
    for idx, (start, _, _, principal) in enumerate(starts):
        end = starts[idx + 1][0] if idx + 1 < len(starts) else event_indices[-1] + 1
        repayments = [
            (pos, amount)
            for pos, src, dst, amount in movements
            if start < pos < end and (src, dst) == (executor, V4)
        ]
        matching = [pos for pos, amount in repayments if amount == principal]
        if len(matching) != 1:
            ambiguous.append({"start_log": start, "matching_returns": matching})
            continue
        repayment = matching[0]
        payouts = [
            (pos, amount)
            for pos, src, dst, amount in movements
            if repayment < pos < end and (src, dst) == (executor, BENEFICIARY)
        ]
        groups.append(
            {
                "take_log": start,
                "repaid_log": repayment,
                "principal_weth_wei": str(principal),
                "payout_logs": [pos for pos, _ in payouts],
                "payout_weth_wei": str(sum(amount for _, amount in payouts)),
                "other_weth_repayments": len(repayments) - 1,
            }
        )
    return {
        "shape_events": shape_hits,
        "erc20_transfer_events": erc20_count,
        "log_count": len(logs),
        "manager_to_executor_weth_receipts": len(starts),
        "paired_groups": groups,
        "ambiguous_groups": ambiguous,
        "paired_group_count": len(groups),
        "paid_group_count": sum(bool(group["payout_logs"]) for group in groups),
        "beneficiary_weth_wei": str(sum(int(group["payout_weth_wei"]) for group in groups)),
    }


def _rank_key(row: Row) -> tuple[int, int, int, int, int, int, str]:
    evidence = row["funding"]
    return (
        -evidence["paired_group_count"],
        -evidence["paid_group_count"],
        -row["shapes"],
        -int(evidence["beneficiary_weth_wei"]),
        row["block_number"],
        row["transaction_index"],
        row["transaction_hash"],
    )


def scan(directory: Path) -> Row:
    archive = {}
    for row in _read_csv(directory / "executor-logs.csv"):
        archive.setdefault((row["day"], row["transaction_hash"]), []).append(
            {
                "logIndex": row["log_index"],
                "address": row["address"],
                "data": row["data"],
                "topics": json.loads(row["topics"]),
            }
        )
    rows = []
    for parsed in _read_csv(directory / "rankable.csv"):
        row = {
            key: int(value)
            if key in {"block_number", "transaction_index", "logs", "transfers", "shapes", "bin"}
            else value
            for key, value in parsed.items()
        }
        row["observed_executor"] = _bool(row, "observed_executor")
        if row["observed_executor"]:
            row["funding"] = _financing(archive.pop((row["day"], row["transaction_hash"])))
            assert (row["shapes"], row["transfers"], row["logs"]) == (
                row["funding"]["shape_events"],
                row["funding"]["erc20_transfer_events"],
                row["funding"]["log_count"],
            )
        rows.append(row)
    if archive:
        raise ValueError("executor archive rows missing from the ranking input")
    executor_rows = [row for row in rows if row["observed_executor"]]
    by_stratum = []
    for day in sorted({row["day"] for row in rows}):
        in_day = [row for row in executor_rows if row["day"] == day]
        for bin_value in (0, 1, 20, 50, 100, 200):
            items = [row for row in in_day if row["bin"] == bin_value]
            controls = [
                row
                for row in rows
                if row["day"] == day and row["bin"] == bin_value and not row["observed_executor"]
            ]
            by_stratum.append(
                {
                    "day": day,
                    "bin": bin_value,
                    "executor_count": len(items),
                    "paired_transactions": sum(
                        row["funding"]["paired_group_count"] > 0 for row in items
                    ),
                    "recurring_transactions": sum(
                        row["funding"]["paired_group_count"] > 1 for row in items
                    ),
                    "best_executor": min(items, key=_rank_key, default=None),
                    "non_executor_comparator": None if items else next(iter(controls), None),
                }
            )
    return {
        "coverage": _read_csv(directory / "coverage.csv"),
        "thresholds": _read_csv(directory / "thresholds.csv"),
        "rows": by_stratum,
        "executor_transactions": len(executor_rows),
        "executor_transactions_by_day": dict(Counter(row["day"] for row in executor_rows)),
        "exact_target": next(
            (row for row in executor_rows if row["transaction_hash"] == FINGERPRINT), None
        ),
        "recurring_samples": sorted(
            [row for row in executor_rows if row["funding"]["paired_group_count"] > 1],
            key=_rank_key,
        )[:16],
    }


def _get(directory: Path, stem: str, lane: str, method: str, params: list[Any]) -> Any:
    path = directory / f"{stem}-{lane}"
    meta = json.loads(path.with_suffix(".meta.json").read_bytes())
    request = path.with_suffix(".request.json").read_bytes()
    response = path.with_suffix(".response.json").read_bytes()
    if (
        meta["source"] != SOURCES[lane]
        or meta["failure"] is not None
        or meta["http"] != "200"
        or meta["request_sha256"] != sha256(request).hexdigest()
        or meta["response_sha256"] != sha256(response).hexdigest()
    ):
        raise ValueError(f"retained source identity or raw bytes disagree for {lane}")
    if json.loads(request) != {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}:
        raise ValueError(f"retained {lane} request differs from frozen selection")
    envelope = json.loads(response)
    if envelope.get("id") != 1 or "error" in envelope or envelope.get("result") is None:
        raise ValueError(f"invalid successful envelope for {lane}")
    return envelope["result"]


def _receipt_leaf(receipt: Row) -> bytes:
    content = rlp.encode(
        [
            int(receipt["status"], 16),
            int(receipt["cumulativeGasUsed"], 16),
            data_bytes(receipt["logsBloom"]),
            [
                [
                    data_bytes(event["address"]),
                    [data_bytes(topic_value) for topic_value in event["topics"]],
                    data_bytes(event["data"]),
                ]
                for event in receipt["logs"]
            ],
        ]
    )
    tx_type = int(receipt["type"], 16)
    return bytes([tx_type]) + content if tx_type else content


def _selector(name: str) -> bytes:
    return keccak(name.encode())[:4]


def _within(path: tuple[int, ...], parent: tuple[int, ...]) -> bool:
    return path[: len(parent)] == parent


def rooted_sample(directory: Path, row: Row) -> Row:
    tx, height, block_hash, index = (
        row["transaction_hash"],
        row["block_number"],
        row["block_hash"],
        row["transaction_index"],
    )
    stem = tx[2:14]
    header = _get(directory, stem, "header", "eth_getBlockByNumber", [hex(height), False])
    prague_header(header, height, block_hash)
    receipts = _get(directory, stem, "receipts", "eth_getBlockReceipts", [block_hash])
    if (
        len(receipts) != len(header["transactions"])
        or "0x" + indexed_trie_root([_receipt_leaf(value) for value in receipts]).hex()
        != header["receiptsRoot"]
        or any(
            (value["transactionHash"], value["blockHash"], int(value["transactionIndex"], 16))
            != (header["transactions"][position], block_hash, position)
            for position, value in enumerate(receipts)
        )
        or header["transactions"][index] != tx
    ):
        raise ValueError("rooted receipts cannot bind this case")
    raw_receipt = receipts[index]
    bound = receipt_header(header, 1, height, (tx,))
    receipt = decode_receipt(raw_receipt, bound.ref, tx, index)
    previous_gas = int(receipts[index - 1]["cumulativeGasUsed"], 16) if index else 0
    if receipt.gas_used != int(raw_receipt["cumulativeGasUsed"], 16) - previous_gas:
        raise ValueError("gas use disagrees with rooted cumulative gas")
    result = {
        "case_role": row["case_role"],
        "transaction_hash": tx,
        "height": height,
        "transaction_index": index,
        "block_hash": block_hash,
        "timestamp_utc": datetime.fromtimestamp(int(header["timestamp"], 16), UTC).isoformat(),
        "rooted_receipts": len(receipts),
        "receipt_root": header["receiptsRoot"],
        "status": receipt.status,
        "sender": receipt.sender,
        "recipient": receipt.recipient,
        "block_fee_recipient": header["miner"],
        "gas_used": receipt.gas_used,
        "effective_gas_price": str(receipt.effective_gas_price),
        "gas_paid_wei": str(receipt.gas_used * receipt.effective_gas_price),
        "receipt_funding": _financing(raw_receipt["logs"], receipt.recipient or EXECUTOR),
        "raw_evidence_sha256": {
            lane: {
                kind: sha256((directory / f"{stem}-{lane}.{kind}.json").read_bytes()).hexdigest()
                for kind in ("request", "response")
            }
            for lane in ("header", "receipts", "trace")
            if (directory / f"{stem}-{lane}.response.json").is_file()
        },
        "trace": None,
    }
    if (
        result["receipt_funding"].get("shape_events") != row["shapes"]
        or len(receipt.logs) != row["logs"]
    ):
        raise ValueError("archival log coverage differs from rooted receipt")
    if not (directory / f"{stem}-trace.response.json").is_file():
        return result
    trace = _get(
        directory,
        stem,
        "trace",
        "debug_traceTransaction",
        [
            tx,
            {"tracer": "callTracer", "tracerConfig": {"withLog": True}, "timeout": "10s"},
        ],
    )
    try:
        frames = nested_calls(trace)
        paths = _receipt_positions(trace, frames, receipt)
    except ValueError as error:
        result["trace"] = {"status": "unresolved", "reason": str(error)}
        return result
    unlocks = {
        path: frame
        for path, frame in frames.items()
        if first_failure(frames, path) is None
        and frame.recipient == V4
        and frame.data[:4] == _selector("unlock(bytes)")
    }
    matches = []
    for funding in result["receipt_funding"]["paired_groups"]:
        take = paths[funding["take_log"]]
        repay = paths[funding["repaid_log"]]
        matches_in = sorted(
            [path for path in unlocks if _within(take, path) and _within(repay, path)],
            key=len,
            reverse=True,
        )
        if not matches_in:
            continue
        scope = matches_in[0]
        take_parents = [
            path
            for path, frame in frames.items()
            if _within(path, scope)
            and _within(take, path)
            and frame.recipient == V4
            and frame.data[:4] == _selector("take(address,address,uint256)")
            and frame.error is None
            and abi(("address", "address", "uint256"), frame.data[4:])
            == (WETH, receipt.recipient, int(funding["principal_weth_wei"]))
        ]
        settlements = [
            path
            for path, frame in frames.items()
            if _within(path, scope)
            and path > repay
            and frame.recipient == V4
            and frame.error is None
            and frame.data == _selector("settle()")
            and frame.output is not None
            and abi(("uint256",), frame.output) == (int(funding["principal_weth_wei"]),)
        ]
        if len(take_parents) == len(settlements) == 1:
            matches.append(
                {
                    "take_log": funding["take_log"],
                    "repaid_log": funding["repaid_log"],
                    "unlock_path": scope,
                    "take_path": take_parents[0],
                    "settle_path": settlements[0],
                }
            )
    inherited = replay_trace_funding(receipt, trace)
    native_movements = [
        {"path": path, "from": f.sender, "to": f.recipient, "value_wei": str(f.value)}
        for path, f in frames.items()
        if f.kind == "CALL"
        and f.value
        and first_failure(frames, path) is None
        and {f.sender, f.recipient}
        & {receipt.sender, receipt.recipient, BENEFICIARY, header["miner"]}
    ]
    generic = analyze_swap_funding(
        tuple(proof.leg for proof in inherited.resolved_swaps),
        inherited.links,
    )
    payout_to_fee_recipient = sum(
        int(move["value_wei"])
        for move in native_movements
        if move["to"] == header["miner"] and move["from"] in (receipt.sender, receipt.recipient)
    )
    scope_sizes: Counter[tuple[int, ...]] = Counter()
    for proof in inherited.resolved_swaps:
        matching_unlock = [path for path in unlocks if _within(proof.call_path, path)]
        if matching_unlock:
            scope_sizes[max(matching_unlock, key=len)] += 1
    result["trace"] = {
        "status": "rooted_logs_match",
        "frames": len(frames),
        "failed_frames": sum(f.error is not None for f in frames.values()),
        "manager_unlock_count": len(unlocks),
        "supported_swaps_by_unlock": [
            {"unlock_path": path, "supported_swaps_beneath": scope_sizes[path]}
            for path in sorted(unlocks)
        ],
        "confirmed_take_settle_groups": matches,
        "native_moves_touched_actors": native_movements,
        "supported_swap_legs": len(inherited.resolved_swaps),
        "funding_links": len(inherited.links),
        "generic_funded_path": generic.longest_witnessed_path,
        "explicit_native_payments_to_block_fee_recipient_wei": str(payout_to_fee_recipient),
    }
    if receipt.recipient == EXECUTOR and matches and result["receipt_funding"]["paid_group_count"]:
        result["conditional_weth_equivalent_after_visible_fees_wei"] = str(
            int(result["receipt_funding"]["beneficiary_weth_wei"])
            - int(result["gas_paid_wei"])
            - payout_to_fee_recipient
        )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--rooted", action="store_true")
    args = parser.parse_args()
    cohort = scan(args.directory)
    cohort["schema"] = "oasis.eth-batch-cohort-verification.v1"
    cohort["local_input_sha256"] = {
        name: sha256((args.directory / name).read_bytes()).hexdigest()
        for name in (
            "plan.json",
            "listing.xml",
            "objects.json",
            "original-object-ledger.jsonl",
            "scan.sql",
            "coverage.csv",
            "thresholds.csv",
            "rankable.csv",
            "executor-logs.csv",
        )
    }
    cohort["archive_objects"] = json.loads((args.directory / "objects.json").read_bytes())
    if args.rooted:
        plan_bytes = (args.directory / "rpc-plan.json").read_bytes()
        selected = json.loads(plan_bytes)
        rpc_stop = json.loads((args.directory / "rpc/STOP.json").read_bytes())
        if rpc_stop["plan_sha256"] != sha256(plan_bytes).hexdigest():
            raise ValueError("retained RPC plan and STOP disagree")
        cohort["rpc_plan"] = selected
        cohort["rpc_stop"] = rpc_stop
        cohort["rooted_samples"] = [
            rooted_sample(args.directory / "rpc", row) for row in selected["samples"]
        ]
    print(json.dumps(cohort, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
