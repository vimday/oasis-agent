"""Offline complete window valuation, cash and action-gas replay; no capital authority."""

import argparse
from dataclasses import asdict
from pathlib import Path
from typing import Literal

from pydantic import Field

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.farming.position_return_replay import PositionReturnRequest, replay_position_return
from oasis_agent.launch.models import StrictModel
from oasis_agent.models.common import canonical_json, sha256_hex


class Request(StrictModel):
    schema_version: Literal["oasis.position-return-input.v1"] = Field(alias="schema")
    request: PositionReturnRequest


def run(input_path: Path, evidence_path: Path, output: Path) -> None:
    if not evidence_path.is_file() or output.exists():
        raise ValueError("existing retained evidence and new report required")
    raw = input_path.read_bytes()
    request = Request.from_json(raw)
    store = EvidenceStore(evidence_path)
    try:
        result = replay_position_return(store, request.request)
        report = {
            "schema": "oasis.position-return-report.v1",
            "input_sha256": sha256_hex(raw),
            **asdict(result),
            "valuation_role": "feed-valued integer LP claims; not executable exit or received cash",
            "rounding": "opening debit/action gas ceiling; closing NAV/signed cash floor per leg",
            "embedded_losses": "inventory/IL/LVR/protocol effects are not deducted a second time",
        }
        with output.open("xb") as stream:
            stream.write(canonical_json(report) + b"\n")
    finally:
        store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        run(args.input, args.evidence, args.output)
    except Exception:  # noqa: BLE001 - retained input must not enter error logs
        parser.exit(3, "Position return replay refused; no accepted report produced.\n")
    print("Marked cash/action-gas return replayed; full costs and release remain unresolved.")


if __name__ == "__main__":
    main()
