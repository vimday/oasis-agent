"""Offline binding of raw LP owner settlement and whole transaction gas proofs."""

import argparse
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.farming.cash_window import CashWindowRequest
from oasis_agent.farming.position_action_costs import ActionCostProof, bind_position_action_costs
from oasis_agent.launch.models import StrictModel
from oasis_agent.models.common import canonical_json


class Request(StrictModel):
    schema_version: Literal["oasis.lp-action-cost-input.v1"] = Field(alias="schema")
    cash_window: CashWindowRequest
    proofs: Annotated[tuple[ActionCostProof, ...], Field(max_length=10_000)]


def verify(input_path: Path, evidence_path: Path, output: Path) -> None:
    if not evidence_path.is_file() or output.exists():
        raise ValueError("existing evidence and a new output required")
    request = Request.from_json(input_path.read_bytes())
    store = EvidenceStore(evidence_path)
    try:
        result = bind_position_action_costs(store, request.cash_window, request.proofs)
        report = {
            "schema": "oasis.lp-action-cost-report.v1",
            **asdict(result),
            "unresolved": [
                "failed_attempts_and_other_account_operations",
                "quote_or_usd_price_valuation",
                "full_cost_and_net_returns",
                "runtime_attestation_and_finality",
            ],
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
        verify(args.input, args.evidence, args.output)
    except Exception:  # noqa: BLE001 - retained inputs may include hostile content
        parser.exit(3, "Action cost binding refused; no accepted report produced.\n")
    print("Owner-paid exclusive action costs bound; full LP net return remains unresolved.")


if __name__ == "__main__":
    main()
