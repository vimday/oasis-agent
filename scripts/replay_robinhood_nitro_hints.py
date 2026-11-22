"""Replay retained Robinhood Nitro block/transaction hints without network I/O."""

import argparse
from pathlib import Path

from oasis_agent.arbitrage.cycle_evidence import read_bounded
from oasis_agent.evidence.robinhood_nitro import (
    MAX_DECODE_BYTES,
    FeedReplay,
    NitroHintJournal,
    read_rpc_anchors,
)
from oasis_agent.models.common import canonical_json


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "decode", type=Path, nargs="*", help="retained decode files in arrival order"
    )
    parser.add_argument(
        "--checkpoint", type=Path, required=True, help="SQLite continuity checkpoint"
    )
    parser.add_argument(
        "--start-sequence",
        type=int,
        help="first required sequence; defaults to the first received sequence in a new checkpoint",
    )
    parser.add_argument(
        "--rpc-capture",
        type=Path,
        action="append",
        default=[],
        help="existing same-operator RPC campaign directory linked to a supplied decode file",
    )
    parser.add_argument("--limit", type=int, default=1024, help="maximum block hints in the view")
    args = parser.parse_args()
    replays = tuple(FeedReplay.parse(read_bounded(path, MAX_DECODE_BYTES)) for path in args.decode)
    replay_digests = {replay.decode_sha256 for replay in replays}
    anchors = tuple(
        anchor
        for capture in args.rpc_capture
        for anchor in read_rpc_anchors(capture, replay_digests)
    )
    journal = NitroHintJournal(args.checkpoint)
    try:
        report = journal.ingest(replays, anchors, start=args.start_sequence, limit=args.limit)
    finally:
        journal.close()
    print(canonical_json(report).decode())
    return 0 if report["status"] == "contiguous" else 3


if __name__ == "__main__":
    raise SystemExit(main())
