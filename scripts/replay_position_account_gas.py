"""Offline position marks joined with all direct-account gas; no net PnL or authority."""

import argparse
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Literal

from pydantic import Field

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.farming.position_account_gas import (
    PositionAccountGasRequest,
    replay_position_account_gas,
)
from oasis_agent.launch.models import StrictModel
from oasis_agent.models.common import canonical_json, sha256_hex


class ReplayStore(EvidenceStore):
    """Open existing evidence read-only; never create or migrate historical tables."""

    def __init__(self, path: Path) -> None:
        self._connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)


class Request(StrictModel):
    schema_version: Literal["oasis.position-account-gas-input.v1"] = Field(alias="schema")
    request: PositionAccountGasRequest


def run(input_path: Path, evidence_path: Path, output: Path) -> None:
    if not evidence_path.is_file() or output.exists():
        raise ValueError("existing evidence and exclusive new output required")
    raw = input_path.read_bytes()
    request = Request.from_json(raw)
    store = ReplayStore(evidence_path)
    try:
        result = replay_position_account_gas(store, request.request)
        with output.open("xb") as stream:
            stream.write(
                canonical_json(
                    {
                        "schema": "oasis.position-account-gas-report.v1",
                        "input_sha256": sha256_hex(raw),
                        **asdict(result),
                        "expense_treatment": (
                            "whole direct-account gas once; LP allocation unresolved"
                        ),
                        "missing": [
                            "internal cash and reimbursements",
                            "LP expense allocation and complete account NAV",
                            "other costs, baselines and economic attribution",
                        ],
                        "target_admitted": False,
                    }
                )
                + b"\n"
            )
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
    except Exception:  # noqa: BLE001 - retained hostile bytes never enter errors
        parser.exit(3, "Position/account gas replay refused; no accepted report produced.\n")
    print("Position/account gas joined; LP allocation and full net PnL remain unresolved.")


if __name__ == "__main__":
    main()
