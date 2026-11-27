"""Assess retained multihop shadow evidence without network, keys, or live authority.

Use --plan, --plan-sha256, --evidence, and --capture-dir together. The digest is
the SHA-256 of the canonical frozen plan and must come from the operator's prior
commitment. The content-addressed capture directory stores exact retained bytes
at filenames equal to their lowercase SHA-256. A run without inputs reports
missing evidence. Reports can support independent review only.
"""

import argparse
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError

from oasis_agent.evidence.rpc_validation import strict_digest, strict_json_object
from oasis_agent.fastpath.multihop_readiness import (
    MultihopShadowEvidence,
    MultihopShadowPlan,
    assess_multihop_shadow,
)
from oasis_agent.models.common import canonical_json, sha256_hex

MAX_LOCAL_EVIDENCE_BYTES = 32 << 20


def _read_local(path: Path, limit: int = MAX_LOCAL_EVIDENCE_BYTES) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ValueError("evidence must be a regular, non-symlink file")
    with path.open("rb") as evidence:
        raw = evidence.read(limit + 1)
    if not raw or len(raw) > limit:
        raise ValueError("evidence is empty or exceeds its byte limit")
    return raw


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--plan-sha256", help="Canonical plan digest from the frozen commitment")
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--capture-dir", type=Path)
    parser.add_argument("--out", type=Path, help="Write a new file instead of stdout")
    parser.add_argument(
        "--require-review-thresholds",
        action="store_true",
        help="Exit 2 if the scoped shadow evidence does not satisfy its frozen thresholds",
    )
    args = parser.parse_args(argv)
    supplied = (args.plan, args.plan_sha256, args.evidence, args.capture_dir)
    if any(part is not None for part in supplied) and not all(
        part is not None for part in supplied
    ):
        parser.error("--plan, --plan-sha256, --evidence, and --capture-dir are supplied together")

    def contains(digest: str) -> bool:
        if args.capture_dir is None:
            return False
        try:
            return sha256_hex(_read_local(args.capture_dir / digest)) == digest
        except OSError, ValueError:
            return False

    try:
        plan = None
        evidence = None
        if args.plan is not None:
            strict_digest(args.plan_sha256, "plan SHA-256")
            if args.capture_dir.is_symlink() or not args.capture_dir.is_dir():
                raise ValueError("capture directory must be a regular, non-symlink directory")
            plan = MultihopShadowPlan.model_validate(strict_json_object(_read_local(args.plan)))
            evidence = MultihopShadowEvidence.model_validate(
                strict_json_object(_read_local(args.evidence))
            )
        report = assess_multihop_shadow(
            plan,
            evidence,
            expected_plan_sha256=args.plan_sha256,
            now_utc_ms=time.time_ns() // 1_000_000,
            contains=contains,
        )
        payload = canonical_json(report) + b"\n"
        if args.out is None:
            sys.stdout.buffer.write(payload)
        else:
            with args.out.open("xb") as output:
                output.write(payload)
    except OSError, ValueError, TypeError, ValidationError:
        print(
            "multihop readiness refused malformed, missing, oversized, or unsafe input/output",
            file=sys.stderr,
        )
        return 2
    return (
        2
        if args.require_review_thresholds and not report["shadow_thresholds_met_for_review"]
        else 0
    )


if __name__ == "__main__":
    raise SystemExit(main())
