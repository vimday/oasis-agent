"""Select diverse transactions and similar pool sequences from a retained daily log export."""

import argparse
import csv
import json
import re
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date
from fractions import Fraction
from functools import lru_cache
from hashlib import sha256
from itertools import groupby, islice
from operator import itemgetter
from pathlib import Path

from oasis_agent.arbitrage.cycle_plan import address, hash32
from oasis_agent.evidence.swap_flow import MAX_SWAP_EVENTS

if __package__:
    from .rank_archive_swaps import BALANCER, V4, WIDTHS
else:
    from rank_archive_swaps import BALANCER, V4, WIDTHS

COLUMNS = [
    "day",
    "block_number",
    "block_hash",
    "transaction_hash",
    "transaction_index",
    "log_index",
    "address",
    "sig",
    "topic1",
    "data",
]
BANDS = ((5, "5-19"), (20, "20-49"), (50, "50-99"), (100, "100-199"), (200, "200+"))
MAX_LINE = 16_384


def natural(value):
    if type(value) is int and 0 <= value < 2**64:
        return value
    if isinstance(value, str) and re.fullmatch("0|[1-9][0-9]{0,19}", value) and int(value) < 2**64:
        return int(value)
    raise ValueError("invalid archive position")


def identity(row):
    day = row["day"]
    if date.fromisoformat(day).isoformat() != day:
        raise ValueError("invalid archive day")
    return (
        day,
        natural(row["block_number"]),
        hash32(row["block_hash"]),
        hash32(row["transaction_hash"]),
        natural(row["transaction_index"]),
    )


@lru_cache(maxsize=4096)
def venue_identity(sig, emitter, pool):
    return (sig, address(emitter), hash32(pool) if pool else "")


def event(row):
    sig = row["sig"]
    if sig not in WIDTHS:
        sig = hash32(sig)
    encoded = row["data"]
    if (
        sig not in WIDTHS
        or len(encoded) != 2 + 64 * WIDTHS[sig]
        or re.fullmatch(r"0x[0-9a-fA-F]*", encoded) is None
    ):
        raise ValueError("invalid event export")
    indexed = row["topic1"]
    if not isinstance(indexed, str) or re.fullmatch(r"0x[0-9a-fA-F]{64}", indexed) is None:
        raise ValueError("invalid indexed topic")
    # Amounts and trader addresses do not identify a venue. Singleton pool IDs do.
    return natural(row["log_index"]), venue_identity(
        sig, row["address"], indexed if sig in (V4, BALANCER) else ""
    )


def reference_logs(path):
    with path.open("rb") as stream:
        raw = stream.read(16_000_001)
    if len(raw) > 16_000_000:
        raise ValueError("oversized reference")
    logs = json.loads(raw)
    if not isinstance(logs, list) or not 1 <= len(logs) <= 4096:
        raise ValueError("expected one transaction's archive log rows")
    positions = []
    selected = []
    selected_data = []
    fixed = None
    for log in logs:
        row = {**log, "day": log["date"]}
        key = identity(row)
        if fixed is not None and key != fixed:
            raise ValueError("reference mixes transactions")
        fixed = key
        positions.append(natural(log["log_index"]))
        topics = log["topics"]
        if not isinstance(topics, list):
            raise ValueError("invalid reference topics")
        if topics and hash32(topics[0]) in WIDTHS:
            if len(topics) < 2:
                raise ValueError("missing indexed reference word")
            selected.append(event({**row, "sig": topics[0], "topic1": topics[1]}))
            selected_data.append((topics[1].lower(), row["data"].lower()))
    if positions != list(range(positions[0], positions[0] + len(logs))) or len(selected) < 5:
        raise ValueError("reference logs unordered, incomplete or below the screening threshold")
    return fixed, selected, selected_data, sha256(raw).hexdigest()


@dataclass(frozen=True)
class Candidate:
    day: str
    block: int
    block_hash: str
    tx: str
    index: int
    events: int
    distinct_venues: int
    ordered_family: str


def summarize(key, sequence, chain_id):
    venues = Counter(venue for _, venue in sequence)
    payload = [chain_id, [venue for _, venue in sequence]]
    digest = sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()
    return Candidate(
        *key, len(sequence), len({(emitter, pool) for _, emitter, pool in venues}), digest
    ), venues


