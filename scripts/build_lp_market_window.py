"""Replay retained two-source v4 activity/price evidence into lagged market inputs."""

import argparse
from pathlib import Path
from typing import Annotated

from pydantic import Field

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.launch.models import Address, StrictModel
from oasis_agent.models.common import EvidenceRef
from oasis_agent.models.v4 import PoolKey
from oasis_agent.shadow.market_window import build_market_window


class Request(StrictModel):
    swap_references: tuple[EvidenceRef, EvidenceRef]
    price_references: tuple[EvidenceRef, EvidenceRef]
    pool: PoolKey
    pool_manager: Address
    mature_prefix: bool = False
    horizon_seconds: Annotated[int, Field(strict=True, ge=1, le=86400)]
    max_lateness_seconds: Annotated[int, Field(strict=True, ge=1, le=86400)]


def build(request_path: Path, evidence_path: Path, report_path: Path) -> None:
    request = Request.from_json(request_path.read_bytes())
    if not evidence_path.is_file() or report_path.exists():
        raise ValueError("existing evidence and new report path required")
    store = EvidenceStore(evidence_path)
    try:
        result = build_market_window(
            store,
            swap_references=request.swap_references,
            price_references=request.price_references,
            pool=request.pool,
            pool_manager=request.pool_manager,
            horizon_seconds=request.horizon_seconds,
            max_lateness_seconds=request.max_lateness_seconds,
            mature_prefix=request.mature_prefix,
        )
        with report_path.open("xb") as output:
            output.write(result.payload + b"\n")
    finally:
        store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    try:
        build(args.request, args.evidence, args.report)
    except Exception:  # noqa: BLE001 - avoid printing hostile retained input strings
        parser.exit(1, "Market-window replay failed; no execution eligibility granted.\n")
    print("Market inputs retained; inventory, reference and economic gates remain unresolved.")


if __name__ == "__main__":
    main()
