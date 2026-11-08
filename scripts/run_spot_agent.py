"""Run the spot agent: live OKX onchainos claims in, decisions out.

Every decision is appended to a hash-linked journal before the next tick. By default the
book is paper-only and this command cannot sign, approve or broadcast. `--live` trades the
owner-approved OKX Agentic Wallet through its own TEE signer (this process never holds a
key) and requires a per-trade cap, a slippage bound, the exact confirmation phrase and a
Base RPC for receipts. A live run directory can never be resumed as shadow or vice versa.
A provider call budget bounds total usage; the loop stops (state preserved) when it would
overrun.
"""

import argparse
import copy
import json
import os
import signal
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from oasis_agent.errors import OasisError
from oasis_agent.launch import gmgn_source
from oasis_agent.launch.gmgn_source import GmgnSource
from oasis_agent.launch.onchainos_source import (
    BASE_USDC,
    OnchainosSource,
    default_binary,
    paced,
    run_cli,
)
from oasis_agent.launch.spot_agent import (
    AgentConfig,
    AgentState,
    SpotAgent,
    reconcile_with_journal,
    state_from_json,
    state_to_json,
)
from oasis_agent.launch.spot_executor import (
    APPROVED_WALLET,
    LiveExecutor,
    LiveGate,
    LiveRefused,
    LiveUnresolved,
    preflight,
    rpc_receipt_reader,
)
from oasis_agent.launch.spot_lifecycle import HUMAN_REVIEW_REASONS, LIVE_HALT, LIVE_SCHEMA
from oasis_agent.launch.spot_report import read_journal
from oasis_agent.models.common import canonical_json
from oasis_agent.rpc.client import RpcClient
from oasis_agent.rpc.transport import JsonRpcTransport
from oasis_agent.sniping.journal import ExecutionJournal

MAX_CONSECUTIVE_ERRORS = 5
LIVE_SUBMIT_TIMEOUT_SECONDS = 120.0
PRINTED = frozenset(
    {
        "paper_buy",
        "paper_sell",
        "live_buy",
        "live_sell",
        "halt_review",
        "tick_summary",
        "tick_error",
    }
)


# Worst case per tick: hot + signals + enrich_top * (price, security, advanced, gmgn, 2 quotes)
# + one sell quote and one security read per open position.
def tick_budget(config: AgentConfig, calls_per_swap: int = 0) -> int:
    # + batched forward-outcome price reads (at most MAX_PENDING / 40).
    # + live swaps: at most every held position sold and every slot bought in one tick.
    swaps = 2 * config.max_positions
    return 2 + config.enrich_top * 6 + config.max_positions * 2 + 50 + swaps * calls_per_swap


def save_state(path: Path, state: AgentState) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(canonical_json(state_to_json(state)) + b"\n")
    temporary.replace(path)


def load_state(path: Path, config: AgentConfig) -> AgentState:
    if not path.exists():
        return AgentState(config.initial_cash_usd_micros)
    raw: dict[str, Any] = json.loads(path.read_bytes())
    return state_from_json(raw)


def resume(out: Path, config: AgentConfig) -> tuple[AgentState, int]:
    """State for a run directory, money state from the journal; raises if unresumable."""
    state = load_state(out / "state.json", config)
    journal_path = out / "journal.db"
    if not journal_path.exists():
        return state, 0
    policy, records = read_journal(journal_path)
    state = reconcile_with_journal(state, records, policy.get("schema"))
    # The call budget covers the whole run directory, across restarts.
    return state, sum(r["provider_calls"] for r in records if r["kind"] == "tick_summary")


def _error_record(
    at: int, error: BaseException, consecutive: int, mode: str = "shadow"
) -> dict[str, Any]:
    # Type only: exception messages can embed provider content.
    return {
        "at": at,
        "kind": "tick_error",
        "mode": mode,
        "execution_eligible": False,
        "error_type": type(error).__name__,
        "consecutive": consecutive,
    }