def retain(rows, key, value, limit):
    if len(rows) == limit and key >= rows[-1][0]:
        return
    rows.append((key, value))
    rows.sort(key=lambda item: item[0])
    del rows[limit:]


class Day:
    def __init__(self):
        self.counts = Counter()
        self.families = {band: Counter() for _, band in BANDS}
        self.samples = {band: [] for _, band in BANDS}
        self.closest = []
        self.largest = []
        self.overlap = Counter()

    def include(self, candidate, venues, reference, ref_venues, limit):
        band = next(name for minimum, name in reversed(BANDS) if candidate.events >= minimum)
        family = candidate.ordered_family
        self.counts[band] += 1
        counts = self.families[band]
        # Sorted transaction hashes make the first distinct families a reproducible control.
        if family not in counts and len(self.samples[band]) < limit:
            self.samples[band].append(candidate)
        counts[family] += 1
        retain(self.largest, (-candidate.events, candidate.tx), candidate, limit * len(BANDS))
        if candidate.tx != reference.tx:
            shared = sum(min(n, ref_venues.get(venue, 0)) for venue, n in venues.items())
            self.overlap["compared"] += 1
            if shared:
                union = candidate.events + reference.events - shared
                self.overlap["nonzero"] += 1
                self.overlap["at_least_half"] += 2 * shared >= union
                self.overlap["same_ordered_family"] += family == reference.ordered_family
                retain(
                    self.closest,
                    (-Fraction(shared, union), candidate.tx),
                    (shared, union, candidate),
                    limit,
                )

    def report(self):
        selected = [c for group in self.samples.values() for c in group]
        baseline = [c for _, c in self.largest[: len(selected)]]
        return {
            "transactions": sum(self.counts.values()),
            "bands": {
                band: {
                    "transactions": self.counts[band],
                    "distinct_ordered_families": len(self.families[band]),
                    "selected": [asdict(c) for c in self.samples[band]],
                }
                for _, band in BANDS
            },
            "reference_comparisons": {
                key: self.overlap[key]
                for key in ("compared", "nonzero", "at_least_half", "same_ordered_family")
            },
            "closest": [
                {**asdict(c), "shared_observations": n, "union_observations": d}
                for _, (n, d, c) in self.closest
            ],
            "highest_event_baseline": [asdict(c) for c in baseline],
            "unique_families_at_equal_budget": {
                "stratified": len({c.ordered_family for c in selected}),
                "highest_event": len({c.ordered_family for c in baseline}),
            },
        }


def screen(archive: Path, reference: Path, *, chain_id: int, per_band: int = 3):
    if (
        type(chain_id) is not int
        or not 0 < chain_id < 2**64
        or type(per_band) is not int
        or not 1 <= per_band <= 100
    ):
        raise ValueError("invalid chain or representative count")
    reference_id, ref_sequence, ref_data, ref_sha = reference_logs(reference)
    ref, ref_venues = summarize(reference_id, ref_sequence, chain_id)
    days = {}
    heights = {ref.block: (ref.day, ref.block_hash)}
    block_numbers = {ref.block_hash: ref.block}
    positions = {}
    seen = set()
    digest = sha256()
    rows = 0
    previous = None
    with archive.open("rb") as source:

        def lines():
            while line := source.readline(MAX_LINE + 1):
                if len(line) > MAX_LINE:
                    raise ValueError("oversized archive row")
                digest.update(line)
                yield line.decode("utf-8")

        reader = csv.DictReader(lines())
        if reader.fieldnames != COLUMNS:
            raise ValueError("expected the ordered eight-topic archive export columns")
        for _, group in groupby(reader, key=itemgetter(*COLUMNS[:5])):
            rows_for_tx = list(islice(group, MAX_SWAP_EVENTS + 1))
            if not 5 <= len(rows_for_tx) <= MAX_SWAP_EVENTS or any(
                None in row or None in row.values() for row in rows_for_tx
            ):
                raise ValueError("unexpected archive transaction size or columns")
            key = identity(rows_for_tx[0])
            day, height, block, tx, index = key
            block_positions = positions.setdefault(block, {})
            if (
                (previous is not None and (day, tx) <= previous)
                or tx in seen
                or index in block_positions
            ):
                raise ValueError("unordered or repeated archive transaction")
            if (
                heights.setdefault(height, (day, block)) != (day, block)
                or block_numbers.setdefault(block, height) != height
            ):
                raise ValueError("conflicting archive block")
            sequence = [event(row) for row in rows_for_tx]
            indices = [i for i, _ in sequence]
            if indices != sorted(set(indices)):
                raise ValueError("unordered or duplicate log position")
            if tx == ref.tx and (
                key != reference_id
                or sequence != ref_sequence
                or [(r["topic1"].lower(), r["data"].lower()) for r in rows_for_tx] != ref_data
            ):
                raise ValueError("archive differs from reference logs")
            candidate, venues = summarize(key, sequence, chain_id)
            if day not in days:
                days[day] = Day()
            days[day].include(candidate, venues, ref, ref_venues, per_band)
            seen.add(tx)
            block_positions[index] = (indices[0], indices[-1])
            previous = (day, tx)
            rows += candidate.events
    for block_positions in positions.values():
        last = -1
        for index in sorted(block_positions):
            first, end = block_positions[index]
            if first <= last:
                raise ValueError("overlapping or unordered receipt log ranges")
            last = end
    if not rows:
        raise ValueError("empty candidate archive")
    return {
        "schema": "oasis-multihop-archive-screen-v1",
        "chain_id": chain_id,
        "archive_sha256": digest.hexdigest(),
        "reference_logs_sha256": ref_sha,
        "reference": asdict(ref),
        "reference_in_archive": ref.tx in seen,
        "event_rows": rows,
        "per_day_per_band_limit": per_band,
        "days": {day: counts.report() for day, counts in days.items()},
    }


