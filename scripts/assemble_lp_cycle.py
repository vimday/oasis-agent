"""Assemble one evidence-bound LP shadow cycle and append it to a JSONL stream.

Run from the repository root as ``python -m scripts.assemble_lp_cycle``.

``run_lp_shadow.py`` consumes ``LpCycle`` records and nothing in this repository
produced one from live evidence, so this is the operator's producer step: it
rebuilds the market window from retained two-provider captures, derives the
declared position's decision-block inventory from its own retained position
proofs, derives the decision-block reference price from both pool currencies'
retained USD feed observations, binds the operator's economic declaration, and
appends exactly one canonical cycle line.

Everything that is not a declaration is re-read from the retained evidence store
before the cycle is written, and nothing is written when any check refuses. This
tool holds no key, reserves no capital and offers no signing or broadcast path:
``execution_eligible`` is never asserted, and an appended cycle is an input to a
shadow decision, not permission to trade.
"""

import argparse
from pathlib import Path
from typing import Annotated

from pydantic import Field

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.execution.lp_cycle_assembly import build_lp_cycle, parse_lp_cycle_declaration
from oasis_agent.execution.lp_position_inventory import agreed_position_inventory
from oasis_agent.execution.lp_reference_price import (
    ReferenceRequest,
    agreed_reference_price,
    currency_feed,
    reference_deviation,
)
from oasis_agent.launch.models import Address, StrictModel
from oasis_agent.models.common import EvidenceRef
from oasis_agent.models.position_scope import LpPositionScope
from oasis_agent.models.v4 import PoolKey
from oasis_agent.shadow.market_window import build_market_window
from oasis_agent.shadow.service import serialize_cycle


class WindowRequest(StrictModel):
    """The same replay request ``build_lp_market_window.py`` accepts."""

    swap_references: tuple[EvidenceRef, EvidenceRef]
    price_references: tuple[EvidenceRef, EvidenceRef]
    pool: PoolKey
    pool_manager: Address
    mature_prefix: bool = False
    horizon_seconds: Annotated[int, Field(strict=True, ge=1, le=86400)]
    max_lateness_seconds: Annotated[int, Field(strict=True, ge=1, le=86400)]


class PositionRequest(StrictModel):
    """The declared position whose own inventory the cycle must carry."""

    scope: LpPositionScope
    references: tuple[EvidenceRef, EvidenceRef]


class AssemblyRequest(StrictModel):
    """Retained evidence the cycle is rebuilt from, plus the two declarations."""

    window: WindowRequest
    position: PositionRequest
    reference: ReferenceRequest
    retrieved_at: str | None = None


def assemble(
    request_path: Path, declaration_path: Path, evidence_path: Path, output_path: Path
) -> None:
    request = AssemblyRequest.from_json(request_path.read_bytes())
    declaration = parse_lp_cycle_declaration(declaration_path.read_bytes())
    if not evidence_path.is_file():
        raise ValueError("an existing evidence database is required")
    store = EvidenceStore(evidence_path)
    try:
        window = build_market_window(
            store,
            swap_references=request.window.swap_references,
            price_references=request.window.price_references,
            pool=request.window.pool,
            pool_manager=request.window.pool_manager,
            horizon_seconds=request.window.horizon_seconds,
            max_lateness_seconds=request.window.max_lateness_seconds,
            mature_prefix=request.window.mature_prefix,
        )
        inventory0, inventory1 = agreed_position_inventory(
            store, window, request.position.references, scope=request.position.scope
        )
        reference = agreed_reference_price(
            store,
            window,
            pool=request.reference.pool,
            currency0=currency_feed(window.chain_id, request.reference.currency0),
            currency1=currency_feed(window.chain_id, request.reference.currency1),
            providers=request.reference.providers,
            max_deviation=reference_deviation(request.reference.max_reference_deviation),
        )
        assembled = build_lp_cycle(
            store,
            declaration,
            window=window,
            slot0_references=request.window.price_references,
            inventory0=inventory0,
            inventory1=inventory1,
            reference_price_x192=reference,
            position_scope=request.position.scope,
            retrieved_at=request.retrieved_at,
        )
        line = serialize_cycle(assembled.cycle)
    finally:
        store.close()
    with output_path.open("ab") as stream:
        stream.write(line + b"\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--declaration", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        assemble(args.request, args.declaration, args.evidence, args.output)
    except Exception:  # noqa: BLE001 - avoid printing hostile retained input strings
        parser.exit(1, "Cycle assembly failed; no execution eligibility granted.\n")
    print("LP cycle appended; the shadow decision remains observe-only.")


if __name__ == "__main__":
    main()
