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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--cycles", type=int, default=100)
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
        with args.input.open("rb") as source:
            for _ in range(args.cycles):
                line = source.readline(1_000_001)
                if not line:
                    break
                if len(line) > 1_000_000:
                    raise ValueError("cycle exceeds size bound")
                cycle = LpCycle.from_json(line)
                result = service.consume(cycle, now=int(time.time()))
                print(result.decision, flush=True)
                if result.decision == "halt":
                    raise ValueError("shadow halted")
    except (ValueError, TypeError, KeyError, OSError):
        journal.fault(now=int(time.time()))
        raise SystemExit("LP shadow halted; details redacted; inspect journal") from None
    finally:
        journal.close()
        store.close()


if __name__ == "__main__":
    main()
