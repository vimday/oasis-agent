"""Evaluate a shadow spot run: signal quality, paper book and operations (read-only)."""

import argparse
import json
import sys
from pathlib import Path

from oasis_agent.launch.spot_report import build_report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True, help="run directory of run_spot_agent")
    args = parser.parse_args()
    try:
        report = build_report(args.run)
    except (OSError, ValueError, KeyError) as error:
        print(f"spot shadow report refused: {type(error).__name__}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
