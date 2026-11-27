"""Build the November 2026 four-chain weak screen from retained public logs, offline."""

import argparse
import csv
import gzip
import hashlib
import io
import json
from collections import Counter
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path

from oasis_agent.arbitrage.cycle_plan import address, hash32
from oasis_agent.arbitrage.cycle_protocols import topic

RAW = Path("reports/research")
OUT = RAW / "multichain-inventory-20261127"
MANIFEST = Path("docs/verification/MULTIHOP_SCREENED_SOURCES_20261127.json")
COLUMNS = ("chain_id", "tx_hash", "block_number", "matched_events", "source")
NOV23_DECOMPRESSED_SHA256 = "9cf45ec8018bc79f17124adef0ce124af879671d3a4c69a870452eae10aed0c5"
SWAPS = {
    topic(signature): name
    for name, signature in {
        "v2": "Swap(address,uint256,uint256,uint256,uint256,address)",
        "aerodrome": "Swap(address,address,uint256,uint256,uint256,uint256)",
        "v3": "Swap(address,address,int256,int256,uint160,uint128,int24)",
        "pancake_v3": "Swap(address,address,int256,int256,uint160,uint128,int24,uint128,uint128)",
        "v4": "Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)",
        "infinity_cl": "Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24,uint16)",
    }.items()
}
ETH_SWAPS = {
    key: name for key, name in SWAPS.items() if name in ("v2", "v3", "pancake_v3", "v4")
} | {
    topic(signature): name
    for name, signature in {
        "balancer_v2": "Swap(bytes32,address,address,uint256,uint256)",
        "curve": "TokenExchange(address,int128,uint256,int128,uint256)",
        "curve_crypto": "TokenExchange(address,uint256,uint256,uint256,uint256,uint256,uint256)",
        "bancor": "Conversion(address,address,address,uint256,uint256,int256)",
    }.items()
}
Row = tuple[int, str, int, int, str]
Anchor = tuple[int, str, int]


def require(condition: bool, detail: object) -> None:
    if not condition:
        raise ValueError(detail)


def quantity(value: object) -> int:
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, str):
        if value.startswith("0x") and value[2:] and all(c in "0123456789abcdef" for c in value[2:]):
            return int(value, 16)
        if value.isascii() and value.isdecimal():
            return int(value)
    raise ValueError(f"invalid unsigned log quantity {value!r}")


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def fingerprint(path: Path, raw: bytes) -> dict:
    return {"path": path.as_posix(), "bytes": len(raw), "sha256": digest(raw)}


def read_json(path: Path) -> tuple[dict, dict]:
    raw = path.read_bytes()
    return json.loads(raw), fingerprint(path, raw)


def verify_hash(expected: str, actual: dict) -> None:
    require(expected == actual["sha256"], ("source SHA mismatch", actual["path"]))


