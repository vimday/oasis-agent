"""Rebuild the four numbered second CCS shards and their exact-amount closed groups."""

import argparse
import csv
import gzip
import hashlib
import io
import json
import re
from collections import Counter
from itertools import pairwise
from pathlib import Path

from replay_published_multihop_shards import REVISION, SHARDS, closed_exact, published_rows

TIERS = (20, 50, 100, 200)
PRIOR_CATALOG = Path("docs/verification/MULTICHAIN_MULTIHOP_ALL_HASHES_20261127.tsv.gz")
HEX = re.compile(r"0x[0-9a-f]{64}\Z")
ADD = re.compile(r"0x[0-9a-f]{40}\Z")
HASH_COLUMNS = (
    "chain_id",
    "chain",
    "tx_hash",
    "block_number",
    "transaction_index",
    "source_row",
    "published_indices",
    "published_groups",
    "published_max_exact_closed_group",
    "published_exact_closed_groups",
    "source",
    "new_to_prior_catalog",
)
GROUP_COLUMNS = (
    "chain_id",
    "chain",
    "tx_hash",
    "source_row",
    "group_number",
    "market_events",
    "closing_token",
    "principal_raw",
    "return_raw",
    "gross_raw",
    "log_indices",
    "route_tokens",
    "leg_in_amounts_raw",
    "leg_out_amounts_raw",
)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def git_blob(data):
    return hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()


def quantity(value):
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, str):
        return int(value, 16 if value.startswith("0x") else 10)
    raise ValueError("missing integer quantity")


def tsv(columns, rows):
    out = io.StringIO(newline="")
    writer = csv.writer(out, delimiter="\t", lineterminator="\n")
    writer.writerow(columns)
    writer.writerows(rows)
    return gzip.compress(out.getvalue().encode(), compresslevel=9, mtime=0)


def file_record(path):
    data = path.read_bytes()
    return {"path": path.as_posix(), "sha256": sha(data), "bytes": len(data)}


def read_group(ordinal, grouped):
    legs = grouped["swaps"]
    assert legs
    indices = tuple(quantity(leg["index"]) for leg in legs)
    assert min(indices) >= 0 and len(set(indices)) == len(indices)
    tokens = [(leg["in_token"].lower(), leg["out_token"].lower()) for leg in legs]
    assert all(ADD.fullmatch(t) for edge in tokens for t in edge)
    amounts = [(quantity(leg["in_amount"]), quantity(leg["out_amount"])) for leg in legs]
    markets = tuple(leg["exchange"].lower() for leg in legs)
    assert all(ADD.fullmatch(s) for s in markets)
    if len(legs) < 2 or not closed_exact(legs):
        return indices, None
    amount_in, amount_out = amounts[0][0], amounts[-1][1]
    assert all(x > 0 for pair in amounts for x in pair)
    group = {
        "group_number": ordinal,
        "market_events": len(legs),
        "closing_token": tokens[0][0],
        "principal_raw": amount_in,
        "return_raw": amount_out,
        "gross_raw": amount_out - amount_in,
        "log_indices": indices,
        "route_tokens": (tokens[0][0], *(y for _, y in tokens)),
        "leg_in_amounts_raw": tuple(x for x, _ in amounts),
        "leg_out_amounts_raw": tuple(y for _, y in amounts),
        "markets": markets,
        "route_symbols": (
            legs[0].get("in_token_name"),
            *(leg.get("out_token_name") for leg in legs),
        ),
    }
    return indices, group


def projected_row(chain, row_number, doc):
    tx = doc["transaction"]
    chain_id = SHARDS[chain][0]
    identity = tx["hash"].lower()
    assert HEX.fullmatch(identity)
    assert tx.get("chainId") is None or quantity(tx["chainId"]) == chain_id
    block = quantity(doc["block_number"])
    tx_index = quantity(tx["transactionIndex"])
    block_utc = quantity(doc["block_timestamp"])
    assert block > 0 and tx_index >= 0 and block_utc > 0
    groups = [read_group(i, g) for i, g in enumerate(doc["arbitrages"], 1)]
    distinct = {i for indices, _ in groups for i in indices}
    exact = [g for _, g in groups if g is not None]
    return {
        "chain_id": chain_id,
        "chain": chain,
        "tx_hash": identity,
        "block_number": block,
        "transaction_index": tx_index,
        "source_row": row_number,
        "block_utc": block_utc,
        "published_indices": len(distinct),
        "published_groups": len(groups),
        "published_max_exact_closed_group": max((g["market_events"] for g in exact), default=0),
        "published_exact_closed_groups": len(exact),
        "reported_group_legs": sum(len(indices) for indices, _ in groups),
        "source": "CCS24_shard_2",
    }, exact


