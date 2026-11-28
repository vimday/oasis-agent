"""Audit new 2026 EVM L2 paths without an event-count ceiling."""

import argparse
import json
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path

import httpx
from replay_y2026_bsc_rh_landed import RH_WETH, keys, matches, rooted
from replay_y2026_era_window import rebuild_hash
from replay_y2026_op_arb import header_hash, moves, receipt_commitment
from replay_y2026_open_tail import observed_fee, receipt_paths, row_roles

from oasis_agent.arbitrage.cycle_protocols import topic
from oasis_agent.evidence.nitro_block import nitro_header
from oasis_agent.models.common import canonical_json, sha256_hex
from oasis_agent.rpc.budget import BudgetPolicy, RpcBudget
from scripts.capture_public_rpc import durable_new
from scripts.measure_robinhood_quote_delivery import _body, _fetch, _read

ROOT = Path(__file__).resolve().parents[3]
INDEXED = ROOT / "reports/research/y2026-open-tail-20261127/scope.json"
NETWORKS = {
    "arbitrum": [
        42161,
        "https://arb1.arbitrum.io/rpc",
        "https://arbitrum-one-rpc.publicnode.com",
        384,
        [18902, 27890, 48060],
        528049766,
        "0x82af49447d8a07e3bd95bd0d56f35241523fbab1",
    ],
    "era": [
        324,
        "https://mainnet.era.zksync.io",
        "https://zksync.drpc.org",
        128,
        [3805, 7293, 14918],
        73127909,
        "0x5aea5775959fbc2557cc8789bc1bf90a239d9a91",
    ],
    "optimism": [
        10,
        "https://optimism.drpc.org",
        "https://optimism-rpc.publicnode.com",
        128,
        [4724, 8692, 15287],
        160097704,
        "0x4200000000000000000000000000000000000006",
    ],
    "robinhood": [
        4663,
        "https://robinhood-rpc.publicnode.com",
        "https://rpc.mainnet.chain.robinhood.com",
        384,
        [36190, 48415, 68442],
        124359201,
        RH_WETH,
    ],
}
FOLLOW_UP = {
    "era": [
        324,
        "https://zksync.drpc.org",
        "https://mainnet.era.zksync.io",
        72,
        [230, 790, 1340],
        73127909,
        "0x5aea5775959fbc2557cc8789bc1bf90a239d9a91",
    ],
    "optimism": [
        10,
        "https://mainnet.optimism.io",
        "https://optimism-rpc.publicnode.com",
        96,
        [42, 182, 610],
        160097704,
        "0x4200000000000000000000000000000000000006",
    ],
    "robinhood": [
        4663,
        "https://rpc.mainnet.chain.robinhood.com",
        "https://robinhood-rpc.publicnode.com",
        192,
        [290, 800, 1544],
        124359201,
        RH_WETH,
    ],
}
TIERS = (1, 2, 3, 5, 10, 20, 50, 100, 200)
AUDIT_COUNT = 5
INTERVAL = 600
SCAN_CAP = 2 + 3 * 3
AUDIT_CAP = 1 + 2 * AUDIT_COUNT
FINANCE = {
    topic("FlashLoan(address,address,uint256)"),
    topic("Flash(address,address,uint256,uint256,uint256,uint256)"),
    topic("FlashLoan(address,address,address,uint256,uint8,uint256,uint16)"),
}


def read(path):
    return json.loads(path.read_bytes())


def digest(path):
    return sha256_hex(path.read_bytes())


