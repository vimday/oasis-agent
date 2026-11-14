"""Offline wallet evidence comparison using retained Routescan pages and RPC captures."""

import argparse
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from oasis_agent.arbitrage import aave_wallet
from oasis_agent.arbitrage.cycle_evidence import read_bounded
from oasis_agent.arbitrage.cycle_plan import utc
from oasis_agent.evidence.public_receipts import decode_receipt, quantity
from oasis_agent.evidence.public_rpc_plan import PublicReadPlan, PublicReceiptPlan
from oasis_agent.evidence.rpc_validation import strict_json_object
from oasis_agent.models.common import BlockRef, canonical_json, sha256_hex
from oasis_agent.rpc.budget import BudgetPolicy, RpcBudget

from . import capture_public_rpc
from .research_aave_receipts import replay as replay_positions
from .research_metric import publish

ENDPOINT = "https://api.routescan.io/v2/network/mainnet/evm/1/etherscan/api"


def load(path: Path, limit: int = 128 * 1024) -> dict[str, Any]:
    return strict_json_object(read_bounded(path, limit))


def index(
    directory: Path,
    account: str,
    first: int,
    last: int,
    tokens: dict[str, int],
) -> tuple[tuple[aave_wallet.Movement, ...], dict[str, object]]:
    if not 1 <= last - first + 1 <= 1024:
        raise ValueError("wallet_index_window_limit")
    plan_raw = read_bounded(directory / "plan.json", 128 * 1024)
    plan = strict_json_object(plan_raw)
    report_raw = read_bounded(directory / "summary.json", 128 * 1024)
    summary = strict_json_object(report_raw)
    if (
        plan["account"] != account
        or type(plan["chain_id"]) is not int
        or plan["chain_id"] != 1
        or plan["endpoint"] != ENDPOINT
        or summary["status"] != "complete"
        or any(
            (directory / name).exists() or (directory / name).is_symlink()
            for name in ("STOP", "ACTIVE.json")
        )
    ):
        raise ValueError("wallet_index_identity_or_incomplete")
    queries, metadata = plan["queries"], summary["rows"]
    if (
        not isinstance(queries, list)
        or not isinstance(metadata, list)
        or type(plan["max_new_requests"]) is not int
        or not 1 <= len(queries) == len(metadata) == plan["max_new_requests"] <= 128
    ):
        raise ValueError("wallet_index_capture_count")
    policy = (BudgetPolicy("routescan_wallet_partitions", len(queries), 650),)
    ledger = directory / "original-budget.sqlite3"
    if ledger.is_symlink() or not ledger.is_file():
        raise ValueError("wallet_index_original_ledger_missing")
    with RpcBudget(ledger, policy, read_only=True) as budget:
        if budget.counts()["routescan_wallet_partitions"] != len(queries):
            raise ValueError("wallet_index_ledger_mismatch")
    coverage: dict[tuple[str, str | None], set[int]] = {}
    seen = set()
    decoded = []
    time: datetime = utc(plan["frozen_at"])
    total_bytes = 0
    for ordinal, (query, row) in enumerate(zip(queries, metadata, strict=True), 1):
        if (
            not isinstance(query, list)
            or len(query) != 2
            or not isinstance(query[1], dict)
            or not isinstance(row, dict)
        ):
            raise ValueError("wallet_index_metadata_shape")
        name, params = query
        if (
            not isinstance(name, str)
            or re.fullmatch(r"[a-z0-9-]{1,32}", name) is None
            or name in seen
            or row["name"] != name
            or row["http"] != 200
            or row["error"] is not None
            or row["reserved"] is not True
            or type(row["budget"]) is not int
            or row["budget"] != ordinal
            or load(directory / f"{name}.meta.json") != row
        ):
            raise ValueError("wallet_index_metadata_mismatch")
        seen.add(name)
        started, finished = utc(row["start_utc"]), utc(row["finish_utc"])
        if not time <= started <= finished:
            raise ValueError("wallet_index_clock_mismatch")
        time = finished
        req_raw = read_bounded(directory / f"{name}.request.json", 4096)
        res_raw = read_bounded(directory / f"{name}.response.json", 2 * 1024 * 1024)
        total_bytes += len(res_raw)
        if total_bytes > 32 * 1024 * 1024:
            raise ValueError("wallet_index_total_size_limit")
        if (sha256_hex(req_raw), sha256_hex(res_raw)) != (
            row["request_sha256"],
            row["response_sha256"],
        ):
            raise ValueError("wallet_index_wire_hash_mismatch")
        request = strict_json_object(req_raw)
        url = urlsplit(request["url"])
        pairs = parse_qsl(url.query, strict_parsing=True)
        if (
            request["method"] != "GET"
            or url._replace(query="").geturl() != ENDPOINT
            or len(pairs) != len(params)
            or dict(pairs) != params
        ):
            raise ValueError("wallet_index_request_mismatch")
        kind: aave_wallet.Kind
        action = params["action"]
        token = params.get("contractaddress")
        if action == "tokentx" and token in tokens:
            kind = "erc20"
        elif action in ("txlist", "txlistinternal") and token is None:
            kind = "external" if action == "txlist" else "internal"
        else:
            raise ValueError("wallet_index_method_outside_plan")
        start = aave_wallet.decimal_quantity(params["startblock"], 64)
        end = aave_wallet.decimal_quantity(params["endblock"], 64)
        expected = {
            "module": "account",
            "action": action,
            "address": account,
            "startblock": str(start),
            "endblock": str(end),
            "page": "1",
            "offset": "200",
            "sort": "asc",
            **({"contractaddress": token} if token else {}),
        }
        if params != expected or not first <= start <= end <= last:
            raise ValueError("wallet_index_filter_mismatch")
        heights = coverage.setdefault((kind, token), set())
        partition = set(range(start, end + 1))
        if heights & partition:
            raise ValueError("wallet_index_partition_overlap")
        heights.update(partition)
        entries = aave_wallet.index_rows(
            kind,
            strict_json_object(res_raw),
            account,
            start,
            end,
            (token, tokens[token]) if token else None,
        )
        if type(row["count"]) is not int or row["count"] != len(entries):
            raise ValueError("wallet_index_row_count_mismatch")
        decoded.extend(entries)
    expected_keys = {("erc20", token) for token in tokens} | {
        ("internal", None),
        ("external", None),
    }
    if set(coverage) != expected_keys or any(
        v != set(range(first, last + 1)) for v in coverage.values()
    ):
        raise ValueError("wallet_index_partition_gap")
    return tuple(decoded), {
        "class": "routescan_index_claims",
        "plan_sha256": sha256_hex(plan_raw),
        "summary_sha256": sha256_hex(report_raw),
        "covered_query_partitions": len(queries),
    }