def prior_inventory(path):
    with gzip.open(path, "rt", encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream, delimiter="\t"))
    index = {
        (quantity(x["chain_id"]), x["tx_hash"]): quantity(x["block_number"])
        if x["block_number"]
        else None
        for x in rows
    }
    assert len(index) == len(rows)
    return index


def paper_input(root, previous):
    scope_raw = (root / "download-plan.json").read_bytes()
    scope = json.loads(scope_raw)
    stop = json.loads((root / "DOWNLOAD_STOP.json").read_bytes())
    tree_record = file_record(previous / "paper-tree.json")
    assert tree_record["sha256"] == scope["tree_response_sha256"]
    assert scope["revision"] == REVISION and stop["scope_sha256"] == sha(scope_raw)
    assert stop["logical_gets"] == {"github_raw": 4}
    downloads = {item["chain"]: item for item in stop["results"]}
    specs = {row["chain"]: row for row in scope["records"]}
    assert specs.keys() == downloads.keys() == SHARDS.keys()
    return specs, downloads, tree_record


def qualifiers(rows, prior, by_chain_rows):
    new = [x for x in rows if (x["chain_id"], x["tx_hash"]) not in prior]
    event = min(new, key=lambda x: (-x["published_indices"], x["tx_hash"]))
    exact = min(
        (x for x in new if x != event),
        key=lambda x: (-x["published_max_exact_closed_group"], x["tx_hash"]),
    )
    ranked = [("largest_published_index_count", event), ("longest_published_exact_group", exact)]
    if event["chain_id"] == exact["chain_id"]:
        another = min(
            (x for x in new if x["chain_id"] != event["chain_id"]),
            key=lambda x: (-x["published_indices"], x["tx_hash"]),
        )
        ranked.append(("largest_on_a_different_chain", another))
    axes = ("published_indices", "published_max_exact_closed_group")
    top_by_chain = {
        chain: {axis: min(items, key=lambda x: (-x[axis], x["tx_hash"])) for axis in axes}
        for chain, items in by_chain_rows.items()
    }
    return top_by_chain, ranked


