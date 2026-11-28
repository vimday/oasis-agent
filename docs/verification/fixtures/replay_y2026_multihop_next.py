"""Expand 2026 Ethereum, Optimism and Arbitrum settlement samples from public chain data."""

import argparse
import csv
import gzip
import json
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path

from replay_y2026_bsc_rh_landed import keys, matches
from replay_y2026_eth_bsc_fresh import reconcile
from replay_y2026_eth_bsc_untouched import PATH_TOPICS
from replay_y2026_l2_fresh import Lane, case, digest, read, unique_block_order, verify_head
from replay_y2026_op_arb import receipt_commitment
from replay_y2026_open_tail import receipt_paths, row_roles

from oasis_agent.evidence.public_receipts import quantity as q
from oasis_agent.models.common import canonical_json
from oasis_agent.rpc.budget import BudgetPolicy, RpcBudget
from scripts.capture_public_rpc import durable_new
from scripts.measure_robinhood_quote_delivery import _body, _read

ROOT = Path(__file__).resolve().parents[3]
DOCS = ROOT / "docs/verification"
CATALOG = ROOT / "reports/research/gold-book-20261127/all-hashes.tsv.gz"
TOPICS = ROOT / "reports/research/y2026-bsc-rh-landed-20261127/scope.json"
INITIAL = ROOT / "reports/research/y2026-multihop-next-20261128"
PRIOR_REPORTS = [
    DOCS / f"{name}.json"
    for name in (
        "Y2026_MULTICHAIN_EXPANDED_20261128",
        "Y2026_ETH_BSC_FRESH_20261128",
        "Y2026_L2_FRESH_20261128",
        "Y2026_L2_FUNDING_HOLDOUT_20261128",
        "Y2026_OPEN_TAIL_CASH_20261127",
    )
]
NETWORKS = {
    "ethereum": {
        "chain": 1,
        "width": 4,
        "offsets": [3927, 19943, 338703, 1227303],
        "scan": "https://ethereum-rpc.publicnode.com",
        "audit": "https://eth.nodely.dev",
        "trace": "https://eth.drpc.org",
        "weth": "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
    },
    "optimism": {
        "chain": 10,
        "width": 80,
        "offsets": [3441, 15563, 34387, 57623, 83271],
        "scan": "https://mainnet.optimism.io",
        "audit": "https://optimism.drpc.org",
        "weth": "0x4200000000000000000000000000000000000006",
    },
    "arbitrum": {
        "chain": 42161,
        "width": 224,
        "offsets": [8171, 26873, 58719, 130213, 233387],
        "scan": "https://arb1.arbitrum.io/rpc",
        "audit": "https://arbitrum-one-rpc.publicnode.com",
        "weth": "0x82af49447d8a07e3bd95bd0d56f35241523fbab1",
    },
}
BANDS = (2, 3, 5, 10, 20, 50, 100, 200)


def scope_for(frozen, driver=None):
    old = read(INITIAL / "scope.json")
    assert digest(INITIAL / "capture.py") == old["driver_sha256"]
    symbols = {topic: PATH_TOPICS.get(topic, "shape_only") for topic in read(TOPICS)["topics"]}
    assert set(symbols) == set(old["index_topics"])
    return {
        "schema": "oasis.2026-multihop-next.v1",
        "frozen_utc": frozen,
        "driver_sha256": driver or digest(Path(__file__)),
        "reference_sha256": {
            str(p.relative_to(ROOT)): digest(p) for p in [CATALOG, TOPICS, *PRIOR_REPORTS]
        },
        "initial_scope_sha256": digest(INITIAL / "scope.json"),
        "initial_stop_log_sha256": digest(INITIAL / "stderr.log"),
        "index_topics": symbols,
        "networks": NETWORKS,
        "minimum_interval_ms": 600,
        "budgets": {"audit": 15, "trace": 4},
        "max_selected_blocks_per_l2": 6,
        "selection": {
            "ethereum": "one each by market event count, linked depth, best WETH closure minus gas",
            "l2": "top event-count receipts from distinct blocks, ties by distinct venues",
        },
        "acceptance": "hash-bound headers, ordered full receipt root or independent indexed events",
        "rpc_spending_usd": 0,
        "retry": False,
    }


