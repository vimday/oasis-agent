"""Prepare a fixed Metric/V3 probe packet or decode retained bytes offline. No RPC client."""

import argparse
import sys
from pathlib import Path
from typing import Any

from oasis_agent.arbitrage import metric_probe, metric_probe_build
from oasis_agent.arbitrage.cycle_evidence import read_bounded
from oasis_agent.evidence.rpc_validation import strict_json_object
from oasis_agent.models.common import sha256_hex

from .research_metric import publish


def run(plan_path: Path, artifact_path: Path, capture_path: Path | None) -> dict[str, Any]:
    raw = read_bounded(plan_path, 32 * 1024)
    strict_json_object(raw)
    plan = metric_probe.Plan.model_validate_json(raw)
    artifact = read_bounded(artifact_path, 4 * 1024 * 1024)
    runtime = metric_probe_build.materialize(plan, artifact)
    result: dict[str, Any]
    if capture_path is None:
        result = {
            "schema": "oasis-metric-probe-packet-v1",
            "status": "prepared_offline",
            "scenario": plan.scenario,
            "requests": metric_probe.requests(plan),
            "request_hash": metric_probe.request_hash(plan),
            "probe_runtime_hash": plan.probe_runtime_hash,
            "build_artifact_sha256": plan.build_artifact_sha256,
            "execution_eligible": False,
            "network_requests_sent": 0,
        }
    else:
        capture_raw = read_bounded(capture_path, 2 * 1024 * 1024)
        strict_json_object(capture_raw)
        capture = metric_probe.Capture.model_validate_json(capture_raw)
        if capture.plan_file_sha256 != sha256_hex(raw):
            raise ValueError("probe_capture_plan_hash_mismatch")
        result = metric_probe.evaluate(plan, capture, runtime)
        result["capture_file_sha256"] = sha256_hex(capture_raw)
    result["plan_file_sha256"] = sha256_hex(raw)
    implementation = {"entry_point": sha256_hex(Path(__file__).read_bytes())}
    for module in (metric_probe, metric_probe_build):
        if module.__file__ is None:
            raise ValueError("probe_implementation_source_missing")
        path = Path(module.__file__)
        implementation[path.name] = sha256_hex(path.read_bytes())
    result["implementation_sha256"] = implementation
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "replay"))
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--capture", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if (args.command == "replay") != (args.capture is not None):
        parser.error("--capture is required exactly for replay")
    try:
        result = run(args.plan, args.artifact, args.capture)
        publish(args.output, result)
        print(f"{result['status']} | execution eligible false")
        return 3 if result["status"] == "rejected_or_incomplete" else 0
    except (ValueError, TypeError, KeyError, OSError):
        print("Probe research refused: invalid input/build or output exists", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
