"""Build one replayable cost bundle for one journal-matched spot fill, from frozen RPC.

Usage:
  python -m scripts.capture_spot_cost --run RUN_DIR --intent INTENT_ID \
      --frozen PRIMARY.json --frozen SECONDARY.json --out BUNDLE_DIR

Each --frozen file is `oasis.frozen-rpc.v1` retained responses of ONE declared
provider (its `source`); the first is the primary. Requests are answered only from
those files: nothing here opens a network connection, wallet or key. The journal is
read once (hash-verified, read-only). BUNDLE_DIR must not exist; on success it holds
`evidence.db` and `binding.json`, already re-verified, and

  python -m scripts.report_spot_shadow --run RUN_DIR \
      --cost-binding BUNDLE_DIR/binding.json --evidence BUNDLE_DIR/evidence.db

replays the same fee. On any refusal nothing is published and the exit code is 2.
"""

import argparse
import json
import sys
from pathlib import Path

from oasis_agent.launch.spot_cost_binding import BASE_CHAIN_ID
from oasis_agent.launch.spot_cost_capture import (
    DIGEST_FILE_BYTES,
    CostCaptureRefused,
    collect_order_cost,
    frozen_origin,
    frozen_sessions,
    load_frozen,
    write_cost_bundle,
)
from oasis_agent.launch.spot_report import read_journal


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--intent", required=True)
    parser.add_argument("--frozen", type=Path, action="append", default=[])
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if len(args.frozen) != 2:
        parser.error("exactly two --frozen files (primary, secondary) are required")
    if args.out.resolve().is_relative_to(args.run.resolve()):
        parser.error("--out must be outside the run directory (it holds original evidence)")
    try:
        # Each file is read once; its digest is of exactly the bytes that were parsed.
        loaded = [load_frozen(path.read_bytes()) for path in args.frozen]
        frozen = [parsed for parsed, _ in loaded]
        sources = tuple(f.get("source") if isinstance(f, dict) else None for f in frozen)
        if not all(isinstance(s, str) for s in sources) or sources[0] == sources[1]:
            raise CostCaptureRefused("provider_pair_invalid")
        pair = (str(sources[0]), str(sources[1]))
        origin = frozen_origin(pair, (loaded[0][1], loaded[1][1]), DIGEST_FILE_BYTES)
        sessions = frozen_sessions(dict(zip(pair, frozen, strict=True)), chain_id=BASE_CHAIN_ID)
        _, records = read_journal(args.run / "journal.db")
        header, captures = collect_order_cost(records, args.intent, sources=pair, sessions=sessions)
        view = write_cost_bundle(args.out, records, args.intent, header, captures, origin=origin)
    except CostCaptureRefused as refused:
        print(f"spot cost capture refused: {refused.category}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as error:
        print(f"spot cost capture refused: {type(error).__name__}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {"bundle": str(args.out), "origin": view["origin"], **view["native_wei"]},
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