def known():
    txs = {name: set() for name in NETWORKS}
    ranges = {name: set() for name in NETWORKS}
    with gzip.open(CATALOG, "rt") as source:
        for row in csv.DictReader(source, delimiter="\t"):
            if row["chain"] in txs:
                txs[row["chain"]].add(row["tx_hash"])
    expanded, fresh_eth, fresh_l2, holdout, open_tail = map(read, PRIOR_REPORTS)
    for name in NETWORKS:
        row = expanded["lanes"][name]
        indexed = row["all_multi_event"]
        txs[name].update(indexed if name == "ethereum" else (x["transaction"] for x in indexed))
        ranges[name].update((h["height"], h["height"]) for h in row.get("blocks", []))
        ranges[name].update((w["first"], w["last"]) for w in row.get("windows", []))
        if name == "ethereum":
            row = fresh_eth["chains"][name]
            txs[name].update(c["tx"] for c in row["candidates"])
            ranges[name].update((h["height"], h["height"]) for h in row["blocks"])
        else:
            now = fresh_l2["chains"][name]
            txs[name].update(r["tx"] for r in now["verified_samples"])
            ranges[name].update((w["first"], w["last"]) for w in now["windows"])
            old = holdout["chains"][name]
            txs[name].update(row[0] for row in old["multi_event_keys"])
            tail = open_tail["chains"][name]
            txs[name].update(row["tx"] for row in tail["rows"])
            for prior in (old, tail):
                ranges[name].update((w["first"], w["last"]) for w in prior["windows"])
    return txs, ranges


def ranges_for(anchor, spec, seen_ranges):
    result = [(anchor - n - spec["width"] + 1, anchor - n) for n in spec["offsets"]]
    assert all(first <= last for first, last in result)
    assert all(
        last < old_first or first > old_last
        for first, last in result
        for old_first, old_last in seen_ranges
    ), "scope overlaps earlier block windows"
    return result


def thresholds(counts):
    return {band: sum(v for n, v in counts.items() if n >= band) for band in BANDS}


def locate(ask, spec, prior_ranges):
    chain = spec["chain"]
    assert q(ask("chain", "eth_chainId", [])) == chain
    h = ask("anchor", "eth_getBlockByNumber", ["latest", False])
    verify_head(chain, h)
    return ranges_for(q(h["number"]), spec, prior_ranges)


def reuse_asks(root, lane, endpoint, action, scope, capture):
    """Reopen successful original calls before requesting any still-unrequested receipts."""
    archived = INITIAL / root.name / lane
    original = read(INITIAL / "scope.json")
    policy = (BudgetPolicy(lane, original["budgets"][lane], original["minimum_interval_ms"]),)
    with RpcBudget(archived / "original.sqlite3", policy, read_only=True) as ledger:
        used = ledger.counts()[lane]
    assert len(list(archived.glob("*.request"))) == used
    seen = []

    def run(ask):
        def from_archive(label, method, args):
            if len(seen) == used:
                assert not (archived / label).with_suffix(".request").exists()
                return ask(label, method, args)
            i = len(seen) + 1
            prefix = archived / label
            body = _body(i, method, args)
            val, _ = _read(prefix, body, i, 1)
            seen.append(
                {"label": label, "method": method, "meta": read(prefix.with_suffix(".meta"))}
            )
            return val

        action(from_archive)

    current = Lane(root / lane, lane, endpoint, capture, scope).run(run)
    assert len(seen) == used
    return current | {"reused_in_scope": used, "reused_calls": seen}


