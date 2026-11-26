"""Audit the frozen three-day queue against original RPC bytes and block receipts."""

import argparse
import json
from collections import Counter
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from replay_eth_twenty_flow import load, receipt_leaf
from replay_funded_walk_cases import examine

from oasis_agent.evidence.nitro_block import indexed_trie_root
from oasis_agent.evidence.state_trie import prague_header
from oasis_agent.rpc.budget import BudgetPolicy, RpcBudget
from scripts.rank_archive_swaps import WIDTHS
from scripts.replay_weth_principal import analyze as analyze_principal
from scripts.screen_multihop_archive import event, summarize

SOURCES = {
    "header": "https://ethereum.publicnode.com",
    "receipts": "https://eth.nodely.dev",
    "trace": "https://eth.drpc.org",
}


def check_projection(root, case, pick):
    number, block, tx, index = case["height"], case["hash"], case["tx"], case["index"]
    header = load(root, f"{number}-header", "eth_getBlockByNumber", [hex(number), False])
    day = datetime.fromtimestamp(int(header["timestamp"], 16), UTC).date().isoformat()
    receipt = load(root, f"{number}-receipts", "eth_getBlockReceipts", [block])[index]
    sequence = [
        event(
            {
                "sig": log["topics"][0],
                "topic1": log["topics"][1],
                "log_index": int(log["logIndex"], 16),
                "address": log["address"],
                "data": log["data"],
            }
        )
        for log in receipt["logs"]
        if log["topics"] and log["topics"][0] in WIDTHS
    ]
    candidate, _ = summarize((day, number, block, tx, index), sequence, 1)
    assert (day, candidate.ordered_family, candidate.events, candidate.distinct_venues) == (
        pick["day"],
        pick["ordered_family"],
        pick["events"],
        pick["distinct_venues"],
    )


def unresolved_trace(root, height, block_hash, tx, index, pick, reasons):
    header = load(root, f"{height}-header", "eth_getBlockByNumber", [hex(height), False])
    prague_header(header, height, block_hash)
    receipts = load(root, f"{height}-receipts", "eth_getBlockReceipts", [block_hash])
    assert header["transactions"] == [r["transactionHash"] for r in receipts]
    assert all(
        r["blockHash"] == block_hash and int(r["transactionIndex"], 16) == n
        for n, r in enumerate(receipts)
    )
    receipt = receipts[index]
    assert header["transactions"][index] == tx and int(receipt["status"], 16) == 1
    assert (
        "0x" + indexed_trie_root([receipt_leaf(r) for r in receipts]).hex()
        == header["receiptsRoot"]
    )
    gas = int(receipt["cumulativeGasUsed"], 16) - (
        int(receipts[index - 1]["cumulativeGasUsed"], 16) if index else 0
    )
    assert gas == int(receipt["gasUsed"], 16)
    trace = load(
        root,
        tx[2:14] + "-trace",
        "debug_traceTransaction",
        [tx, {"tracer": "callTracer", "tracerConfig": {"withLog": True}, "timeout": "10s"}],
    )
    assert (trace["from"], trace["to"]) == (receipt["from"], receipt["to"])
    frames = [trace]
    ops = Counter()
    while frames:
        f = frames.pop()
        ops[f["type"]] += 1
        frames.extend(f.get("calls", []))
    assert {"CREATE2", "SELFDESTRUCT"} <= ops.keys()
    stems = [f"{height}-header", f"{height}-receipts", tx[2:14] + "-trace"]
    wires = {}
    for stem in stems:
        m = json.loads((root / (stem + ".meta.json")).read_bytes())
        assert m["source"] == SOURCES[stem.rsplit("-", 1)[1]]
        wires[stem] = {
            k: m[k] for k in ("source", "request_sha256", "response_sha256", "completed_utc")
        }
    row = {
        "chain_id": 1,
        "height": height,
        "hash": block_hash,
        "index": index,
        "tx": tx,
        "sender": receipt["from"],
        "executor": receipt["to"],
        "receipt_count": len(receipts),
        "receipt_root": header["receiptsRoot"],
        "logs": len(receipt["logs"]),
        "frames": sum(ops.values()),
        "opcodes": dict(ops),
        "trace_status": "unsupported trace opcode",
        "archive_events": pick["events"],
        "funded_depth_lower_bound": None,
        "longest_path_logs": None,
        "gas_used": gas,
        "gas_paid_wei": str(gas * int(receipt["effectiveGasPrice"], 16)),
        "direct_fee_recipient_wei": None,
        "combined_visible_eth_weth_after_gas_wei": None,
        "other_erc20_visible_deltas": None,
        "selected_by": reasons,
        "wires": wires,
    }
    check_projection(root, row, pick)
    return row


