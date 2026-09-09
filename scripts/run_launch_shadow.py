"""Replay retained launch observations without RPC, signing or broadcast access."""

import argparse
import json
from pathlib import Path

from oasis_agent.launch.models import Cycle, Target
from oasis_agent.shadow.sniper_runner import SniperJournal


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="one strict Cycle JSON per line")
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument(
        "--target", type=Path, help="strict target config; defaults remain unverified"
    )
    args = parser.parse_args()
    target = Target() if args.target is None else Target.from_json(args.target.read_bytes())
    journal = SniperJournal(args.journal, target)
    try:
        with args.input.open() as stream:
            for line in stream:
                cycle = Cycle.from_json(line)
                result = journal.append(cycle)
                print(
                    json.dumps(
                        {
                            key: result[key]
                            for key in ("seq", "digest", "decision", "execution_eligible")
                        }
                    )
                )
    finally:
        journal.close()


if __name__ == "__main__":
    main()