def eth_summary(scope, root, spec, seen, prior_ranges, capture):
    chain = spec["chain"]
    topics = scope["index_topics"]
    heads = {}
    windows = []
    cases = {}
    totals = Counter()
    hits = []

    def headers(ask):
        for lo, hi in locate(ask, spec, prior_ranges):
            row = []
            for height in range(lo, hi + 1):
                h = ask(f"h{height}", "eth_getBlockByNumber", [hex(height), False])
                assert q(h["number"]) == height
                verify_head(chain, h)
                row.append(h)
            assert all(right["parentHash"] == left["hash"] for left, right in pairwise(row))
            for h in row:
                heads[q(h["number"])] = h
            windows.append(
                {
                    "first": lo,
                    "last": hi,
                    "first_hash": row[0]["hash"],
                    "last_hash": row[-1]["hash"],
                }
            )

    def receipts(ask):
        assert q(ask("chain", "eth_chainId", [])) == chain
        for height, h in heads.items():
            block_rows = ask(f"r{height}", "eth_getBlockReceipts", [h["hash"]])
            _, committed = receipt_commitment(chain, h, block_rows)
            totals["roots"] += 1
            totals["receipts"] += len(block_rows)
            for r in block_rows:
                if q(r["status"]) == 0:
                    assert not r["logs"]
                    totals["reverts"] += 1
                    totals["revert_fee_wei"] += q(r["gasUsed"]) * q(r["effectiveGasPrice"])
                events = sum(matches(log, topics) for log in r["logs"])
                if events < 2 or r["transactionHash"] in seen:
                    continue
                g = receipt_paths(r, topics)
                net_fee = q(r["gasUsed"]) * q(r["effectiveGasPrice"])
                pool_base = max(
                    (
                        c["gross_raw"] - net_fee
                        for c in g["closed_at_same_holder"]
                        if c["asset"] == spec["weth"]
                    ),
                    default=None,
                )
                tx = r["transactionHash"]
                hits.append(
                    {
                        "tx": tx,
                        "height": height,
                        "events": events,
                        "linked_count": len(g["longest_linked"]),
                        "best_wrapped_close_less_receipt_fee_wei": pool_base,
                    }
                )
                cases[tx] = (r, h, g, committed)

    original = read(INITIAL / "scope.json")
    scan = Lane(INITIAL / root.name / "scan", "scan", spec["scan"], False, original).run(headers)
    audit = reuse_asks(root, "audit", spec["audit"], receipts, scope, capture) if heads else None
    selected = []
    if hits:
        top = max(hits, key=lambda r: (r["events"], r["linked_count"], -r["height"]))
        linked = max(hits, key=lambda r: (r["linked_count"], r["events"], -r["height"]))
        ranked = [r for r in hits if r["best_wrapped_close_less_receipt_fee_wei"] is not None]
        economic = max(
            ranked, key=lambda r: r["best_wrapped_close_less_receipt_fee_wei"], default=None
        )
        selected = list(dict.fromkeys(item["tx"] for item in (top, linked, economic) if item))

    traced, trace = {}, None
    if hits and economic:
        tx = economic["tx"]
        r, h, g, _ = cases[tx]

        def trace_one(ask):
            assert q(ask("chain", "eth_chainId", [])) == chain
            envelope = ask("envelope", "eth_getTransactionByHash", [tx])
            call = ask("calls", "debug_traceTransaction", [tx, {"tracer": "callTracer"}])
            diff = ask(
                "diff",
                "debug_traceTransaction",
                [tx, {"tracer": "prestateTracer", "tracerConfig": {"diffMode": True}}],
            )
            traced[tx] = reconcile(
                "ethereum", chain, envelope, (r, h, g), {f"{tx}-calls": call, f"{tx}-diff": diff}
            )

        trace = Lane(root / "trace", "trace", spec["trace"], capture, scope).run(trace_one)
    return {
        "windows": windows,
        "blocks_screened": totals["roots"],
        "coverage": totals,
        "at_least": thresholds(Counter(row["events"] for row in hits)),
        "candidate_keys": hits,
        "verified_samples": [
            {
                "tx": tx,
                "block": h["hash"],
                "height": q(h["number"]),
                "block_timestamp": q(h["timestamp"]),
                "receipts_root": committed,
                "funding": g,
                "network_fee_wei": q(r["gasUsed"]) * q(r["effectiveGasPrice"]),
                "role_token_movements": row_roles(r),
                "trace": traced.get(tx),
            }
            for tx in selected
            for r, h, g, committed in (cases[tx],)
        ],
        "scan": scan,
        "audit": audit,
        "trace": trace,
    }


