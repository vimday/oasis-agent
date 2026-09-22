"""Replay two retained holder sources and score a candidate; never signs or calls RPC."""

import argparse
from pathlib import Path

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.launch.holder_scoring import HolderIdentity, score_with_holders
from oasis_agent.launch.models import StrictModel
from oasis_agent.launch.scoring import Candidate, ScorePolicy
from oasis_agent.models.common import EvidenceRef, canonical_json


class Request(StrictModel):
    candidate: Candidate
    policy: ScorePolicy
    identity: HolderIdentity
    references: tuple[EvidenceRef, EvidenceRef]


def run(request_path: Path, evidence_path: Path, output_path: Path) -> None:
    if request_path.stat().st_size > 1_000_000:
        raise ValueError("request too large")
    request = Request.from_json(request_path.read_bytes())
    if not evidence_path.is_file() or output_path.exists():
        raise ValueError("existing evidence and new output required")
    store = EvidenceStore(evidence_path)
    try:
        result = score_with_holders(
            request.candidate,
            request.policy,
            store=store,
            references=request.references,
            identity=request.identity,
        )
        with output_path.open("xb") as handle:
            handle.write(canonical_json(result.model_dump(mode="json")) + b"\n")
    finally:
        store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        run(args.request, args.evidence, args.output)
    except Exception:  # noqa: BLE001 - never expose retained provider diagnostics
        parser.exit(1, "Holder-bound scoring failed; execution remains disabled.\n")
    print("Holder evidence replayed; research only, execution disabled.")


if __name__ == "__main__":
    main()
