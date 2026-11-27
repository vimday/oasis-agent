"""Rebuild the transaction receipts for the historical Base signal and its direct child."""

import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
from replay_base_mixed_whole import AERO, DIR, UNI, digest, read, write
from replay_y2026_op_arb import receipt_commitment

from oasis_agent.arbitrage.cycle_protocols import abi, data_bytes, topic
from oasis_agent.evidence.log_bloom import possible_log_emitters
from oasis_agent.evidence.rpc_validation import rpc_quantity
from oasis_agent.rpc.budget import BudgetPolicy, RpcBudget
from scripts.measure_robinhood_quote_delivery import _body, _fetch, _read

HEADERS = {63043739: 3, 63043740: 14}
ROOT = DIR / "receipts"
URL = "https://base.drpc.org"
POLICY = BudgetPolicy("archive", 3, 300)
SWAP = topic("Swap(address,address,int256,int256,uint160,uint128,int24)")


def plan(frozen):
    files = [
        f"{n + offset:02}-{source}.response"
        for n in HEADERS.values()
        for offset, source in enumerate(("official", "state"))
    ]
    return {
        "schema": "oasis-base-mixed-child-receipts-v1",
        "frozen_utc": frozen,
        "source": URL,
        "cap": POLICY.max_requests,
        "usd": 0,
        "whole_scope_sha256": digest(DIR / "scope.json"),
        "driver_sha256": digest(Path(__file__)),
        "headers": {name: digest(DIR / name) for name in files},
    }


def replay(capture):
    locked = read(ROOT / "scope.json")
    assert locked == plan(locked["frozen_utc"])
    path = ROOT / "original.sqlite3"
    if capture and path.exists():
        raise ValueError("archive attempt already exists")
    no = 0
    failure = None
    observed = []
    proofs = {}
    with (
        RpcBudget(path, (POLICY,), read_only=not capture) as ledger,
        httpx.Client(timeout=5, follow_redirects=False, trust_env=False) as client,
    ):

        def ask(method, params):
            nonlocal no
            no += 1
            prefix = ROOT / f"{no:02}-archive"
            request = _body(no, method, params)
            if capture:
                delay = ledger.reserve(POLICY.source, now_ms=time.time_ns() // 1_000_000)
                if delay:
                    time.sleep((delay + 1) / 1000)
                    assert ledger.reserve(POLICY.source, now_ms=time.time_ns() // 1_000_000) == 0
                _fetch(client, URL, prefix, request)
            if prefix.with_suffix(".meta").is_file():
                proofs[prefix.name] = read(prefix.with_suffix(".meta"))
            return _read(prefix, request, no, 1)[0]

        try:
            assert rpc_quantity(ask("eth_chainId", [])) == 8453
            for height, n in HEADERS.items():
                head = read(DIR / f"{n:02}-official.response")["result"]
                peer = read(DIR / f"{n + 1:02}-state.response")["result"]
                assert (head["hash"], head["receiptsRoot"]) == (peer["hash"], peer["receiptsRoot"])
                possible = sorted(
                    possible_log_emitters(
                        (UNI, AERO), matching_header_blooms=(head["logsBloom"], peer["logsBloom"])
                    )
                )
                raws = ask("eth_getBlockReceipts", [head["hash"]])
                _, root = receipt_commitment(8453, head, raws)
                changes = []
                for tx in raws:
                    assert tx["blockHash"] == head["hash"]
                    matched = [log for log in tx["logs"] if log["address"] in (UNI, AERO)]
                    if not matched:
                        continue
                    changes.append(
                        {
                            "transaction_hash": tx["transactionHash"],
                            "index": rpc_quantity(tx["transactionIndex"]),
                            "topics": [
                                {
                                    "pool": log["address"],
                                    "index": rpc_quantity(log["logIndex"]),
                                    "topic0": log["topics"][0],
                                    "amount_and_price": (
                                        abi(
                                            ("int256", "int256", "uint160", "uint128", "int24"),
                                            data_bytes(log["data"]),
                                        )
                                        if log["topics"][0] == SWAP
                                        else None
                                    ),
                                }
                                for log in matched
                            ],
                        }
                    )
                observed.append(
                    {
                        "height": height,
                        "hash": head["hash"],
                        "root": root,
                        "receipts": len(raws),
                        "bloom_possible_emitters": possible,
                        "pool_log_transactions": changes,
                    }
                )
        except (AssertionError, KeyError, ValueError, OSError, httpx.HTTPError) as exc:
            failure = {"at": no, "type": type(exc).__name__, "details": str(exc)}
        counts = ledger.counts()
    return {
        "schema": locked["schema"],
        "scope_sha256": digest(ROOT / "scope.json"),
        "blocks": observed,
        "stopped": failure,
        "proofs": proofs,
        "counts": counts,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "capture", "replay"))
    args = parser.parse_args()
    if args.action == "prepare":
        write(ROOT / "scope.json", plan(datetime.now(UTC).isoformat()))
    else:
        result = replay(args.action == "capture")
        if args.action == "capture":
            write(ROOT / "STOP.json", result["stopped"])
        print(json.dumps(result, indent=2, sort_keys=True))