@dataclass
class Screen:
    key: str
    chain_id: int
    threshold: int
    kind: str
    scope: str
    signatures: dict[str, str]
    files: list[dict] = field(default_factory=list)
    by_tx: dict[str, Anchor] = field(default_factory=dict)
    by_block: dict[int, str] = field(default_factory=dict)
    parent_by_block: dict[int, str] = field(default_factory=dict)
    events: dict[tuple[int, int], tuple] = field(default_factory=dict)
    counts: Counter[str] = field(default_factory=Counter)
    topic_counts: Counter[str] = field(default_factory=Counter)
    input_logs: int = 0
    duplicates: int = 0

    def block(self, height: int, block_hash: str) -> None:
        require(
            self.by_block.setdefault(height, block_hash) == block_hash,
            (self.key, "block conflict", height),
        )

    def transaction(self, tx: str, anchor: Anchor) -> None:
        self.block(anchor[0], anchor[1])
        require(
            self.by_tx.setdefault(tx, anchor) == anchor,
            (self.key, "transaction identity conflict", tx),
        )

    def event(self, log: dict, height: int, block_hash: str, allowed: set[str] | None) -> None:
        self.input_logs += 1
        if log.get("removed") is True:
            raise ValueError((self.key, "removed log in historical capture"))
        topics = tuple(hash32(value) for value in log["topics"])
        signature = topics[0] if topics else None
        if allowed is not None:
            require(signature in allowed, (self.key, "unexpected topic", signature))
        if signature is None or signature not in self.signatures:
            return
        tx = hash32(log["transactionHash"])
        pos = quantity(log["logIndex"])
        sender = address(log["address"])
        tx_index = quantity(log["transactionIndex"])
        self.transaction(tx, (height, block_hash, tx_index))
        key = (height, pos)
        evidence = (tx, tx_index, sender, topics, log.get("data"))
        if key in self.events:
            require(self.events[key] == evidence, (self.key, "log identity conflict", key))
            self.duplicates += 1
            return
        self.events[key] = evidence
        self.counts[tx] += 1
        self.topic_counts[self.signatures[signature]] += 1

    def export(self) -> list[Row]:
        return [
            (self.chain_id, tx, self.by_tx[tx][0], matched, self.key)
            for tx, matched in self.counts.items()
            if matched >= self.threshold
        ]

    def manifest(self) -> dict:
        rows = self.export()
        return {
            "source": self.key,
            "chain_id": self.chain_id,
            "kind": self.kind,
            "scope": self.scope,
            "minimum_matched_events_per_transaction": self.threshold,
            "source_files": self.files,
            "input_rows_or_logs": self.input_logs,
            "matched_events_before_threshold": sum(self.counts.values()),
            "matched_event_counts_by_signature": dict(self.topic_counts) or None,
            "distinct_matched_transactions_before_threshold": len(self.counts),
            "duplicate_event_rows_removed": self.duplicates,
            "exported_rows": len(rows),
            "maximum_matched_events": max(self.counts.values(), default=0),
        }


def archive(path: Path, dates: tuple[str, ...], coverage: Path, frequency: Path) -> list[Screen]:
    raw = path.read_bytes()
    by_day = {
        day: Screen(
            f"eth_archive_{day[-2:]}",
            1,
            5,
            "ethereum_full_day_log_projection",
            f"UTC {day}; unfiltered address universe in the eight-signature daily scan.",
            ETH_SWAPS,
        )
        for day in dates
    }
    meta = fingerprint(path, raw)
    coverage_raw = coverage.read_bytes()
    frequency_raw = frequency.read_bytes()
    if path.suffix == ".gz":
        raw = gzip.decompress(raw)
        meta["decompressed_sha256"] = digest(raw)
        verify_hash(
            NOV23_DECOMPRESSED_SHA256, {"path": str(path), "sha256": meta["decompressed_sha256"]}
        )
    if frequency.suffix == ".csv":
        frequency_rows = list(csv.DictReader(io.StringIO(frequency_raw.decode())))
        expected = {
            day: {
                int(row["events"]): int(row["txs"])
                for row in frequency_rows
                if row["day"] == day and int(row["events"]) >= 5
            }
            for day in dates
        }
    else:
        frozen = json.loads(frequency_raw)
        verify_hash(frozen["archive_sha256"], meta)
        expected = {day: frozen["days"][day]["transactions"] for day in dates}
    for screen in by_day.values():
        screen.files.append(
            meta
            | {
                "coverage_record": fingerprint(coverage, coverage_raw),
                "frequency_witness": fingerprint(frequency, frequency_raw),
            }
        )
    topics = set(ETH_SWAPS)
    with io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8", newline="") as lines:
        for item in csv.DictReader(lines):
            by_day[item["day"]].event(
                {
                    "address": item["address"],
                    "logIndex": item["log_index"],
                    "transactionHash": item["transaction_hash"],
                    "transactionIndex": item["transaction_index"],
                    "topics": [item["sig"], item["topic1"]] if item["topic1"] else [item["sig"]],
                    "data": item["data"],
                },
                quantity(item["block_number"]),
                hash32(item["block_hash"]),
                topics,
            )
    for day, screen in by_day.items():
        require(
            bool(screen.counts) and min(screen.counts.values()) >= 5,
            (screen.key, "unexpected full-day CSV coverage or distinct event denominator"),
        )
        witness = expected[day]
        actual = (
            Counter(screen.counts.values()) if isinstance(witness, dict) else len(screen.counts)
        )
        require(actual == witness, (day, "full-day hash/event denominator disagrees with witness"))
    return list(by_day.values())