def plan(stamp, source, follow_up=False, exact_follow_up=None):
    topics = read(INDEXED)["topics"]
    result = {
        "schema": "oasis.2026-fresh-l2.funded-arbs.v1",
        "frozen_utc": stamp,
        "driver_sha256": digest(source),
        "networks": FOLLOW_UP if follow_up else NETWORKS,
        "index_topics": topics,
        "financing_topics": sorted(FINANCE),
        "budgets": {"scan": SCAN_CAP, "audit": AUDIT_CAP},
        "minimum_interval_ms": 1100 if follow_up else INTERVAL,
        "select_per_source": AUDIT_COUNT,
        "ranking": (
            "One candidate per block. Rank market event count, distinct emitters, height, tx index."
        ),
        "scope": "first refusal stops the lane; no retries, no keys, no RPC spend",
        "rpc_spending_usd": 0,
    }
    if follow_up:
        result["disjoint_method_limitation_follow_up"] = True
    if exact_follow_up:
        result["receipt_only_from"] = exact_follow_up
        result["networks"] = {
            name: [*FOLLOW_UP[name][:2], endpoint, *FOLLOW_UP[name][3:]]
            for name, endpoint in {
                "era": "https://zksync-era-rpc.publicnode.com",
                "optimism": "https://optimism.drpc.org",
            }.items()
        }
        result["budgets"]["audit"] = 7
        result["select_per_source"] = 3
    return result


