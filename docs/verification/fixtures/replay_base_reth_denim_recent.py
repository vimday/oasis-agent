"""Compare public Base whole-call results with retained real Denim transaction evidence."""

import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
from eth_account import Account
from eth_hash.auto import keccak
from replay_base_denim_aleph import validate_header
from replay_base_executor_attempts import BLOCKS, PANEL, SCOPE, D
from replay_base_executor_attempts import lane as original_lane
from replay_base_mixed_whole import CHAIN, digest, read, write
from replay_multihop_second_shard_receipts import user_transaction
from replay_y2026_base_elysium_blocks import cohort

from oasis_agent.arbitrage.cycle_protocols import topic
from oasis_agent.evidence.base_denim import parse_header_timestamp_ms
from oasis_agent.evidence.rpc_validation import rpc_quantity
from oasis_agent.rpc.budget import BudgetPolicy, RpcBudget
from scripts.measure_robinhood_quote_delivery import _body, _fetch, _read

ENDPOINTS = {"official": "https://mainnet.base.org", "drpc": "https://base.drpc.org"}
BUDGETS = tuple(BudgetPolicy(key, 8 if key == "official" else 7, 250) for key in ENDPOINTS)
CASES = {
    "same": "0x39e1096ab88cb4b68539e05edfa098f2d84a0b98d3308fe865df810eef072786",
    "cross": "0x04ac22bfd2dc8c4b14244b66acd665fb16074d74306b26d2b326dff6e7944a45",
}
PAYEES = {
    "same": "0x2c1198d3c540dbb993ec0b2800ed5f2aa15297e3",
    "cross": "0x2c1198d3c540dbb993ec0b2800ed5f2aa15297e3",
}
ASSETS = {
    "same": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
    "cross": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
}
FIELDS = ("number", "parentHash", "timestamp", "baseFeePerGas", "miner", "gasLimit")
TRANSFER = topic("Transfer(address,address,uint256)")


def frozen(utc):
    return {
        "schema": "oasis.base-reth-denim-recent-remote.v1",
        "frozen_utc": utc,
        "driver_sha256": digest(Path(__file__)),
        "prior_transaction_scope_sha256": SCOPE,
        "prior_receipt_index_sha256": digest(PANEL),
        "chain_id": CHAIN,
        "cases": CASES,
        "endpoints": ENDPOINTS,
        "budgets": {policy.source: policy.max_requests for policy in BUDGETS},
        "cost_usd": 0,
        "state_overrides": False,
        "block_overrides": False,
    }


