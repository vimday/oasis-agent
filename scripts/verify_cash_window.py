"""Verify a complete retained two-provider LP cash window; no RPC or signing."""

import argparse
from pathlib import Path

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.farming.cash_window import CashWindowRequest, EvidencePair, replay_cash_window
from oasis_agent.launch.models import Address, StrictModel


class Request(StrictModel):
    pool_manager: Address
    opening: EvidencePair
    closing: EvidencePair
    core: EvidencePair
    ownership: EvidencePair
    payments: tuple[EvidencePair, ...]


def verify(request_path: Path, evidence_path: Path, report_path: Path) -> None:
    request = Request.from_json(request_path.read_bytes())
    if not evidence_path.is_file() or report_path.exists():
        raise ValueError("existing evidence and new output required")
    store = EvidenceStore(evidence_path)
    try:
        result = replay_cash_window(
            store,
            CashWindowRequest(
                request.pool_manager,
                request.opening,
                request.closing,
                request.core,
                request.ownership,
                request.payments,
            ),
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
    except Exception:  # noqa: BLE001 - do not expose retained RPC details
        parser.exit(1, "Cash window verification failed; execution remains disabled.\n")
    print("Position cash reconciled; account NAV, all costs and release remain unresolved.")


if __name__ == "__main__":
    main()
