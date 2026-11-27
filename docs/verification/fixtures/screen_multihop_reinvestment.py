"""Find cross-group, changing-principal continuations in eight immutable public CCS shards."""

import json
from collections import Counter
from pathlib import Path

from replay_multihop_second_shards import git_blob, projected_row
from replay_published_multihop_shards import REVISION, SHARDS

DOC = Path(__file__).resolve().parents[1]
RESEARCH = DOC.parents[1] / "reports/research"
TIERS = (20, 50, 100, 200)


def ordered_runs(groups):
    """Only connect adjacent source groups with distinct logs and an exactly reused return."""
    run = []
    for group in groups:
        if run:
            prev = run[-1]
            if not (
                group["group_number"] == prev["group_number"] + 1
                and group["closing_token"] == prev["closing_token"]
                and group["principal_raw"] == prev["return_raw"]
                and prev["return_raw"] != prev["principal_raw"]
                and min(group["log_indices"]) > max(prev["log_indices"])
            ):
                if len(run) > 1:
                    yield run
                run = []
        run.append(group)
    if len(run) > 1:
        yield run


def describe(run):
    markets = {pool for g in run for pool in g["markets"]}
    return {
        "first_group": run[0]["group_number"],
        "last_group": run[-1]["group_number"],
        "groups": len(run),
        "legs": sum(g["market_events"] for g in run),
        "unique_markets": sorted(markets),
        "closing_token": run[0]["closing_token"],
        "initial_principal": run[0]["principal_raw"],
        "terminal_return": run[-1]["return_raw"],
        "increasing_rounds": sum(g["gross_raw"] > 0 for g in run),
        "decreasing_rounds": sum(g["gross_raw"] < 0 for g in run),
        "zero_rounds": sum(g["gross_raw"] == 0 for g in run),
    }


def main():
    original = RESEARCH / "multichain-inventory-20261127"
    second = RESEARCH / "multihop-second-shards-20261127"
    sources = json.loads((DOC / "MULTIHOP_SECOND_SHARDS_SOURCES_20261127.json").read_bytes())
    cases = []
    summary = []
    seen = set()
    for chain, (chain_id, first_blob) in SHARDS.items():
        second_blob = sources["chains"][chain]["source"]["blob"]
        for shard, (folder, blob) in enumerate(((original, first_blob), (second, second_blob)), 1):
            raw = (folder / f"{chain}-paper-{shard}.json").read_bytes()
            if git_blob(raw) != blob:
                raise ValueError("public source differs from the immutable blob")
            counts = Counter()
            for ordinal, line in enumerate(raw.splitlines(), 1):
                row, exact = projected_row(chain, ordinal, json.loads(line))
                key = (chain_id, row["tx_hash"])
                if key in seen:
                    raise ValueError("duplicate transaction within the joined shards")
                seen.add(key)
                counts["transactions"] += 1
                counts["multi_group"] += row["published_groups"] > 1
                runs = [
                    describe(run)
                    for run in ordered_runs(exact)
                    if row["reported_group_legs"] == row["published_indices"]
                ]
                longest = max((run["legs"] for run in runs), default=0)
                counts["has_changed_principal_link"] += bool(runs)
                counts["max_legs"] = max(counts["max_legs"], longest)
                for depth in TIERS:
                    counts[f"at_least_{depth}"] += longest >= depth
                long = [run for run in runs if run["legs"] >= TIERS[0]]
                if long:
                    cases.append(
                        {
                            "chain": chain,
                            "chain_id": chain_id,
                            "tx_hash": row["tx_hash"],
                            "block_number": row["block_number"],
                            "source_row": ordinal,
                            "published_events": row["published_indices"],
                            "shard": shard,
                            "runs": long,
                        }
                    )
            summary.append({"chain": chain, "shard": shard, "blob": blob, **counts})
    return {
        "revision": REVISION,
        "evidence": "published asset and amount matches; custody and fees require receipts",
        "summary": summary,
        "cases": sorted(cases, key=lambda c: (c["chain_id"], c["tx_hash"])),
    }


if __name__ == "__main__":
    if not __debug__:
        raise SystemExit("run without Python optimization")
    print(json.dumps(main(), indent=2, sort_keys=True))
