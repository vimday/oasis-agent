"""Replay declared inventory frames with complete raw direct-account gas; observation only."""

import argparse
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Literal

from pydantic import Field

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.execution.account_risk_reader import (
    AccountObservation,
    EvidenceReplayAccountRiskReader,
)
from oasis_agent.launch.models import StrictModel
from oasis_agent.models.common import canonical_json, sha256_hex
from oasis_agent.risk.portfolio_manifest import PortfolioManifest


class ReplayStore(EvidenceStore):
    """Read existing evidence without migration or writes."""

    def __init__(self, path: Path) -> None:
        self._connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)


class Request(StrictModel):
    schema_version: Literal["oasis.account-risk-gas-input.v1"] = Field(alias="schema")
    manifest: PortfolioManifest
    opening: dict[str, AccountObservation]
    closing: dict[str, AccountObservation]


def run(input_path: Path, evidence_path: Path, output: Path) -> None:
    if not evidence_path.is_file() or output.exists():
        raise ValueError("existing evidence and exclusive output required")
    raw = input_path.read_bytes()
    request = Request.from_json(raw)
    # This interface accepts no native scalar explanation, including reimbursements.
    # The library's existing operator-declaration path remains explicitly separate.
    if any(
        o.native_gas_evidence is None or o.native_gas_units or o.non_trading_native_units
        for o in request.closing.values()
    ):
        raise ValueError("every closing account requires complete gas evidence and no overrides")
    store = ReplayStore(evidence_path)
    try:
        opening = EvidenceReplayAccountRiskReader(
            store, request.manifest, request.opening
        ).reconcile()
        closing = EvidenceReplayAccountRiskReader(
            store, request.manifest, request.closing, previous=opening
        ).reconcile()
        if any(a.unresolved_operations for a in closing.accounts):
            raise ValueError("unexplained movement refuses the whole frame report")
        with output.open("xb") as stream:
            stream.write(
                canonical_json(
                    {
                        "schema": "oasis.account-risk-gas-report.v1",
                        "input_sha256": sha256_hex(raw),
                        "opening": opening.model_dump(mode="json"),
                        "closing": closing.model_dump(mode="json"),
                        "gas_proofs": {
                            key: asdict(o.native_gas_evidence)
                            for key, o in request.closing.items()
                            if o.native_gas_evidence is not None
                        },
                        "lp_cash_proofs": {
                            key: [asdict(proof) for proof in o.lp_cash]
                            for key, o in request.closing.items()
                            if o.lp_cash
                        },
                        "expense_treatment": (
                            "gas explains observed native debit; no second NAV debit"
                        ),
                        "coverage": "declared manifest inventory; full outer-account gas quorum",
                        "unresolved": [
                            "native internal cash, reimbursements and other unclassified purposes",
                            "LP expense allocation, other costs and fair baselines",
                            "finality, sustained economic/OOS and release acceptance",
                        ],
                        "net_pnl": None,
                        "target_admitted": False,
                        "out_of_sample_accepted": False,
                        "canary_accepted": False,
                        "execution_eligible": False,
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
    except Exception:  # noqa: BLE001 - hostile retained values are never logged
        parser.exit(3, "Account inventory/gas replay refused; no accepted report produced.\n")
    print("Declared account inventory and complete gas reconciled; release remains closed.")


if __name__ == "__main__":
    main()