def native(path: Path, block: BlockRef, account: str) -> tuple[int, str]:
    plan, _ = capture_public_rpc.load_plan(path / "plan.json")
    if (
        not isinstance(plan, PublicReadPlan)
        or plan.chain_id != block.chain_id
        or plan.block_number != block.number
        or plan.block_hash != block.block_hash
    ):
        raise ValueError("wallet_native_boundary_mismatch")
    capture = capture_public_rpc.replay(path)
    if capture["status"] != "complete" or BlockRef(**capture["block"]["ref"]) != block:
        raise ValueError("wallet_native_boundary_unverified")
    calls = [
        row
        for row in capture["exports"]["records"]
        if row["request"]["method"] == "eth_getBalance" and row["request"]["params"][0] == account
    ]
    if len(calls) != 1:
        raise ValueError("wallet_native_balance_call_missing")
    return quantity(calls[0]["response"]["result"]), capture["capture_sha256"]


def run(args: argparse.Namespace) -> dict[str, Any]:
    positions = replay_positions(
        args.plan, args.position, args.cashflows, tuple(args.position_boundaries)
    )
    if positions["chain_id"] != 1:
        raise ValueError("wallet_index_ethereum_only")
    endpoints = positions["position_boundaries"]
    opening, closing, tokens, native_refs = {}, {}, {}, []
    for role, balances, native_path in zip(
        ("opening", "closing"), (opening, closing), args.native_boundaries, strict=True
    ):
        endpoint = endpoints[role]
        ref = BlockRef(**endpoint["block"])
        for item in endpoint["reserves"]:
            if item["role"] == "underlying":
                balances[item["token"]] = item["balance_raw"]
                tokens[item["token"]] = item["decimals"]
        balances[None], digest = native(native_path, ref, positions["account"])
        native_refs.append(digest)
    first, last = (
        endpoints["opening"]["block"]["number"] + 1,
        endpoints["closing"]["block"]["number"],
    )
    movements, index_ref = index(args.wallet_index, positions["account"], first, last, tokens)
    captures = [*args.position, *args.wallet_receipt]
    if not 1 <= len(captures) <= 192:
        raise ValueError("wallet_receipt_count_limit")
    receipts, evidence = [], []
    volume = 0
    for directory in captures:
        plan, _ = capture_public_rpc.load_plan(directory / "plan.json")
        if (
            not isinstance(plan, PublicReceiptPlan)
            or plan.chain_id != 1
            or not first <= plan.block_number <= last
        ):
            raise ValueError("wallet_receipt_outside_scope")
        report = capture_public_rpc.replay(directory)
        evidence.append(
            {
                "capture_sha256": report["capture_sha256"],
                "source": plan.source,
                "block_number": plan.block_number,
                "status": report["status"],
            }
        )
        if report["status"] != "complete":
            continue
        volume += len(canonical_json(report["exports"]))
        if volume > 64 * 1024 * 1024:
            raise ValueError("wallet_receipt_total_size_limit")
        block = BlockRef(**report["block"]["ref"])
        positions_in_block = dict(report["block"]["receipt_positions"])
        for row in report["exports"]["records"]:
            request = row["request"]
            if request["method"] == "eth_getTransactionReceipt":
                tx = request["params"][0]
                receipts.append(
                    decode_receipt(row["response"]["result"], block, tx, positions_in_block[tx])
                )
    return {
        "schema": "oasis-aave-wallet-comparison-v1",
        "chain_id": 1,
        "account": positions["account"],
        "assets": [
            {"address": None, "currency": "ETH", "decimals": 18},
            *(
                {"address": address, "decimals": decimals}
                for address, decimals in sorted(tokens.items())
            ),
        ],
        "first_block": first,
        "last_block": last,
        "position_capture_hashes": [endpoints[k]["capture_sha256"] for k in ("opening", "closing")],
        "native_capture_hashes": native_refs,
        "index": index_ref,
        "receipts": evidence,
        "comparison": aave_wallet.reconcile_wallet(
            positions["account"], opening, closing, movements, tuple(receipts)
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--cashflows", type=Path, required=True)
    parser.add_argument("--position", type=Path, action="append", required=True)
    parser.add_argument("--position-boundaries", type=Path, nargs=2, required=True)
    parser.add_argument("--native-boundaries", type=Path, nargs=2, required=True)
    parser.add_argument("--wallet-index", type=Path, required=True)
    parser.add_argument("--wallet-receipt", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    try:
        args = parser.parse_args()
        report = run(args)
        publish(args.output, report)
        return 0
    except (ValueError, TypeError, OSError, KeyError, sqlite3.Error):
        print("Wallet evidence comparison refused", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
