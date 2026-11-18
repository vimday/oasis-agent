"""CLI evaluator for the 9-gate profit-engine canary readiness report.

Reads local build artifacts, Anvil E2E settlement results, BaseTime/radar output,
public-shadow summary, and optional operator signoff/infra proof, writes an
immutable readiness verdict JSON (`execution_eligible=false`), and exits 0 on
report write (or 2 when `--require-ready` is set and any gate is blocked).
"""

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from oasis_agent.live.canary_readiness import (
    DEFAULT_MIN_LCB_NET_EDGE_BPS,
    DEFAULT_MIN_SHADOW_TICKS,
    evaluate_profit_engine_readiness,
)


def build_parser() -> argparse.ArgumentParser:
    """Construct CLI parser for profit-engine readiness gate evaluation."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--anvil-report", type=Path, default=None, help="Anvil E2E JSON report")
    parser.add_argument("--radar-report", type=Path, default=None, help="Radar or BaseTime JSON")
    parser.add_argument("--shadow-summary", type=Path, default=None, help="Public shadow summary")
    parser.add_argument(
        "--operator-signoff",
        type=Path,
        default=None,
        help="Optional owner scope signoff manifest JSON",
    )
    parser.add_argument(
        "--infra-proof",
        type=Path,
        default=None,
        help="Optional dual-provider RPC & isolated signer proof JSON",
    )
    parser.add_argument(
        "--contracts-out-dir",
        type=Path,
        default=None,
        help="Directory holding compiled Foundry JSON artifacts (default: contracts/out)",
    )
    parser.add_argument(
        "--min-shadow-ticks",
        type=int,
        default=DEFAULT_MIN_SHADOW_TICKS,
        help=(
            f"Minimum untouched public-shadow ticks required (default: {DEFAULT_MIN_SHADOW_TICKS})"
        ),
    )
    parser.add_argument(
        "--min-lcb-bps",
        type=int,
        default=DEFAULT_MIN_LCB_NET_EDGE_BPS,
        help=f"Minimum cost-adjusted LCB net edge in bps (default: {DEFAULT_MIN_LCB_NET_EDGE_BPS})",
    )
    parser.add_argument("--out", type=Path, required=True, help="Output JSON readiness report path")
    parser.add_argument(
        "--require-ready",
        action="store_true",
        help="Exit with status 2 when release_permitted is False",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run readiness gate evaluation, persist JSON report, and return exit code."""
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.min_shadow_ticks <= 0 or args.min_lcb_bps < 0:
        print(
            "invalid thresholds: --min-shadow-ticks must be > 0 and --min-lcb-bps must be >= 0",
            file=sys.stderr,
        )
        return 2
    if args.out.is_symlink():
        print(f"refusing symlink output path: {args.out}", file=sys.stderr)
        return 2

    report = evaluate_profit_engine_readiness(
        contracts_out_dir=args.contracts_out_dir,
        anvil_report_path=args.anvil_report,
        radar_report_path=args.radar_report,
        shadow_summary_path=args.shadow_summary,
        operator_signoff_path=args.operator_signoff,
        infra_proof_path=args.infra_proof,
        min_shadow_ticks=args.min_shadow_ticks,
        min_lcb_net_edge_bps=args.min_lcb_bps,
    )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(report.to_json(indent=2), encoding="utf-8")

    summary = {
        "schema_version": report.schema_version,
        "release_permitted": report.release_permitted,
        "execution_eligible": report.execution_eligible,
        "passed_count": report.passed_count,
        "total_gates": report.total_gates,
        "blocked_gates": list(report.blocked_gates),
        "report_sha256": report.report_sha256,
    }
    print(json.dumps(summary, indent=2))

    if args.require_ready and not report.release_permitted:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
