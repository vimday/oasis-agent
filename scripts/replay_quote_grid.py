"""Replay retained direct quote capacity; no network, approval or money authority."""

import argparse
from dataclasses import asdict
from pathlib import Path
from typing import Literal

from pydantic import Field

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.farming.quote_grid_replay import QuoteGridRequest, replay_quote_grid
from oasis_agent.launch.models import StrictModel
from oasis_agent.models.common import canonical_json, sha256_hex


class Request(StrictModel):
    schema_version: Literal["oasis.quote-grid-input.v1"] = Field(alias="schema")
    request: QuoteGridRequest


def run(input_path: Path, quotes: Path, states: Path, output: Path) -> None:
    if not quotes.is_file() or not states.is_file() or output.exists():
        raise ValueError("existing evidence stores and new report required")
    raw = input_path.read_bytes()
    request = Request.from_json(raw)
    quote_store, state_store = EvidenceStore(quotes), EvidenceStore(states)
    try:
        result = replay_quote_grid(quote_store, state_store, request.request)
        report = {
            "schema": "oasis.quote-grid-report.v1",
            "input_sha256": sha256_hex(raw),
            **asdict(result),
            "impact_role": "average output shortfall versus raw pool spot, including fees",
            "capacity_role": "sampled direct-quote base units; not settled depth or USD",
            "freshness_role": "historical block only; not a current execution quote",
        }
        with output.open("xb") as stream:
            stream.write(canonical_json(report) + b"\n")
    finally:
        quote_store.close()
        state_store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("input", "quotes", "states", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    try:
        run(args.input, args.quotes, args.states, args.output)
    except Exception:  # noqa: BLE001 - raw input never enters the error log
        parser.exit(3, "Quote grid replay refused; no accepted report produced.\n")
    print("Historical quote/spot comparison retained; target admission remains false.")


if __name__ == "__main__":
    main()
