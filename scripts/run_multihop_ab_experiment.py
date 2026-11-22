"""Offline read-only CLI for pre-registered multi-hop Arm A vs Arm B cycle scan evaluation."""

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from oasis_agent.fastpath.incremental_scanner import run_multihop_ab_experiment


def build_parser() -> argparse.ArgumentParser:
    """Build CLI argument parser for `run_multihop_ab_experiment`."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--blocks",
        type=int,
        default=120,
        help="Number of fixed-block 34-pool snapshots to evaluate (default: 120).",
    )
    parser.add_argument(
        "--changed-pools-per-block",
        type=int,
        default=1,
        help="Number of pools mutated per incremental block after block 0 (default: 1).",
    )
    parser.add_argument(
        "--max-hops",
        type=int,
        default=4,
        choices=(2, 3, 4),
        help="Maximum cycle hop count for both arms (default: 4).",
    )
    parser.add_argument(
        "--full-scan-every",
        type=int,
        default=200,
        help="Periodic full-scan checkpoint interval in blocks; 0 disables (default: 200).",
    )
    parser.add_argument(
        "--min-p99-reduction",
        type=float,
        default=0.50,
        help="Pre-registered minimum p99 latency reduction ratio for Hypothesis A (default: 0.50).",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=20261122,
        help="Deterministic RNG seed for snapshot generation (default: 20261122).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Optional non-symlink JSON path to write the report.",
    )
    parser.add_argument(
        "--require-pass",
        action="store_true",
        help="Exit with code 2 when Hypothesis A does not pass.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run Arm A vs Arm B offline benchmark, print canonical JSON, and optionally check pass."""
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    if args.out is not None and args.out.is_symlink():
        raise ValueError("--out cannot be a symlink")

    report = run_multihop_ab_experiment(
        num_blocks=args.blocks,
        changed_pools_per_block=args.changed_pools_per_block,
        max_hops=args.max_hops,
        full_scan_every_n_blocks=args.full_scan_every,
        min_p99_reduction_ratio=args.min_p99_reduction,
        seed=args.seed,
    )
    payload = report.to_dict()
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered, encoding="utf-8")
    sys.stdout.write(rendered)
    sys.stdout.flush()
    if args.require_pass and not report.hypothesis_a_passed:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
