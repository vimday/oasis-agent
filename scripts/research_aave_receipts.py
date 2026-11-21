"""Interpret bounded historical receipt captures; never infer a complete borrowing ledger."""

import argparse
import sys
from pathlib import Path
from typing import Any

from oasis_agent.arbitrage import aave_cashflows, aave_history, aave_scaled
from oasis_agent.arbitrage.cycle_evidence import read_bounded
from oasis_agent.evidence.public_receipts import decode_receipt
from oasis_agent.evidence.public_rpc_plan import PublicReadPlan, PublicReceiptPlan
from oasis_agent.evidence.rpc_validation import strict_json_object
from oasis_agent.models.common import BlockRef, canonical_json, sha256_hex

from . import capture_public_rpc
from .research_metric import publish


def boundary(
    directory: Path,
    plan: aave_history.AaveHistoryPlan,
    bindings: aave_cashflows.Plan,
    *,
    number: int,
    block_hash: str,
) -> dict[str, Any]:
    context, _ = capture_public_rpc.load_plan(directory / "plan.json")
    if (
        not isinstance(context, PublicReadPlan)
        or context.chain_id != plan.chain_id
        or context.block_number != number
        or context.block_hash != block_hash
    ):
        raise ValueError("position_boundary_plan_mismatch")
    report = capture_public_rpc.replay(directory)
    if report["status"] != "complete":
        raise ValueError("position_boundary_incomplete")
    calls = {}
    for row in report["exports"]["records"]:
        req = row["request"]
        if req["method"] == "eth_call":
            call = req["params"][0]
            if "from" in call:
                raise ValueError("position_boundary_sender_override")
            calls[call["to"], call["data"]] = row["response"]["result"]
    return {
        "block": report["block"]["ref"],
        "source": context.source,
        "capture_sha256": report["capture_sha256"],
        **aave_cashflows.position_snapshot(plan, bindings, calls),
    }


def replay(
    plan_path: Path,
    paths: list[Path],
    cashflow_path: Path | None = None,
    boundaries: tuple[Path, Path] | None = None,
    scaled: bool = False,
) -> dict[str, Any]:
    if not 1 <= len(paths) <= 64:
        raise ValueError("capture_count_limit")
    if scaled and (cashflow_path is None or boundaries is None):
        raise ValueError("scaled_replay_requires_cashflows_and_boundaries")
    raw = read_bounded(plan_path, 32 * 1024)
    strict_json_object(raw)
    plan = aave_history.AaveHistoryPlan.model_validate_json(raw)
    bindings, bindings_digest = None, None
    if cashflow_path is not None:
        binding_raw = read_bounded(cashflow_path, 32 * 1024)
        strict_json_object(binding_raw)
        bindings = aave_cashflows.Plan.model_validate_json(binding_raw)
        bindings_digest = sha256_hex(binding_raw)
    receipts = []
    before_hashes: dict[int, str] = {}
    evidence = []
    incomplete = False
    export_bytes = 0
    for path in paths:
        capture_plan, _ = capture_public_rpc.load_plan(path / "plan.json")
        if (
            not isinstance(capture_plan, PublicReceiptPlan)
            or capture_plan.chain_id != plan.chain_id
        ):
            raise ValueError("receipt_capture_chain_or_schema")
        transport = capture_public_rpc.replay(path)
        export_bytes += len(canonical_json(transport["exports"]))
        if export_bytes > 32 * 1024 * 1024:
            raise ValueError("receipt_cohort_response_limit")
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
                previous = transport["block"]["parent_hash"]
                if before_hashes.setdefault(block.number, previous) != previous:
                    raise ValueError("receipt_cohort_parent_conflict")
                receipts.append(
                    decode_receipt(
                        row["response"]["result"],
                        block,
                        row["request"]["params"][0],
                        dict(transport["block"]["receipt_positions"])[row["request"]["params"][0]],
                    )
                )
    ordered = tuple(sorted(receipts, key=lambda r: (r.block.number, r.transaction_index)))
    refs: dict[int, BlockRef] = {}
    positions = set()
    for receipt in ordered:
        number = receipt.block.number
        position = (number, receipt.transaction_index)
        if refs.setdefault(number, receipt.block) != receipt.block or position in positions:
            raise ValueError("receipt_cohort_block_or_transaction_conflict")
        positions.add(position)
    report = aave_history.summarize(plan, ordered)
    incomplete |= not receipts
    report.update(
        status="incomplete_capture" if incomplete else "selected_receipts_observed",
        plan_sha256=sha256_hex(raw),
        evidence=evidence,
        implementation_sha256=sha256_hex(Path(aave_history.__file__).read_bytes()),
    )
    if incomplete:
        report["event_flow_totals"] = None
    if bindings is not None:
        report["cashflow_plan_sha256"] = bindings_digest
        report["cashflow_implementation_sha256"] = sha256_hex(
            Path(aave_cashflows.__file__).read_bytes()
        )
        report["underlying_cashflows"] = (
            aave_cashflows.reconcile(plan, bindings, ordered) if not incomplete else None
        )
    if boundaries is not None:
        if bindings is None or incomplete:
            raise ValueError("position_boundaries_require_complete_receipt_inputs_and_bindings")
        first, last = ordered[0].block, ordered[-1].block
        opening = boundary(
            boundaries[0],
            plan,
            bindings,
            number=first.number - 1,
            block_hash=before_hashes[first.number],
        )
        closing = boundary(
            boundaries[1], plan, bindings, number=last.number, block_hash=last.block_hash
        )
        report["position_boundaries"] = {
            "opening": opening,
            "closing": closing,
            "evidence_class": "pinned_view_call_observations",
            "independently_reconciled": False,
        }
        if scaled:
            flows = report["underlying_cashflows"]
            if any(row["status"] != "matched" for row in flows["matches"]):
                raise ValueError("scaled_replay_requires_matched_underlying")
            scaled_balances = [
                {
                    row["token"]: row["scaled_balance_raw"]
                    for row in endpoint["reserves"]
                    if row["scaled_balance_raw"] is not None
                }
                for endpoint in (opening, closing)
            ]
            report["scaled_positions"] = aave_scaled.replay(
                plan, bindings, ordered, scaled_balances[0], scaled_balances[1]
            )
            report["scaled_implementation_sha256"] = sha256_hex(
                Path(aave_scaled.__file__).read_bytes()
            )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--capture", action="append", required=True, type=Path)
    parser.add_argument("--cashflows", type=Path)
    parser.add_argument("--boundaries", type=Path, nargs=2, metavar=("START", "END"))
    parser.add_argument("--scaled", action="store_true")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        boundaries = (args.boundaries[0], args.boundaries[1]) if args.boundaries else None
        report = replay(args.plan, args.capture, args.cashflows, boundaries, args.scaled)
        publish(args.output, report)
        print(f"{report['status']} | selected receipts only | net profit unknown")
        if args.scaled and report["scaled_positions"]["status"] == "residual":
            return 3
        return 0
    except ValueError, TypeError, OSError:
        print("Aave receipt interpretation refused; no complete ledger claim", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
