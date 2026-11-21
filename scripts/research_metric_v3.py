"""Compose pinned Metric/V3 quotes from retained RPC exports; no network I/O."""

import argparse
import sys
from pathlib import Path
from typing import Any

from oasis_agent.arbitrage import cycle_evidence, cycle_plan, cycle_protocols, metric, metric_v3
from oasis_agent.evidence.rpc_validation import strict_json_object
from oasis_agent.models.common import canonical_json, sha256_hex

from .research_metric import publish
from .run_cycle_research import _source


def report(path: Path, inputs: list[tuple[str, Path]]) -> dict[str, Any]:
    raw = cycle_evidence.read_bounded(path, 128 * 1024)
    strict_json_object(raw)
    plan = metric_v3.CrossVenuePlan.model_validate_json(raw)
    records = cycle_evidence.load_exports(inputs, plan.sources)
    result = metric_v3.evaluate(plan, records)
    result["plan_file_sha256"] = sha256_hex(raw)
    files = [
        Path(m.__file__) for m in (metric_v3, cycle_evidence, cycle_plan, cycle_protocols, metric)
    ]
    files.append(Path(__file__))
    result["implementation_sha256"] = sha256_hex(
        canonical_json({p.name: sha256_hex(p.read_bytes()) for p in files})
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--export", dest="exports", type=_source, action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = report(args.plan, args.exports)
        publish(args.output, result)
        print(
            f"{result['decision']} | {result['quote_complete_points']}/{result['planned_points']} "
            "complete quotes | execution eligible false"
        )
        return 0
    except OSError, ValueError, TypeError:
        print("Metric/V3 research refused: invalid input or output exists", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