def prescreen(path: Path, column: str, key: str, scope: str) -> Screen:
    screen = Screen(key, 1, 5, "ethereum_secondary_selection", scope, ETH_SWAPS)
    raw = path.read_bytes()
    screen.files.append(fingerprint(path, raw))
    with io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8", newline="") as lines:
        for item in csv.DictReader(lines):
            screen.input_logs += 1
            tx = hash32(item["transaction_hash"])
            screen.transaction(
                tx,
                (
                    quantity(item["block_number"]),
                    hash32(item["block_hash"]),
                    quantity(item["transaction_index"]),
                ),
            )
            require(tx not in screen.counts, (key, "duplicate prescreen row", tx))
            screen.counts[tx] = quantity(item[column])
    return screen


def ledger_hashes(path: Path, response_id: int) -> tuple[str, str, dict]:
    raw = path.read_bytes()
    entries = [e for e in map(json.loads, raw.splitlines()) if e.get("id") == response_id]
    sent, received = entries
    require(sent.get("action", sent.get("type")) == "reserve", (path, response_id))
    require(
        received.get("action", received.get("type")) in ("received", "receive"), (path, response_id)
    )
    require(received.get("http", received.get("status")) == 200, (path, response_id))
    return sent["sha256"], received["sha256"], fingerprint(path, raw)


def rpc(screen: Screen, response: Path, request: Path, witness: Path) -> None:
    body, response_meta = read_json(response)
    call, request_meta = read_json(request)
    require(call["method"] == "eth_getLogs" and call["id"] == body["id"], (request, response))
    if witness.suffix == ".jsonl":
        req_sha, res_sha, witness_meta = ledger_hashes(witness, call["id"])
    else:
        proof, witness_meta = read_json(witness)
        req_sha, res_sha = proof["request_sha256"], proof["response_sha256"]
        require(proof.get("body_complete", True) is True, witness)
    verify_hash(req_sha, request_meta)
    verify_hash(res_sha, response_meta)
    (filters,) = call["params"]
    choices = filters["topics"][0]
    allowed = {hash32(x) for x in (choices if isinstance(choices, list) else [choices])}
    require(allowed <= SWAPS.keys(), (request, "unknown requested topic"))
    first, last = quantity(filters["fromBlock"]), quantity(filters["toBlock"])
    addrs = filters.get("address")
    emitters = (
        None
        if addrs is None
        else {address(value) for value in (addrs if isinstance(addrs, list) else [addrs])}
    )
    logs = body["result"]
    require(isinstance(logs, list), response)
    for log in logs:
        height = quantity(log["blockNumber"])
        require(first <= height <= last, (response, "log outside requested heights", height))
        require(
            emitters is None or address(log["address"]) in emitters,
            (response, "log outside requested emitters"),
        )
        screen.event(log, height, hash32(log["blockHash"]), allowed)
    screen.files.append(
        response_meta
        | {
            "request": request_meta,
            "response_digest_witness": witness_meta,
            "requested_from_block": first,
            "requested_through_block": last,
            "response_log_rows": len(logs),
            "requested_topics": sorted(allowed),
            "requested_emitter_count": None if emitters is None else len(emitters),
        }
    )


