"""Capture pinned holder balances for a later coherent market decision cycle.

Read-only: URLs stay in environment variables, no signer or broadcast imports.
"""

import argparse
import json
import os
from dataclasses import asdict
from pathlib import Path

from oasis_agent.evidence.exact_capture import wire_envelope
from oasis_agent.evidence.rpc_capture import BoundedExchangeRecorder
from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.launch.holder_capture import (
    collect_holder_quorum,
    persist_holder_quorum,
    replay_holder_evidence,
)
from oasis_agent.launch.models import HolderTarget, parse_target
from oasis_agent.models.common import canonical_json
from oasis_agent.rpc.transport import JsonRpcTransport


def capture(
    *,
    target: HolderTarget,
    candidates: tuple[str, ...],
    block: int,
    output: Path,
    urls: tuple[str, str],
    deadline_seconds: int = 60,
) -> dict[str, object]:
    if not all(urls) or urls[0] == urls[1]:
        raise ValueError("two distinct RPC endpoints required")
    sources = ("primary", "secondary")
    output.mkdir(parents=True, exist_ok=False)
    recorders = (BoundedExchangeRecorder(), BoundedExchangeRecorder())
    sessions = tuple(
        (JsonRpcTransport(url, timeout=10, retries=0, recorder=recorder), recorder, source)
        for url, recorder, source in zip(urls, recorders, sources, strict=True)
    )
    try:
        quorum = collect_holder_quorum(
            sessions[0],
            sessions[1],
            target=target,
            block_number=block,
            candidates=candidates,
            deadline_seconds=deadline_seconds,
        )
        store = EvidenceStore(output / "evidence.db")
        try:
            refs = persist_holder_quorum(store, quorum)
            # Replay exact bytes from storage, not just the in-memory interpretation.
            retained = tuple(replay_holder_evidence(store, ref).observation for ref in refs)
        finally:
            store.close()
        report: dict[str, object] = {
            "schema": "oasis.holder-capture-report.v1",
            "execution_eligible": False,
            "status": "captured",
            "chain_id": target.chain_id,
            "block": block,
            "target_ca": target.target_ca,
            "raw_replay_verified": True,
            "supply_covered": all(item.complete for item in retained),
            "evidence": [asdict(ref) for ref in refs],
            "holder_observations": [item.model_dump(mode="json") for item in retained],
            "unresolved": [
                "candidate_discovery",
                "token_behavior_and_control_plane",
                "provider_independence",
                "beneficial_owner_clusters",
                "economic_edge",
            ],
        }
        with (output / "report.json").open("xb") as handle:
            handle.write(canonical_json(report) + b"\n")
        return report
    except Exception:  # noqa: BLE001 - redact hostile RPC/transport exceptions at I/O boundary
        # Preserve partial wire data for diagnosis without publishing a successful sample.
        failure_refs = []
        store = EvidenceStore(output / "evidence.db")
        try:
            for recorder, source in zip(recorders, sources, strict=True):
                if recorder.exchanges:
                    failure_refs.append(
                        asdict(
                            store.put(
                                "holder_capture_failure",
                                canonical_json(
                                    {
                                        "schema": "oasis.holder-capture-failure.v1",
                                        "execution_eligible": False,
                                        "requested_block": block,
                                        "requested_chain_id": target.chain_id,
                                        "target_ca": target.target_ca,
                                    }
                                ),
                                None,
                                source,
                                request=wire_envelope(recorder.exchanges, response=False),
                                raw_response=wire_envelope(recorder.exchanges, response=True),
                            )
                        )
                    )
        finally:
            store.close()
        with (output / "HALT.json").open("xb") as handle:
            handle.write(
                canonical_json(
                    {
                        "status": "halt",
                        "execution_eligible": False,
                        "reason": "holder_capture_or_binding_failed",
                        "partial_evidence": failure_refs,
                    }
                )
                + b"\n"
            )
        raise ValueError(
            "holder capture failed; inspect retained evidence; no eligibility granted"
        ) from None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument(
        "--candidates", type=Path, required=True, help="JSON array of public addresses"
    )
    parser.add_argument("--block", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True, help="new evidence/report directory")
    parser.add_argument("--deadline-seconds", type=int, default=60)
    args = parser.parse_args()
    try:
        target = parse_target(args.target.read_bytes())
        if not isinstance(target, HolderTarget):
            raise ValueError("explicit v3 holder target required")
        if args.candidates.stat().st_size > 100_000:
            raise ValueError("candidate input too large")
        candidates = json.loads(args.candidates.read_bytes())
        if not isinstance(candidates, list) or any(
            not isinstance(item, str) for item in candidates
        ):
            raise ValueError("candidate addresses must be a JSON array of strings")
        result = capture(
            target=target,
            candidates=tuple(candidates),
            block=args.block,
            output=args.output,
            urls=(
                os.environ.get("OASIS_LAUNCH_RPC_PRIMARY", ""),
                os.environ.get("OASIS_LAUNCH_RPC_SECONDARY", ""),
            ),
            deadline_seconds=args.deadline_seconds,
        )
    except Exception:  # noqa: BLE001 - never print endpoint values or provider response text
        parser.exit(1, "Holder capture unavailable; no execution eligibility granted.\n")
    print(
        "Holder raw replay verified; supply covered:",
        result["supply_covered"],
        "; execution_eligible: false",
    )


if __name__ == "__main__":
    main()
