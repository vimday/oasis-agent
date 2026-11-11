"""Reconcile historical case receipts and a block-end call from exact retained bytes."""

import argparse
import sys
from pathlib import Path

from oasis_agent.arbitrage.cycle_evidence import read_bounded
from oasis_agent.arbitrage.cycle_plan import Plan
from oasis_agent.arbitrage.cycle_replay import replay_case
from oasis_agent.evidence.rpc_validation import strict_json_object
from scripts.run_cycle_research import _publish


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("plan", "primary", "secondary", "block-end", "output"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    args = parser.parse_args()
    try:
        raw = read_bounded(args.plan, 128 * 1024)
        strict_json_object(raw)
        plan = Plan.model_validate_json(raw)
        report = replay_case(plan, args.primary, args.secondary, args.block_end)
        _publish(args.output, report)
    except (ValueError, TypeError, OSError) as exc:
        print(f"Historical replay refused ({type(exc).__name__})", file=sys.stderr)
        return 2
    print(report["historical_case"]["status"])
    print("Block-end call |", report["block_end_atomic_call"]["executor_return_word_raw"])
    print("Exact winner prestate |", report["exact_winner_prestate"]["status"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