def audit_queue(report, reviewed: Path):
    """Keep the frozen selection, deduplicate trace work and coalesce block reads."""
    with reviewed.open("rb") as src:
        raw = src.read(16_000_001)
    if len(raw) > 16_000_000:
        raise ValueError("oversized reviewed list")
    entries = json.loads(raw)
    if not isinstance(entries, list):
        raise ValueError("expected a list of reviewed transaction identities")
    known = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"chain_id", "block_hash", "tx"}:
            raise ValueError("invalid reviewed identity")
        chain = entry["chain_id"]
        if type(chain) is not int or not 0 < chain < 2**64:
            raise ValueError("invalid reviewed chain")
        known.add((chain, hash32(entry["block_hash"]), hash32(entry["tx"])))
    chain = report["chain_id"]
    work = {}
    for group in report["days"].values():
        lanes = [("reference_overlap", group["closest"])]
        lanes.extend((band, group["bands"][band]["selected"]) for _, band in BANDS)
        for reason, candidates in lanes:
            for c in candidates:
                key = (chain, c["block_hash"], c["tx"])
                if key not in work:
                    work[key] = {
                        "block": c["block"],
                        "block_hash": c["block_hash"],
                        "index": c["index"],
                        "tx": c["tx"],
                        "selected_by": [],
                    }
                work[key]["selected_by"].append(reason)
    remaining = [row for key, row in work.items() if key not in known]
    blocks = len({row["block_hash"] for row in remaining})
    return {
        "chain_id": chain,
        "reviewed_sha256": sha256(raw).hexdigest(),
        "selected_unique": len(work),
        "already_reviewed": len(work) - len(remaining),
        "blocks_to_check": blocks,
        "traces_to_check": len(remaining),
        "max_reads_before_cache": 2 * blocks + len(remaining),
        "transactions": remaining,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "archive", type=Path, help="Eight-topic CSV sorted by UTC day, tx hash and log index"
    )
    parser.add_argument(
        "--reference-logs",
        type=Path,
        required=True,
        help="Single transaction's ordered AWS Ethereum log rows in JSON",
    )
    parser.add_argument(
        "--chain-id", type=int, required=True, help="Network of both retained inputs"
    )
    parser.add_argument("--per-band", type=int, default=3)
    parser.add_argument(
        "--reviewed",
        type=Path,
        help="Add an audit queue, skipping listed {chain_id, block_hash, tx} identities",
    )
    args = parser.parse_args()
    report = screen(
        args.archive, args.reference_logs, chain_id=args.chain_id, per_band=args.per_band
    )
    if args.reviewed is not None:
        report["audit_queue"] = audit_queue(report, args.reviewed)
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
