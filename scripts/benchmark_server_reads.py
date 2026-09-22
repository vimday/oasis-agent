"""Synthetic local journal-read benchmark; no RPC, capital, or live-data claims.

Run from the repository with ``python -m scripts.benchmark_server_reads``.
The fixture builder is intentionally shared with the development test suite.
"""

import argparse
import json
import platform
import sqlite3
import statistics
import tempfile
import time
from pathlib import Path

from oasis_agent.server.databases import ReadOnlyDatabases
from tests.unit.server_helpers import build_shadow_db, make_server_config


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=20_000)
    parser.add_argument("--samples", type=int, default=200)
    args = parser.parse_args()
    if args.rows < 1 or args.samples < 20:
        parser.error("rows must be positive and samples must be at least 20")
    with tempfile.TemporaryDirectory(prefix="oasis-read-benchmark-") as directory:
        root = Path(directory)
        config = make_server_config(root)
        build_shadow_db(root / "shadow.db", health_count=args.rows, decision_count=args.rows)
        db = ReadOnlyDatabases(config)
        try:
            started = time.perf_counter_ns()
            records = db.decision_chain()
            cold_ns = time.perf_counter_ns() - started
            assert len(records) == args.rows
            values = []
            for _ in range(args.samples):
                started = time.perf_counter_ns()
                db.decision_chain()
                values.append(time.perf_counter_ns() - started)
            print(
                json.dumps(
                    {
                        "kind": "synthetic_journal_read_benchmark",
                        "python": platform.python_version(),
                        "sqlite": sqlite3.sqlite_version,
                        "rows_per_chain": args.rows,
                        "samples": args.samples,
                        "cold_ms": cold_ns / 1_000_000,
                        "warm_median_us": statistics.median(values) / 1_000,
                        "warm_p95_us": sorted(values)[(95 * len(values) + 99) // 100 - 1] / 1_000,
                    },
                    indent=2,
                )
            )
        finally:
            db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
