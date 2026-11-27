"""Identify the transaction year of the frozen eight-chain catalog from dated chain evidence."""

import csv
import gzip
import json
from bisect import bisect_left
from collections import Counter, defaultdict
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from render_multihop_casebook import rows
from replay_multihop_second_shards import git_blob
from replay_published_multihop_shards import SHARDS

from oasis_agent.rpc.budget import BudgetPolicy, RpcBudget

DOC = Path(__file__).resolve().parents[1]
RESEARCH = DOC.parents[1] / "reports/research"
ROOT = RESEARCH / "y2026-annual-20261127"
CAT = RESEARCH / "gold-book-20261127/all-hashes.tsv.gz"
SCOPE_SHA = "a175b90a05580a16169ffda9d42ce4240fa2f25ecbcbaee5615f3c1cbf055653"


def digest(path):
    return sha256(path.read_bytes()).hexdigest()


def read(path):
    return json.loads(path.read_bytes())


def decoded(folder, label, method, params, source):
    req, res, meta = (folder / (label + ext) for ext in (".request", ".response", ".meta"))
    m = read(meta)
    q = read(req)
    assert m["http"] == 200 and m["source"] == source
    assert q["method"] == method and q["params"] == params and q["jsonrpc"] == "2.0"
    assert (digest(req), digest(res)) == (m["request_sha256"], m["response_sha256"])
    v = read(res)
    assert q["id"] == v["id"] == m["seq"] and v["jsonrpc"] == "2.0"
    return v["result"], m


def published_dates():
    by_key = {}
    manifest = read(DOC / "MULTIHOP_SECOND_SHARDS_SOURCES_20261127.json")
    for chain, (chain_id, first) in SHARDS.items():
        second = manifest["chains"][chain]["source"]["blob"]
        for number, (directory, expected) in enumerate(
            [
                ("multichain-inventory-20261127", first),
                ("multihop-second-shards-20261127", second),
            ],
            1,
        ):
            raw = (RESEARCH / directory / f"{chain}-paper-{number}.json").read_bytes()
            assert git_blob(raw) == expected
            for line in raw.splitlines():
                row = json.loads(line)
                key = (chain_id, row["transaction"]["hash"].lower())
                assert key not in by_key
                by_key[key] = (row["block_number"], row["block_timestamp"])
    return by_key


def stamp(t):
    return datetime.fromtimestamp(t, UTC).isoformat()


def chain_evidence(chain, spec):
    folder = ROOT / chain
    stop = read(folder / "STOP.json")
    assert stop["scope_sha256"] == SCOPE_SHA and stop["error"] is None
    with RpcBudget(
        folder / "original.sqlite3", (BudgetPolicy(chain, spec["cap"], 150),), read_only=True
    ) as budget:
        assert budget.counts() == stop["counts"]
    value, _ = decoded(folder, "chain", "eth_chainId", [], spec["url"])
    assert int(value, 16) == spec["chain_id"]
    headers = {}
    direct = {}
    for height in spec["heights"]:
        h, meta = decoded(
            folder, "h" + str(height), "eth_getBlockByNumber", [hex(height), False], spec["url"]
        )
        assert int(h["number"], 16) == height
        headers[height] = {
            "block_hash": h["hash"],
            "timestamp": int(h["timestamp"], 16),
            "source": spec["url"],
            "wire": meta,
        }
    for tx in spec["missing"]:
        receipt, rmeta = decoded(folder, tx, "eth_getTransactionReceipt", [tx], spec["url"])
        if receipt is None:
            continue
        h, meta = decoded(
            folder, "extra-" + tx, "eth_getBlockByHash", [receipt["blockHash"], False], spec["url"]
        )
        height = int(h["number"], 16)
        assert h["hash"] == receipt["blockHash"] and int(receipt["blockNumber"], 16) == height
        assert (
            h["transactions"][int(receipt["transactionIndex"], 16)]
            == tx
            == receipt["transactionHash"]
        )
        direct[tx] = {
            "height": height,
            "timestamp": int(h["timestamp"], 16),
            "block_hash": h["hash"],
            "receipt": rmeta,
            "header": meta,
        }
    times = [headers[h]["timestamp"] for h in sorted(headers)]
    assert times == sorted(times)
    return headers, direct, stop["counts"]


