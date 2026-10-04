"""Offline no-action LP window and marked hold comparison; no capital authority."""

import argparse
from dataclasses import asdict
from pathlib import Path
from typing import Literal

from pydantic import Field

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.farming.position_hold_return import (
    PositionHoldRequest,
    replay_position_hold_return,
)
from oasis_agent.launch.models import StrictModel
from oasis_agent.models.common import canonical_json, sha256_hex


class Request(StrictModel):
    schema_version: Literal["oasis.position-hold-input.v1"] = Field(alias="schema")
    request: PositionHoldRequest


def run(input_path: Path, evidence_path: Path, output: Path) -> None:
    if not evidence_path.is_file() or output.exists():
        raise ValueError("existing retained evidence and new report required")
    raw = input_path.read_bytes()
    request = Request.from_json(raw)
    store = EvidenceStore(evidence_path)
    try:
        result = replay_position_hold_return(store, request.request)
        report = {
            "schema": "oasis.position-hold-report.v1",
            "input_sha256": sha256_hex(raw),
            **asdict(result),
            "valuation_role": "integer LP claims and identical opening-inventory marked baseline",
            "hold_rounding": "upper closing hold mark for conservative excess; not lower PnL",
            "fee_role": "uncollected claims, not received income",
            "gas_scope": "no LP modifications observed; other account or failed gas unresolved",
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
        parser.exit(3, "Position hold replay refused; no accepted report produced.\n")
    print("Marked LP/hold comparison replayed; full costs and release remain unresolved.")


if __name__ == "__main__":
    main()
