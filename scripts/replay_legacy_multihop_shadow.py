"""Recheck the four-pool Base shadow model from retained parsed attribution."""

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from oasis_agent.errors import RpcError
from oasis_agent.evidence.base_denim import BASE_CHAIN_ID
from oasis_agent.evidence.rpc_validation import (
    strict_digest,
    strict_integer,
    strict_json_object,
    strict_list,
    strict_object,
    strict_string,
)
from oasis_agent.fastpath import (
    BlockPoolSnapshot,
    FastpathBridge,
    FastpathPoolGraph,
    PoolKind,
    PoolNode,
    TickLiquidity,
    run_multihop_ab_experiment,
)
from oasis_agent.fastpath import live_shadow_runner as shadow
from oasis_agent.fastpath.bridge import FastpathCallError, FastpathUnavailableError
from oasis_agent.models.common import (
    canonical_json,
    normalize_address,
    normalize_hash32,
    sha256_hex,
)
from oasis_agent.sniping.venues import USDC, WETH

LEGACY_MAX_HOPS: Final[int] = 2


def _decimal(value: object, name: str) -> int:
    raw = strict_string(value, name)
    amount = int(raw)
    if amount <= 0 or str(amount) != raw:
        raise ValueError(f"{name} must be a positive canonical decimal string")
    return amount


def _pool_node(
    spec: shadow.BasePoolSpec, item: dict[str, object], block_number: int, timestamp_ms: int
) -> PoolNode:
    if item.get("pool_id") != spec.address or item.get("kind") != spec.kind.value:
        raise ValueError("legacy pool identity does not match the four-pool run configuration")
    reserve0 = reserve1 = sqrt_price = tick = liquidity = None
    ticks: tuple[TickLiquidity, ...] = ()
    if spec.kind is PoolKind.V2:
        reserve0 = _decimal(item.get("reserve0_weth_wei"), "reserve0_weth_wei")
        reserve1 = _decimal(item.get("reserve1_usdc_raw"), "reserve1_usdc_raw")
    else:
        sqrt_price = _decimal(item.get("sqrt_price_x96"), "sqrt_price_x96")
        tick = strict_integer(item.get("tick"), "tick")
        liquidity = _decimal(item.get("liquidity"), "liquidity")
        ticks = shadow._active_ticks(tick, liquidity)
    return PoolNode(
        chain_id=BASE_CHAIN_ID,
        pool_id=spec.address,
        kind=spec.kind,
        token0=spec.token0,
        token1=spec.token1,
        decimals0=spec.decimals0,
        decimals1=spec.decimals1,
        fee_pips=strict_integer(item.get("fee_pips"), "fee_pips"),
        block_number=block_number,
        timestamp_ms=timestamp_ms,
        reserve0=reserve0,
        reserve1=reserve1,
        sqrt_price_x96=sqrt_price,
        tick=tick,
        liquidity=liquidity,
        ticks=ticks,
    )


