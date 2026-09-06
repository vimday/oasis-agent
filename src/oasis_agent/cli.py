from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from .evm import decode_hook_permissions
from .reporting import write_report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="oasis-agent")
    sub = parser.add_subparsers(dest="command", required=True)
    analyze = sub.add_parser("analyze-hook")
    analyze.add_argument("--fixture", action="store_true")
    report = sub.add_parser("report-benchmark")
    report.add_argument("--fixture", action="store_true")
    report.add_argument("--output", type=Path, default=Path("reports/benchmark-hook-analysis.md"))
    backtest = sub.add_parser("backtest")
    backtest.add_argument("--fixture", action="store_true")
    scan = sub.add_parser("scan")
    scan.add_argument("--fixture", action="store_true")
    simulate = sub.add_parser("simulate")
    simulate.add_argument("--fixture", action="store_true")
    execute = sub.add_parser("execute")
    execute.add_argument("--allow-broadcast", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "analyze-hook":
        if not args.fixture:
            raise SystemExit("--fixture is required for offline hook analysis")
        permissions = decode_hook_permissions("0x1191Ac2561817686EFE4296B37BA5a5419Eaa880")
        print(
            json.dumps(
                {
                    "command": "analyze-hook",
                    "mode": "fixture",
                    "live_chain": False,
                    "broadcast": False,
                    "provenance": "deterministic fixture",
                    "permissions": asdict(permissions),
                },
                sort_keys=True,
            )
        )
        return 0
    if args.command == "report-benchmark":
        if not args.fixture:
            raise SystemExit("--fixture is required for the offline benchmark command")
        write_report(args.output)
        print(args.output)
        return 0
    if args.command in {"backtest", "scan", "simulate"}:
        if not args.fixture:
            raise SystemExit("--fixture is required for offline commands")
        print(
            json.dumps(
                {
                    "command": args.command,
                    "mode": "fixture",
                    "live_chain": False,
                    "broadcast": False,
                    "provenance": "deterministic fixture",
                },
                sort_keys=True,
            )
        )
        return 0
    if args.command == "execute":
        raise SystemExit("broadcast is disabled in the initial release")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
