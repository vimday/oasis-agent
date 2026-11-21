"""Replay pool discovery and optional hash-pinned quotes for a sealed shadow window."""

import argparse
import sqlite3
import sys
from pathlib import Path

from oasis_agent.arbitrage.cycle_evidence import load_exports, read_bounded
from oasis_agent.arbitrage.cycle_plan import Plan
from oasis_agent.arbitrage.cycle_queue import CycleShadowQueue
from oasis_agent.arbitrage.cycle_shadow import ShadowWindow, replay_shadow
from oasis_agent.evidence.chain_store import CanonicalChainStore
from oasis_agent.evidence.ingestion import replay_block_range_evidence
from oasis_agent.evidence.rpc_validation import strict_json_object
from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.models.common import evidence_ref_from_json
from scripts.import_public_range import import_range
from scripts.run_cycle_research import _publish, _source


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("cycle-plan", "window", "store", "output"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--refs", type=Path)
    parser.add_argument(
        "--capture", type=_source, action="append", default=[], help="FILTER=CAPTURE"
    )
    parser.add_argument("--export", type=_source, action="append", default=[])
    parser.add_argument("--chain-store", type=Path, help="persist verified per-filter coverage")
    parser.add_argument("--queue-session", help="freeze a named offline replay outbox session")
    args = parser.parse_args()
    if args.queue_session and args.chain_store is None:
        parser.error("--queue-session requires --chain-store")
    queue = None
    try:
        if args.queue_session:
            queue = CycleShadowQueue(args.chain_store)
        cycle_raw = read_bounded(args.cycle_plan, 128 * 1024)
        window_raw = read_bounded(args.window, 16 * 1024)
        strict_json_object(cycle_raw)
        strict_json_object(window_raw)
        plan = Plan.model_validate_json(cycle_raw)
        window = ShadowWindow.model_validate_json(window_raw)
        if args.refs is None and not args.capture:
            raise ValueError("refs or public captures required")
        raw_refs = strict_json_object(read_bounded(args.refs, 64 * 1024)) if args.refs else {}
        refs = {}
        for name, item in raw_refs.items():
            if not isinstance(item, dict) or not item.get("retrieved_at"):
                raise ValueError("retained evidence timestamp required")
            refs[name] = evidence_ref_from_json(item)
        store = EvidenceStore(args.store, require_existing=not args.capture)
        try:
            if len(args.capture) > 64:
                raise ValueError("capture limit")
            for name, path in args.capture:
                if name in refs:
                    raise ValueError("duplicate filter capture")
                refs[name] = import_range(path, store).evidence
            quotes = load_exports(args.export, plan.sources) if args.export else None
            report = (
                queue.publish(args.queue_session, plan, window, store, refs, quote_records=quotes)
                if queue is not None
                else replay_shadow(plan, window, store, refs, quote_records=quotes)
            )
            if args.chain_store is not None and queue is None:
                chain_store = CanonicalChainStore(args.chain_store)
                try:
                    # Validate the complete input before mutating; all filters commit together.
                    # Missing filters remain missing in this report even if older data exists.
                    with chain_store.atomic():
                        for name, ref in sorted(refs.items()):
                            chain_store.apply(
                                f"{window.cycle_config_sha256}:{name}",
                                replay_block_range_evidence(store, ref),
                                evidence=ref,
                            )
                        report["retained_filter_coverage"] = {
                            name: chain_store.coverage(
                                f"{window.cycle_config_sha256}:{name}",
                                plan.chain_id,
                                window.start_block,
                                window.end_block,
                            )
                            for name in report["coverage"]
                        }
                finally:
                    chain_store.close()
        finally:
            store.close()
    except (ValueError, TypeError, OSError, sqlite3.Error) as exc:
        if queue is not None:
            try:
                queue.fault(args.queue_session)
            except ValueError, OSError, sqlite3.Error:
                print(
                    "Queue fault could not be recorded; inspect its original database",
                    file=sys.stderr,
                )
        print(f"Shadow replay refused ({type(exc).__name__})", file=sys.stderr)
        return 2
    finally:
        if queue is not None:
            queue.close()
    try:
        _publish(args.output, report)
    except (ValueError, TypeError, OSError) as exc:
        print(
            f"Shadow output refused ({type(exc).__name__}); inspect retained queue", file=sys.stderr
        )
        return 2
    print(report["status"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