def replay_legacy_shadow(directory: Path) -> dict[str, object]:
    """Replay the historical scan model and validate the retained integer results."""
    summary = strict_json_object((directory / "shadow_summary.json").read_bytes())
    summary_sha256 = strict_digest(summary.pop("summary_sha256", None), "summary_sha256")
    if summary_sha256 != sha256_hex(canonical_json(summary)):
        raise ValueError("legacy summary digest mismatch")
    records = [
        strict_json_object(line)
        for line in (directory / "shadow_attribution.jsonl").read_bytes().splitlines()
    ]
    if (
        shadow.verify_attribution_chain(records) != summary.get("chain_head_sha256")
        or summary.get("ticks_completed") != len(records)
        or summary.get("execution_eligible") is not False
        or summary.get("chain_id") != BASE_CHAIN_ID
    ):
        raise ValueError("legacy summary does not match the Base attribution chain")
    mode = strict_string(summary.get("mode"), "mode")
    if mode not in {"fixture", "public-shadow"}:
        raise ValueError("unsupported legacy attribution mode")
    weth, usdc = normalize_address(WETH), normalize_address(USDC)
    specs = shadow.canonical_base_weth_usdc_pools()
    bridge = FastpathBridge()
    snapshots: list[BlockPoolSnapshot] = []
    blocks: list[dict[str, object]] = []
    previous_block: int | None = None
    for row in records:
        if row.get("mode") != mode or row.get("chain_id") != BASE_CHAIN_ID:
            raise ValueError("legacy attribution contains a conflicting chain or mode")
        number = strict_integer(row.get("block_number"), "block_number")
        block_hash = normalize_hash32(strict_string(row.get("block_hash"), "block_hash"))
        timestamp_ms = strict_integer(row.get("base_time_timestamp_ms"), "base_time_timestamp_ms")
        for field in (
            "header_request_sha256",
            "header_response_sha256",
            "batch_request_sha256",
            "batch_response_sha256",
        ):
            strict_digest(row.get(field), field)
        if previous_block is not None and number <= previous_block:
            raise ValueError("legacy attribution heights must strictly increase")
        pools = tuple(
            _pool_node(spec, strict_object(item, "pool"), number, timestamp_ms)
            for spec, item in zip(specs, strict_list(row.get("pools"), "pools"), strict=True)
        )
        l2 = strict_integer(row.get("l2_gas_cost_wei"), "l2_gas_cost_wei")
        l1 = strict_integer(row.get("l1_data_fee_wei"), "l1_data_fee_wei")
        graph = FastpathPoolGraph(bridge=bridge)
        for pool in pools:
            graph.upsert_pool(pool)
        measured = shadow._measure_pairwise_quotes(
            graph, specs, pools, weth=weth, usdc=usdc, l2_gas_cost_wei=l2, l1_data_fee_wei=l1
        )
        fields = (
            "pairwise_quotes_evaluated",
            "pairwise_quote_failures",
            "best_gross_delta_wei",
            "best_net_delta_wei",
            "best_pair",
        )
        if canonical_json(measured) != canonical_json(tuple(row.get(field) for field in fields)):
            raise ValueError(f"legacy pairwise model differs at Base block {number}")
        report = graph.scan_cycles(
            [weth],
            max_hops=LEGACY_MAX_HOPS,
            min_amount_in=shadow.SCAN_MIN_AMOUNT_IN_WEI,
            max_amount_in=shadow.SCAN_MAX_AMOUNT_IN_WEI,
            min_net_profit=1,
            gas_cost_in_base=l2,
            l1_fee_in_base=l1,
        )
        counts = (report.cycles_evaluated, len(report.candidates))
        if counts != (row.get("cycles_evaluated"), row.get("candidates_found")):
            raise ValueError(f"legacy cycle model differs at Base block {number}")
        snapshots.append(
            BlockPoolSnapshot(
                block_number=number,
                timestamp_ms=timestamp_ms,
                pools=pools,
                base_tokens=(weth,),
                gas_cost_in_base=l2,
                l1_fee_in_base=l1,
                # Legacy attribution has no parent hash. Never reuse across samples.
                force_full_scan=previous_block is not None,
            )
        )
        blocks.append(
            {
                "block_number": number,
                "block_hash": block_hash,
                "pool_count": len(pools),
                "cycles_evaluated": report.cycles_evaluated,
                "candidates_found": len(report.candidates),
                **dict(zip(fields, measured, strict=True)),
            }
        )
        previous_block = number
    expected_range = (summary.get("first_block_number"), summary.get("last_block_number"))
    if (blocks[0]["block_number"], blocks[-1]["block_number"]) != expected_range:
        raise ValueError("legacy summary block range disagrees with attribution")
    ab = run_multihop_ab_experiment(
        snapshots,
        max_hops=LEGACY_MAX_HOPS,
        min_amount_in=shadow.SCAN_MIN_AMOUNT_IN_WEI,
        max_amount_in=shadow.SCAN_MAX_AMOUNT_IN_WEI,
        min_net_profit=1,
        bridge=bridge,
        deduplicate_cycles=False,
    )
    return {
        "schema": "oasis.base-legacy-multihop-model-replay.v1",
        "chain_id": BASE_CHAIN_ID,
        "mode": mode,
        "base_token": weth,
        "source_scope": "retained_parsed_attribution",
        "summary_sha256": summary_sha256,
        "chain_head_sha256": summary["chain_head_sha256"],
        "cl_state": "synthetic_plus_minus_200_tick_bracket",
        "whole_state_evidence": "unavailable",
        "missing_evidence": [
            "original_header_and_batch_rpc_bytes",
            "parent_hash_and_state_root",
            "provider_receive_clock",
            "initialized_tick_pages",
            "exact_transaction_simulation_and_settlement",
        ],
        "execution_eligible": False,
        "release_permitted": False,
        "blocks": blocks,
        "ab": {
            "exact_blocks": ab.exact_bit_for_bit_blocks,
            "total_blocks": ab.total_blocks,
            "candidate_recall": ab.recall,
            "arm_b_full_scans": ab.arm_b.full_scan_blocks,
            "hypothesis_a_passed": ab.hypothesis_a_passed,
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="Directory with legacy shadow artifacts.")
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        evidence = replay_legacy_shadow(args.directory)
    except (
        OSError,
        ValueError,
        TypeError,
        RpcError,
        FastpathCallError,
        FastpathUnavailableError,
    ) as exc:
        print(f"legacy multi-hop replay failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(evidence, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
