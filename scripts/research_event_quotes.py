"""Run the prespecified anonymous realtime quote experiment."""

import argparse
from dataclasses import asdict
from pathlib import Path

from oasis_agent.arbitrage import event_quote, event_quote_capture
from oasis_agent.arbitrage.event_quote import CHAINS
from oasis_agent.arbitrage.event_quote_replay import replay
from oasis_agent.models.common import canonical_json, sha256_hex

PROTOCOL = "docs/verification/EVENT_QUOTE_PROTOCOL_20261114.md"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("chain", choices=(*CHAINS, "replay"))
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.chain == "replay":
        print(canonical_json(replay(args.out)).decode())
        return 0
    chain = CHAINS[args.chain]
    protocol = (
        "docs/verification/ELYSIUM_EVENT_QUOTE_PROTOCOL_20261114.md"
        if args.chain == "elysium-live"
        else PROTOCOL
    )
    plan = {
        "schema": event_quote_capture.PLAN_SCHEMA,
        "chain": asdict(chain),
        "protocol_sha256": sha256_hex(Path(protocol).read_bytes()),
        "source_sha256": {
            module.__name__: sha256_hex(Path(module.__file__).read_bytes())
            for module in (event_quote, event_quote_capture)
        },
        "budget": event_quote_capture.CAPS,
        "route": (
            [pool.model_dump() for pool, _ in event_quote.BASE_LEGS]
            if args.chain == "base"
            else [
                event_quote.ELYSIUM_V4.model_dump(),
                {"pool": event_quote.AERO_CL, "quoter": event_quote.AERO},
                {"pool": event_quote.ELYSIUM_VOL},
            ]
            if args.chain == "elysium-live"
            else None
        ),
    }
    result = event_quote_capture.run(chain, args.out, plan)
    print(canonical_json(result).decode())
    return 3 if result["decision"] == "incomplete" else 0


if __name__ == "__main__":
    raise SystemExit(main())
