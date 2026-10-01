"""Replay raw market inputs and audit Phase 1 dataset coverage without evaluation."""

import argparse
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.farming.lp_dataset_coverage import assess_dataset_coverage
from oasis_agent.launch.models import StrictModel
from oasis_agent.shadow.market_window import build_market_window
from scripts.build_lp_market_window import Request


class WindowInput(StrictModel):
    request: str
    evidence: str


class CoverageInput(StrictModel):
    schema_version: Literal["oasis.lp-coverage-input.v1"] = Field(alias="schema")
    windows: Annotated[tuple[WindowInput, ...], Field(min_length=1, max_length=200_000)]


def check(input_path: Path, output: Path) -> bool:
    if output.exists():
        raise ValueError("coverage output must be a new file")
    declaration = CoverageInput.from_json(input_path.read_bytes())
    windows = []
    for item in declaration.windows:
        request = Request.from_json((input_path.parent / item.request).read_bytes())
        evidence = input_path.parent / item.evidence
        if not evidence.is_file():
            raise ValueError("retained evidence database is required")
        store = EvidenceStore(evidence)
        try:
            windows.append(
                build_market_window(
                    store,
                    swap_references=request.swap_references,
                    price_references=request.price_references,
                    pool=request.pool,
                    pool_manager=request.pool_manager,
                    horizon_seconds=request.horizon_seconds,
                    max_lateness_seconds=request.max_lateness_seconds,
                    mature_prefix=request.mature_prefix,
                )
            )
        finally:
            store.close()
    report = assess_dataset_coverage(tuple(windows))
    with output.open("xb") as stream:
        stream.write(report.payload + b"\n")
    return report.coverage_passed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        passed = check(args.input, args.output)
    except Exception:  # noqa: BLE001 - retained input can contain hostile strings
        parser.exit(2, "Dataset replay refused; no acceptance report produced.\n")
    if not passed:
        parser.exit(3, "Dataset coverage insufficient; diagnostic report retained.\n")
    print("Coverage passed; economic acceptance and execution remain unavailable.")


if __name__ == "__main__":
    main()
