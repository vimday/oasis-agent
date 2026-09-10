"""Base launch monitor: observe by default, sign and broadcast only with --live.

Observation mode (the default) never reads signing keys. Live mode requires an
explicit --live flag and OASIS_PRIVATE_KEY in the environment; it signs through
eth_account and broadcasts through the dual-provider RPC with a durable,
tx_hash-bound intent journal so an unknown broadcast outcome recovers fail-closed.
"""

import argparse
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

from eth_account import Account

from oasis_agent.sniping.config import ExecutionConfig
from oasis_agent.sniping.discovery import VenueScanner
from oasis_agent.sniping.event_wakeup import EventWakeup, alchemy_websocket_url
from oasis_agent.sniping.journal import ExecutionJournal
from oasis_agent.sniping.monitor import Monitor
from oasis_agent.sniping.rpc import JsonRpc

DEFAULT_ENV = Path(__file__).resolve().parents[1] / ".env"
DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "configs" / "launch_execution.json"
DEFAULT_DATA = Path(__file__).resolve().parents[1] / "data"
OBSERVE_STOP = DEFAULT_DATA / "launch-observe.stop"
LIVE_STOP = DEFAULT_DATA / "launch-execution.stop"
OBSERVE_JOURNAL = DEFAULT_DATA / "launch-observe-fixed.db"
LIVE_JOURNAL = DEFAULT_DATA / "launch-execution-live.db"
OLD_EXECUTION_DB = DEFAULT_DATA / "launch-execution-2026-09-09.db"


def load_env(path: Path, include_key: bool) -> dict[str, str]:
    allowed = {"BASE_RPC_PRIMARY", "BASE_RPC_SECONDARY"}
    if include_key:
        allowed.add("OASIS_PRIVATE_KEY")
    env = {name: os.environ[name] for name in allowed if name in os.environ}
    if path.exists():
        with path.open() as stream:
            for raw_line in stream:
                name, separator, raw_value = raw_line.partition("=")
                if separator and name.strip() in allowed:
                    env.setdefault(name.strip(), raw_value.strip().strip("\"'"))
    return env


def read_wallet(path: Path) -> str:
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        row = db.execute("SELECT payload FROM execution_policy WHERE id=1").fetchone()
        if row is None:
            raise ValueError("previous public wallet identity missing")
        value = json.loads(row[0])["wallet"]
        if not isinstance(value, str):
            raise ValueError("invalid public wallet identity")
        return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--journal", type=Path, default=None)
    parser.add_argument("--stop-file", type=Path, default=None)
    parser.add_argument("--env", type=Path, default=DEFAULT_ENV)
    parser.add_argument("--wallet", help="public wallet address (observe mode only)")
    parser.add_argument(
        "--multi-venue",
        action="store_true",
        help="discover Aerodrome classic/Slipstream and Uniswap v4",
    )
    parser.add_argument(
        "--discovery-from-block", type=int, help="explicit start of retained creation-event history"
    )
    parser.add_argument(
        "--events", action="store_true", help="wake scans using Alchemy WebSocket liquidity events"
    )
    parser.add_argument(
        "--live",
        action="store_true",
        dest="live",
        help="sign and broadcast (requires OASIS_PRIVATE_KEY in .env)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_false",
        dest="live",
        help="observe only, never sign (default)",
    )
    parser.set_defaults(live=False)
    args = parser.parse_args(argv)
    if args.multi_venue and (args.discovery_from_block is None or args.discovery_from_block < 0):
        parser.error("--multi-venue requires --discovery-from-block (include preexisting pools)")

    env = load_env(args.env, include_key=args.live)
    primary = env.get("BASE_RPC_PRIMARY")
    secondary = env.get("BASE_RPC_SECONDARY")
    if not primary or not secondary or primary == secondary:
        parser.error("two distinct Base RPC endpoints are required")
    config = ExecutionConfig.from_json(args.config.read_text())

    account: Account | None = None
    if args.live:
        private_key = env.get("OASIS_PRIVATE_KEY", "")
        if not private_key:
            parser.error("--live requires OASIS_PRIVATE_KEY in .env")
        created = Account.from_key(private_key)
        account = created
        wallet = created.address
        journal_path = args.journal or LIVE_JOURNAL
        stop_file = args.stop_file or LIVE_STOP
        dry_run = False
        schema = "oasis.launch-execution-policy.v1"
    else:
        wallet = args.wallet or read_wallet(OLD_EXECUTION_DB)
        journal_path = args.journal or OBSERVE_JOURNAL
        stop_file = args.stop_file or OBSERVE_STOP
        dry_run = True
        schema = "oasis.launch-observation-policy.v2"

    journal_path.parent.mkdir(parents=True, exist_ok=True)
    policy = {
        "schema": schema,
        "config": config.model_dump(mode="json", by_alias=True),
        "wallet": wallet,
        "dry_run": dry_run,
        "launch_execution_authorized": not dry_run,
    }
    print(
        f"mode={'LIVE' if args.live else 'OBSERVE'} wallet={wallet} "
        f"journal={journal_path} stop_file={stop_file}"
    )
    rpc = JsonRpc(primary, secondary, config)
    wakeup: EventWakeup | None = None
    if args.events:
        ws_url = alchemy_websocket_url(primary)
        if ws_url is None:
            print("--events requires the Alchemy endpoint; falling back to HTTP polling")
        else:
            wakeup = EventWakeup(ws_url, config.target_ca, config.v3_factory)
            wakeup.start()
    try:
        with ExecutionJournal(journal_path, policy) as journal:
            scanner = (
                VenueScanner(rpc, config.target_ca, args.discovery_from_block, journal)
                if args.multi_venue
                else None
            )
            if scanner is not None:
                scanner.bootstrap(rpc.begin_snapshot())
            monitor = Monitor(
                config,
                rpc,
                wallet,
                account,
                journal,
                stop_file,
                dry_run=dry_run,
                venue_scanner=scanner,
                wakeup=wakeup,
            )
            monitor.run()
    finally:
        if wakeup is not None:
            wakeup.close()
        rpc.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