def l2_summary(scope, root, name, spec, seen, prior_ranges, capture):
    chain = spec["chain"]
    topics = scope["index_topics"]
    groups = defaultdict(list)
    windows = []
    verified = []

    def discover(ask):
        for lo, hi in locate(ask, spec, prior_ranges):
            lower, upper = [ask(f"h{n}", "eth_getBlockByNumber", [hex(n), False]) for n in (lo, hi)]
            assert (q(lower["number"]), q(upper["number"])) == (lo, hi)
            stamps = [verify_head(chain, h) for h in (lower, upper)]
            events = ask(
                f"logs-{lo}",
                "eth_getLogs",
                [{"fromBlock": hex(lo), "toBlock": hex(hi), "topics": [sorted(topics)]}],
            )
            positions = []
            hashes = {lo: lower["hash"], hi: upper["hash"]}
            for event in events:
                n, hash_, index, tx, offset, *_ = keys(event)
                assert lo <= n <= hi and event["removed"] is False and matches(event, topics)
                assert hashes.setdefault(n, hash_) == hash_
                positions.append((n, index, offset))
                if tx not in seen:
                    groups[tx].append(event)
            assert positions == sorted(set(positions))
            windows.append(
                {
                    "first": lo,
                    "last": hi,
                    "utc": stamps,
                    "edge_hashes": [lower["hash"], upper["hash"]],
                    "market_events": len(events),
                }
            )

    def audit(ask):
        assert q(ask("chain", "eth_chainId", [])) == chain
        for events in unique_block_order(groups, scope["max_selected_blocks_per_l2"]):
            n, hash_, _, tx, *_ = keys(events[0])
            h = ask(f"h{n}", "eth_getBlockByHash", [hash_, False])
            assert q(h["number"]) == n and h["hash"] == hash_
            if name == "optimism":
                receipts = [ask(f"r{n}", "eth_getTransactionReceipt", [tx])]
                basis = scope | {"receipt_only_from": True}
            else:
                receipts = ask(f"r{n}", "eth_getBlockReceipts", [hash_])
                basis = scope
                # Preserve the recorded decision made by the historical receipt decoder.
                if any(q(r["type"]) not in (0, 2, 0x6A) for r in receipts):
                    raise ValueError("unsupported_nitro_receipt_type_or_position")
            verified.append(case(name, chain, spec["weth"], h, receipts, tx, groups, basis))

    original = read(INITIAL / "scope.json")
    scan = Lane(INITIAL / root.name / "scan", "scan", spec["scan"], False, original).run(discover)
    checked = reuse_asks(root, "audit", spec["audit"], audit, scope, capture) if groups else None
    return {
        "windows": windows,
        "blocks_screened": spec["width"] * len(windows),
        "at_least": thresholds(Counter(map(len, groups.values()))),
        "candidate_keys": [
            {"tx": tx, "events": len(events)} for tx, events in groups.items() if len(events) >= 2
        ],
        "verified_samples": verified,
        "scan": scan,
        "audit": checked,
    }


def summarize(root, scope, capture):
    prior, occupied = known()

    def network(name):
        local = root / name
        if capture:
            local.mkdir()
        spec = scope["networks"][name]
        if name == "ethereum":
            return name, eth_summary(scope, local, spec, prior[name], occupied[name], capture)
        return name, l2_summary(scope, local, name, spec, prior[name], occupied[name], capture)

    with ThreadPoolExecutor(max_workers=len(NETWORKS)) as pool:
        entries = dict(pool.map(network, NETWORKS))
    calls = sum(
        sum(item["counts"].values()) + item.get("reused_in_scope", 0)
        for chain in entries.values()
        for lane in ("scan", "audit", "trace")
        if (item := chain.get(lane))
    )
    return {
        "scope_sha256": digest(root / "scope.json"),
        "chains": entries,
        "rpc_reservations": calls,
        "rpc_spending_usd": 0,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "capture", "replay"))
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    if args.action == "prepare":
        durable_new(
            args.root / "scope.json", canonical_json(scope_for(datetime.now(UTC).isoformat()))
        )
        return
    scope = read(args.root / "scope.json")
    assert digest(args.root / "capture.py") == scope["driver_sha256"]
    assert scope == scope_for(scope["frozen_utc"], scope["driver_sha256"])
    print(
        json.dumps(summarize(args.root, scope, args.action == "capture"), indent=2, sort_keys=True)
    )


if __name__ == "__main__":
    main()
