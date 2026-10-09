"""Replay retained account proofs into an existing global risk journal; never execute."""

import argparse
import time
from pathlib import Path

from oasis_agent.execution.account_risk_observation import (
    RiskObservationRequest,
    observe_account_risk,
)
from oasis_agent.models.common import canonical_json, sha256_hex
from oasis_agent.risk.account_risk import GlobalRiskJournal
from scripts.replay_account_risk_gas import ReplayStore


def run(input_path: Path, evidence: Path, journal_path: Path, output: Path) -> bool:
    if not evidence.is_file() or not journal_path.is_file() or output.exists():
        raise ValueError("existing evidence/journal and exclusive output required")
    paths = [p.resolve() for p in (input_path, evidence, journal_path, output)]
    if len(set(paths)) != len(paths):
        raise ValueError("input, evidence, risk journal and output must be distinct")
    raw = input_path.read_bytes()
    request = RiskObservationRequest.from_json(raw)
    store = ReplayStore(evidence)
    try:
        store._connection.execute("BEGIN")  # One coherent read-only evidence snapshot.
        journal = GlobalRiskJournal(journal_path, request.manifest.policy, require_existing=True)
        try:
            state, digest = observe_account_risk(store, journal, request, now=int(time.time()))
        finally:
            journal.close()
    finally:
        store.close()
    # Ledger commit precedes report publication. A lost report is recovered by
    # replaying the same input; it never resets risk or applies losses twice.
    with output.open("xb") as stream:
        stream.write(
            canonical_json(
                {
                    "schema": "oasis.risk-observation-report.v1",
                    "input_sha256": sha256_hex(raw),
                    "manifest_sha256": request.manifest.digest,
                    "observation_sha256": sha256_hex(
                        canonical_json(request.model_dump(mode="json", by_alias=True))
                    ),
                    "journal_digest": digest,
                    "state": state.model_dump(mode="json"),
                    "execution_eligible": False,
                    "net_pnl": None,
                    "unresolved": [
                        "native internal cash and external funding purpose coverage",
                        "transaction/envelope binding, finality and LP exit settlement",
                        "live adapter integration, target admission and multi-week OOS",
                    ],
                }
            )
            + b"\n"
        )
    return state.halted


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        halted = run(args.input, args.evidence, args.journal, args.output)
    except Exception:  # noqa: BLE001 - do not echo hostile input or provider data
        parser.exit(3, "Observation/report refused; inspect retained journal before retry.\n")
    if halted:
        parser.exit(3, "Observation persisted; global risk halt remains latched.\n")
    print("Observation persisted; execution remains disabled.")


if __name__ == "__main__":
    main()
