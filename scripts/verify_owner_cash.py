"""Reconstruct two-source owner cash from a retained evidence database, without RPC."""

import argparse
from pathlib import Path

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.farming.owner_quorum import replay_owner_cash_quorum
from oasis_agent.farming.position_ledger import PositionLedgerEntry
from oasis_agent.launch.models import Address, StrictModel
from oasis_agent.models.chain import BlockSnapshot
from oasis_agent.models.common import EvidenceRef
from oasis_agent.models.v4 import PoolKey


class Request(StrictModel):
    references: tuple[EvidenceRef, EvidenceRef]
    header: BlockSnapshot
    pool_manager: Address
    pool_key: PoolKey
    entry: PositionLedgerEntry
    owner: Address


def verify(request_path: Path, evidence_path: Path, report_path: Path) -> None:
    request = Request.from_json(request_path.read_bytes())
    if not evidence_path.is_file() or report_path.exists():
        raise ValueError("existing evidence and new output required")
    store = EvidenceStore(evidence_path)
    try:
        result = replay_owner_cash_quorum(
            store,
            request.references,
            header=request.header,
            pool_manager=request.pool_manager,
            pool_key=request.pool_key,
            entry=request.entry,
            owner=request.owner,
        )
        with report_path.open("xb") as handle:
            handle.write(result.payload + b"\n")
    finally:
        store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    try:
        verify(args.request, args.evidence, args.report)
    except Exception:  # noqa: BLE001 - retained RPC diagnostics may contain credentials
        parser.exit(1, "Owner cash verification failed; execution remains disabled.\n")
    print("Two-source cash reconstructed; finality, token risk and full-cost PnL remain required.")


if __name__ == "__main__":
    main()
