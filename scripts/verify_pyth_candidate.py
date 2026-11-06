"""Inspect one retained Pyth capture or compare two at a supplied clock, offline."""

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from oasis_agent.evidence.pyth_candidate import compare_pyth_candidates, replay_pyth_candidate
from oasis_agent.models.common import canonical_json, sha256_hex


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wire", type=Path, action="append", required=True)
    parser.add_argument("--at-ms", type=int, help="Explicit comparison clock, Unix milliseconds")
    args = parser.parse_args()
    if (len(args.wire), args.at_ms is not None) not in ((1, False), (2, True)):
        parser.error("use one --wire for historical inspection, or two with --at-ms")
    try:
        captures = [replay_pyth_candidate(path) for path in args.wire]
        scope = (
            compare_pyth_candidates(*captures, at_ms=args.at_ms)
            if len(captures) == 2
            else "single_source_historical"
        )
    except (OSError, TypeError, ValueError):
        print(
            "Pyth candidate refused: invalid capture, disagreement or stale review clock",
            file=sys.stderr,
        )
        return 3
    report = {
        "schema": "oasis.pyth-candidate-replay.v2",
        "scope": scope,
        "review_at_ms": args.at_ms,
        "captures": [asdict(capture) for capture in captures],
        "rpc_requests": 0,
        "source_independence_verified": False,
        "receiver_trust_verified": False,
        "oracle_validated": False,
        "nav_verified": False,
        "execution_eligible": False,
    }
    print(
        json.dumps(report | {"evidence_hash": sha256_hex(canonical_json(report))}, sort_keys=True)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
