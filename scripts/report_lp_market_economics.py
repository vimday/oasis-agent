"""Replay retained public-RPC market evidence into pool-flow research diagnostics.

Signed pool markout includes the observed core swap deltas; it is not a fee-only
estimate, LVR decomposition, LP-share attribution or account net profit.
"""

import argparse
from dataclasses import asdict
from pathlib import Path

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.models.common import canonical_json, sha256_hex
from oasis_agent.models.v4 import SwapDirection
from oasis_agent.shadow.market_window import MarketWindow, build_market_window
from scripts.build_lp_market_window import Request

Q192 = 1 << 192


def summarize(window: MarketWindow) -> dict[str, object]:
    """Keep exact token1-base-unit rational sums, including negative markouts.

    pool_markout1 = pool_delta0 * future_pool_price + pool_delta1.
    Each negative event contributes to adverse loss before aggregation so a
    profitable opposite-direction event cannot hide the adverse-flow burden.
    Pending and deferred events are explicitly excluded, never filled with zero.
    """
    directions = []
    for direction in SwapDirection:
        events = [item for item in window.matured if item.value.direction is direction]
        numerators = [
            item.value.amount0 * item.value.future_price_x192.value + item.value.amount1 * Q192
            for item in events
        ]
        directions.append(
            {
                "direction": direction.value,
                "matured_swaps": len(events),
                "gross_token1_leg_units": sum(abs(item.value.amount1) for item in events),
                "pool_markout1_numerator": sum(numerators),
                "adverse_markout1_numerator": sum(max(-value, 0) for value in numerators),
                "favorable_markout1_numerator": sum(max(value, 0) for value in numerators),
                "loss_event_count": sum(value < 0 for value in numerators),
            }
        )
    return {
        "schema": "oasis.lp-market-economics.v1",
        "chain_id": window.chain_id,
        "pool_id": window.pool_id,
        "decision_block": asdict(window.decision_block),
        "market_window_hash": sha256_hex(window.payload),
        "input_references": [asdict(ref) for ref in window.input_references],
        "horizon_seconds": window.horizon_seconds,
        "max_lateness_seconds": window.max_lateness_seconds,
        "swap_count": window.swap_count,
        "matured_count": len(window.matured),
        "pending_count": window.pending_count,
        "deferred_count": window.deferred_count,
        "markouts_complete": window.markouts_complete,
        "rational_denominator": Q192,
        "valuation_unit": "token1 base units; future pool price, not independent USD valuation",
        "directions": directions,
        "limitations": [
            "pool core-flow diagnostic; not an LP account return or fee-only estimate",
            "pending and deferred swaps excluded from markout sums, explicitly counted",
            "no liquidity share, fee decomposition, gas, rebalance or failed execution costs",
            "no independent reference valuation, routing elasticity or out-of-sample acceptance",
        ],
        "out_of_sample_accepted": False,
        "execution_eligible": False,
    }


def report(request_path: Path, evidence_path: Path, output: Path) -> dict[str, object]:
    if not evidence_path.is_file() or output.exists():
        raise ValueError("existing evidence and new report path required")
    request = Request.from_json(request_path.read_bytes())
    store = EvidenceStore(evidence_path)
    try:
        window = build_market_window(
            store,
            swap_references=request.swap_references,
            price_references=request.price_references,
            pool=request.pool,
            pool_manager=request.pool_manager,
            horizon_seconds=request.horizon_seconds,
            max_lateness_seconds=request.max_lateness_seconds,
            mature_prefix=request.mature_prefix,
        )
        result = summarize(window)
        result["pool"] = asdict(request.pool)
        with output.open("xb") as stream:
            stream.write(canonical_json(result) + b"\n")
        return result
    finally:
        store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        report(args.request, args.evidence, args.output)
    except Exception:  # noqa: BLE001 - never echo hostile RPC or retained input
        parser.exit(2, "Market economics replay refused; no report produced.\n")
    print("Pool-flow diagnostics retained; no profitability or execution acceptance.")


if __name__ == "__main__":
    main()