def replay(root):
    docs = Path(__file__).resolve().parents[1]
    raw = (docs / "MULTIHOP_FAMILY_SCREEN_20261127.json").read_bytes()
    seed = json.loads(raw)
    queue = seed["audit_queue"]["transactions"]
    scope = json.loads((root / "scope.json").read_bytes())
    assert scope["cases"] == queue and scope["selection_sha256"] == sha256(raw).hexdigest()
    chosen = {}
    for day in seed["days"].values():
        for group in [day["closest"], *(v["selected"] for v in day["bands"].values())]:
            chosen.update((r["tx"], r) for r in group)
    complete = [json.loads(line) for line in (root / "completed.jsonl").read_text().splitlines()]
    assert complete == [
        [c["block"], c["block_hash"], c["tx"], c["index"]] for c in queue[: len(complete)]
    ]
    totals = Counter()
    rows = []
    for n, h, tx, idx in complete:
        try:
            case = examine(root, n, idx, tx, "")
        except ValueError as exc:
            if str(exc) != "unsupported trace opcode":
                raise
            rows.append(
                unresolved_trace(root, n, h, tx, idx, chosen[tx], queue[len(rows)]["selected_by"])
            )
            totals["unsupported_trace"] += 1
            continue
        totals["trace_reconciled"] += 1
        stem = tx[2:14] + "-trace"
        principal = analyze_principal(
            block_raw=(root / f"{n}-header.response.json").read_bytes(),
            receipts_raw=(root / f"{n}-receipts.response.json").read_bytes(),
            call_trace_request=(root / (stem + ".request.json")).read_bytes(),
            call_trace_raw=(root / (stem + ".response.json")).read_bytes(),
        )
        groups = [u["proof"] for u in principal["unlocks"] if u["proof"] is not None]
        totals["tested_v4_unlocks"] += principal["observed_unlocks"]
        totals["verified_weth_principal_groups"] += len(groups)
        totals["transactions_with_verified_weth_principal"] += bool(groups)
        assert case["hash"] == h
        check_projection(root, case, chosen[tx])
        for stem, m in case["wires"].items():
            assert m["source"] == SOURCES[stem.rsplit("-", 1)[1]]
        depth = len(case["path"])
        native = int(case["combined_visible_eth_weth_after_gas_wei"])
        mixed = bool(case["other_erc20_visible_deltas"])
        totals["with_direct_payment"] += int(case["direct_fee_recipient_wei"]) > 0
        totals["with_other_erc20_deltas"] += mixed
        totals["positive_visible_eth_weth_no_other_erc20_delta"] += native > 0 and not mixed
        totals["nonpositive_visible_eth_weth_no_other_erc20_delta"] += native <= 0 and not mixed
        rows.append(
            {
                k: case[k]
                for k in (
                    "chain_id",
                    "height",
                    "hash",
                    "index",
                    "tx",
                    "executor",
                    "sender",
                    "receipt_count",
                    "receipt_root",
                    "logs",
                    "frames",
                    "unresolved",
                    "resolved",
                    "gas_used",
                    "gas_price_wei",
                    "gas_paid_wei",
                    "direct_fee_recipient_wei",
                    "combined_visible_eth_weth_after_gas_wei",
                    "other_erc20_visible_deltas",
                    "wires",
                )
            }
            | {
                "trace_status": "receipt_logs_matched",
                "observed_v4_unlocks": principal["observed_unlocks"],
                "verified_weth_principal_groups": groups,
                "unproved_unlocks_by_reason": principal["unproved_unlocks_by_reason"],
                "archive_events": chosen[tx]["events"],
                "funded_depth_lower_bound": depth or None,
                "longest_path_logs": [r["log"] for r in case["path"]],
                "selected_by": queue[len(rows)]["selected_by"],
            }
        )
    policy = tuple(BudgetPolicy(lane, 40, 0) for lane in SOURCES)
    with RpcBudget(root / "original.sqlite3", policy, read_only=True) as b:
        reservations = b.counts()
    attempts = {lane: len(list(root.glob("*-" + lane + ".meta.json"))) for lane in SOURCES}
    assert reservations == attempts
    stops = []
    for p in root.glob("stop-*.json"):
        meta = json.loads(p.read_bytes())
        lane = p.stem.split("-", 1)[1]
        assert meta["source"] == SOURCES[lane] and meta["failure"]
        assert len(complete) < len(queue)
        pending = queue[len(complete)]
        base = (pending["tx"][2:14] if lane == "trace" else str(pending["block"])) + "-" + lane
        assert p.read_bytes() == (root / (base + ".meta.json")).read_bytes()
        for side in ("request", "response"):
            fn = root / (base + "." + side + ".json")
            assert (
                sha256(fn.read_bytes() if fn.exists() else b"").hexdigest()
                == meta[side + "_sha256"]
            )
        stops.append(
            {
                "tx": pending["tx"],
                "lane": lane,
                "failure": meta["failure"],
                "raw": {
                    k: meta[k]
                    for k in ("source", "completed_utc", "request_sha256", "response_sha256")
                },
            }
        )
    assert (len(queue) == len(complete) and not stops) or len(stops) == 1
    return {
        "schema": "oasis-retained-historical-trace-queue-v1",
        "scope_sha256": sha256((root / "scope.json").read_bytes()).hexdigest(),
        "chain_id": 1,
        "selected": len(queue),
        "verified": len(rows),
        "reserved_free_rpcs": reservations,
        "stops": stops,
        "totals": dict(totals),
        "samples": rows,
    }


if __name__ == "__main__":
    if not __debug__:
        raise RuntimeError("evidence verification requires assertions")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scope", type=Path)
    print(json.dumps(replay(parser.parse_args().scope), sort_keys=True, indent=2))
