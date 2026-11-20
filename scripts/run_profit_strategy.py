"""Keyless profit-strategy runner CLI bridging to the isolated Unix socket authority.

Supports `shadow` (default; status + headroom observation only, never signs or
broadcasts), `dry_run`, and `live` authority handshake modes, plus `--reconcile-only`
for `profit-strategy-recovery`. Never loads private keys, `eth_account`, or `.env`.
"""

import argparse
import fcntl
import json
import os
import sys
from collections.abc import Sequence
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from oasis_agent.live.profit_strategy_daemon import (
    ProfitStrategyBridge,
    ProfitStrategyDaemonConfig,
    atomic_write_json_file,
)
from oasis_agent.live.shutdown import run_with_term_handler
from oasis_agent.reporting.live_release import require_live_start


def build_parser() -> argparse.ArgumentParser:
    """Construct strict CLI parser for `profit-strategy` and `profit-strategy-recovery`."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("shadow", "dry_run", "live"),
        default="shadow",
        help="Execution bridge mode (default: shadow; never signs or broadcasts in shadow).",
    )
    parser.add_argument(
        "--authority-socket",
        type=Path,
        required=True,
        help="Path to the isolated profit authority Unix socket.",
    )
    parser.add_argument(
        "--risk-config",
        type=Path,
        required=True,
        help="Read-only CapitalRiskConfig JSON path.",
    )
    parser.add_argument(
        "--policy-config",
        type=Path,
        default=None,
        help="Optional non-secret AtomicExecutionPolicy JSON path for client admission.",
    )
    parser.add_argument(
        "--readiness-report",
        type=Path,
        default=None,
        help="Required in live mode: emitted 9-gate profit-engine readiness report.",
    )
    parser.add_argument(
        "--account",
        default=None,
        help="Optional operator account address for status queries without --policy-config.",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=None,
        help="Optional private 0700 state directory for durable pending/halt persistence.",
    )
    parser.add_argument(
        "--out-json",
        type=Path,
        default=None,
        help="Optional output path for deterministic status or recovery JSON.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single status-sync tick and exit.",
    )
    parser.add_argument(
        "--reconcile-only",
        action="store_true",
        help="Inspect and reconcile pending/unresolved strategy state without new exposure.",
    )
    parser.add_argument(
        "--poll-interval-ms",
        type=int,
        default=1000,
        help="Polling interval between ticks in milliseconds (default: 1000).",
    )
    parser.add_argument(
        "--max-ticks",
        type=int,
        default=None,
        help="Maximum ticks to execute before clean exit.",
    )
    parser.add_argument(
        "--socket-timeout-s",
        type=float,
        default=10.0,
        help="Authority Unix socket exchange timeout in seconds (default: 10.0).",
    )
    return parser


def _validate_state_dir(directory: Path) -> None:
    if (
        not directory.is_absolute()
        or directory.is_symlink()
        or directory != directory.resolve()
        or not directory.is_dir()
        or directory.stat().st_mode & 0o077
    ):
        raise ValueError("strategy state directory must be an existing private 0700 directory")
    if directory.stat().st_uid != os.getuid():
        raise ValueError("strategy state directory must be owned by this service identity")


def main(argv: Sequence[str] | None = None) -> int:
    """Run one-shot, recovery reconciliation, or continuous profit-strategy loop."""
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.once and args.reconcile_only:
        parser.error("--once and --reconcile-only are mutually exclusive")
    if args.once and args.max_ticks is not None and args.max_ticks != 1:
        parser.error("--once conflicts with --max-ticks != 1")
    if args.out_json is not None and args.out_json.is_symlink():
        parser.error("--out-json cannot be a symlink")
    if args.mode == "live":
        if args.policy_config is None:
            parser.error("--mode live requires --policy-config")
        if not args.reconcile_only:
            if args.readiness_report is None:
                parser.error("--mode live requires --readiness-report")
            require_live_start()

    max_ticks = 1 if args.once else args.max_ticks
    if args.state_dir is not None:
        _validate_state_dir(args.state_dir)
        lock_path = args.state_dir / "profit_strategy.lock"
        if lock_path.is_symlink():
            raise ValueError("strategy lock file cannot be a symlink")
        lock_cm: Any = lock_path.open("a", encoding="utf-8")
    else:
        lock_cm = nullcontext(None)

    config = ProfitStrategyDaemonConfig(
        authority_socket=args.authority_socket,
        mode=args.mode,
        risk_config_path=args.risk_config,
        policy_config_path=args.policy_config,
        account=args.account,
        state_dir=args.state_dir,
        poll_interval_ms=args.poll_interval_ms,
        max_ticks=max_ticks,
        socket_timeout_s=args.socket_timeout_s,
        readiness_report_path=args.readiness_report,
    )

    with lock_cm as lock_handle:
        if lock_handle is not None:
            fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        bridge = ProfitStrategyBridge(config)

        if args.reconcile_only:
            result = bridge.reconcile_pending()
            payload = result.to_dict()
            if args.out_json is not None:
                atomic_write_json_file(args.out_json, payload, mode=0o600)
            print(json.dumps(payload, sort_keys=True), flush=True)
            return 3 if (result.halted or result.pending_count > 0) else 0

        snapshots = bridge.run_loop(out_json=args.out_json)
        final_payload = snapshots[-1].to_dict() if snapshots else bridge.sync_status().to_dict()
        print(json.dumps(final_payload, sort_keys=True), flush=True)
        return 0


def _cli_entry() -> None:
    exit_code = main()
    if exit_code != 0:
        raise SystemExit(exit_code)


if __name__ == "__main__":
    try:
        completed = run_with_term_handler(_cli_entry)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError):
        print("Profit strategy daemon refused or stopped; details redacted.", file=sys.stderr)
        raise SystemExit(3) from None
    if not completed:
        raise SystemExit(0)
