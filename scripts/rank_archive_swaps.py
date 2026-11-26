"""Rank retained archive candidates by unique raw-amount links for subsequent funding review."""

import argparse
import csv
import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from itertools import groupby
from pathlib import Path

from oasis_agent.arbitrage.cycle_plan import address, hash32
from oasis_agent.arbitrage.cycle_protocols import data_bytes, topic
from oasis_agent.evidence.swap_flow import MAX_SWAP_EVENTS

# Only these event signatures are part of the historical discovery cohort.
V2 = topic("Swap(address,uint256,uint256,uint256,uint256,address)")
V3 = topic("Swap(address,address,int256,int256,uint160,uint128,int24)")
PANCAKE = topic("Swap(address,address,int256,int256,uint160,uint128,int24,uint128,uint128)")
V4 = topic("Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)")
BALANCER = topic("Swap(bytes32,address,address,uint256,uint256)")
BANCOR = topic("Conversion(address,address,address,uint256,uint256,int256)")
CURVE = topic("TokenExchange(address,int128,uint256,int128,uint256)")
CURVE_CRYPTO = topic("TokenExchange(address,uint256,uint256,uint256,uint256,uint256,uint256)")

WIDTHS = {V2: 4, V3: 5, PANCAKE: 7, V4: 6, BALANCER: 2, BANCOR: 3, CURVE: 4, CURVE_CRYPTO: 6}


@dataclass(frozen=True)
class Candidate:
    tx: str
    height: int
    block_hash: str
    index: int
    events: int
    pools: int
    amount_depth: int
    indices: tuple[int, ...]


def _amounts(sig: str, data: str) -> tuple[int, int] | None:
    raw = data_bytes(data)
    if sig not in WIDTHS or len(raw) != 32 * WIDTHS[sig]:
        raise ValueError("unrecognized archive event or malformed data width")
    signed = sig in {V3, PANCAKE, V4}
    w = [int.from_bytes(raw[i : i + 32], signed=signed) for i in range(0, len(raw), 32)]
    if sig == V2:
        if bool(w[0]) == bool(w[1]) or bool(w[2]) == bool(w[3]):
            return None
        return sum(w[:2]), sum(w[2:])
    if signed:
        a, b = w[:2]
        if a * b >= 0:
            return None
        return (-min(a, b), max(a, b)) if sig == V4 else (max(a, b), -min(a, b))
    pair = (w[1], w[3]) if sig in {CURVE, CURVE_CRYPTO} else (w[0], w[1])
    return pair if min(pair) > 0 else None


def rank_transaction(tx: str, rows: Iterator[dict[str, str]]) -> Candidate:
    tx = hash32(tx)
    binding = None
    indices: list[int] = []
    pools: list[tuple[str, str | None]] = []
    inputs: defaultdict[int, list[int]] = defaultdict(list)
    outputs: defaultdict[int, list[int]] = defaultdict(list)
    starts: list[int] = []
    for i, row in enumerate(rows):
        height, index = int(row["block_number"]), int(row["transaction_index"])
        observed = height, hash32(row["block_hash"]), index
        if binding is None:
            binding = observed
        log_index = int(row["log_index"])
        if (
            tx != hash32(row["transaction_hash"])
            or binding != observed
            or min(height, index, log_index) < 0
            or (indices and log_index <= indices[-1])
            or i >= MAX_SWAP_EVENTS
        ):
            raise ValueError("archive rows exceed bounds or disagree on inclusion or order")
        indices.append(log_index)
        sig = row["sig"]
        pools.append(
            (address(row["address"]), hash32(row["topic1"]) if sig in {BALANCER, V4} else None)
        )
        pair = _amounts(sig, row["data"])
        if pair is not None:
            starts.append(i)
            inputs[pair[0]].append(i)
            outputs[pair[1]].append(i)
    if binding is None:
        raise ValueError("empty archive transaction")
    links = {
        src[0]: inputs[q][0]
        for q, src in outputs.items()
        if len(src) == len(inputs[q]) == 1 and src[0] != inputs[q][0]
    }
    longest: list[int] = []
    for start in starts:
        at: int | None = start
        seen: set[tuple[str, str | None]] = set()
        path: list[int] = []
        while at is not None and pools[at] not in seen:
            seen.add(pools[at])
            path.append(indices[at])
            at = links.get(at)
        if len(path) > len(longest):
            longest = path
    return Candidate(tx, *binding, len(indices), len(set(pools)), len(longest), tuple(longest))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("candidates", type=Path, help="ordered candidates.csv from the archive SQL")
    path = parser.parse_args().candidates
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    with path.open(newline="") as stream:
        ranked = [
            rank_transaction(tx, rows)
            for tx, rows in groupby(csv.DictReader(stream), key=lambda row: row["transaction_hash"])
        ]
    if len({r.tx for r in ranked}) != len(ranked):
        raise ValueError("transaction recurs in multiple groups")
    depth = sorted(ranked, key=lambda r: (-r.amount_depth, -r.pools, -r.events, r.tx))
    events = sorted(ranked, key=lambda r: (-r.events, -r.pools, r.tx))
    selection = depth[:1] + [r for r in events if r.tx != depth[0].tx][:1] if depth else []
    print(
        json.dumps(
            {
                "selected": [asdict(r) for r in selection],
                "by_depth": [asdict(r) for r in depth[:15]],
                "by_events": [asdict(r) for r in events[:15]],
                "amount_depth_histogram": Counter(r.amount_depth for r in ranked),
                "processed_txs": len(ranked),
                "input_sha256": digest,
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
