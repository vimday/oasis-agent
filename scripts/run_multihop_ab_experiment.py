"""Offline read-only CLI for pre-registered multi-hop Arm A vs Arm B cycle scan evaluation."""

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from oasis_agent.errors import RpcError
from oasis_agent.fastpath.incremental_scanner import run_multihop_ab_experiment
from oasis_agent.fastpath.multihop_replay import load_multihop_replay
from scripts.run_multicall_100tick_campaign import (
    WETH_ADDR,
    decode_retained_multihop_snapshot,
)


def build_parser() -> argparse.ArgumentParser:
    """Build CLI argument parser for `run_multihop_ab_experiment`."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--blocks",
        type=int,
        help="Number of fixed-block 34-pool snapshots to evaluate (default: 120).",
    )
    parser.add_argument(
        "--changed-pools-per-block",
        type=int,
        help="Number of pools mutated per incremental block after block 0 (default: 1).",
    )
    parser.add_argument(
        "--max-hops",
        type=int,
        choices=(2, 3, 4),
        help="Maximum cycle hop count for synthetic input (default: 4).",
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
        help="Deterministic RNG seed for snapshot generation (default: 20261122).",
    )
    parser.add_argument(
        "--snapshot-manifest",
        type=Path,
        help="Replay a retained Base campaign manifest without network access.",
    )
    parser.add_argument(
        "--base-token",
        help="Select one anchor address in the retained scan inputs (default: WETH).",
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
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.out is not None and args.out.is_symlink():
        raise ValueError("--out cannot be a symlink")

    dataset = None
    if args.snapshot_manifest is not None:
        if any(
            value is not None
            for value in (args.blocks, args.changed_pools_per_block, args.max_hops, args.seed)
        ):
            parser.error("synthetic input options cannot be used with --snapshot-manifest")
        if args.out is not None and args.out.resolve().is_relative_to(
            args.snapshot_manifest.resolve().parent
        ):
            parser.error("--out must be outside the replay capture directory")
        try:
            dataset = load_multihop_replay(
                args.snapshot_manifest,
                base_token=WETH_ADDR if args.base_token is None else args.base_token,
                decode_snapshot=decode_retained_multihop_snapshot,
            )
        except (OSError, ValueError, TypeError, RpcError) as exc:
            print(f"multihop replay failed: {exc}", file=sys.stderr)
            return 2
    elif args.base_token is not None:
        parser.error("--base-token requires --snapshot-manifest")

    report = run_multihop_ab_experiment(
        snapshots=dataset.snapshots if dataset is not None else None,
        num_blocks=120 if args.blocks is None else args.blocks,
        changed_pools_per_block=(
            1 if args.changed_pools_per_block is None else args.changed_pools_per_block
        ),
        max_hops=dataset.max_hops if dataset is not None else (args.max_hops or 4),
        deduplicate_cycles=dataset is None,
        full_scan_every_n_blocks=args.full_scan_every,
        min_p99_reduction_ratio=args.min_p99_reduction,
        seed=20261122 if args.seed is None else args.seed,
    )
    payload = report.to_dict()
    if dataset is not None:
        payload.update(
            replay=dataset.metadata,
            seed=None,
            changed_pools_per_block=None,
        )
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
