"""Reconcile historical case receipts and a block-end call from exact retained bytes."""

import argparse
import sys
from pathlib import Path

from oasis_agent.arbitrage.cycle_evidence import read_bounded
from oasis_agent.arbitrage.cycle_plan import Plan
from oasis_agent.arbitrage.cycle_replay import replay_case
from oasis_agent.evidence.public_rpc_plan import PublicNitroReceiptPlan
from oasis_agent.evidence.rpc_validation import strict_json_object
from scripts import capture_public_rpc
from scripts.run_cycle_research import _publish


def committed_receipts(path: Path, chain_id: int, block_number: int) -> tuple[object, str]:
    plan, _ = capture_public_rpc.load_plan(path / "plan.json")
    if (
        not isinstance(plan, PublicNitroReceiptPlan)
        or plan.chain_id != chain_id
        or plan.block_number != block_number
    ):
        raise ValueError("expected Nitro block receipt capture")
    result = capture_public_rpc.replay(path)
    if result["status"] != "complete":
        raise ValueError("Nitro receipt capture incomplete")
    match = [
        row["response"]["result"]
        for row in result["exports"]["records"]
        if row["request"]["method"] == "eth_getBlockReceipts"
    ]
    if len(match) != 1:
        raise ValueError("expected exactly one verified Nitro receipt result")
    return match[0], result["capture_sha256"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("plan", "primary", "secondary", "block-end", "output"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--nitro-receipts", type=Path, nargs=2, metavar=("TRIGGER", "WINNER"))
    args = parser.parse_args()
    try:
        raw = read_bounded(args.plan, 128 * 1024)
        strict_json_object(raw)
        plan = Plan.model_validate_json(raw)
        receipt_blocks = None
        if args.nitro_receipts:
            if plan.case is None:
                raise ValueError("Nitro receipt comparison requires a historical case")
            receipt_blocks = (
                committed_receipts(args.nitro_receipts[0], plan.chain_id, plan.case.trigger_block),
                committed_receipts(args.nitro_receipts[1], plan.chain_id, plan.case.winner_block),
            )
        report = replay_case(
            plan, args.primary, args.secondary, args.block_end, receipt_blocks=receipt_blocks
        )
        _publish(args.output, report)
    except (ValueError, TypeError, OSError) as exc:
        print(f"Historical replay refused ({type(exc).__name__})", file=sys.stderr)
        return 2
    print(report["historical_case"]["status"])
    print("Block-end call |", report["block_end_atomic_call"]["executor_return_word_raw"])
    print("Exact winner prestate |", report["exact_winner_prestate"]["status"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
