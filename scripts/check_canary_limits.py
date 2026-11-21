"""Validate owner canary instructions without wallet access or execution authority."""

import argparse
from pathlib import Path

from oasis_agent.models.common import canonical_json
from oasis_agent.risk.canary_instructions import parse_canary_instructions


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    try:
        limits = parse_canary_instructions(args.config.read_bytes())
    except OSError, ValueError, TypeError:
        parser.exit(1, "Invalid canary instructions; execution remains disabled.\n")
    print(limits.payload.decode())
    print(canonical_json({"limits_digest": limits.digest}).decode())
    if limits.blockers:
        parser.exit(2)


if __name__ == "__main__":
    main()
