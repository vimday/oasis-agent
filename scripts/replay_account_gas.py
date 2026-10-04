"""Offline direct-account gas coverage and USD costing; no LP allocation or authority."""

import argparse
from dataclasses import asdict
from pathlib import Path
from typing import Literal

from pydantic import Field

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.farming.account_gas_coverage import AccountGasRequest, replay_account_gas
from oasis_agent.launch.models import StrictModel
from oasis_agent.models.common import canonical_json, sha256_hex


class Request(StrictModel):
    schema_version: Literal["oasis.account-gas-input.v1"] = Field(alias="schema")
    request: AccountGasRequest


def run(input_path: Path, evidence_path: Path, output: Path) -> None:
    if not evidence_path.is_file() or output.exists():
        raise ValueError("existing evidence and exclusive new report required")
    raw = input_path.read_bytes()
    request = Request.from_json(raw)
    store = EvidenceStore(evidence_path)
    try:
        result = replay_account_gas(store, request.request)
        with output.open("xb") as stream:
            stream.write(
                canonical_json(
                    {
                        "schema": "oasis.account-gas-report.v1",
                        "input_sha256": sha256_hex(raw),
                        **asdict(result),
                        "coverage_basis": (
                            "two-provider complete RPC block enumerations, not trie proofs"
                        ),
                        "missing": [
                            "internal/AA reimbursements",
                            "LP attribution",
                            "other costs and NAV",
                        ],
                        "target_admitted": False,
                        "oos_accepted": False,
                        "canary_accepted": False,
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
    except Exception:  # noqa: BLE001 - hostile retained bytes never enter error logs
        parser.exit(3, "Account gas replay refused; no accepted report produced.\n")
    print("Direct-account gas replayed; full net PnL and release remain unresolved.")


if __name__ == "__main__":
    main()
