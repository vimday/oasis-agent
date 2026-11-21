"""Offline correlation of original Titan captures and bounded parent/target header captures."""

import argparse
import sys
from pathlib import Path
from typing import Any

from oasis_agent.arbitrage import metric_context
from oasis_agent.arbitrage.cycle_evidence import read_bounded
from oasis_agent.evidence.public_rpc_plan import PublicHeadPlan
from oasis_agent.evidence.rpc_validation import strict_json_object
from oasis_agent.models.common import sha256_hex

from . import capture_public_rpc, research_titan_state
from .research_metric import publish


def replay(
    plan_path: Path, titan_plan: Path, titan_capture: Path, captures: list[Path]
) -> dict[str, Any]:
    raw = read_bounded(plan_path, 32 * 1024)
    strict_json_object(raw)
    plan = metric_context.ContextPlan.model_validate_json(raw)
    if not 1 <= len(captures) <= 4:
        raise ValueError("header_capture_count")
    titan = research_titan_state.replay(titan_plan, titan_capture)
    observations = []
    for path in captures:
        p, plan_raw = capture_public_rpc.load_plan(path / "plan.json")
        if not isinstance(p, PublicHeadPlan) or type(p.block_tag) is not int or p.chain_id != 1:
            raise ValueError("fixed_ethereum_header_capture_required")
        r = capture_public_rpc.replay(path)
        if r["plan_sha256"] != sha256_hex(plan_raw):
            raise ValueError("capture_plan_changed_during_replay")
        headers = [
            v["response"]["result"]
            for v in r["exports"]["records"]
            if v["request"]["method"] == "eth_getBlockByNumber"
        ]
        if r["status"] == "complete" and len(headers) != 1:
            raise ValueError("header_capture_shape")
        observations.append(
            {
                "source": p.source,
                "block_number": p.block_tag,
                "status": r["status"],
                "raw_header": headers[0] if r["status"] == "complete" else None,
                "capture_sha256": r["capture_sha256"],
                "scope_sha256": r["scope_sha256"],
                "retained_bytes_verified": r["retained_bytes_verified"],
                "original_http_bytes_verified": r["original_http_bytes_verified"],
            }
        )
    result = metric_context.correlate(plan, titan, observations)
    result["plan_file_sha256"] = sha256_hex(raw)
    result["implementation_sha256"] = {
        "context": sha256_hex(Path(metric_context.__file__).read_bytes()),
        "entry_point": sha256_hex(Path(__file__).read_bytes()),
        "titan_entry_point": sha256_hex(Path(research_titan_state.__file__).read_bytes()),
        "capture_replay": sha256_hex(Path(capture_public_rpc.__file__).read_bytes()),
    }
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--titan-plan", type=Path, required=True)
    parser.add_argument("--titan-capture", type=Path, required=True)
    parser.add_argument("--header-capture", type=Path, required=True, action="append")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = replay(args.plan, args.titan_plan, args.titan_capture, args.header_capture)
        publish(args.output, result)
        print(f"{result['status']} | execution eligible false")
        return 0 if result["status"] == "header_observations_agree" else 3
    except ValueError, TypeError, OSError:
        print("Metric context refused: invalid evidence or output exists", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
