"""Run the Base ETH/USDC v4 LP loop in shadow (paper) or live mode.

Shadow never signs. Live signs only through the separately run digest signer:

```bash
# terminal 1: the key stays in this process only
mkdir -m 700 -p ~/.oasis
python scripts/run_lp_signer_daemon.py --socket ~/.oasis/signer.sock \
  --account 0xe653df20a8c525edf381942ffc47704b7bdfe97d --key-file ~/.oasis/canary.key \
  --audit ~/.oasis/signer-audit.jsonl --max-signatures 200 --ttl-seconds 86400

# terminal 2
python scripts/run_live_lp.py --mode live --signer-socket ~/.oasis/signer.sock
```

Touch `data/live/STOP` to stop after the current tick. A loss-cap breach, a
runtime/USDC implementation change, repeated reverts or an unresolved
submission latch `halted` in the state file; clear it with `--clear-halt`
only after reviewing the cause.
"""

import argparse
import fcntl
import json
import os
import time
from dataclasses import asdict
from pathlib import Path

from oasis_agent.execution.socket_key_provider import SocketKeyProvider
from oasis_agent.live.chain import Rpc
from oasis_agent.live.config import LiveConfig
from oasis_agent.live.runner import LiveLoop
from oasis_agent.live.state import StateStore

ROOT = Path(__file__).resolve().parents[1]


def rpc_url(name: str) -> str:
    if name in os.environ:
        return os.environ[name]
    for line in (ROOT / ".env").read_text().splitlines():
        key, _, value = line.partition("=")
        if key.strip() == name:
            return value.strip()
    raise SystemExit(f"{name} is not set in the environment or .env")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--mode", choices=["shadow", "live"], required=True)
    parser.add_argument("--config", type=Path, default=ROOT / "configs/live.base-eth-usdc.json")
    parser.add_argument("--state-dir", type=Path, default=ROOT / "data/live")
    parser.add_argument("--rpc-env", default="BASE_RPC_PRIMARY_ALT")
    parser.add_argument("--signer-socket", type=Path)
    parser.add_argument("--hours", type=float, help="stop after this many hours")
    parser.add_argument("--ticks", type=int, help="stop after this many ticks")
    parser.add_argument("--clear-halt", action="store_true")
    parser.add_argument("--report", action="store_true")
    parser.add_argument(
        "--acknowledge-flow",
        action="store_true",
        help="treat the next wallet balance change as an external deposit/withdrawal (not P&L)",
    )
    parser.add_argument(
        "--drop-pending",
        action="store_true",
        help="forget a pending submission proven absent from chain (no receipt, nonce unused)",
    )
    args = parser.parse_args()

    config = LiveConfig.load(args.config)
    args.state_dir.mkdir(parents=True, exist_ok=True)
    # One process per state file: two loops would race on the nonce and the state.
    lock = (args.state_dir / f"{args.mode}.lock").open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit(f"another {args.mode} loop holds {lock.name}") from None
    store = StateStore(args.state_dir / f"{args.mode}.state.json")
    if args.drop_pending:
        loop = LiveLoop.recovery(config, Rpc(rpc_url(args.rpc_env)), store, args.mode)
        print(loop.drop_pending())
        store.save(loop.state)
        return
    if args.acknowledge_flow:
        state = store.load(args.mode)
        state.acknowledge_flow = True
        if state.halted and state.halted.startswith("unexplained wallet change"):
            state.halted = None
        state.record("flow_acknowledged")
        store.save(state)
        print("next wallet balance change will be booked as an external flow")
    if args.clear_halt:
        state = store.load(args.mode)
        print(f"clearing halt: {state.halted}")
        state.halted = None
        state.consecutive_reverts = 0
        store.save(state)
    if args.report or args.clear_halt:
        state = store.load(args.mode)
        print(
            json.dumps(
                {
                    "halted": state.halted,
                    "position": state.position and asdict(state.position),
                    "pending": state.pending and asdict(state.pending),
                    "initial_nav_usd": state.initial_nav_usd,
                    "external_flow_usd": state.external_flow_usd,
                    "daily_loss_usd": state.daily_loss_usd,
                    "last_nav_usd": state.last_nav_usd,
                    "gas_spent_eth": state.gas_spent_wei / 1e18,
                    "recent": state.history[-15:],
                },
                indent=1,
                default=str,
            )
        )
        return
    signer = None
    if args.mode == "live":
        if args.signer_socket is None:
            raise SystemExit("--signer-socket is required in live mode")
        signer = SocketKeyProvider(args.signer_socket.expanduser().absolute(), config.account)
    elif args.signer_socket is not None:
        raise SystemExit("shadow mode never signs; drop --signer-socket")
    log_path = args.state_dir / f"{args.mode}.log"

    def log(line: str) -> None:
        stamped = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {line}"
        print(stamped, flush=True)
        with log_path.open("a") as handle:
            handle.write(stamped + "\n")

    loop = LiveLoop(
        config,
        Rpc(rpc_url(args.rpc_env)),
        store,
        mode=args.mode,
        signer=signer,
        stop_file=args.state_dir / "STOP",
        log=log,
        secondary=Rpc(rpc_url(config.secondary_rpc_env)) if args.mode == "live" else None,
    )
    until = time.time() + args.hours * 3600 if args.hours else None
    loop.run(max_ticks=args.ticks, until=until)
    print(json.dumps(loop.report(), indent=1, default=str))


if __name__ == "__main__":
    main()