def compile_corpus(root, previous, prior_path):
    specs, downloads, tree_record = paper_input(root, previous)
    prior = prior_inventory(prior_path)
    all_rows = []
    history = {}
    by_chain_rows = {}
    all_groups = {}
    for chain, spec in specs.items():
        path = root / f"{chain}-paper-2.json"
        raw = path.read_bytes()
        d = downloads[chain]
        assert d["error"] is None and d["status"] == 200
        assert len(raw) == d["bytes"] == spec["bytes"]
        assert git_blob(raw) == d["git_blob"] == spec["blob"] and sha(raw) == d["sha256"]
        source_1 = {
            row["tx_hash"]: quantity(row["block"]) for row, _ in published_rows(previous, chain)
        }
        chain_rows = []
        seen = set()
        overlap_1 = []
        overlap_prior = []
        for num, line in enumerate(raw.splitlines(), 1):
            row, exact = projected_row(chain, num, json.loads(line))
            tx = row["tx_hash"]
            assert tx not in seen
            seen.add(tx)
            key = (row["chain_id"], tx)
            if key in prior:
                assert prior[key] in (None, row["block_number"]), (key, prior[key], row)
                overlap_prior.append(tx)
            if tx in source_1:
                assert source_1[tx] == row["block_number"], (key, source_1[tx], row)
                overlap_1.append(tx)
            row["new_to_prior_catalog"] = int(key not in prior)
            chain_rows.append(row)
            all_groups[key] = exact
        all_rows.extend(chain_rows)
        by_chain_rows[chain] = chain_rows
        history[chain] = {
            "source": spec | {"sha256": d["sha256"], "received_utc": d["finished_utc"]},
            "first_shard_blob": SHARDS[chain][1],
            "second_shard_transactions": len(chain_rows),
            "overlap_first_shard": len(overlap_1),
            "overlap_first_shard_hashes": sorted(overlap_1),
            "overlap_prior_catalog": len(overlap_prior),
            "overlap_prior_catalog_hashes": sorted(overlap_prior),
            "new_identities": len(chain_rows) - len(overlap_prior),
            "covered_transaction_height_range": [
                min(x["block_number"] for x in chain_rows),
                max(x["block_number"] for x in chain_rows),
            ],
            "multiple_published_groups": sum(x["published_groups"] > 1 for x in chain_rows),
            "overlapping_published_group_indices": sum(
                x["reported_group_legs"] > x["published_indices"] for x in chain_rows
            ),
            "at_least_distinct_published_indices": {
                t: sum(x["published_indices"] >= t for x in chain_rows) for t in TIERS
            },
            "at_least_in_one_exact_closed_group": {
                t: sum(x["published_max_exact_closed_group"] >= t for x in chain_rows)
                for t in TIERS
            },
            "exact_group_count": sum(x["published_exact_closed_groups"] for x in chain_rows),
            "longest_exact_group_histogram": dict(
                sorted(Counter(x["published_max_exact_closed_group"] for x in chain_rows).items())
            ),
        }
    all_rows.sort(key=lambda x: (x["chain_id"], x["tx_hash"]))
    top, ranked = qualifiers(all_rows, prior, by_chain_rows)
    case_rows = []
    for rank, row in ranked:
        exact = all_groups[row["chain_id"], row["tx_hash"]]
        profit = Counter()
        for group in exact:
            profit[group["closing_token"]] += group["gross_raw"]
        case_rows.append(
            row
            | {
                "selection": rank,
                "exact_closed_groups": exact,
                "gross_sum_by_closing_token_raw": dict(profit),
                "exact_groups_with_reset_principal_and_gap": sum(
                    a["closing_token"] == b["closing_token"]
                    and a["principal_raw"] == b["principal_raw"]
                    and a["return_raw"] != b["principal_raw"]
                    for a, b in pairwise(exact)
                ),
            }
        )
    hash_bytes = tsv(HASH_COLUMNS, ([x[k] for k in HASH_COLUMNS] for x in all_rows))
    prefix = GROUP_COLUMNS[:4]
    group_bytes = tsv(
        GROUP_COLUMNS,
        (
            [
                *(row[k] for k in prefix),
                *(
                    g[k] if not isinstance(g[k], tuple) else ",".join(map(str, g[k]))
                    for k in GROUP_COLUMNS[4:]
                ),
            ]
            for row in all_rows
            for g in all_groups[row["chain_id"], row["tx_hash"]]
        ),
    )
    evidence = {
        "schema": "oasis.ccs24-second-shards.v1",
        "revision": REVISION,
        "source_tree": tree_record,
        "download_plan": file_record(root / "download-plan.json"),
        "download_stop": file_record(root / "DOWNLOAD_STOP.json"),
        "prior_catalog": file_record(prior_path) | {"path": PRIOR_CATALOG.as_posix()},
        "prior_catalog_identities": len(prior),
        "selection_rule": file_record(root / "selection-rule.json"),
        "chains": history,
        "all_shard_rows": len(all_rows),
        "new_identities": sum(x["new_to_prior_catalog"] for x in all_rows),
        "observed_gas_and_receipts": "Independent verification has a separate scope.",
        "top_by_chain": top,
        "independent_receipt_queue": case_rows,
        "exports": {
            "hashes": {"sha256": sha(hash_bytes), "rows": len(all_rows)},
            "closed_groups": {
                "sha256": sha(group_bytes),
                "rows": sum(map(len, all_groups.values())),
            },
        },
    }
    return evidence, hash_bytes, group_bytes


def main():
    if not __debug__:
        raise RuntimeError("run with assertions enabled")
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("root", type=Path)
    p.add_argument(
        "--previous", type=Path, default=Path("reports/research/multichain-inventory-20261127")
    )
    p.add_argument(
        "--prior-catalog",
        type=Path,
        default=PRIOR_CATALOG,
    )
    p.add_argument("--hashes", type=Path, required=True)
    p.add_argument("--groups", type=Path, required=True)
    args = p.parse_args()
    summary, hashes, groups = compile_corpus(args.root, args.previous, args.prior_catalog)
    args.hashes.write_bytes(hashes)
    args.groups.write_bytes(groups)
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