def run_loop(
    agent: SpotAgent,
    journal: ExecutionJournal,
    state_path: Path,
    *,
    interval: int,
    max_ticks: int,
    call_budget: int,
    spent: int,
    should_stop: Callable[[], bool],
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """0 on a clean stop, 3 after repeated tick errors, 4 on an unresolved live order."""
    per_swap = agent.executor.calls_per_swap() if agent.executor is not None else 0
    per_tick, ticks, errors = tick_budget(agent.config, per_swap), 0, 0
    while not should_stop():
        if spent + agent.provider_calls() + per_tick > call_budget:
            print("call budget would be exceeded; stopping with state preserved")
            return 0
        started = clock()
        # tick() returns its records only at the end, so a raise mid-tick would leave
        # mutations (e.g. a paper buy) that were never journaled. Roll them back.
        before = copy.deepcopy(agent.state)
        submitted = agent.submissions()
        unresolved = False
        try:
            records = agent.tick(int(started))
            errors = 0
            booked = sum(r["kind"] in ("live_buy", "live_sell") for r in records)
            if agent.submissions() - submitted != booked:
                # Keep what was booked (those trades are real) and halt for the rest.
                unresolved = True
                error_record = _error_record(int(started), LiveUnresolved("x"), 1, agent.mode)
                records.append(error_record | {"category": "submission_without_booked_trade"})
        except LiveUnresolved as error:
            # A swap may have executed without a proven fill: never tick again on a book
            # that may be wrong. The executor already journaled intent and outcome.
            unresolved = True
            agent.state = before
            records = [_error_record(int(started), error, errors + 1, agent.mode)]
            records[0]["category"] = error.category
        except Exception as error:  # noqa: BLE001 - one bad tick must not lose the run
            errors += 1
            agent.state = before
            records = [_error_record(int(started), error, errors, agent.mode)]
            # Rolling back a book after a real swap would forget it; halt for review.
            if agent.submissions() != submitted:
                unresolved = True
        human = [
            r["reason"]
            for r in records
            if r["kind"] == "halt_review" and r.get("reason") in HUMAN_REVIEW_REASONS
        ]
        if agent.executor is not None and (unresolved or human):
            # Persist the halt before anything else from this tick: if a later write
            # fails, the run still refuses to resume rather than trading on.
            journal.append(
                LIVE_HALT,
                {
                    "at": int(started),
                    "kind": LIVE_HALT,
                    "mode": agent.mode,
                    "execution_eligible": False,
                    "reasons": sorted({*human, *(["unresolved_submission"] if unresolved else [])}),
                },
            )
            unresolved = True
        for record in records:
            journal.append(record["kind"], record)
            if record["kind"] in PRINTED:
                print(json.dumps(record, sort_keys=True), flush=True)
        save_state(state_path, agent.state)
        if unresolved:
            print(
                "spot agent halted: live order unresolved or position needs a human; "
                "this run directory will refuse to resume",
                file=sys.stderr,
            )
            return 4
        if errors >= MAX_CONSECUTIVE_ERRORS:
            print("spot shadow halted after repeated tick errors", file=sys.stderr)
            return 3
        ticks += 1
        if max_ticks and ticks >= max_ticks:
            return 0
        while not should_stop() and clock() < started + interval:
            sleep(1)
    return 0


def _build_agent(
    args: argparse.Namespace,
    gate: LiveGate | None,
    config: AgentConfig,
    state: AgentState,
    source: OnchainosSource,
    gmgn: GmgnSource | None,
    journal: ExecutionJournal,
) -> SpotAgent:
    """The agent for this run; with a gate, a live executor bound to the run's journal."""
    if gate is None:
        return SpotAgent(source, config, state, gmgn=gmgn)
    transport = JsonRpcTransport(os.environ[args.rpc_env])
    RpcClient(transport, expected_chain_id=8453)  # refuses a non-Base endpoint
    executor = LiveExecutor(
        gate,
        read=paced(run_cli(args.binary)),
        # Unpaced and unretried: a swap is submitted exactly once.
        submit=run_cli(args.binary, timeout=LIVE_SUBMIT_TIMEOUT_SECONDS),
        receipt=rpc_receipt_reader(transport),
        emit=journal.append,
    )
    if not any(r["kind"] == "live_start" for r in read_journal(journal.path)[1]):
        # Fresh live run: the book is the wallet's own USDC and nothing else. Paper
        # positions/PnL from a leftover state file must never become live holdings.
        state = AgentState(
            executor.balance(BASE_USDC),
            cooldown_until=state.cooldown_until,
            security=state.security,
            outcomes=state.outcomes,
        )
        journal.append(
            "live_start",
            {
                "at": int(time.time()),
                "kind": "live_start",
                "mode": "live",
                "execution_eligible": False,
                "cash_usd_micros": state.cash_usd_micros,
                **gate.describe(),
            },
        )
    return SpotAgent(source, config, state, gmgn=gmgn, executor=executor)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="AgentConfig JSON; defaults if omitted")
    parser.add_argument("--out", type=Path, required=True, help="run directory")
    parser.add_argument("--interval", type=int, default=120, help="seconds between ticks")
    parser.add_argument("--ticks", type=int, default=0, help="0 = until budget or signal")
    parser.add_argument("--call-budget", type=int, default=20_000)
    parser.add_argument("--binary", type=Path, default=default_binary())
    parser.add_argument("--gmgn", type=Path, default=gmgn_source.default_binary())
    parser.add_argument("--no-gmgn", action="store_true", help="OKX-only decisions")
    live = parser.add_argument_group("live trading (Agentic Wallet; off by default)")
    live.add_argument("--live", action="store_true", help="trade the approved wallet")
    live.add_argument("--max-trade-usd", type=int, help="hard per-trade cap, whole USD")
    live.add_argument("--slippage-bps", type=int, default=100)
    live.add_argument("--confirm", help=f'exactly "LIVE SPOT ON BASE FROM {APPROVED_WALLET}"')
    live.add_argument("--rpc-env", default="BASE_RPC_PRIMARY", help="env var with a Base RPC URL")
    live.add_argument(
        "--preflight-only", action="store_true", help="run read-only live checks and exit"
    )
    args = parser.parse_args()
    if args.interval < 30:
        parser.error("--interval must be at least 30 seconds")
    config = (
        AgentConfig.model_validate_json(args.config.read_bytes()) if args.config else AgentConfig()
    )
    for binary in [args.binary] + ([] if args.no_gmgn else [args.gmgn]):
        if not binary.is_absolute() or not binary.is_file() or not os.access(binary, os.X_OK):
            parser.error(f"executable not found: {binary.name}")
    args.out.mkdir(parents=True, exist_ok=True)
    raw_dir = args.out / "raw"
    raw_dir.mkdir(exist_ok=True)
    try:
        state, spent = resume(args.out, config)
    except (ValueError, KeyError, TypeError) as error:
        print(f"spot agent refused to resume: {error}", file=sys.stderr)
        return 2
    gate = None
    if args.live:
        if args.max_trade_usd is None or args.confirm is None:
            parser.error("--live requires --max-trade-usd and --confirm")
        try:
            gate = LiveGate(
                APPROVED_WALLET, args.max_trade_usd * 1_000_000, args.slippage_bps, args.confirm
            )
        except ValueError as error:
            parser.error(str(error))
        if not os.environ.get(args.rpc_env):
            parser.error(f"--live requires a Base RPC URL in {args.rpc_env}")
        try:
            RpcClient(JsonRpcTransport(os.environ[args.rpc_env]), expected_chain_id=8453).chain_id()
            checked = preflight(gate, paced(run_cli(args.binary)))
        except LiveRefused as refused:
            print(f"live preflight refused: {refused.category}", file=sys.stderr)
            return 2
        except (ValueError, OSError, OasisError) as error:
            # Type only: RPC errors can embed the endpoint URL.
            print(f"live preflight refused: rpc_{type(error).__name__}", file=sys.stderr)
            return 2
        print(json.dumps({"live_preflight": "passed", **checked}, sort_keys=True), flush=True)
        if args.preflight_only:
            return 0
    elif args.max_trade_usd is not None or args.confirm is not None:
        parser.error("--max-trade-usd/--confirm are only valid with --live")
    source = OnchainosSource(paced(run_cli(args.binary)), retain=raw_dir)
    gmgn = None
    if not args.no_gmgn:
        runner = paced(run_cli(args.gmgn, interpreter_dir=args.gmgn.parent))
        gmgn = GmgnSource(runner, clock=time.time, retain=raw_dir)
    stop = False

    def request_stop(*_: object) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    policy: dict[str, Any] = {
        "schema": "oasis.spot-agent-shadow.v1",
        "config": config.model_dump(mode="json"),
    }
    if gate is not None:
        # A distinct policy makes the journal refuse a shadow directory resumed as live.
        policy = policy | {"schema": LIVE_SCHEMA, "gate": gate.describe()}
    try:
        journal_context = ExecutionJournal(args.out / "journal.db", policy)
    except ValueError as error:
        # e.g. a shadow run directory reopened with --live: the policy binding refuses.
        print(f"spot agent refused to open journal: {error}", file=sys.stderr)
        return 2
    with journal_context as journal:
        try:
            agent = _build_agent(args, gate, config, state, source, gmgn, journal)
        except (ValueError, OSError, OasisError) as error:  # SourceFailure is a ValueError
            # Nothing has been submitted yet; refuse without a traceback or URL.
            print(f"live start refused: {type(error).__name__}", file=sys.stderr)
            return 2
        return run_loop(
            agent,
            journal,
            args.out / "state.json",
            interval=args.interval,
            max_ticks=args.ticks,
            call_budget=args.call_budget,
            spent=spent,
            should_stop=lambda: stop,
        )


if __name__ == "__main__":
    sys.exit(main())
