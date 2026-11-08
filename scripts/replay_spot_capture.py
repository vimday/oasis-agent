"""Replay a captured shadow spot session offline through the production SpotAgent.

Usage: python -m scripts.replay_spot_capture --run RUN_DIR --out REPORT.json [--session ID]

Reads RUN_DIR/capture.db and RUN_DIR/raw/ only; no CLI, network or wallet is touched.
Writes a new report file (never into the run directory's evidence) holding every
replayed decision record, per-cycle digests, the final state digest and candidate
coverage. Exits 0 when every completed cycle reproduced its recorded decisions, 2 when
the archive cannot reproduce them (the fixed reason is printed).
"""

import argparse
import json
import sys
from pathlib import Path

from oasis_agent.launch.spot_capture import ReplayRefused, replay_session
from oasis_agent.launch.spot_coverage import coverage_report
from oasis_agent.models.common import canonical_json


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True, help="shadow run directory")
    parser.add_argument("--out", type=Path, required=True, help="new replay report file")
    parser.add_argument("--session", help="capture session id; default the latest")
    args = parser.parse_args()
    if args.out.resolve().is_relative_to(args.run.resolve()):
        parser.error("--out must be outside the run directory (it holds original evidence)")
    try:
        replay = replay_session(args.run / "capture.db", args.run / "raw", args.session)
    except ReplayRefused as refused:
        print(f"replay refused: {refused.category}", file=sys.stderr)
        return 2
    report = replay | {"coverage": coverage_report(replay["records"])}
    with args.out.open("xb") as handle:  # never overwrite an earlier result
        handle.write(canonical_json(report) + b"\n")
    print(
        json.dumps(
            {
                "session_id": report["session_id"],
                "cycles_replayed": report["cycles_replayed"],
                "session_complete": report["session_complete"],
                "final_state_sha256": report["final_state_sha256"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