class Lane:
    def __init__(self, root, name, endpoint, capture, scope):
        self.root = root
        self.name = name
        self.endpoint = endpoint
        self.capture = capture
        self.frozen_unix_ns = round(datetime.fromisoformat(scope["frozen_utc"]).timestamp() * 10**9)
        self.policy = (BudgetPolicy(name, scope["budgets"][name], scope["minimum_interval_ms"]),)
        self.ledger = None
        self.client = None
        self.rows = []
        self.failure = None

    def run(self, action):
        if self.capture:
            self.root.mkdir()
        with (
            RpcBudget(
                self.root / "original.sqlite3", self.policy, read_only=not self.capture
            ) as ledger,
            httpx.Client(timeout=4, follow_redirects=False, trust_env=False) as client,
        ):
            self.ledger = ledger
            self.client = client
            try:
                action(self.ask)
            except (
                OSError,
                ValueError,
                KeyError,
                TypeError,
                AssertionError,
                httpx.HTTPError,
            ) as exc:
                self.failure = str(exc) or "integrity assertion failed"
            result = {"counts": ledger.counts(), "failure": self.failure, "rows": self.rows}
        stop = self.root / "STOP.json"
        if self.capture:
            durable_new(stop, canonical_json(result))
        else:
            recorded = read(stop)
            assert result["counts"] == recorded["counts"] and self.rows == recorded["rows"]
            assert (self.failure is None) == (recorded["failure"] is None)
            result = recorded
        assert len(self.rows) == result["counts"][self.name]
        return result

    def ask(self, label, method, args):
        i = len(self.rows) + 1
        path = self.root / label
        body = _body(i, method, args)
        if self.capture:
            while wait := self.ledger.reserve(self.name, now_ms=time.time_ns() // 10**6):
                time.sleep((wait + 1) / 1000)
        try:
            if self.capture:
                _fetch(self.client, self.endpoint, path, body)
            item, _ = _read(path, body, i, 1)
            assert read(path.with_suffix(".meta"))["retrieved_unix_ns"] >= self.frozen_unix_ns
            if item is None:
                raise ValueError("null RPC result")
            return item
        finally:
            proof = {"id": i, "label": label, "method": method, "source": self.endpoint}
            assert path.with_suffix(".request").read_bytes() == body
            for suffix in (".request", ".response", ".partial", ".meta"):
                if (p := path.with_suffix(suffix)).exists():
                    proof[suffix[1:] + "_sha256"] = digest(p)
            self.rows.append(proof)


def verify_head(chain, block):
    height = int(block["number"], 16)
    if chain in (4663, 42161):
        nitro_header(block, height, block["hash"], chain_id=chain)
    elif chain == 324:
        assert rebuild_hash(block) == block["hash"]
    else:
        assert header_hash(block) == block["hash"]
    stamp = datetime.fromtimestamp(int(block["timestamp"], 16), UTC)
    assert stamp.year == 2026
    return stamp.isoformat()


def unique_block_order(groups, limit):
    order = sorted(
        (g for g in groups.values() if len(g) >= 2),
        key=lambda g: (-len(g), -len({x["address"] for x in g}), *keys(g[0])[0:3:2]),
    )
    heights = set()
    for g in order:
        height = keys(g[0])[0]
        if height not in heights:
            heights.add(height)
            yield g
        if len(heights) == limit:
            return


def case(name, chain, weth, h, receipts, tx, groups, scope):
    stamp = verify_head(chain, h)
    height = int(h["number"], 16)
    e = groups[tx][0]
    idx = keys(e)[2]
    assert (keys(e)[0], e["blockHash"], tx) == (height, h["hash"], h["transactions"][idx])
    if name in ("arbitrum", "robinhood"):
        proof = rooted(chain, h, receipts)
        receipt = receipts[idx]
    elif name == "optimism" and not scope.get("receipt_only_from"):
        b, root = receipt_commitment(chain, h, receipts)
        proof = {
            "height": b.number,
            "hash": b.block_hash,
            "receipts_root": root,
            "transactions": len(receipts),
            "reverts": sum(r["status"] == "0x0" for r in receipts),
        }
        receipt = receipts[idx]
    else:
        [receipt] = receipts
        proof = {"height": height, "hash": h["hash"], "transactions": len(h["transactions"])}
    assert receipt["transactionHash"] == tx and receipt["blockHash"] == h["hash"]
    assert int(receipt["transactionIndex"], 16) == idx and receipt["status"] == "0x1"
    topics = scope["index_topics"]
    candidates = [keys(event) for event in receipt["logs"] if matches(event, topics)]
    assert candidates == [keys(event) for event in groups[tx]]
    funding = receipt_paths(receipt, topics)
    funded_logs = {i for leg in funding["receipt_legs"] for i in (leg["in_log"], leg["out_log"])}
    network = (
        {
            "complete_network_fee_wei": int(receipt["gasUsed"], 16)
            * int(receipt["effectiveGasPrice"], 16),
            "included_l1_gas": int(receipt["gasUsedForL1"], 16),
        }
        if name == "robinhood"
        else observed_fee(name, receipt)
    )
    roles = row_roles(receipt)
    all_cost = []
    for walk in funding["closed_at_same_holder"]:
        if walk["asset"] == weth:
            cost = network.get("complete_network_fee_wei", network.get("receipt_network_wei"))
            if cost is not None:
                all_cost.append(
                    {
                        "legs": walk["legs"],
                        "holder": walk["holder"],
                        "conditional_weth_path_after_entire_receipt_fee_wei": walk["gross_raw"]
                        - cost,
                    }
                )
    return {
        "tx": tx,
        "utc": stamp,
        "index": idx,
        "block": proof,
        "payer": receipt["from"],
        "to": receipt["to"],
        "events": len(candidates),
        "funding": funding,
        "fee": network,
        "payer_recipient_erc20_deltas": roles,
        "path_fee_comparison": all_cost,
        "owner_all_cost_net": None,
        "fee_native_currency": "ETH",
        "financing_event_indexes": [
            keys(event)[4] for event in receipt["logs"] if matches(event, FINANCE)
        ],
        "funding_movements": [
            [i, a, u, v, n] for i, (a, u, v, n) in moves(receipt).items() if i in funded_logs
        ],
        "receipt_basis": "receipt provider and independent scan-log match"
        if chain == 324 or scope.get("receipt_only_from")
        else "reconstructed header hash and full ordered receipt trie",
    }


def run_chain(root, name, scope, capture):
    chain, index, audit, width, offsets, floor, weth = scope["networks"][name]
    base = root / name
    if capture:
        base.mkdir()
    groups = defaultdict(list)
    results, windows = [], []
    index_topics = scope["index_topics"]

    def discover(ask):
        assert int(ask("chain", "eth_chainId", []), 16) == chain
        head = ask("head", "eth_getBlockByNumber", ["latest", False])
        verify_head(chain, head)
        height = int(head["number"], 16)
        for offset in offsets:
            last = height - offset
            first = last - width + 1
            assert first > floor
            heads = [
                ask(f"head-{n}", "eth_getBlockByNumber", [hex(n), False]) for n in (first, last)
            ]
            assert [int(h["number"], 16) for h in heads] == [first, last]
            stamps = [verify_head(chain, h) for h in heads]
            events = ask(
                f"logs-{first}",
                "eth_getLogs",
                [
                    {
                        "fromBlock": hex(first),
                        "toBlock": hex(last),
                        "topics": [sorted(set(index_topics) | FINANCE)],
                    }
                ],
            )
            positions = []
            hashes = {int(h["number"], 16): h["hash"] for h in heads}
            accepted = []
            for event in events:
                n, hash_, tx_idx, _, idx, *_ = keys(event)
                assert first <= n <= last and event["removed"] is False
                assert hashes.setdefault(n, hash_) == hash_
                assert matches(event, set(index_topics) | FINANCE)
                positions.append((n, tx_idx, idx))
                if matches(event, index_topics):
                    accepted.append(event)
            assert positions == sorted(set(positions))
            for event in accepted:
                groups[event["transactionHash"]].append(event)
            windows.append(
                {
                    "first": first,
                    "last": last,
                    "utc": stamps,
                    "logs_with_financing_shapes": len(events),
                    "edge_hashes": [h["hash"] for h in heads],
                }
            )

    def verify(ask):
        assert int(ask("chain", "eth_chainId", []), 16) == chain
        for events in unique_block_order(groups, scope["select_per_source"]):
            first = events[0]
            n, hash_, _, tx, *_ = keys(first)
            h = ask(f"h{n}", "eth_getBlockByHash", [hash_, False])
            assert int(h["number"], 16) == n and h["hash"] == hash_
            rows = (
                [ask(f"r{n}", "eth_getTransactionReceipt", [tx])]
                if chain == 324 or scope.get("receipt_only_from")
                else ask(f"r{n}", "eth_getBlockReceipts", [hash_])
            )
            results.append(case(name, chain, weth, h, rows, tx, groups, scope))

    prior = root.parent / "y2026-l2-fresh-follow-20261128"
    if expected := scope.get("receipt_only_from"):
        assert digest(prior / "scope.json") == expected
        scan = Lane(prior / name / "scan", "scan", index, False, read(prior / "scope.json")).run(
            discover
        )
    else:
        scan = Lane(base / "scan", "scan", index, capture, scope).run(discover)
    details = Lane(base / "audit", "audit", audit, capture, scope).run(verify) if groups else None
    return {
        "chain_id": chain,
        "windows": windows,
        "blocks_screened": len(windows) * width,
        "by_market_event_minimum": {n: sum(len(g) >= n for g in groups.values()) for n in TIERS},
        "max_market_events": max((len(g) for g in groups.values()), default=0),
        "verified_samples": results,
        "scan": scan,
        "audit": details,
    }


def panel(root, capture):
    frozen = root / "scope.json"
    scope = read(frozen)
    assert scope == plan(
        scope["frozen_utc"],
        root / "capture.py",
        scope.get("disjoint_method_limitation_follow_up", False),
        scope.get("receipt_only_from"),
    )
    if capture:
        assert digest(Path(__file__)) == scope["driver_sha256"]
    with ThreadPoolExecutor(max_workers=len(scope["networks"])) as pool:
        runs = {
            name: pool.submit(run_chain, root, name, scope, capture) for name in scope["networks"]
        }
        results = {name: run.result() for name, run in runs.items()}
    return {"scope_sha256": digest(frozen), "rpc_spending_usd": 0, "chains": results}


def read_case_receipt(root, name, row):
    base = root / name / "audit"
    label = f"r{row['block']['height']}"
    original = read((base / label).with_suffix(".request"))
    body = _body(original["id"], original["method"], original["params"])
    payload, _ = _read(base / label, body, original["id"], 1)
    if isinstance(payload, dict):
        assert payload["transactionHash"] == row["tx"]
        return payload
    r = payload[row["index"]]
    assert r["transactionHash"] == row["tx"]
    return r


def special_paths(dirs, chains):
    arb, rh, op = (
        chains[n]["verified_samples"][i]
        for n, i in (("arbitrum", 0), ("robinhood", 1), ("optimism", 0))
    )
    assert arb["events"] == 15 and rh["events"] == 9 and op["events"] == 25
    arb_t = moves(read_case_receipt(dirs[0], "arbitrum", arb))
    arb_weth = NETWORKS["arbitrum"][-1]
    split_payments = []
    for events, transfer_ids in (
        ((12, 30), (17, 21, 24, 28)),
        ((45, 62), (49, 53, 56, 60)),
        ((113, 130), (117, 121, 124, 128)),
        ((151, 168), (155, 159, 162, 166)),
        ((184, 209), (196, 200, 203, 207)),
    ):
        route = [arb_t[i] for i in transfer_ids]
        left, right = (
            next(leg for leg in arb["funding"]["receipt_legs"] if leg["event"] == i) for i in events
        )
        assert route[0][1] == left["receiver"] and route[-1][2] == right["pool"]
        assert all(a[2] == b[1] for a, b in pairwise(route))
        assert {(x[0], x[3]) for x in route} == {(arb_weth, right["in_raw"])}
        split_payments.append({"events": events, "transfers": transfer_ids, "wei": right["in_raw"]})
    payments = [i for i, (a, _, b, _) in arb_t.items() if (a, b) == (arb_weth, arb["to"])]
    assert payments == [15, 47, 115, 153, 194]
    arb_credit = sum(arb_t[i][3] for i in payments)
    assert arb["payer_recipient_erc20_deltas"] == [
        {"address": arb["to"], "token": arb_weth, "transfer_delta_raw": arb_credit}
    ]

    rh_t = moves(read_case_receipt(dirs[1], "robinhood", rh))
    legs = rh["funding"]["closed_at_same_holder"]
    assert len(legs) == 3 and {(x["asset"], x["holder"]) for x in legs} == {
        (RH_WETH, legs[0]["holder"])
    }
    assert all(a["received_raw"] != b["paid_raw"] for a, b in pairwise(legs))
    rh_payouts = []
    for i, walk in zip((32, 41, 50), legs, strict=True):
        asset, source, dest, amount = rh_t[i]
        assert (asset, source, amount) == (RH_WETH, walk["holder"], walk["gross_raw"])
        rh_payouts.append({"i": i, "to": dest, "raw": amount})
    assert len({x["to"] for x in rh_payouts}) == 1 and not rh["payer_recipient_erc20_deltas"]
    rh_credit = sum(x["raw"] for x in rh_payouts)
    assert all(
        sum(n * ((dst == addr) - (src == addr)) for a, src, dst, n in rh_t.values() if a == RH_WETH)
        == expected
        for addr, expected in ((legs[0]["holder"], 0), (rh_payouts[0]["to"], rh_credit))
    )
    exact = [p for p in op["funding"]["closed_at_same_holder"] if p["legs"] == [226, 227, 228, 229]]
    [op_cycle] = exact
    [op_balance] = op["payer_recipient_erc20_deltas"]
    assert op_cycle["asset"] == op_balance["token"] and op_balance["address"] == op["to"]
    return {
        "arbitrum": {
            "tx": arb["tx"],
            "forwarded_subflows": split_payments,
            "weth_to_executor_indexes": payments,
            "weth_to_executor_wei": arb_credit,
            "after_known_network_if_same_owner_wei": arb_credit
            - arb["fee"]["complete_network_fee_wei"],
        },
        "robinhood": {
            "tx": rh["tx"],
            "three_separate_weth_cycles": legs,
            "beneficiary_payout_transfers": rh_payouts,
            "beneficiary_weth_wei": rh_credit,
            "after_known_network_if_same_owner_wei": rh_credit
            - rh["fee"]["complete_network_fee_wei"],
        },
        "optimism": {
            "tx": op["tx"],
            "four_leg_weth_cycle": op_cycle,
            "visible_cycle_minus_known_cost_wei": op_cycle["gross_raw"]
            - op["fee"]["known_network_fee_wei"],
            "visible_executor_weth_minus_known_cost_wei": (
                op_balance["transfer_delta_raw"] - op["fee"]["known_network_fee_wei"]
            ),
            "operator_fee_unknown": op["fee"]["operator_fee_wei"] is None,
        },
    }


def consolidate(root):
    dirs = [root / f"y2026-l2-fresh-{name}-20261128" for name in ("v1", "follow", "exact")]
    panels = []
    scope_proofs = {}
    rpc_count = 0
    for folder in dirs:
        data = panel(folder, False)
        assert (folder / "result.json").read_text() == json.dumps(
            data, indent=2, sort_keys=True
        ) + "\n"
        scope_proofs[folder.name] = {
            "scope_sha256": data["scope_sha256"],
            "result_sha256": digest(folder / "result.json"),
            "lanes": {},
        }
        panels.append(data["chains"])
        for name, chain in data["chains"].items():
            tracked = scope_proofs[folder.name]["lanes"][name] = {}
            for lane in ("scan", "audit"):
                if chain[lane] is None:
                    continue
                reused = lane == "scan" and folder == dirs[2]
                count = chain[lane]["counts"][lane]
                if not reused:
                    rpc_count += count
                tracked[lane] = {
                    "requests": count,
                    "first_failure": chain[lane]["failure"],
                    "replay_only": reused,
                }
    case_chains = {**panels[0], **panels[1], **panels[2]}
    chains = {}
    for name, row in case_chains.items():
        cases = [
            {key: val for key, val in item.items() if key not in ("funding", "funding_movements")}
            | {
                "longest_verified_linked": item["funding"]["longest_linked"],
                "closed_cycles": item["funding"]["closed_at_same_holder"],
            }
            for item in row["verified_samples"]
        ]
        coverage = {k: v for k, v in row.items() if k not in ("scan", "audit", "verified_samples")}
        chains[name] = coverage | {"verified_samples": cases}
    return {
        "scopes": scope_proofs,
        "chains": chains,
        "special_cases": special_paths(dirs, case_chains),
        "rpc_reserved_count": rpc_count,
        "rpc_spending_usd": 0,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("mode", choices=("freeze", "capture", "replay", "consolidate"))
    ap.add_argument("root", type=Path)
    modes = ap.add_mutually_exclusive_group()
    modes.add_argument("--follow-up", action="store_true")
    modes.add_argument("--exact-follow-up", action="store_true")
    args = ap.parse_args()
    source = Path(__file__)
    frozen = args.root / "scope.json"
    if args.mode == "freeze":
        args.root.mkdir()
        prior = args.root.parent / "y2026-l2-fresh-follow-20261128/scope.json"
        durable_new(
            frozen,
            canonical_json(
                plan(
                    datetime.now(UTC).isoformat(),
                    source,
                    args.follow_up,
                    digest(prior) if args.exact_follow_up else None,
                )
            ),
        )
        durable_new(args.root / "capture.py", source.read_bytes())
        return
    result = (
        consolidate(args.root)
        if args.mode == "consolidate"
        else panel(args.root, args.mode == "capture")
    )
    print(
        json.dumps(
            result,
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