def year_at_height(heights, headers, height):
    pos = bisect_left(heights, height)
    if pos == len(heights):
        return None
    hi = heights[pos]
    if hi == height:
        return stamp(headers[hi]["timestamp"])[:4]
    if pos == 0:
        return None
    lower, upper = (stamp(headers[h]["timestamp"])[:4] for h in (heights[pos - 1], hi))
    return lower if lower == upper else None


def failed_index_window(spec):
    lane = "eth-quarter"
    folder = ROOT / lane
    label = "q" + str(spec["starts"][0])
    m = read(folder / (label + ".meta"))
    stop = read(folder / "STOP.json")
    assert (m["http"], m["seq"], m["source"]) == (529, 1, spec["url"])
    assert [digest(folder / (label + ext)) for ext in (".request", ".response")] == [
        m["request_sha256"],
        m["response_sha256"],
    ]
    assert read(folder / (label + ".response"))["error"]["code"] == "overloaded"
    assert stop["scope_sha256"] == SCOPE_SHA and stop["error"]
    with RpcBudget(
        folder / "original.sqlite3", (BudgetPolicy(lane, spec["cap"], 150),), read_only=True
    ) as budget:
        assert budget.counts() == stop["counts"] == {lane: 1}
    return {
        "scope_sha256": SCOPE_SHA,
        "wire": m,
        "attempted_start": spec["starts"][0],
        "blocks_completed": 0,
    }


def main():
    assert digest(ROOT / "scope.json") == SCOPE_SHA
    scope = read(ROOT / "scope.json")
    assert (
        digest(ROOT / "capture.py") == scope["driver_sha256"]
        and digest(CAT) == scope["catalog_sha256"]
    )
    cases = {(int(r["chain_id"]), r["tx_hash"]): r for r in rows(None)}
    paper = published_dates()
    evidence = {c: chain_evidence(c, s) for c, s in scope["lanes"].items() if "heights" in s}
    years = defaultdict(Counter)
    case_years = defaultdict(Counter)
    sources = Counter()
    saved = []
    total = 0
    with gzip.open(CAT, "rt") as f:
        for row in csv.DictReader(f, delimiter="\t"):
            total += 1
            chain = row["chain"]
            key = (int(row["chain_id"]), row["tx_hash"])
            height = int(row["block_number"]) if row["block_number"] else None
            if row["published_source_row"]:
                block, timestamp = paper.pop(key)
                assert block == height
                year, method = stamp(timestamp)[:4], "author_dated_block"
            else:
                headers, direct, _ = evidence[chain]
                if key[1] in direct:
                    height = direct[key[1]]["height"]
                    year, method = (
                        stamp(direct[key[1]]["timestamp"])[:4],
                        "receipt_and_indexed_header",
                    )
                elif height is not None:
                    year = year_at_height(sorted(headers), headers, height)
                    method = (
                        "exact_height_header" if height in headers else "bracketed_by_dated_heights"
                    )
                else:
                    year, method = None, "unresolved"
            year = year or "unknown"
            years[chain][year] += 1
            sources[method] += 1
            if key in cases:
                case_years[chain][year] += 1
                if year == "2026":
                    saved.append(
                        {
                            "chain": chain,
                            "chain_id": key[0],
                            "tx_hash": key[1],
                            "block": height,
                            "year_basis": method,
                            "shape": row["shape"],
                            "review": row["review"],
                            "screen_events": row["screen_events_max"] or None,
                            "funded_market_swaps_lb": row["funded_market_swaps_lb"] or None,
                            "network_fee_raw": row["observed_network_cost_native_raw"] or None,
                            "fee_basis": row["cost_basis"] or None,
                        }
                    )
    assert (
        total == 118083
        and not paper
        and sum(sum(v.values()) for v in case_years.values()) == len(cases) == 705
    )
    return {
        "schema": "oasis.y2026.year-register.v1",
        "scope_sha256": SCOPE_SHA,
        "catalog_sha256": digest(CAT),
        "all_years": years,
        "curated_years": case_years,
        "evidence_counts": sources,
        "year_2026_shapes": Counter(x["shape"] for x in saved),
        "dated_providers": {
            c: {"headers": headers, "located_transactions": direct, "reservations": counts}
            for c, (headers, direct, counts) in evidence.items()
        },
        "incomplete_ethereum_index": failed_index_window(scope["lanes"]["eth-quarter"]),
        "year_2026_cases": saved,
    }


if __name__ == "__main__":
    if not __debug__:
        raise SystemExit("run without -O")
    print(json.dumps(main(), indent=2, sort_keys=True))
