"""Replay pool discovery and optional hash-pinned quotes for a sealed shadow window."""

import argparse
import sqlite3
import sys
from pathlib import Path

from oasis_agent.arbitrage.cycle_evidence import load_exports, read_bounded
from oasis_agent.arbitrage.cycle_plan import Plan
from oasis_agent.arbitrage.cycle_shadow import ShadowWindow, replay_shadow
from oasis_agent.evidence.rpc_validation import strict_json_object
from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.models.common import evidence_ref_from_json
from scripts.run_cycle_research import _publish, _source


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("cycle-plan", "window", "store", "refs", "output"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--export", type=_source, action="append", default=[])
    args = parser.parse_args()
    try:
        cycle_raw = read_bounded(args.cycle_plan, 128 * 1024)
        window_raw = read_bounded(args.window, 16 * 1024)
        strict_json_object(cycle_raw)
        strict_json_object(window_raw)
        plan = Plan.model_validate_json(cycle_raw)
        window = ShadowWindow.model_validate_json(window_raw)
        raw_refs = strict_json_object(read_bounded(args.refs, 64 * 1024))
        refs = {}
        for name, item in raw_refs.items():
            if not isinstance(item, dict) or not item.get("retrieved_at"):
                raise ValueError("retained evidence timestamp required")
            refs[name] = evidence_ref_from_json(item)
        store = EvidenceStore(args.store, require_existing=True)
        try:
            quotes = load_exports(args.export, plan.sources) if args.export else None
            report = replay_shadow(plan, window, store, refs, quote_records=quotes)
        finally:
            store.close()
        _publish(args.output, report)
    except (ValueError, TypeError, OSError, sqlite3.Error) as exc:
        print(f"Shadow replay refused ({type(exc).__name__})", file=sys.stderr)
        return 2
    print(report["status"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
