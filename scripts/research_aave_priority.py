"""Replay the five Aave wallet residual leads against receipts and a same-index query."""

import argparse
import sys
from collections import Counter
from pathlib import Path
from sqlite3 import Error as DatabaseError
from typing import Any
from urllib.parse import urlencode

from oasis_agent.arbitrage.aave_wallet import decimal_quantity
from oasis_agent.arbitrage.cycle_evidence import read_bounded
from oasis_agent.arbitrage.cycle_plan import address, hash32, utc
from oasis_agent.evidence.public_receipts import (
    decode_receipt,
    receipt_header,
    transaction_for_receipt,
)
from oasis_agent.evidence.rpc_validation import strict_json_object, strict_list, strict_object
from oasis_agent.evidence.state_trie import prague_header
from oasis_agent.models.common import canonical_json, sha256_hex
from oasis_agent.rpc.budget import BudgetPolicy, RpcBudget

from . import capture_public_rpc as rpc
from . import research_aave_wallet as wallet


def indexed_value_counts(body: bytes, block: int, account: str) -> Counter[tuple[str, str, int]]:
    envelope = strict_json_object(body)
    rows = strict_list(envelope.get("result"), "transaction index rows")
    if (
        envelope.get("status") != "1"
        or envelope.get("message") != "OK"
        or not 1 <= len(rows) < 2000
    ):
        raise ValueError("transaction index response rejected")
    counts: Counter[tuple[str, str, int]] = Counter()
    for item in rows:
        row = strict_object(item, "internal index row")
        if decimal_quantity(row.get("blockNumber"), 64) != block or row.get("isError") not in {
            "0",
            "1",
        }:
            raise ValueError("internal index row context mismatch")
        sender, recipient = address(row.get("from")), address(row.get("to"))
        value = decimal_quantity(row.get("value"))
        if account in (sender, recipient) and value and row["isError"] == "0":
            if row.get("type") != "call":
                raise ValueError("unsupported indexed account value movement")
            counts[sender, recipient, value] += 1
    return counts