def sqd(screen: Screen, directory: Path, truncated: bool = False) -> None:
    path = directory / "response.ndjson"
    req, request_meta = read_json(directory / "request.json")
    done, done_meta = read_json(directory / "done.json")
    raw = path.read_bytes()
    meta = fingerprint(path, raw)
    require(req["includeAllBlocks"] is True, directory)
    allowed = {hash32(item) for query in req.get("logs", []) for item in query["topic0"]}
    require(allowed <= SWAPS.keys(), (directory, "unknown requested topic"))
    next_height = first = quantity(req["fromBlock"])
    parent = None
    consumed_bytes = 0
    tail = b""
    with io.BytesIO(raw) as lines:
        for line in lines:
            if not line.endswith(b"\n") and truncated and lines.tell() == len(raw):
                tail = line
                break
            block = json.loads(line)
            header = block["header"]
            height, block_hash = quantity(header["number"]), hash32(header["hash"])
            parent_hash = hash32(header["parentHash"])
            require(
                height == next_height and (parent is None or parent_hash == parent),
                (directory, height),
            )
            screen.block(height, block_hash)
            screen.parent_by_block[height] = parent_hash
            for log in block.get("logs", []):
                screen.event(log, height, block_hash, allowed or None)
            next_height += 1
            parent = block_hash
            consumed_bytes += len(line)
    if truncated:
        require(
            next_height - first == 4078
            and bool(tail)
            and done.get("stopped") is True
            and "response byte ceiling" in done.get("error", ""),
            (directory, "unexpected truncated archive prefix"),
        )
        meta["unparsed_truncated_tail"] = {"bytes": len(tail), "sha256": digest(tail)}
        meta["complete_line_prefix_sha256"] = digest(raw[:consumed_bytes])
    else:
        verify_hash(done["response_sha256"], meta)
        require(
            first == done["verified_from"] and next_height - 1 == done["verified_to"], directory
        )
    require(first < next_height <= quantity(req["toBlock"]) + 1, directory)
    screen.files.append(
        meta
        | {
            "request": request_meta,
            "capture_result": done_meta,
            "requested_from_block": first,
            "requested_through_block": req["toBlock"],
            "covered_from_block": first,
            "covered_through_block": next_height - 1,
            "covered_blocks": next_height - first,
            "missing_requested_blocks": req["toBlock"] - next_height + 1,
            "expected_topic_filter": sorted(allowed) or None,
        }
    )


def log_screen(chain: int, key: str, kind: str, scope: str) -> Screen:
    return Screen(key, chain, 2, kind, scope, SWAPS)


