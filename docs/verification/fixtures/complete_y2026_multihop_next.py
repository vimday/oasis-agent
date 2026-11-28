"""Complete checks for the 2026 settlement expansion without repeating collected RPCs."""

import argparse
import json
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from replay_y2026_multihop_next import (
    INITIAL,
    NETWORKS,
    Lane,
    case,
    digest,
    keys,
    read,
    receipt_paths,
    reconcile,
    scope_for,
    summarize,
    unique_block_order,
)

from oasis_agent.arbitrage.cycle_protocols import calldata, data_bytes
from oasis_agent.evidence.public_receipts import quantity as q
from oasis_agent.models.common import canonical_json
from scripts.capture_public_rpc import durable_new

RECEIPTS = INITIAL.with_name("y2026-multihop-next-receipts-20261128")
PINNED_SOURCE = RECEIPTS / "nitro-v3.9.6-receipt.go"
SOURCE_SHA = "aa5dd0395f8518710f3b264e7246daf9311e7aff9362f4d8004ea1328c625e1a"


def scope_for_followup(frozen):
    assert digest(PINNED_SOURCE) == SOURCE_SHA
    return {
        "schema": "oasis.2026-multihop-next-exact-followup.v1",
        "frozen_utc": frozen,
        "driver_sha256": digest(Path(__file__)),
        "prior_report_sha256": digest(RECEIPTS / "stdout.json"),
        "prior_scope_sha256": digest(RECEIPTS / "scope.json"),
        "nitro_v3_9_6_receipt_source_sha256": SOURCE_SHA,
        "minimum_interval_ms": 600,
        "budgets": {"trace": 1, "audit": 4, "operator": 2},
        "selection": "reuse original senders, state diffs and receipts; acquire unrequested proofs",
        "rpc_spending_usd": 0,
    }


def arbitrum(root, scope, capture, arb, prior_scope):
    topic_groups = defaultdict(list)
    w = NETWORKS["arbitrum"]
    eligible = {r["tx"] for r in arb["candidate_keys"]}
    for window in arb["windows"]:
        raw = read(INITIAL / "arbitrum/scan" / f"logs-{window['first']}.response")["result"]
        for event in raw:
            if event["transactionHash"] in eligible:
                topic_groups[event["transactionHash"]].append(event)
    picks = list(unique_block_order(topic_groups, prior_scope["max_selected_blocks_per_l2"]))
    assert arb["audit"]["failure"] == "unsupported_nitro_receipt_type_or_position"
    first = len(arb["verified_samples"])
    n, hash_, _, tx, *_ = keys(picks[first][0])
    assert arb["audit"]["rows"][-1]["label"] == f"r{n}"
    local = RECEIPTS / "arbitrum/audit"
    header, rows = (read(local / f"{p}{n}.response")["result"] for p in ("h", "r"))
    assert header["hash"] == hash_
    add = [case("arbitrum", w["chain"], w["weth"], header, rows, tx, topic_groups, prior_scope)]
    unrelated = [r["transactionHash"] for r in rows if q(r["type"]) == 4]
    assert unrelated and tx not in unrelated
    old_files = {p.name for p in local.glob("*.request")}

    def remaining(ask):
        for events in picks[first + 1 :]:
            height, block_hash, _, tx, *_ = keys(events[0])
            assert not old_files & {f"h{height}.request", f"r{height}.request"}
            head = ask(f"h{height}", "eth_getBlockByHash", [block_hash, False])
            receipts = ask(f"r{height}", "eth_getBlockReceipts", [block_hash])
            assert (q(head["number"]), head["hash"]) == (height, block_hash)
            add.append(
                case(
                    "arbitrum", w["chain"], w["weth"], head, receipts, tx, topic_groups, prior_scope
                )
            )

    evidence = Lane(root / "audit", "audit", w["audit"], capture, scope).run(remaining)
    return {"additional_verified_samples": add, "unrelated_type4": unrelated}, evidence


