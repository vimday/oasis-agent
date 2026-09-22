"""Run bounded evidence-backed LP shadow cycles from a JSONL input producer.

This consumer validates retained evidence, pins code/config on restart, and
never signs. Input producers must acquire genuine features and reconciliation;
providing a JSON file does not prove their economic correctness.
"""

import argparse
import time
from pathlib import Path

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.shadow.journal import ShadowJournal
from oasis_agent.shadow.service import LpCycle, LpShadowService, parse_policy
from oasis_agent.shadow.stream import cycle_lines


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--cycles", type=int, default=100)
    parser.add_argument("--follow", action="store_true", help="follow an append-only input stream")
    parser.add_argument("--poll-ms", type=int, default=500)
    parser.add_argument("--idle-seconds", type=int, default=60)
    parser.add_argument("--stop-file", type=Path)
    args = parser.parse_args()
    if not 1 <= args.cycles <= 10000 or not args.evidence.is_file():
        parser.error("existing evidence database and 1..10000 cycles required")
    values = parse_policy(args.policy.read_bytes())
    journal = ShadowJournal(
        args.journal, chain_id=values.pop("chain_id"), max_age_seconds=values.pop("max_age_seconds")
    )
    store = EvidenceStore(args.evidence)
    try:
        service = LpShadowService(store, journal, **values)
        completed = 0
        for line in cycle_lines(
            args.input,
            follow=args.follow,
            poll_ms=args.poll_ms,
            idle_seconds=args.idle_seconds,
            stop_file=args.stop_file,
        ):
            cycle = LpCycle.from_json(line)
            result = service.consume_once(cycle, now=int(time.time()))
            if result.halted:
                raise ValueError("shadow halted")
            if not result.duplicate:
                completed += 1
                print(result.record["decision"], flush=True)
            if completed >= args.cycles:
                break
    except (ValueError, TypeError, KeyError, OSError):
        journal.fault(now=int(time.time()))
        raise SystemExit("LP shadow halted; details redacted; inspect journal") from None
    finally:
        journal.close()
        store.close()


if __name__ == "__main__":
    main()
