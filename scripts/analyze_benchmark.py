#!/usr/bin/env python3

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oasis_agent.reporting import write_report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", action="store_true", required=True)
    parser.add_argument("--output", type=Path, default=Path("reports/benchmark-hook-analysis.md"))
    args = parser.parse_args()
    write_report(args.output)


if __name__ == "__main__":
    main()
