"""Offline action-time owner gas valuation; never produces full LP net returns."""

import argparse
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.farming.cash_window import CashWindowRequest
from oasis_agent.farming.position_action_costs import ActionCostProof
from oasis_agent.farming.position_cost_valuation import (
    ActionPriceProof,
    quote_position_action_costs,
)
from oasis_agent.farming.price_evidence import UsdFeedPolicy
from oasis_agent.launch.models import StrictModel
from oasis_agent.models.common import canonical_json, sha256_hex


class Request(StrictModel):
    schema_version: Literal["oasis.position-action-price-input.v1"] = Field(alias="schema")
    cash_window: CashWindowRequest
    costs: Annotated[tuple[ActionCostProof, ...], Field(min_length=1, max_length=10_000)]
    prices: Annotated[tuple[ActionPriceProof, ...], Field(min_length=1, max_length=10_000)]
    native_policy: UsdFeedPolicy
    quote_policy: UsdFeedPolicy


def run(input_path: Path, evidence_path: Path, output: Path) -> None:
    if not evidence_path.is_file() or output.exists():
        raise ValueError("existing retained evidence and new output required")
    raw = input_path.read_bytes()
    request = Request.from_json(raw)
    store = EvidenceStore(evidence_path)
    try:
        result = quote_position_action_costs(
            store,
            request.cash_window,
            request.costs,
            request.prices,
            native_policy=request.native_policy,
            quote_policy=request.quote_policy,
        )
        report = {
            "schema": "oasis.position-action-price-report.v1",
            "input_sha256": sha256_hex(raw),
            **asdict(result),
            "quote_cost_by_owner": result.quote_cost_by_owner,
            "rounding": "ceiling per whole attributed action transaction",
            "out_of_sample_accepted": False,
            "canary_accepted": False,
            "unresolved": [
                "failed_attempts_and_other_account_operations",
                "sponsored_or_shared_transaction_gas",
                "full_costed_lp_share_returns_and_baselines",
                "token_feed_deployment_safety_and_finality",
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
        run(args.input, args.evidence, args.output)
    except Exception:  # noqa: BLE001 - hostile retained data must not enter error logs
        parser.exit(3, "Action-time gas valuation refused; no accepted report produced.\n")
    print("Attributed action gas priced; full net return and execution remain unresolved.")


if __name__ == "__main__":
    main()
