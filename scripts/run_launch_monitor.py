"""Run the Base launch sniper monitor (user-authorized execution).

Usage:
    .venv/bin/python scripts/run_launch_monitor.py [--config configs/launch_execution.json]
        [--journal data/launch-execution-YYYYMMDD.db] [--stop-file data/launch-execution.stop]
        [--dry-run]

Secrets come from the process environment only: BASE_ALCHEMY_RPC_URL (primary),
BASE_RPC_URL (secondary) and OASIS_PRIVATE_KEY. The key is never printed,
journaled, or written to disk by this script.
"""

import argparse
import sys
import time
from pathlib import Path

from eth_account import Account

from oasis_agent.sniping.config import ExecutionConfig
from oasis_agent.sniping.journal import ExecutionJournal
from oasis_agent.sniping.monitor import Monitor
from oasis_agent.sniping.rpc import JsonRpc

DEFAULT_ENV = Path(__file__).resolve().parents[1] / ".env"
DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "configs" / "launch_execution.json"
DEFAULT_DATA = Path(__file__).resolve().parents[1] / "data"
DEFAULT_STOP = DEFAULT_DATA / "launch-execution.stop"


def load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            value = value.strip()
            if value[:1] in ('"', "'") and value[-1:] == value[:1]:
                value = value[1:-1]
            env[key.strip()] = value
    return env


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Base launch sniper monitor")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--journal", type=Path, default=None)
    parser.add_argument("--stop-file", type=Path, default=DEFAULT_STOP)
    parser.add_argument("--env", type=Path, default=DEFAULT_ENV)
    parser.add_argument(
        "--dry-run", action="store_true", help="simulate every step, broadcast nothing"
    )
    args = parser.parse_args(argv)

    env = load_env(args.env)
    primary = env.get("BASE_ALCHEMY_RPC_URL") or env.get("BASE_RPC_URL")
    secondary = env.get("BASE_RPC_URL")
    private_key = env.get("OASIS_PRIVATE_KEY", "")
    if not primary or not secondary:
        print("missing BASE_ALCHEMY_RPC_URL / BASE_RPC_URL in env", file=sys.stderr)
        return 2
    if not private_key:
        print("missing OASIS_PRIVATE_KEY in env", file=sys.stderr)
        return 2

    config = (
        ExecutionConfig.from_json(args.config.read_text())
        if args.config.exists()
        else ExecutionConfig()
    )
    journal_path = args.journal or (DEFAULT_DATA / "launch-execution-2026-09-09.db")
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    args.stop_file.parent.mkdir(parents=True, exist_ok=True)

    account = Account.from_key(private_key)
    rpc = JsonRpc(primary, secondary, config)
    policy = {
        "schema": "oasis.launch-execution-policy.v1",
        "config": config.model_dump(mode="json", by_alias=True),
        "wallet": account.address,
        "dry_run": args.dry_run,
        "created_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    with ExecutionJournal(journal_path, policy) as journal:
        monitor = Monitor(
            config,
            rpc,
            account.address,
            account,
            journal,
            args.stop_file,
            dry_run=args.dry_run,
        )
        monitor.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
