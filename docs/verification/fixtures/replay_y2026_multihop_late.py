"""Verify new Robinhood and Gnosis 2026 market settlements from public receipt roots."""

import argparse
import csv
import gzip
import json
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from replay_y2026_bsc_rh_landed import RH_WETH, keys, matches, rooted
from replay_y2026_eth_bsc_untouched import PATH_TOPICS
from replay_y2026_l2_fresh import Lane, digest, read, unique_block_order, verify_head
from replay_y2026_multihop_next import BANDS, CATALOG, DOCS, TOPICS
from replay_y2026_op_arb import receipt_commitment
from replay_y2026_open_tail import receipt_paths, row_roles

from oasis_agent.evidence.public_receipts import quantity as q
from oasis_agent.models.common import canonical_json
from scripts.capture_public_rpc import durable_new

FRESH = DOCS / "Y2026_L2_FRESH_20261128.json"
EXPANDED = DOCS / "Y2026_MULTICHAIN_EXPANDED_20261128.json"
HOLDOUT = DOCS / "Y2026_L2_FUNDING_HOLDOUT_20261128.json"
NETWORKS = {
    "robinhood": {
        "chain": 4663,
        "scan": "https://rpc.mainnet.chain.robinhood.com",
        "audit": "https://robinhood-rpc.publicnode.com",
        "width": 128,
        "offsets": [181, 1591, 5339, 12391, 21941],
        "native_currency": "ETH",
        "wrapped_native": RH_WETH,
    },
    "gnosis": {
        "chain": 100,
        "scan": "https://rpc.gnosischain.com",
        "audit": "https://gnosis-rpc.publicnode.com",
        "width": 72,
        "offsets": [349, 3827, 12017, 42967, 170501],
        "native_currency": "xDAI",
        "wrapped_native": "0xe91d153e0b41518a2ce8dd3d7944fa863463a97d",
    },
}


def plan(frozen):
    return {
        "schema": "oasis.2026-multihop-late.v1",
        "frozen_utc": frozen,
        "driver_sha256": digest(Path(__file__)),
        "references": {p.name: digest(p) for p in (CATALOG, TOPICS, FRESH, EXPANDED, HOLDOUT)},
        "networks": NETWORKS,
        "index_topics": {t: PATH_TOPICS.get(t, "shape_only") for t in read(TOPICS)["topics"]},
        "minimum_interval_ms": 600,
        "budgets": {"scan": 17, "audit": 13},
        "rank": "event count descending, distinct emitters, chronological ties, one per block",
        "audit_max": 6,
        "rpc_spending_usd": 0,
        "retry": False,
    }


def earlier(name):
    with gzip.open(CATALOG, "rt") as source:
        known = {
            row["tx_hash"] for row in csv.DictReader(source, delimiter="\t") if row["chain"] == name
        }
    if name != "robinhood":
        return known, []
    datasets = (
        read(FRESH)["chains"][name],
        read(EXPANDED)["lanes"][name],
        read(HOLDOUT)["chains"][name],
    )
    known.update(row["tx"] for row in datasets[0]["verified_samples"])
    known.update(row["transaction"] for row in datasets[1]["all_multi_event"])
    known.update(row[0] for row in datasets[2]["multi_event_keys"])
    ranges = [(w["first"], w["last"]) for source in datasets for w in source["windows"]]
    return known, ranges


def audited(block, receipts, groups, tx, net, topics):
    chain, height = net["chain"], q(block["number"])
    events = groups[tx]
    idx = q(events[0]["transactionIndex"])
    if chain == 4663:
        commitment = rooted(chain, block, receipts)["receipts_root"]
    else:
        _, commitment = receipt_commitment(chain, block, receipts)
    r = receipts[idx]
    assert r["transactionHash"] == tx and r["blockHash"] == block["hash"]
    assert [keys(log) for log in r["logs"] if matches(log, topics)] == list(map(keys, events))
    gas, price = q(r["gasUsed"]), q(r["effectiveGasPrice"])
    native_fee = gas * price
    parent_gas = q(r["gasUsedForL1"]) if chain == 4663 else 0
    assert 0 <= parent_gas <= gas
    assert q(r["status"]) == 1
    f = receipt_paths(r, topics)
    weth = net["wrapped_native"]
    closed = f["closed_at_same_holder"]
    return {
        "tx": tx,
        "timestamp_utc": verify_head(chain, block),
        "height": height,
        "hash": block["hash"],
        "index": idx,
        "receipts_root": commitment,
        "receipts_in_block": len(receipts),
        "reverts_in_block": sum(q(item["status"]) == 0 for item in receipts),
        "market_events": len(events),
        "network_fee": {
            "amount_native_wei": native_fee,
            "asset": net["native_currency"],
            "gas_used": gas,
            "gas_price": price,
            "included_l1_wei": parent_gas * price,
        },
        "payer": r["from"],
        "executor": r["to"],
        "funding": f,
        "role_token_amounts": row_roles(r),
        "conditional_wrapped_cycle_net": [
            c | {"gross_less_entire_network_fee_wei": c["gross_raw"] - native_fee}
            for c in closed
            if c["asset"] == weth
        ],
        "owner_all_cost_net": None,
    }


