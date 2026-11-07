"""Run the shadow spot agent: live OKX onchainos claims in, paper decisions out.

Every decision is appended to a hash-linked journal before the next tick. The book is
paper-only; this command holds no key and cannot sign, approve or broadcast. A provider
call budget bounds total usage; the loop stops (state preserved) when it would overrun.
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

from oasis_agent.launch import gmgn_source
from oasis_agent.launch.gmgn_source import GmgnSource
from oasis_agent.launch.onchainos_source import OnchainosSource, default_binary, paced, run_cli
from oasis_agent.launch.spot_agent import (
    AgentConfig,
    AgentState,
    SpotAgent,
    reconcile_with_journal,
    state_from_json,
    state_to_json,
)
from oasis_agent.launch.spot_report import read_journal
from oasis_agent.models.common import canonical_json
from oasis_agent.sniping.journal import ExecutionJournal

MAX_CONSECUTIVE_ERRORS = 5
PRINTED = frozenset({"paper_buy", "paper_sell", "halt_review", "tick_summary", "tick_error"})


# Worst case per tick: hot + signals + enrich_top * (price, security, advanced, gmgn, 2 quotes)
# + one sell quote and one security read per open position.
def tick_budget(config: AgentConfig) -> int:
    # + batched forward-outcome price reads (at most MAX_PENDING / 40).
    return 2 + config.enrich_top * 6 + config.max_positions * 2 + 50


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
    records = read_journal(journal_path)[1]
    state = reconcile_with_journal(state, records)
    # The call budget covers the whole run directory, across restarts.
    return state, sum(r["provider_calls"] for r in records if r["kind"] == "tick_summary")


def _error_record(at: int, error: BaseException, consecutive: int) -> dict[str, Any]:
    # Type only: exception messages can embed provider content.
    return {
        "at": at,
        "kind": "tick_error",
        "mode": "shadow",
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
    """0 on a clean stop, 3 after repeated tick errors. Journal before state file."""
    per_tick, ticks, errors = tick_budget(agent.config), 0, 0
    while not should_stop():
        if spent + agent.provider_calls() + per_tick > call_budget:
            print("call budget would be exceeded; stopping with state preserved")
            return 0
        started = clock()
        # tick() returns its records only at the end, so a raise mid-tick would leave
        # mutations (e.g. a paper buy) that were never journaled. Roll them back.
        before = copy.deepcopy(agent.state)
        try:
            records = agent.tick(int(started))
            errors = 0
        except Exception as error:  # noqa: BLE001 - one bad tick must not lose the run
            errors += 1
            agent.state = before
            records = [_error_record(int(started), error, errors)]
        for record in records:
            journal.append(record["kind"], record)
            if record["kind"] in PRINTED:
                print(json.dumps(record, sort_keys=True), flush=True)
        save_state(state_path, agent.state)
        if errors >= MAX_CONSECUTIVE_ERRORS:
            print("spot shadow halted after repeated tick errors", file=sys.stderr)
            return 3
        ticks += 1
        if max_ticks and ticks >= max_ticks:
            return 0
        while not should_stop() and clock() < started + interval:
            sleep(1)
    return 0


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
        print(f"spot shadow refused to resume: {error}", file=sys.stderr)
        return 2
    source = OnchainosSource(paced(run_cli(args.binary)), retain=raw_dir)
    gmgn = None
    if not args.no_gmgn:
        runner = paced(run_cli(args.gmgn, interpreter_dir=args.gmgn.parent))
        gmgn = GmgnSource(runner, clock=time.time, retain=raw_dir)
    agent = SpotAgent(source, config, state, gmgn=gmgn)
    stop = False

    def request_stop(*_: object) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    policy = {"schema": "oasis.spot-agent-shadow.v1", "config": config.model_dump(mode="json")}
    with ExecutionJournal(args.out / "journal.db", policy) as journal:
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