def exchange(root, client, source, number, name, method, args):
    stem = root / f"{source}-{number:02d}-{name}"
    body = _body(number, method, args)
    if client is not None:
        with RpcBudget(root / "original.sqlite3", BUDGETS, require_existing=True) as ledger:
            delay = ledger.reserve(source, now_ms=time.time_ns() // 10**6)
            if delay:
                time.sleep((delay + 1) / 1_000)
                if ledger.reserve(source, now_ms=time.time_ns() // 10**6):
                    raise ValueError("source pacing did not advance")
        _fetch(client, ENDPOINTS[source], stem, body)
    value, _ = _read(stem, body, number, 1)
    return value, read(stem.with_suffix(".meta"))


def original_cases():
    plan = read(D / "scope.json")
    entries, _ = original_lane("envelopes", plan["lanes"]["envelopes"], SCOPE, plan["utc"])
    market, receipts, _ = cohort("base", read(BLOCKS / "scope.json"))
    assert market["blocks"] == read(PANEL)["base"]["blocks"]
    cases = {}
    for name, identifier in CASES.items():
        tx, receipt = entries[identifier], receipts[identifier]
        signed = user_transaction(tx, CHAIN)
        assert "0x" + keccak(signed).hex() == identifier
        assert tx["from"] == receipt["from"] == Account.recover_transaction(signed).lower()
        assert (
            tx["blockHash"] == receipt["blockHash"]
            and tx["transactionIndex"] == receipt["transactionIndex"]
        )
        assert tx["type"] == "0x2"
        parent_no = rpc_quantity(receipt["blockNumber"]) - 1
        parent = read(BLOCKS / "base" / f"h{parent_no}.response")["result"]
        child = read(BLOCKS / "base" / f"h{parent_no + 1}.response")["result"]
        assert parent["hash"] == child["parentHash"] and child["hash"] == receipt["blockHash"]
        assert (parent["timestamp"] == child["timestamp"]) == (name == "same")
        call = {
            key: tx[key]
            for key in (
                "from",
                "to",
                "gas",
                "nonce",
                "maxFeePerGas",
                "maxPriorityFeePerGas",
                "value",
                "accessList",
            )
        }
        call["data"] = tx["input"]
        cases[name] = receipt, parent, child, call
    return cases


def transfer_delta(logs, asset, payee):
    indexed = "0x" + "0" * 24 + payee[2:]
    return sum(
        (int(log["topics"][2] == indexed) - int(log["topics"][1] == indexed)) * int(log["data"], 16)
        for log in logs
        if log["address"] == asset and len(log["topics"]) == 3 and log["topics"][0] == TRANSFER
    )


def projection(logs):
    return [{key: log[key] for key in ("address", "topics", "data")} for log in logs]


def interpret(name, raw, parent, child, receipt, call):
    [sim] = raw
    [outcome] = sim["calls"]
    [number, _, timestamp, base_fee, _, _] = [sim[key] for key in FIELDS]
    assert rpc_quantity(number) == rpc_quantity(parent["number"]) + 1
    assert all(sim[key] == child[key] for key in FIELDS)
    gas, status = rpc_quantity(outcome["gasUsed"]), rpc_quantity(outcome["status"])
    assert status in (0, 1) and 0 < gas <= rpc_quantity(call["gas"])
    assert gas == rpc_quantity(sim["gasUsed"])
    logs = projection(outcome["logs"])
    assert status or not logs
    charged = min(
        rpc_quantity(call["maxFeePerGas"]),
        rpc_quantity(base_fee) + rpc_quantity(call["maxPriorityFeePerGas"]),
    )
    payee, asset = PAYEES[name], ASSETS[name]
    return {
        "header": {key: sim[key] for key in FIELDS},
        "timestamp_s": rpc_quantity(timestamp),
        "call_status": status,
        "gas": gas,
        "returned": outcome["returnData"],
        "logs": logs,
        "recipient": payee,
        "asset": asset,
        "recipient_transfer_delta": transfer_delta(logs, asset, payee),
        "l2_execution_fee_wei": gas * charged,
        "landed_gas_matches": gas == rpc_quantity(receipt["gasUsed"]),
        "landed_logs_match": logs == projection(receipt["logs"]),
        "landed_token_delta": transfer_delta(receipt["logs"], asset, payee),
    }


def run(root, client):
    cases = original_cases()
    proof, comparisons, versions = {}, {}, {}
    for source in ENDPOINTS:

        def call(name, method, args, source=source):
            number = len(proof[source]) + 1
            result, info = exchange(root, client, source, number, name, method, args)
            proof[source][name] = info
            return result

        proof[source] = {}
        assert rpc_quantity(call("chain", "eth_chainId", [])) == CHAIN
        for name, (receipt, original_parent, original_child, tx) in cases.items():
            parent = call(f"{name}-parent", "eth_getBlockByHash", [original_parent["hash"], False])
            child = call(f"{name}-child", "eth_getBlockByHash", [original_child["hash"], False])
            height = rpc_quantity(original_parent["number"])
            pms = validate_header(parent, height, original_parent["hash"])
            cms = validate_header(child, height + 1, original_child["hash"])
            assert cms - pms == 200 and (pms // 1_000 == cms // 1_000) == (name == "same")
            assert (pms, cms) == (
                parse_header_timestamp_ms(original_parent, require_denim=True),
                parse_header_timestamp_ms(original_child, require_denim=True),
            )
            args = [
                {
                    "blockStateCalls": [{"calls": [tx]}],
                    "traceTransfers": False,
                    "validation": True,
                },
                parent["number"],
            ]
            out = call(f"{name}-whole", "eth_simulateV1", args)
            comparison = interpret(name, out, parent, child, receipt, tx)
            if source == "official":
                comparisons[name] = comparison | {
                    "parent_ms": pms,
                    "child_ms": cms,
                    "landed_transaction_index": rpc_quantity(receipt["transactionIndex"]),
                }
            else:
                assert all(comparisons[name][k] == value for k, value in comparison.items())
        if source == "official":
            version = call("version", "web3_clientVersion", [])
            assert isinstance(version, str) and len(version) <= 250
            versions[source] = version
    with RpcBudget(root / "original.sqlite3", BUDGETS, read_only=True) as ledger:
        counts = ledger.counts()
    assert counts == {source: len(reads) for source, reads in proof.items()}
    return {
        "scope_sha256": digest(root / "scope.json"),
        "sources": ENDPOINTS,
        "reported_client_versions": versions,
        "comparisons": comparisons,
        "requests": counts,
        "wire": proof,
        "cost_usd": 0,
        "local_recent_reth_available": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--capture", action="store_true")
    args = parser.parse_args()
    root = args.root
    if args.capture:
        scope = frozen(datetime.now(UTC).isoformat())
        write(root / "scope.json", scope)
        with RpcBudget(root / "original.sqlite3", BUDGETS):
            pass
        try:
            with httpx.Client(timeout=6, follow_redirects=False, trust_env=False) as client:
                result = run(root, client)
        except (AssertionError, KeyError, OSError, ValueError, httpx.HTTPError) as exc:
            write(root / "STOP.json", {"fault": str(exc) or type(exc).__name__})
            raise
        write(root / "STOP.json", {"fault": None})
    else:
        assert read(root / "STOP.json") == {"fault": None}
        scope = read(root / "scope.json")
        assert scope == frozen(scope["frozen_utc"])
        result = run(root, None)
    print(json.dumps(result, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