def network(root, name, scope, capture):
    cfg = scope["networks"][name]
    chain = cfg["chain"]
    topics = scope["index_topics"]
    location = root / name
    if capture:
        location.mkdir()
    known, prior_ranges = earlier(name)
    windows = []
    cases = []
    groups = defaultdict(list)

    def scan(ask):
        assert q(ask("chain", "eth_chainId", [])) == chain
        head = ask("anchor", "eth_getBlockByNumber", ["latest", False])
        verify_head(chain, head)
        now = q(head["number"])
        for n in cfg["offsets"]:
            hi = now - n
            lo = hi - cfg["width"] + 1
            assert all(hi < x or lo > y for x, y in prior_ranges)
            left, right = [ask(f"h{x}", "eth_getBlockByNumber", [hex(x), False]) for x in (lo, hi)]
            assert (q(left["number"]), q(right["number"])) == (lo, hi)
            dates = [verify_head(chain, h) for h in (left, right)]
            hashes = {lo: left["hash"], hi: right["hash"]}
            filters = {"fromBlock": hex(lo), "toBlock": hex(hi), "topics": [sorted(topics)]}
            raw = ask(f"logs-{lo}", "eth_getLogs", [filters])
            positions = []
            for event in raw:
                h, hash_, idx, _tx, seq, *_ = keys(event)
                assert lo <= h <= hi and hashes.setdefault(h, hash_) == hash_
                assert event["removed"] is False and matches(event, topics)
                positions.append((h, idx, seq))
            assert positions == sorted(set(positions))
            for event in raw:
                if event["transactionHash"] not in known:
                    groups[event["transactionHash"]].append(event)
            windows.append(
                {
                    "first": lo,
                    "last": hi,
                    "hashes": [left["hash"], right["hash"]],
                    "utc": dates,
                    "market_logs": len(raw),
                }
            )

    def audit(ask):
        assert q(ask("chain", "eth_chainId", [])) == chain
        for events in unique_block_order(groups, scope["audit_max"]):
            h, hash_, _, tx, *_ = keys(events[0])
            block = ask(f"h{h}", "eth_getBlockByHash", [hash_, False])
            assert (q(block["number"]), block["hash"]) == (h, hash_)
            receipts = ask(f"r{h}", "eth_getBlockReceipts", [hash_])
            cases.append(audited(block, receipts, groups, tx, cfg, topics))

    coverage = Lane(location / "scan", "scan", cfg["scan"], capture, scope).run(scan)
    evidence = (
        Lane(location / "audit", "audit", cfg["audit"], capture, scope).run(audit)
        if groups
        else None
    )
    distribution = Counter(map(len, groups.values()))
    return {
        "chain_id": chain,
        "blocks_screened": cfg["width"] * len(windows),
        "windows": windows,
        "at_least": {k: sum(v for n, v in distribution.items() if n >= k) for k in BANDS},
        "max_market_events": max(distribution, default=0),
        "candidates": [
            {"tx": tx, "height": q(events[0]["blockNumber"]), "market_events": len(events)}
            for tx, events in groups.items()
            if len(events) >= 2
        ],
        "verified": cases,
        "scan": coverage,
        "audit": evidence,
    }


def run(root, scope, capture):
    def lane(name):
        return name, network(root, name, scope, capture)

    with ThreadPoolExecutor(max_workers=len(NETWORKS)) as pool:
        result = dict(pool.map(lane, NETWORKS))
    reserved = sum(
        sum(item["counts"].values())
        for row in result.values()
        for name in ("scan", "audit")
        if (item := row[name])
    )
    return {
        "scope_sha256": digest(root / "scope.json"),
        "chains": result,
        "rpc_reservations": reserved,
        "rpc_spending_usd": 0,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "capture", "replay"))
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    path = args.root / "scope.json"
    if args.action == "prepare":
        durable_new(path, canonical_json(plan(datetime.now(UTC).isoformat())))
        return
    scope = read(path)
    assert scope == plan(scope["frozen_utc"])
    assert digest(args.root / "capture.py") == scope["driver_sha256"]
    print(json.dumps(run(args.root, scope, args.action == "capture"), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
