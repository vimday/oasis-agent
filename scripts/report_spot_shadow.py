"""Evaluate a shadow spot run: signal quality, paper book and operations (read-only).

Optional, offline: `--cost-binding BINDING.json --evidence EVIDENCE.db` adds a
`cost_replay` view for ONE journal-matched order, rebuilt from two retained provider
proofs of its receipt (see oasis_agent.launch.spot_cost_binding). The evidence store is
opened read-only; without both flags the report is exactly the default report.
With them it also adds `receipt_transfers`: the wallet's USDC and order-token Transfer
nets in those same receipts compared with the journal fill (see spot_transfer_check).
"""

import argparse
import json
import sys
from pathlib import Path

from oasis_agent.evidence.store import ReadOnlyEvidenceStore
from oasis_agent.launch.spot_cost_binding import bind_order_cost
from oasis_agent.launch.spot_report import read_journal, report_from_snapshot
from oasis_agent.launch.spot_transfer_check import check_order_transfers


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True, help="run directory of run_spot_agent")
    parser.add_argument("--cost-binding", type=Path, help="declared binding for one order")
    parser.add_argument("--evidence", type=Path, help="existing evidence store (read-only)")
    parser.add_argument(
        "--require-receipt-match",
        action="store_true",
        help="exit 2 unless both retained receipts corroborate the historical journal fill",
    )
    args = parser.parse_args()
    if (args.cost_binding is None) != (args.evidence is None):
        parser.error("--cost-binding and --evidence must be given together")
    if args.require_receipt_match and args.cost_binding is None:
        parser.error("--require-receipt-match needs --cost-binding and --evidence")
    try:
        # ONE verified read of the journal: the base report and the optional cost
        # replay both derive from this snapshot, so a writer appending after it can
        # never make the two halves of one output describe different journals.
        policy, records = read_journal(args.run / "journal.db")
        report = report_from_snapshot(policy, records)
        if args.cost_binding is not None:
            if not args.evidence.is_file():
                raise OSError("evidence store does not exist")
            binding = json.loads(args.cost_binding.read_bytes())
            store = ReadOnlyEvidenceStore(args.evidence)
            try:
                report["cost_replay"] = bind_order_cost(records, binding, store)
                # Same snapshot, same retained receipts: the wallet's USDC/token
                # Transfer nets in this receipt versus the journal fill.
                report["receipt_transfers"] = check_order_transfers(records, binding, store)
            finally:
                store.close()
    except (OSError, ValueError, KeyError) as error:
        print(f"spot shadow report refused: {type(error).__name__}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    if args.require_receipt_match and report["receipt_transfers"]["status"] != "matched":
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
