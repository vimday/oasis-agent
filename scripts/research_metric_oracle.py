"""Diagnose pinned Metric reference guards from retained RPC only; no network."""

import argparse
import sys
from pathlib import Path

from oasis_agent.arbitrage import metric_oracle
from oasis_agent.arbitrage.cycle_evidence import load_exports, read_bounded
from oasis_agent.evidence.rpc_validation import strict_json_object
from oasis_agent.models.common import sha256_hex

from .research_metric import publish
from .run_cycle_research import _source


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--export", dest="exports", type=_source, action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        raw = read_bounded(args.plan, 128 * 1024)
        strict_json_object(raw)
        plan = metric_oracle.OraclePlan.model_validate_json(raw)
        result = metric_oracle.diagnose(plan, load_exports(args.exports, plan.sources))
        result["plan_file_sha256"] = sha256_hex(raw)
        result["implementation_sha256"] = sha256_hex(Path(metric_oracle.__file__).read_bytes())
        publish(args.output, result)
        print(f"{result['status']} | reference guards only | execution eligible false")
        return 0
    except ValueError, TypeError, OSError:
        print("Metric oracle diagnosis refused: invalid input or output exists", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
