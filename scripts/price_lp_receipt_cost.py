"""Offline quote valuation of one retained transaction's complete native fees."""

import argparse
from dataclasses import asdict
from pathlib import Path
from typing import Literal

from pydantic import Field

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.farming.cost_allocation import allocate_priced_cost
from oasis_agent.farming.price_evidence import UsdFeedPolicy
from oasis_agent.farming.receipt_cost_valuation import price_receipt_cost
from oasis_agent.launch.models import StrictModel
from oasis_agent.models.chain import BlockSnapshot
from oasis_agent.models.common import EvidenceRef, canonical_json, sha256_hex


class Request(StrictModel):
    schema_version: Literal["oasis.receipt-cost-input.v1"] = Field(alias="schema")
    header: BlockSnapshot
    transaction_hash: str
    payer: str
    providers: tuple[str, str]
    costs: tuple[EvidenceRef, EvidenceRef]
    native_prices: tuple[EvidenceRef, EvidenceRef]
    quote_prices: tuple[EvidenceRef, EvidenceRef]
    native_policy: UsdFeedPolicy
    quote_policy: UsdFeedPolicy
    quote_decimals: int
    allocation_weights: tuple[tuple[str, int], ...] | None = None


def run(request_path: Path, evidence: Path, output: Path) -> None:
    if output.exists() or not evidence.is_file():
        raise ValueError("new output and existing retained evidence required")
    raw = request_path.read_bytes()
    request = Request.from_json(raw)
    store = EvidenceStore(evidence)
    try:
        result = price_receipt_cost(
            store,
            **{
                name: getattr(request, name)
                for name in Request.model_fields
                if name not in {"schema_version", "allocation_weights"}
            },
        )
    finally:
        store.close()
    allocation = (
        None
        if request.allocation_weights is None
        else allocate_priced_cost(result, request.allocation_weights)
    )
    payload = canonical_json(
        {
            "schema": "oasis.priced-receipt-cost.v1",
            "input_sha256": sha256_hex(raw),
            "block": asdict(request.header.ref),
            "cost": asdict(result),
            "quote_decimals_source": "declared_input",
            "rounding": "ceiling",
            "allocation": None
            if allocation is None
            else {
                "scope": "declared_bookkeeping_not_observed_position_payment",
                "method": "largest_remainder_per_denomination",
                "items": [asdict(item) for item in allocation],
            },
            "out_of_sample_accepted": False,
            "execution_eligible": False,
        }
    )
    with output.open("xb") as stream:
        stream.write(payload + b"\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        run(args.input, args.evidence, args.output)
    except Exception:  # noqa: BLE001 - do not echo untrusted retained payloads
        parser.exit(3, "Receipt cost valuation refused; no accepted report produced.\n")
    print("Whole transaction cost replayed; LP allocation and economic acceptance unresolved.")


if __name__ == "__main__":
    main()
