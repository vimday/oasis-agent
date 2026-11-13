"""Capture a bounded Metric directory across explicit EVM chains; replay offline."""

import argparse
import sys
from pathlib import Path
from typing import Any

from oasis_agent.arbitrage import metric_universe
from oasis_agent.arbitrage.cycle_evidence import read_bounded
from oasis_agent.evidence.rpc_validation import strict_json_object
from oasis_agent.models.common import canonical_json, sha256_hex

from . import research_metric as recorder


def load_plan(path: Path) -> tuple[metric_universe.UniversePlan, bytes]:
    raw = read_bounded(path, 32 * 1024)
    strict_json_object(raw)
    return metric_universe.UniversePlan.model_validate_json(raw), raw


def capture(path: Path, directory: Path) -> int:
    plan, raw = load_plan(path)
    if plan.scenario != "provider_observation":
        raise ValueError("only provider observations may collect")
    return recorder.capture_requests(
        raw, metric_universe.requests(plan), plan.max_capture_seconds, directory
    )


def replay(directory: Path) -> dict[str, Any]:
    plan, raw = load_plan(directory / "plan.json")
    payloads, provenance = recorder.replay_requests(
        raw, metric_universe.requests(plan), plan.max_capture_seconds, directory
    )
    report = metric_universe.discover(plan, payloads)
    report.update(provenance)
    report["implementation_sha256"] = sha256_hex(
        canonical_json(
            {
                "discovery": sha256_hex(Path(metric_universe.__file__).read_bytes()),
                "recorder": sha256_hex(Path(recorder.__file__).read_bytes()),
                "entrypoint": sha256_hex(Path(__file__).read_bytes()),
            }
        )
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    collect = sub.add_parser("capture")
    collect.add_argument("--plan", type=Path, required=True)
    collect.add_argument("--out", type=Path, required=True)
    offline = sub.add_parser("replay")
    offline.add_argument("--capture", type=Path, required=True)
    offline.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "capture":
            code = capture(args.plan, args.out)
            print("Metric universe capture complete" if code == 0 else "Capture stopped; retained")
            return code
        report = replay(args.capture)
        recorder.publish(args.output, report)
        print(f"{len(report['chains'])} isolated chain directories; execution eligible false")
        return 0
    except (OSError, ValueError, TypeError):
        print(
            "Metric discovery refused: invalid input, incomplete capture or output exists",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
