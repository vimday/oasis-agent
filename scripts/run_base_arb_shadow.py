"""Keyless, read-only Base multi-DEX arbitrage & L1/L2 cost shadow attribution CLI."""

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from oasis_agent.errors import RpcError
from oasis_agent.fastpath.bridge import FastpathUnavailableError
from oasis_agent.fastpath.live_shadow_runner import (
    DEFAULT_BASE_RPC_URL,
    DEFAULT_PRIORITY_FEE_WEI,
    ShadowRunnerError,
    build_synthetic_base_rpc_transport,
    build_synthetic_uniswapx_transport,
    run_base_arb_shadow,
)
from oasis_agent.intents.uniswapx_client import UniswapXOrderbookClient


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("synthetic", "public-shadow"),
        default="synthetic",
        help="Offline synthetic mode or bounded read-only Base public-shadow mode.",
    )
    parser.add_argument(
        "--rpc-url",
        default=DEFAULT_BASE_RPC_URL,
        help="Read-only Base HTTPS JSON-RPC endpoint.",
    )
    parser.add_argument(
        "--ticks",
        type=int,
        default=3,
        help="Number of pinned block snapshots to capture (default: 3).",
    )
    parser.add_argument(
        "--interval-ms",
        type=int,
        default=1200,
        help="Milliseconds between consecutive ticks (default: 1200).",
    )
    parser.add_argument(
        "--max-requests",
        type=int,
        default=48,
        help="Hard cumulative request budget across the run (default: 48).",
    )
    parser.add_argument(
        "--skip-uniswapx",
        action="store_true",
        help="Skip optional UniswapX Dutch_V3 open-order polling.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        required=True,
        help="Output directory for shadow_attribution.jsonl and shadow_summary.json.",
    )
    parser.add_argument(
        "--priority-fee-wei",
        type=int,
        default=DEFAULT_PRIORITY_FEE_WEI,
        help=f"L2 priority fee model in wei (default: {DEFAULT_PRIORITY_FEE_WEI}).",
    )
    return parser


def _validate_out_dir_preflight(out_dir: Path) -> None:
    if out_dir.is_symlink():
        raise ShadowRunnerError(f"refusing symlink --out directory: {out_dir}")
    if out_dir.exists() and not out_dir.is_dir():
        raise ShadowRunnerError(f"--out path exists and is not a directory: {out_dir}")
    for fname in ("shadow_summary.json", "shadow_attribution.jsonl"):
        target = out_dir / fname
        if target.is_symlink():
            raise ShadowRunnerError(f"refusing symlink output file: {target}")
        if target.exists():
            raise FileExistsError(f"refusing to overwrite existing output file: {target}")


def _noop_sleep(_seconds: float) -> None:
    return None


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        _validate_out_dir_preflight(args.out)
        transport = build_synthetic_base_rpc_transport() if args.mode == "synthetic" else None
        ux_client = (
            UniswapXOrderbookClient(
                max_requests=args.ticks,
                transport=build_synthetic_uniswapx_transport(),
                sleeper=_noop_sleep,
            )
            if args.mode == "synthetic" and not args.skip_uniswapx
            else None
        )
        result = run_base_arb_shadow(
            rpc_url=args.rpc_url,
            out_dir=args.out,
            mode=args.mode,
            ticks=args.ticks,
            interval_ms=args.interval_ms,
            max_requests=args.max_requests,
            priority_fee_wei=args.priority_fee_wei,
            skip_uniswapx=args.skip_uniswapx,
            transport=transport,
            uniswapx_client=ux_client,
        )
    except (
        OSError,
        ValueError,
        TypeError,
        RpcError,
        FastpathUnavailableError,
    ) as exc:
        print(f"base-arb-shadow failed: {exc}", file=sys.stderr)
        return 2

    print(
        json.dumps(
            {
                "mode": result.summary["mode"],
                "ticks_completed": result.summary["ticks_completed"],
                "requests_used": result.summary["requests_used"],
                "requests_budget": result.summary["requests_budget"],
                "positive_gross_ticks": result.summary["positive_gross_ticks"],
                "positive_net_ticks": result.summary["positive_net_ticks"],
                "best_gross_spread_bps": result.summary["best_gross_spread_bps"],
                "best_net_spread_bps": result.summary["best_net_spread_bps"],
                "mean_net_spread_bps": result.summary["mean_net_spread_bps"],
                "lcb_net_edge_bps": result.summary["lcb_net_edge_bps"],
                "mean_rust_scan_ns": result.summary["mean_rust_scan_ns"],
                "chain_head_sha256": result.summary["chain_head_sha256"],
                "execution_eligible": result.summary["execution_eligible"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
