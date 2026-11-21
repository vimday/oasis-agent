"""Evaluate explicit synthetic lending-loop scenarios offline."""

import argparse
import sys
from pathlib import Path

from oasis_agent.arbitrage import lending_loop
from oasis_agent.arbitrage.cycle_evidence import read_bounded
from oasis_agent.evidence.rpc_validation import strict_json_object
from oasis_agent.models.common import canonical_json, sha256_hex

from .research_metric import publish


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        raw = read_bounded(args.plan, 64 * 1024)
        strict_json_object(raw)
        plan = lending_loop.LendingPlan.model_validate_json(raw)
        report = lending_loop.simulate(plan)
        report["plan_sha256"] = sha256_hex(raw)
        report["implementation_sha256"] = sha256_hex(
            canonical_json(
                {
                    "engine": sha256_hex(Path(lending_loop.__file__).read_bytes()),
                    "entrypoint": sha256_hex(Path(__file__).read_bytes()),
                }
            )
        )
        publish(args.output, report)
        print(f"{len(report['rounds'])} synthetic rounds; execution eligible false")
        return 0
    except OSError, ValueError, TypeError:
        print("Lending scenario refused: invalid input or output exists", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
