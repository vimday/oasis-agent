"""Run the Base spot agent in shadow mode, or inspect the Agentic Wallet read-only."""

import argparse
import copy
import json
import os
import signal
import sqlite3
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from oasis_agent.launch import gmgn_source
from oasis_agent.launch.gmgn_source import GmgnSource
from oasis_agent.launch.onchainos_source import (
    OnchainosSource,
    Runner,
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
from oasis_agent.launch.spot_budget import (
    RESERVE,
    SETTLE,
    budget_ledger,
    full_cycle_calls,
    legacy_error_charge,
)
from oasis_agent.launch.spot_capture import CAPTURE_SCHEMA, CaptureRecorder
from oasis_agent.launch.spot_executor import (
    APPROVED_WALLET,
    LiveGate,
    LiveRefused,
    LiveUnresolved,
    preflight,
)
from oasis_agent.launch.spot_lifecycle import HUMAN_REVIEW_REASONS, LIVE_HALT, SHADOW_SCHEMA
from oasis_agent.launch.spot_plan import (
    ExperimentPlan,
    PlanRefused,
    binding,
    plan_bound_in,
    planned_session,
    read_plan,
)
from oasis_agent.launch.spot_report import read_journal
from oasis_agent.models.common import canonical_json, sha256_hex
from oasis_agent.sniping.journal import ExecutionJournal

MAX_CONSECUTIVE_ERRORS = 5
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


# Budget unit: one logical provider query as counted by `provider_calls()` (one CLI
# invocation the agent asked for). Transport retries inside `paced` are NOT counted, so
# this bounds logical queries, not network requests.
#
# Worst case per full tick: hot + signals + enrich_top * (price, security, advanced, gmgn,
# 2 quotes) + one sell quote and one security read per open position + batched outcome
# price reads (at most MAX_PENDING / 40) + live swaps (every position sold, every slot
# bought).
def tick_budget(config: AgentConfig, calls_per_swap: int = 0) -> int:
    return full_cycle_calls(config.enrich_top, config.max_positions, calls_per_swap)


def manage_budget(held: int, calls_per_swap: int = 0) -> int:
    """Worst case of a manage-only cycle: per held position a security read, a sell quote
    and (live) one sell swap. Nothing else runs, so nothing else is reserved."""
    return held * (2 + calls_per_swap)


def plan_cycle(
    remaining: int, held: int, config: AgentConfig, calls_per_swap: int
) -> tuple[str, int]:
    """("full" | "manage_only" | "stop", reserved calls) for the next cycle.

    A full cycle needs its whole worst case. Otherwise held positions are still managed if
    that worst case fits; with no position there is nothing necessary to do, so the run
    stops rather than start a cycle it cannot finish.
    """
    full = tick_budget(config, calls_per_swap)
    if remaining >= full:
        return "full", full
    need = manage_budget(held, calls_per_swap)
    if held and remaining >= need:
        return "manage_only", need
    return "stop", full if not held else need


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
    # The call budget covers the whole run directory across restarts, including failed
    # and interrupted cycles (see spot_budget): restarts never forget spent calls.
    return state, budget_ledger(records, legacy_error_charge(policy))["charged"]


def start_capture(
    recorder: CaptureRecorder,
    agent: SpotAgent,
    run_policy_sha256: str,
    plan: ExperimentPlan | None,
    archive: Path,
) -> str | None:
    """Write the session's start record; a research run (`plan`) binds its plan into it.

    None means the run may start; otherwise a fixed refusal. A research run refuses when
    another session of the archive already binds the plan (one plan is one trial), and
    when its own start record cannot be read back binding exactly that plan. Nothing here
    calls a provider, so a refused research run has queried nothing.
    """
    if plan is None:
        recorder.start_session(agent, run_policy_sha256)
        return None
    try:
        if plan_bound_in(archive, plan):
            return "plan_already_bound_in_this_archive"
    except PlanRefused as refused:
        return refused.category
    recorder.start_session(agent, run_policy_sha256, binding(plan))
    if not recorder.healthy:
        return "plan_not_bound"
    try:
        planned_session(archive, plan, recorder.session_id)
    except PlanRefused:
        return "plan_not_bound"
    return None


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
    capture: CaptureRecorder | None = None,
) -> int:
    """0 on a clean stop, 3 after repeated tick errors, 4 on an unresolved live order.

    `capture` (shadow only) archives each cycle's ordered inputs and outcome digests in
    its own journal; it never writes the run journal and never raises, so the financial
    write order below is unchanged with or without it.
    """
    if capture is not None and agent.executor is not None:
        raise ValueError("live runs are never captured")
    per_swap = agent.executor.calls_per_swap() if agent.executor is not None else 0
    ticks, errors = 0, 0

    def ended(code: int, reason: str) -> int:
        # Written only on a normal return, after the run's own records: a raise anywhere
        # above leaves no end, so that session is replayable only as a prefix.
        if capture is not None:
            capture.session_end(reason)
        return code

    while not should_stop():
        used = spent + agent.provider_calls()
        held = len(agent.state.positions)
        cycle, reserved = plan_cycle(call_budget - used, held, agent.config, per_swap)
        if cycle == "stop":
            # Resource exhaustion, not a trading fault: journaled so the report can say
            # why the run stopped and what it still holds. Not a live_halt.
            stop_record: dict[str, Any] = {
                "at": int(clock()),
                "kind": "budget_stop",
                "mode": agent.mode,
                "execution_eligible": False,
                "call_budget": call_budget,
                "calls_used": used,
                "reserved_needed": reserved,
                "open_positions": held,
                # sample x horizon units (not tokens): the label work this stop leaves.
                "label_backlog": agent.state.outcomes.backlog(int(clock())),
            }
            journal.append("budget_stop", stop_record)
            print(json.dumps(stop_record, sort_keys=True), flush=True)
            print("call budget cannot cover the next cycle; stopping with state preserved")
            return ended(0, "budget_stop")
        started = clock()
        # Reserve before any provider call: a kill from here on leaves an unsettled
        # reservation that resume charges in full, so the budget can never shrink.
        reserve_id, calls_before = uuid.uuid4().hex, agent.provider_calls()
        journal.append(
            RESERVE,
            {
                "at": int(started),
                "kind": RESERVE,
                "mode": agent.mode,
                "execution_eligible": False,
                "reserve_id": reserve_id,
                "cycle": cycle,
                "reserved": reserved,
            },
        )
        # tick() returns its records only at the end, so a raise mid-tick would leave
        # mutations (e.g. a paper buy) that were never journaled. Roll them back.
        before = copy.deepcopy(agent.state)
        submitted = agent.submissions()
        unresolved = False
        tick_error: str | None = None
        if capture is not None:
            capture.cycle_start(int(started), cycle == "manage_only")
        try:
            records = agent.tick(
                int(started), manage_only=cycle == "manage_only", should_stop=should_stop
            )
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
            tick_error = type(error).__name__
            records = [_error_record(int(started), error, errors + 1, agent.mode)]
            records[0]["category"] = error.category
        except Exception as error:  # noqa: BLE001 - one bad tick must not lose the run
            errors += 1
            agent.state = before
            tick_error = type(error).__name__
            records = [_error_record(int(started), error, errors, agent.mode)]
            # Rolling back a book after a real swap would forget it; halt for review.
            if agent.submissions() != submitted:
                unresolved = True
        if capture is not None:
            # The tick's own return value (before error records are added) and the
            # state after rollback: exactly what replay of this cycle must reproduce.
            capture.cycle_end(None if tick_error else records, tick_error, agent)
        # Write order is a safety contract. (1) A financial halt first: if any later write
        # fails, the run still refuses to resume. (2) Then the budget settle: if it fails,
        # the cycle's reservation stays unsettled and is charged in full (conservative),
        # never refunded. (3) Then the tick's own records.
        human = [
            r["reason"]
            for r in records
            if r["kind"] == "halt_review" and r.get("reason") in HUMAN_REVIEW_REASONS
        ]
        if agent.executor is not None and (unresolved or human):
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
        journal.append(
            SETTLE,
            {
                "at": int(clock()),
                "kind": SETTLE,
                "mode": agent.mode,
                "execution_eligible": False,
                "reserve_id": reserve_id,
                "used": agent.provider_calls() - calls_before,
            },
        )
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
            return ended(4, "unresolved_live")
        if errors >= MAX_CONSECUTIVE_ERRORS:
            print("spot shadow halted after repeated tick errors", file=sys.stderr)
            return ended(3, "repeated_errors")
        if should_stop():
            return ended(0, "stop_requested")
        ticks += 1
        if max_ticks and ticks >= max_ticks:
            return ended(0, "max_ticks")
        while not should_stop() and clock() < started + interval:
            sleep(1)
    return ended(0, "stop_requested")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="AgentConfig JSON; defaults if omitted")
    parser.add_argument("--out", type=Path, required=True, help="run directory")
    parser.add_argument("--interval", type=int, default=120, help="seconds between ticks")
    parser.add_argument("--ticks", type=int, default=0, help="0 = until budget or signal")
    parser.add_argument(
        "--call-budget", type=int, default=20_000, help="total logical provider queries (>=0)"
    )
    parser.add_argument("--binary", type=Path, default=default_binary())
    parser.add_argument("--gmgn", type=Path, default=gmgn_source.default_binary())
    parser.add_argument("--no-gmgn", action="store_true", help="OKX-only decisions")
    live = parser.add_argument_group("read-only Agentic Wallet diagnostics")
    live.add_argument(
        "--live",
        action="store_true",
        help="reserved for a released execution path, or with --preflight-only",
    )
    live.add_argument("--max-trade-usd", type=int, help="hard per-trade cap, whole USD")
    live.add_argument("--slippage-bps", type=int, default=100)
    live.add_argument("--confirm", help=f'exactly "LIVE SPOT ON BASE FROM {APPROVED_WALLET}"')
    # Keep old invocations parseable. This command never opens a chain RPC.
    live.add_argument("--rpc-env", help=argparse.SUPPRESS)
    live.add_argument(
        "--preflight-only", action="store_true", help="run read-only live checks and exit"
    )
    parser.add_argument(
        "--plan",
        type=Path,
        help="research mode: a pre-registered experiment plan (JSON) bound into the capture "
        "before the first provider call; the run refuses to start if it cannot be bound",
    )
    args = parser.parse_args()
    if args.plan is not None and args.live:
        parser.error("--plan is only valid for a shadow research run")
    if args.interval < 30:
        parser.error("--interval must be at least 30 seconds")
    if args.call_budget < 0:
        parser.error("--call-budget must be a non-negative integer")
    if args.preflight_only and not args.live:
        parser.error("--preflight-only requires --live")
    if args.live:
        if not args.preflight_only:
            # A wallet confirmation does not authorize the exact transaction. There is
            # no released spot signer that can independently enforce the risk policy.
            print("spot live refused: independent execution gate unavailable", file=sys.stderr)
            return 2
        if args.call_budget < 3:
            print(
                "spot wallet diagnostics refused: insufficient logical call budget", file=sys.stderr
            )
            return 2
        if args.max_trade_usd is None or args.confirm is None:
            parser.error("--live --preflight-only requires --max-trade-usd and --confirm")
        try:
            gate = LiveGate(
                APPROVED_WALLET, args.max_trade_usd * 1_000_000, args.slippage_bps, args.confirm
            )
        except ValueError as error:
            parser.error(str(error))
        if (
            not args.binary.is_absolute()
            or not os.access(args.binary, os.X_OK)
            or not args.binary.is_file()
        ):
            parser.error(f"executable not found: {args.binary.name}")
        try:
            checked = preflight(gate, run_cli(args.binary))
        except (LiveRefused, OSError) as error:
            reason = error.category if isinstance(error, LiveRefused) else type(error).__name__
            print(f"spot wallet diagnostics refused: {reason}", file=sys.stderr)
            return 2
        print(
            json.dumps(
                {"wallet_diagnostics": "passed", "execution_eligible": False, **checked},
                sort_keys=True,
            ),
            flush=True,
        )
        return 0
    if args.max_trade_usd is not None or args.confirm is not None:
        parser.error("--max-trade-usd/--confirm are only valid with --live")

    config = (
        AgentConfig.model_validate_json(args.config.read_bytes()) if args.config else AgentConfig()
    )
    for binary in [args.binary] + ([] if args.no_gmgn else [args.gmgn]):
        if not binary.is_absolute() or not binary.is_file() or not os.access(binary, os.X_OK):
            parser.error(f"executable not found: {binary.name}")
    plan = None
    if args.plan is not None:
        try:
            plan = read_plan(args.plan)
        except PlanRefused as refused:
            print(f"spot research run refused: {refused.category}", file=sys.stderr)
            return 2
        if time.time() >= plan.window.start:
            # Acceptance needs the plan bound before the window opens; a session started
            # later could never be accepted, so it is not started.
            print("spot research run refused: plan_window_already_open", file=sys.stderr)
            return 2
    args.out.mkdir(parents=True, exist_ok=True)
    raw_dir = args.out / "raw"
    raw_dir.mkdir(exist_ok=True)
    try:
        state, spent = resume(args.out, config)
    except (ValueError, KeyError, TypeError) as error:
        print(f"spot agent refused to resume: {error}", file=sys.stderr)
        return 2
    archive = None
    try:
        archive = ExecutionJournal(args.out / "capture.db", {"schema": CAPTURE_SCHEMA})
    except ValueError, sqlite3.Error:
        print("input capture disabled: archive unavailable", file=sys.stderr)
    if plan is not None and archive is None:
        # A research run without its archive could never show where its plan was bound.
        print("spot research run refused: capture_unavailable", file=sys.stderr)
        return 2
    recorder = None if archive is None else CaptureRecorder(archive, raw_dir)

    def wrap(provider: str, runner: Runner) -> Runner:
        if recorder is None:
            return paced(runner)
        return recorder.logical(provider, paced(recorder.attempt(runner)))

    source_clock = time.time if recorder is None else recorder.source_clock
    source = OnchainosSource(
        wrap("onchainos", run_cli(args.binary)), clock=source_clock, retain=raw_dir
    )
    gmgn = None
    if not args.no_gmgn:
        runner = wrap("gmgn", run_cli(args.gmgn, interpreter_dir=args.gmgn.parent))
        gmgn = GmgnSource(runner, clock=source_clock, retain=raw_dir)
    stop = False

    def request_stop(*_: object) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    policy = {"schema": SHADOW_SCHEMA, "config": config.model_dump(mode="json")}
    try:
        journal_context = ExecutionJournal(args.out / "journal.db", policy)
    except ValueError as error:
        print(f"spot agent refused to open journal: {error}", file=sys.stderr)
        return 2
    with journal_context as journal:
        try:
            agent = SpotAgent(source, config, state, gmgn=gmgn)
            # A research run always has a recorder: it refused above without an archive.
            if recorder is not None:
                digest = sha256_hex(canonical_json(policy))
                refusal = start_capture(recorder, agent, digest, plan, args.out / "capture.db")
                if refusal is not None:
                    print(f"spot research run refused: {refusal}", file=sys.stderr)
                    return 2
            return run_loop(
                agent,
                journal,
                args.out / "state.json",
                interval=args.interval,
                max_ticks=args.ticks,
                call_budget=args.call_budget,
                spent=spent,
                should_stop=lambda: stop or (args.out / "STOP").exists(),
                capture=recorder,
            )
        finally:
            if archive is not None:
                archive.close()


if __name__ == "__main__":
    sys.exit(main())