def inputs() -> list[Screen]:
    screens = archive(
        RAW / "eth-open-archive-20261127/candidates.csv",
        ("2026-11-13", "2026-11-25"),
        RAW / "eth-open-archive-20261127/coverage.csv",
        RAW / "eth-open-archive-20261127/hist.csv",
    )
    screens += archive(
        RAW / "multihop-family-screen-20261127/newday.csv",
        ("2026-11-24",),
        RAW / "multihop-family-screen-20261127/newday-screen.json",
        RAW / "multihop-family-screen-20261127/newday-screen.json",
    )
    screens += archive(
        OUT / "eth-nov23-candidates.csv.gz",
        ("2026-11-23",),
        RAW / "eth-forward-and-tail-20261127/historical/aws-coverage.csv",
        RAW / "eth-forward-and-tail-20261127/historical/aws-hist.csv",
    )
    screens += [
        prescreen(
            RAW / "eth-forward-and-tail-20261127/historical/aws-weth-candidates.csv",
            "events",
            "eth_weth_23",
            "Nov 23 WETH manager-transfer subset; archived aggregate event counts.",
        ),
        prescreen(
            RAW / "eth-batch-cohort-20261127/rankable.csv",
            "shapes",
            "eth_rankable",
            "Nov 13, 24, 25 selected executor or count-bin rows; archived aggregate shape counts.",
        ),
    ]
    for chain, directory, key, scope, truncated in (
        (
            8453,
            "long-dex-20261123/base-mainnet",
            "base_sqd_long",
            "Six topics, full address universe.",
            False,
        ),
        (
            8453,
            "pretrade-tail-20261123/base-3",
            "base_sqd_tail",
            "Six topics, full address universe.",
            False,
        ),
        (
            56,
            "long-dex-20261123/binance-mainnet",
            "bsc_sqd_long",
            "Six topics, full address universe.",
            False,
        ),
        (
            56,
            "long-dex-20261123/expanded/binance-mainnet",
            "bsc_sqd_expanded",
            "Six topics, full address universe.",
            False,
        ),
        (
            56,
            "long-dex-20261123/executor-lookback/binance-mainnet",
            "bsc_sqd_executor",
            "One destination filter, full log rows for matched transactions, partial block range.",
            False,
        ),
        (
            56,
            "pretrade-tail-20261123/bsc-1",
            "bsc_sqd_tail",
            "Six topics, full address universe. Complete prefix through the byte-ceiling stop.",
            True,
        ),
    ):
        item = log_screen(chain, key, "sqd_index_log_projection", scope)
        sqd(item, RAW / directory, truncated)
        screens.append(item)
    for chain, directory, key, scope, names, suffixes in (
        (
            8453,
            "recent-dex-arbitrage-20261123/base",
            "base_rpc_early",
            "Four disjoint 96-block log windows.",
            tuple(f"{i:02}" for i in range(5, 9)),
            (".response.json", ".request.json", "ledger.jsonl"),
        ),
        (
            4663,
            "long-dex-20261123/robinhood-rpc",
            "rh_rpc_long",
            "Eight adjacent 512-block log windows.",
            tuple(f"{i:02}" for i in range(2, 10)),
            (".res", ".req", "ledger.jsonl"),
        ),
        (
            4663,
            "multichain-depth-20261123/robinhood",
            "rh_rpc_depth",
            "Three disjoint 512-block windows, V3/V4 family only.",
            ("03", "04", "05"),
            (".response.json", ".request.json", ".meta.json"),
        ),
        (
            4663,
            "robinhood-tail-512-20261124/run",
            "rh_rpc_tail",
            "One 512-block V3/V4 topic window.",
            ("01-official",),
            (".response", ".request", ".meta"),
        ),
        (
            56,
            "bsc-eight-address-holdout-20261124",
            "bsc_rpc_eight",
            "Eight-address holdout with two pages.",
            ("page0", "page1"),
            (".response.json", ".request.json", ".meta.json"),
        ),
        (
            56,
            "bsc-four-pool-tail-20261124",
            "bsc_rpc_four",
            "Four address-filtered pools in one fixed window.",
            ("logs",),
            (".response.json", ".request.json", ".meta.json"),
        ),
        (
            56,
            "bsc-alternate-long-tail-20261124/single-pool-followup",
            "bsc_rpc_single",
            "Single address-filtered pool; repeated swaps can qualify.",
            ("logs",),
            (".response.json", ".request.json", ".meta.json"),
        ),
    ):
        item = log_screen(chain, key, "eth_getLogs", scope)
        root = RAW / directory
        response_suffix, request_suffix, witness_suffix = suffixes
        for name in names:
            witness = witness_suffix if witness_suffix == "ledger.jsonl" else name + witness_suffix
            rpc(
                item,
                root / (name + response_suffix),
                root / (name + request_suffix),
                root / witness,
            )
        screens.append(item)
    return screens


