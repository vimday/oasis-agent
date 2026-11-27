"""Rank the first arbitrage shard per chain in the authors' pinned CCS 2024 dataset."""

import argparse
import hashlib
import json
import re
from collections import Counter
from itertools import pairwise
from pathlib import Path

REVISION = "79d31e0a07b702b5eabb849ac5d513863cad8bb3"
SHARDS = {
    "arbitrum": (42161, "632bc41447a3bca2ab5df87a60b4e9db867003d7"),
    "ethereum": (1, "15e3072ef2547fd08ae239e8e2edfef7decc9a29"),
    "optimism": (10, "188a3498ee1b13be8c9ce34f4fd94829ef74cf59"),
    "zksync": (324, "559a924f7c560a8b803ce42d490521178287dc6b"),
}
TIERS = (5, 20, 50, 100, 200)


def closed_exact(legs):
    if legs[0]["in_token"].lower() != legs[-1]["out_token"].lower():
        return False
    return all(
        a["out_token"].lower() == b["in_token"].lower()
        and int(a["out_amount"]) == int(b["in_amount"]) > 0
        for a, b in pairwise(legs)
    ) and all(int(s["in_amount"]) > 0 and int(s["out_amount"]) > 0 for s in legs)


def published_rows(root, chain):
    chain_id, expected_blob = SHARDS[chain]
    raw = (root / f"{chain}-paper-1.json").read_bytes()
    if hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest() != expected_blob:
        raise ValueError("published Git blob differs")
    seen = set()
    for ordinal, line in enumerate(raw.splitlines(), 1):
        record = json.loads(line)
        tx = record["transaction"]
        identity = tx["hash"].lower()
        if tx.get("chainId") is not None and int(tx["chainId"], 16) != chain_id:
            raise ValueError("published row has a different chain ID")
        if not re.fullmatch("0x[0-9a-f]{64}", identity) or identity in seen:
            raise ValueError("ambiguous transaction ID")
        seen.add(identity)
        groups = [g["swaps"] for g in record["arbitrages"]]
        event_ids = {s["index"] for legs in groups for s in legs}
        exact = max((len(g) for g in groups if closed_exact(g)), default=0)
        yield (
            {
                "chain_id": chain_id,
                "tx_hash": identity,
                "source_row": ordinal,
                "block": record["block_number"],
                "transaction_index": tx["transactionIndex"],
                "groups": len(groups),
                "distinct_indices": len(event_ids),
                "group_legs_sum": sum(map(len, groups)),
                "max_group": max(map(len, groups)),
                "max_amount_matched_closed_group": exact,
            },
            record,
        )


def summarize(root):
    result = {}
    for chain in SHARDS:
        rows = [r for r, _ in published_rows(root, chain)]
        by_length = min(rows, key=lambda r: (-r["max_amount_matched_closed_group"], r["tx_hash"]))
        by_events = min(rows, key=lambda r: (-r["distinct_indices"], r["tx_hash"]))
        result[chain] = {
            "blob_id": SHARDS[chain][1],
            "sha256": hashlib.sha256((root / f"{chain}-paper-1.json").read_bytes()).hexdigest(),
            "transactions": len(rows),
            "height_range": [min(r["block"] for r in rows), max(r["block"] for r in rows)],
            "multi_group": sum(r["groups"] > 1 for r in rows),
            "overlapping_group_indices": sum(
                r["distinct_indices"] < r["group_legs_sum"] for r in rows
            ),
            "at_least_n_distinct_indices": {
                t: sum(r["distinct_indices"] >= t for r in rows) for t in TIERS
            },
            "at_least_n_amount_matched_in_one_closed_group": {
                t: sum(r["max_amount_matched_closed_group"] >= t for r in rows) for t in TIERS
            },
            "exact_length_histogram": dict(
                sorted(Counter(r["max_amount_matched_closed_group"] for r in rows).items())
            ),
            "highest_event_count": by_events,
            "longest_amount_matched_closed_group": by_length,
        }
    return {"repository_revision": REVISION, "shards": result}


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("research_root", type=Path)
    args = p.parse_args()
    print(json.dumps(summarize(args.research_root), indent=2, sort_keys=True))