def replay(
    priority: Path, baseline: Path, index: Path, receipts: Path, pages: Path, balances: Path
) -> dict[str, Any]:
    prior = wallet.load(baseline, 4 * 1024 * 1024)
    if prior["chain_id"] != 1 or prior["schema"] != "oasis-aave-wallet-comparison-v1":
        raise ValueError("existing Ethereum wallet report required")
    account = address(prior["account"])
    token_map = {row["address"]: row["decimals"] for row in prior["assets"] if row["address"]}
    movements, index_ref = wallet.index(
        index, account, prior["first_block"], prior["last_block"], token_map
    )
    lead = wallet.load(priority)
    if lead["index_ref"] != index_ref or index_ref != prior["index"]:
        raise ValueError("priority index identity changed")
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in lead["duplicate_shapes"]:
        groups.setdefault(hash32(row["tx"]), []).append(row)
    if len(groups) != 5 or len(lead["duplicate_shapes"]) != 6:
        raise ValueError("five transactions and six shapes required")
    plan_raw = read_bounded(pages / "plan.json", 8192)
    plan = wallet.load(pages / "plan.json", 8192)
    targets = [(hash32(tx), block) for tx, block in plan["targets"]]
    if (
        len(targets) != 5
        or set(groups) != {tx for tx, _ in targets}
        or plan["endpoint"] != wallet.ENDPOINT
        or plan["source"] != "routescan_tx_index"
        or plan["max_requests"] != 5
        or plan["minimum_interval_ms"] != 650
        or any((pages / name).exists() for name in ("STOP", "ACTIVE.json"))
    ):
        raise ValueError("transaction index plan or status mismatch")
    ledger = pages / "original-budget.sqlite3"
    if wallet.load(pages / "identity.json", 4096) != {
        "plan_sha256": sha256_hex(plan_raw),
        "ledger": rpc.identity(ledger),
        "root": str(pages.resolve()),
    }:
        raise ValueError("original transaction index ledger required")
    with RpcBudget(ledger, (BudgetPolicy("routescan_tx_index", 5, 650),), read_only=True) as budget:
        if budget.counts() != {"routescan_tx_index": 5}:
            raise ValueError("transaction index reservation mismatch")

    results = []
    verified_receipts = set()
    last_finished = None
    for i, (tx_hash, block) in enumerate(targets):
        resource = receipts / f"receipt-{i}"
        captured = rpc.replay(resource)
        if captured["status"] != "complete" or captured["plan_kind"] != "pinned_receipts":
            raise ValueError("priority receipt not complete")
        records = captured["exports"]["records"]
        if len(records) != 5 or [r["request"]["method"] for r in records[2:4]] != [
            "eth_getTransactionReceipt",
            "eth_getTransactionByHash",
        ]:
            raise ValueError("priority receipt transaction context required")
        block_data = records[1]["response"]["result"]
        pinned = receipt_header(block_data, 1, block, (tx_hash,))
        prague_header(block_data, block, pinned.ref.block_hash)
        receipt = decode_receipt(
            records[2]["response"]["result"],
            pinned.ref,
            tx_hash,
            dict(pinned.receipt_positions)[tx_hash],
        )
        transaction_for_receipt(records[3]["response"]["result"], receipt)
        if receipt.status != 1 or account in (receipt.sender, receipt.recipient):
            raise ValueError("successful third-party transaction required")
        verified_receipts.add(tx_hash)
        request = read_bounded(pages / f"{i}.request.json", 4096)
        body = read_bounded(pages / f"{i}.response.json", 2 * 1024 * 1024)
        meta = wallet.load(pages / f"{i}.meta.json", 4096)
        started, finished = utc(meta["start"]), utc(meta["finish"])
        expected_query = urlencode(
            {"module": "account", "action": "txlistinternal", "txhash": tx_hash}
        )
        if (
            request
            != canonical_json({"method": "GET", "url": wallet.ENDPOINT + "?" + expected_query})
            or meta["http"] != 200
            or meta["error"] is not None
            or meta["budget"] != i + 1
            or (meta["request_sha256"], meta["response_sha256"])
            != (sha256_hex(request), sha256_hex(body))
            or not started <= finished <= utc(plan["expires_utc"])
            or (last_finished is not None and started < last_finished)
        ):
            raise ValueError("transaction index wire provenance mismatch")
        last_finished = finished
        per_tx = indexed_value_counts(body, block, account)
        by_shape: dict[tuple[str, str, int], list[str]] = {}
        for movement in movements:
            if (
                movement.transaction == tx_hash
                and movement.kind == "internal"
                and movement.success
                and movement.value_raw
            ):
                if movement.trace_id is None or movement.block != block:
                    raise ValueError("partition index trace identity missing")
                by_shape.setdefault(
                    (movement.sender, movement.recipient, movement.value_raw), []
                ).append(movement.trace_id)
        account_counts = Counter({shape: len(ids) for shape, ids in by_shape.items()})
        shapes = []
        for group in groups[tx_hash]:
            shape = address(group["from"]), address(group["to"]), group["amount_raw"]
            ids = sorted(by_shape[shape])
            if group["block"] != block or ids != sorted(group["trace_ids"]) or len(ids) != 2:
                raise ValueError("priority movement no longer matches original index")
            shapes.append(
                {
                    "from": shape[0],
                    "to": shape[1],
                    "value_wei": shape[2],
                    "address_index_trace_ids": ids,
                    "transaction_index_count": per_tx[shape],
                }
            )
        boundary = []
        for number, expected_hash in (
            (block - 1, pinned.parent_hash),
            (block, pinned.ref.block_hash),
        ):
            capture_path = balances / f"block-{number}"
            balance_capture = rpc.replay(capture_path)
            if (
                balance_capture["status"] != "complete"
                or balance_capture["plan_kind"] != "pinned_reads"
            ):
                raise ValueError("priority native balance capture incomplete")
            header_raw = balance_capture["exports"]["records"][1]["response"]["result"]
            snap, _ = prague_header(header_raw, number, expected_hash)
            boundary.append(wallet.native(capture_path, snap.ref, account))
        observed = boundary[1][0] - boundary[0][0]
        indexed = sum(
            (row.delta(account) if row.success else 0)
            - (row.gas_paid_raw if row.kind == "external" and row.sender == account else 0)
            for row in movements
            if row.block == block and row.kind != "erc20"
        )
        one_each = sum(
            (s["to"] == account) * s["value_wei"] - (s["from"] == account) * s["value_wei"]
            for s in shapes
        )
        results.append(
            {
                "transaction": tx_hash,
                "block": block,
                "block_hash": pinned.ref.block_hash,
                "transaction_index": receipt.transaction_index,
                "receipt_status": receipt.status,
                "receipt_capture_sha256": captured["capture_sha256"],
                "transaction_index_body_sha256": sha256_hex(body),
                "provider_account_multiplicity_matches": account_counts == per_tx,
                "shapes": shapes,
                "native_block_balance_delta_wei": observed,
                "indexed_native_block_delta_wei": indexed,
                "block_residual_wei": observed - indexed,
                "block_residual_if_one_per_shape_wei": observed - indexed + one_each,
                "balance_capture_hashes": [item[1] for item in boundary],
            }
        )
    missing = set(prior["comparison"]["transactions_without_receipts"])
    if not verified_receipts <= missing:
        raise ValueError("priority receipts must be new against the frozen baseline")
    native = [row for row in prior["comparison"]["assets"] if row["asset"] == "native"]
    if len(native) != 1 or type(native[0]["balance_minus_index_raw"]) is not int:
        raise ValueError("original native residual required")
    return {
        "schema": "oasis-aave-five-priority-v1",
        "chain_id": 1,
        "account": account,
        "status": "internal_value_commitment_unresolved",
        "evidence_class": "receipt_and_same_provider_index_comparison",
        "rows": results,
        "receipt_gap_before": len(missing),
        "receipt_gap_after": len(missing - verified_receipts),
        "native_residual_wei": native[0]["balance_minus_index_raw"],
        "index_ref": index_ref,
        "independent_internal_trace_verified": False,
        "net_profit": None,
        "execution_eligible": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("priority", "baseline", "index", "receipts", "pages", "balances", "output"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = replay(
            args.priority, args.baseline, args.index, args.receipts, args.pages, args.balances
        )
        rpc.publish(args.output, result)
    except (ValueError, TypeError, KeyError, IndexError, OSError, DatabaseError):
        print("Priority Aave evidence refused", file=sys.stderr)
        return 2
    print(result["status"])
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