def render() -> tuple[bytes, bytes]:
    screens = inputs()
    identities: dict[tuple[int, str], Anchor] = {}
    for screen in screens:
        for tx, anchor in screen.by_tx.items():
            key = (screen.chain_id, tx)
            require(identities.setdefault(key, anchor) == anchor, ("cross-source tx conflict", key))
    full_days = {
        tx: matched
        for screen in screens
        if screen.kind == "ethereum_full_day_log_projection"
        for tx, matched in screen.counts.items()
    }
    for screen in screens:
        if screen.kind == "ethereum_secondary_selection":
            for _, tx, _, matched, _ in screen.export():
                require(full_days.get(tx) == matched, (screen.key, "daily count mismatch", tx))
    overlaps = []
    seams = []
    for left, right in combinations(screens, 2):
        if left.chain_id != right.chain_id:
            continue
        common = left.by_block.keys() & right.by_block.keys()
        mismatches = [
            height for height in sorted(common) if left.by_block[height] != right.by_block[height]
        ]
        require(
            not mismatches, (left.key, right.key, "cross-source block conflict", mismatches[:5])
        )
        if common:
            overlaps.append(
                {
                    "sources": [left.key, right.key],
                    "matching_block_hashes": len(common),
                    "first_common_height": min(common),
                    "last_common_height": max(common),
                }
            )
        for previous, following in ((left, right), (right, left)):
            if previous.kind != "sqd_index_log_projection" or not following.parent_by_block:
                continue
            first = min(following.parent_by_block)
            if max(previous.by_block) == first - 1:
                parent = following.parent_by_block[first]
                require(
                    previous.by_block[first - 1] == parent,
                    (previous.key, following.key, "cross-source parent conflict", first),
                )
                seams.append(
                    {
                        "sources": [previous.key, following.key],
                        "child_height": first,
                        "parent_hash": parent,
                        "child_hash": following.by_block[first],
                    }
                )
    rows = sorted(row for screen in screens for row in screen.export())
    identities_by_chain = Counter(chain for chain, _ in {(row[0], row[1]) for row in rows})
    rows_by_chain = Counter(row[0] for row in rows)
    out = io.StringIO(newline="")
    writer = csv.writer(out, delimiter="\t", lineterminator="\n")
    writer.writerow(COLUMNS)
    writer.writerows(rows)
    table = out.getvalue().encode()
    manifest = {
        "schema": "oasis.multihop.screened-sources.v1",
        "date": "2026-11-27",
        "screened_tsv": fingerprint(OUT / "screened.tsv", table)
        | {"columns": COLUMNS, "rows": len(rows)},
        "rows_by_chain_id": dict(rows_by_chain),
        "unique_transactions_by_chain_id": dict(sorted(identities_by_chain.items())),
        "ethereum_archive_event_signatures": {name: sig for sig, name in ETH_SWAPS.items()},
        "evm_filtered_event_signatures": {name: sig for sig, name in SWAPS.items()},
        "cross_source_anchor_checks": {
            "same_height_overlaps": overlaps,
            "sqd_adjacent_parent_checks": seams,
            "block_hash_conflicts": 0,
            "transaction_anchor_conflicts": 0,
            "quarantined_sources": [],
            "scope": "Retained-source anchor comparison. Receipt roots remain unverified here.",
        },
        "method": (
            "Each row preserves one transaction in one source. The Ethereum archive uses at least "
            "five distinct matched log indices from its original eight-signature set. Other EVM "
            "captures use at least two distinct matched log indices from six supported Swap "
            "signatures. The builder deduplicates exact in-source logs and reconciles secondary "
            "Ethereum aggregate event counts to the full-day counts."
        ),
        "limits": (
            "This public signature screen leaves pool bytecode, receipt roots, direction, funding, "
            "amounts, gas, attribution and profitability unverified. SQD projections omit event "
            "data. RPC logs retain address and topic filters. Coverage beyond listed ranges and "
            "filters is unknown. The same transaction can meet different source filters."
        ),
        "sources": [screen.manifest() for screen in screens],
    }
    return table, (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUT / "screened.tsv")
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    args = parser.parse_args()
    table, manifest = render()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(table)
    args.manifest.write_bytes(manifest)


if __name__ == "__main__":
    main()