def ethereum(root, scope, capture, eth, prior_scope):
    ranked = [
        row
        for row in eth["candidate_keys"]
        if row["best_wrapped_close_less_receipt_fee_wei"] is not None
    ]
    top = max(ranked, key=lambda row: row["best_wrapped_close_less_receipt_fee_wei"])
    assert eth["trace"]["failure"] == "callTracer logs do not cover receipt"
    old = RECEIPTS / "ethereum/trace"
    envelope = read(old / "envelope.response")["result"]
    diff = read(old / "diff.response")["result"]
    txid, height = top["tx"], top["height"]
    assert envelope["hash"] == txid
    header = read(INITIAL / "ethereum/scan" / f"h{height}.response")["result"]
    receipt_path = next(
        p
        for location in (INITIAL, RECEIPTS)
        if (p := location / "ethereum/audit" / f"r{height}.response").exists()
    )
    rows = read(receipt_path)["result"]
    receipt = next(row for row in rows if row["transactionHash"] == txid)
    graph = receipt_paths(receipt, prior_scope["index_topics"])
    actual = {}

    def trace(ask):
        args = [txid, {"tracer": "callTracer", "tracerConfig": {"withLog": True}}]
        calls = ask("calls-with-logs", "debug_traceTransaction", args)
        actual[txid] = reconcile(
            "ethereum",
            1,
            envelope,
            (receipt, header, graph),
            {f"{txid}-calls": calls, f"{txid}-diff": diff},
        )

    source = NETWORKS["ethereum"]["trace"]
    evidence = Lane(root / "trace", "trace", source, capture, scope).run(trace)
    return actual, evidence


def optimism(root, scope, capture, op, _):
    weth = NETWORKS["optimism"]["weth"]

    def weth_delta(sample):
        return sum(
            r["transfer_delta_raw"]
            for r in sample["payer_recipient_erc20_deltas"]
            if r["token"] == weth
        )

    ordered = sorted(
        op["verified_samples"],
        key=lambda row: weth_delta(row) - row["fee"]["known_network_fee_wei"],
        reverse=True,
    )
    fees = []

    def verify(ask):
        oracle = "0x420000000000000000000000000000000000000f"
        for sample in ordered[:2]:
            gas = sample["fee"]["gas_units"]
            tx, height = sample["tx"], sample["block"]["height"]
            pin = {"blockHash": sample["block"]["hash"], "requireCanonical": True}
            call = {"to": oracle, "data": calldata("getOperatorFee(uint256)", ("uint256",), (gas,))}
            raw = data_bytes(ask(f"opfee-{height}", "eth_call", [call, pin]))
            assert len(raw) == 32
            operator = int.from_bytes(raw)
            net = weth_delta(sample) - sample["fee"]["known_network_fee_wei"] - operator
            fees.append(
                {
                    "tx": tx,
                    "operator_fee_wei": operator,
                    "conditional_weth_delta_less_fee_wei": net,
                }
            )

    source = NETWORKS["optimism"]["scan"]
    evidence = Lane(root / "operator", "operator", source, capture, scope).run(verify)
    return fees, evidence


def run(root, scope, capture):
    prior_scope = read(RECEIPTS / "scope.json")
    driver = digest(RECEIPTS / "capture.py")
    assert prior_scope == scope_for(prior_scope["frozen_utc"], driver)
    prior = summarize(RECEIPTS, prior_scope, False)
    assert json.loads(json.dumps(prior)) == read(RECEIPTS / "stdout.json")

    def complete(job):
        key = job.__name__
        data, evidence = job(root, scope, capture, prior["chains"][key], prior_scope)
        return key, {"result": data, "proof": evidence}

    with ThreadPoolExecutor(max_workers=3) as pool:
        added = dict(pool.map(complete, (arbitrum, ethereum, optimism)))
    return prior | {
        "followup_scope_sha256": digest(root / "scope.json"),
        "followup": added,
        "rpc_reservations": prior["rpc_reservations"]
        + sum(sum(item["proof"]["counts"].values()) for item in added.values()),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "capture", "replay"))
    parser.add_argument("root", type=Path)
    a = parser.parse_args()
    path = a.root / "scope.json"
    if a.mode == "prepare":
        durable_new(path, canonical_json(scope_for_followup(datetime.now(UTC).isoformat())))
        return
    scope = read(path)
    assert scope == scope_for_followup(scope["frozen_utc"])
    assert digest(a.root / "capture.py") == scope["driver_sha256"]
    print(json.dumps(run(a.root, scope, a.mode == "capture"), sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
