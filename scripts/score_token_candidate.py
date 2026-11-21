"""Score a normalized retained GMGN/OKX candidate; never submits orders."""

import argparse
from pathlib import Path

from oasis_agent.launch.scoring import Candidate, ScorePolicy, score_candidate
from oasis_agent.models.common import canonical_json


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.input.stat().st_size > 1_000_000 or args.policy.stat().st_size > 100_000:
            raise ValueError("oversized input")
        result = score_candidate(
            Candidate.from_json(args.input.read_bytes()),
            ScorePolicy.from_json(args.policy.read_bytes()),
        )
        with args.output.open("xb") as handle:
            handle.write(canonical_json(result.model_dump(mode="json")) + b"\n")
    except ValueError, TypeError, OSError:
        parser.exit(1, "Candidate scoring failed; no execution authorized.\n")
    print(result.action)


if __name__ == "__main__":
    main()
