"""Interpret bounded historical receipt captures; never infer a complete borrowing ledger."""

import argparse
import sys
from pathlib import Path
from typing import Any

from oasis_agent.arbitrage import aave_history
from oasis_agent.arbitrage.cycle_evidence import read_bounded
from oasis_agent.evidence.public_receipts import decode_receipt
from oasis_agent.evidence.public_rpc_plan import PublicReceiptPlan
from oasis_agent.evidence.rpc_validation import strict_json_object
from oasis_agent.models.common import BlockRef, sha256_hex

from . import capture_public_rpc
from .research_metric import publish


def replay(plan_path: Path, paths: list[Path]) -> dict[str, Any]:
    if not 1 <= len(paths) <= 16:
        raise ValueError("capture_count_limit")
    raw = read_bounded(plan_path, 32 * 1024)
    strict_json_object(raw)
    plan = aave_history.AaveHistoryPlan.model_validate_json(raw)
    receipts = []
    evidence = []
    incomplete = False
    for path in paths:
        capture_plan, _ = capture_public_rpc.load_plan(path / "plan.json")
        if (
            not isinstance(capture_plan, PublicReceiptPlan)
            or capture_plan.chain_id != plan.chain_id
        ):
            raise ValueError("receipt_capture_chain_or_schema")
        transport = capture_public_rpc.replay(path)
        incomplete |= transport["status"] != "complete"
        evidence.append({k: v for k, v in transport.items() if k != "exports"})
        for row in transport["exports"]["records"]:
            if (
                row["request"]["method"] == "eth_getTransactionReceipt"
                and "result" in row["response"]
            ):
                if transport["block"] is None:
                    raise ValueError("receipt_without_block")
                block = BlockRef(**transport["block"]["ref"])
                receipts.append(
                    decode_receipt(
                        row["response"]["result"],
                        block,
                        row["request"]["params"][0],
                        dict(transport["block"]["receipt_positions"])[row["request"]["params"][0]],
                    )
                )
    report = aave_history.summarize(plan, tuple(receipts))
    report.update(
        status="incomplete_capture" if incomplete or not receipts else "selected_receipts_observed",
        plan_sha256=sha256_hex(raw),
        evidence=evidence,
        implementation_sha256=sha256_hex(Path(aave_history.__file__).read_bytes()),
    )
    if incomplete:
        report["event_flow_totals"] = None
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--capture", action="append", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        report = replay(args.plan, args.capture)
        publish(args.output, report)
        print(f"{report['status']} | selected receipts only | net profit unknown")
        return 0
    except (ValueError, TypeError, OSError):
        print("Aave receipt interpretation refused; no complete ledger claim", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
