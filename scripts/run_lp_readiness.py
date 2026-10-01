"""WP8 machine evaluator for the Phase-1 LP release gates.

Reads a strict gate manifest (one result per WP0..WP8), evaluates it at the
given time (default now), and writes the canonical assessment payload with its
digest. It checks declarations, not retained evidence contents or release
authority. Even a structurally complete manifest yields
``execution_eligible: false``; no input to this command authorizes execution.
"""

import argparse
import json
from pathlib import Path

from oasis_agent.reporting.lp_readiness import parse_assessment


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--as-of", default=None, help="ISO-8601 evaluation time (default now)")
    args = parser.parse_args()

    assessment = parse_assessment(args.input.read_bytes(), as_of=args.as_of)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(assessment.payload + b"\n")
    print(
        json.dumps(
            {
                "stage": assessment.stage.value,
                "assessment_scope": "declared_gate_structure_only",
                "execution_eligible": assessment.execution_eligible,
                "blockers": list(assessment.blockers),
                "digest": assessment.digest,
            }
        )
    )


if __name__ == "__main__":
    main()
