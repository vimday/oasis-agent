"""Inspect or consume one durable, historical quote replay; no network or execution."""

import argparse
import sqlite3
import sys
from dataclasses import asdict
from pathlib import Path

from oasis_agent.arbitrage.cycle_evidence import load_exports
from oasis_agent.arbitrage.cycle_queue import CycleShadowQueue
from oasis_agent.evidence.store import EvidenceStore

from .run_cycle_research import _publish, _source


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("show", "run-one"))
    parser.add_argument("--chain-store", type=Path, required=True)
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--worker", default="offline-worker")
    parser.add_argument("--lease-ms", type=int, default=30000)
    parser.add_argument("--export", type=_source, action="append", default=[])
    args = parser.parse_args()
    if args.command == "run-one" and not args.export:
        parser.error("run-one requires retained quote --export inputs")
    try:
        if not args.chain_store.is_file():
            raise ValueError("existing queue database required")
        queue = CycleShadowQueue(args.chain_store)
        try:
            store = EvidenceStore(args.store, require_existing=True)
            try:
                lease = None
                if args.command == "run-one":
                    plan, _ = queue.definition(args.session, store)
                    records = load_exports(args.export, plan.sources)
                    lease = queue.claim(args.session, store, args.worker, lease_ms=args.lease_ms)
                    if lease is not None:
                        queue.complete(args.session, store, lease, records)
                view = queue.inspect(args.session, store)
                report = {
                    "schema": "oasis-cycle-outbox-worker-v1",
                    "lease": asdict(lease) if lease else None,
                    "outbox": view,
                    "execution_eligible": False,
                }
            finally:
                store.close()
        finally:
            queue.close()
        _publish(args.output, report)
    except (ValueError, TypeError, OSError, sqlite3.Error) as exc:
        print(f"Offline queue operation refused ({type(exc).__name__})", file=sys.stderr)
        return 2
    print(view["state"])
    return 3 if view["state"] == "invalidated" else 0


if __name__ == "__main__":
    raise SystemExit(main())
