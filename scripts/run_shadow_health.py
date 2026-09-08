"""Bounded public-chain shadow health run; always HOLD/HALT, never trade."""

import argparse
import subprocess
import time
from dataclasses import asdict
from pathlib import Path

from oasis_agent.errors import RpcError
from oasis_agent.evidence.rpc_capture import BoundedExchangeRecorder
from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.models.common import canonical_json
from oasis_agent.rpc.client import RpcClient
from oasis_agent.rpc.transport import JsonRpcTransport
from oasis_agent.shadow.journal import ShadowJournal
from scripts.run_pinned_fork import NETWORKS, PublicPoster


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--network", choices=NETWORKS, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cycles", type=int, default=3)
    args = parser.parse_args()
    if not 1 <= args.cycles <= 20 or args.output.exists():
        parser.error("1..20 cycles and new output directory required")
    args.output.mkdir(parents=True)
    chain, url, _, _ = NETWORKS[args.network]
    store = EvidenceStore(args.output / "evidence.db")
    journal = ShadowJournal(args.output / "shadow.db", chain_id=chain, max_age_seconds=120)
    decisions = []
    previous_number: int | None = None
    try:
        for _ in range(args.cycles):
            recorder = BoundedExchangeRecorder()
            rpc = RpcClient(
                JsonRpcTransport(url, http=PublicPoster(url), recorder=recorder, retries=1),
                expected_chain_id=chain,
            )
            # Resolve latest solely for observation; retain exact height/hash and
            # re-read that height before recording a coherent observation.
            head = rpc.get_block_snapshot("latest")
            target = (
                head.ref.number
                if previous_number is None
                else min(head.ref.number, previous_number + 1)
            )
            header = rpc.get_block_snapshot(target)
            if rpc.get_block_snapshot(header.ref.number) != header:
                raise ValueError("shadow acquisition reorg")
            raw = [
                {
                    "method": x.method,
                    "request": x.request_payload.hex(),
                    "response": x.response_payload.hex(),
                }
                for x in recorder.exchanges
            ]
            ref = store.put(
                "shadow_header",
                canonical_json(asdict(header)),
                header.ref,
                args.network + "-public",
                request={"wire": [x["request"] for x in raw]},
                raw_response={"wire": raw},
                interpretation={"trust": "single_provider_research", "execution_eligible": False},
            )
            decision = journal.observe(header, ref, now=int(time.time()))
            decisions.append(decision)
            previous_number = header.ref.number
            (args.output / "summary.json").write_bytes(
                canonical_json(
                    {
                        "decisions": decisions,
                        "strategy_qualification": "unresolved",
                        "execution_eligible": False,
                    }
                )
            )
            if decision["decision"] == "HALT":
                break
    except (RpcError, ValueError, TypeError, OSError, subprocess.SubprocessError):
        decisions.append(journal.fault(now=int(time.time())))
        (args.output / "summary.json").write_bytes(
            canonical_json(
                {
                    "decisions": decisions,
                    "strategy_qualification": "unresolved",
                    "execution_eligible": False,
                }
            )
        )
        raise RuntimeError("shadow acquisition failed; retained HALT; details redacted") from None
    finally:
        journal.close()
    print(f"{len(decisions)} observations retained at {args.output}; no trading authority")


if __name__ == "__main__":
    main()
