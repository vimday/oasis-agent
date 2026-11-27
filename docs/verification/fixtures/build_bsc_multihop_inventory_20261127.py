"""Assemble the historical BSC inventory from retained JSON, receipts and cited case reports."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
VERIFICATION = "docs/verification/"
REPORT = "reports/research/"
WBNB = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"
USDT = "0x55d398326f99059ff775485246999027b3197955"
HASH = re.compile(r"0x[0-9a-f]{64}")
LEVEL = {
    "indexed_swap_shapes": 0,
    "receipt_swap_shapes": 1,
    "rooted_receipt_swap_shapes": 2,
    "receipt_funding": 3,
    "trace_funding": 4,
}
SAMPLES: dict[str, dict[str, Any]] = {}
COHORTS: list[dict[str, Any]] = []
INPUTS: dict[str, str] = {}


def source(path: str) -> str:
    if path not in INPUTS:
        with (ROOT / path).open("rb") as stream:
            INPUTS[path] = hashlib.file_digest(stream, "sha256").hexdigest()
    return path


def read(path: str) -> Any:
    return json.loads((ROOT / source(path)).read_bytes())


def docs(name: str) -> str:
    return source(f"{VERIFICATION}{name}.md")


def num(value: Any) -> int:
    return int(value, 16) if isinstance(value, str) and value.startswith("0x") else int(value)


def amount(value: Any) -> str:
    return str(num(value))


def row(
    tx: str,
    level: str,
    references: list[str],
    *,
    shape: str = "unresolved",
    block: int | None = None,
    index: int | None = None,
    block_hash: str | None = None,
    status: int | None = 1,
    swaps: int | None = None,
    depth: int | None = None,
    **fields: Any,
) -> None:
    if not HASH.fullmatch(tx):
        raise ValueError(f"invalid transaction hash {tx}")
    entry = SAMPLES.setdefault(
        tx,
        {
            "tx_hash": tx,
            "shape": "unresolved",
            "evidence_level": level,
            "all_cost_profit": None,
            "sources": [],
            "swap_observations": [],
        },
    )
    for key, value in (
        ("block", block),
        ("tx_index", index),
        ("block_hash", block_hash),
        ("receipt_status", status),
    ):
        if value is not None and entry.setdefault(key, value) != value:
            raise ValueError(f"conflicting {key} for {tx}")
    for path in references:
        source(path)
        if path not in entry["sources"]:
            entry["sources"].append(path)
    if swaps is not None:
        observation = {"count": swaps, "evidence_level": level, "source": references[0]}
        if observation not in entry["swap_observations"]:
            entry["swap_observations"].append(observation)
    if LEVEL[level] >= LEVEL[entry["evidence_level"]]:
        entry["evidence_level"] = level
        if swaps is not None:
            entry["swaps"] = swaps
    if shape != "unresolved":
        if entry["shape"] not in {"unresolved", shape}:
            raise ValueError(f"conflicting shapes for {tx}")
        entry["shape"] = shape
    if depth is not None:
        entry["max_proven_funded_pool_swaps"] = max(
            depth, entry.get("max_proven_funded_pool_swaps", 0)
        )
    for key, value in fields.items():
        if entry.setdefault(key, value) != value:
            raise ValueError(f"conflicting {key} for {tx}")


def cohort(ident: str, kind: str, index: str, selector: str, **fields: Any) -> None:
    COHORTS.append(
        {"id": ident, "kind": kind, "index": source(index), "selector": selector, **fields}
    )


def receipt_cost(gas: Any, price: Any) -> dict[str, Any]:
    return {
        "gas_used": num(gas),
        "effective_gas_price_wei": amount(price),
        "receipt_gas_bnb_wei": str(num(gas) * num(price)),
    }


def gross(*assets: tuple[str, Any]) -> dict[str, str]:
    return {token: amount(value) for token, value in assets}


def public_and_recent() -> None:
    discovery = source("docs/strategies/MULTICHAIN_MULTIHOP_DISCOVERY.md")
    explorer = f"{REPORT}multichain-discovery-20261122/eth-explorer/"
    log_path = f"{explorer}binance-mainnet.ndjson"
    block_data = read(log_path)
    tx = next(tx for tx in block_data["transactions"] if tx["transactionIndex"] == 4)
    row(
        tx["hash"],
        "trace_funding",
        [discovery, log_path, f"{explorer}bsc-trace-identity.response"],
        shape="serial",
        block=block_data["header"]["number"],
        block_hash=block_data["header"]["hash"],
        index=tx["transactionIndex"],
        status=num(tx["status"]),
        swaps=4,
        depth=4,
        closed_cycle_depths=[4],
        logical_pools=4,
        gross_raw_by_asset=gross((WBNB, 23_888_063_753_725)),
        observed_external_native_bnb_wei="23888063753724",
        funding_note=(
            "Four distinct pools. Whole gross unwraps and is routed out. Recipient "
            "ownership remains unknown."
        ),
        **receipt_cost(tx["gasUsed"], tx["gasPrice"]),
    )
    replay = f"{VERIFICATION}MULTICHAIN_DISCOVERY_20261122.json"
    residual = next(
        candidate
        for candidate in read(replay)["historical_sqd_replay"]["positive_wbnb_residual_candidates"]
        if candidate["transaction"]["hash"] != tx["hash"]
    )
    other = residual["transaction"]
    row(
        other["hash"],
        "indexed_swap_shapes",
        [replay, log_path, discovery],
        shape="control",
        block=block_data["header"]["number"],
        block_hash=block_data["header"]["hash"],
        index=other["transactionIndex"],
        status=num(other["status"]),
        swaps=len(residual["legs"]),
        visible_wbnb_transfer_balance_wei=amount(residual["wbnb_gross_wei"]),
        funding_note="One wei WBNB transfer residual in a nonclosing observed route.",
        **receipt_cost(other["gasUsed"], other["gasPrice"]),
    )
    cohort(
        "public_one_block",
        "indexed_history",
        log_path,
        (
            "Single block: distinct logs[].transactionIndex and documented Swap ABI + WBNB "
            "transfer filter"
        ),
        first=61_000_000,
        last=61_000_000,
        blocks=1,
        all_logs=len(block_data["logs"]),
        emitting_transactions=len({log["transactionIndex"] for log in block_data["logs"]}),
        individually_reviewed=2,
        individually_attributed=1,
    )

    file = f"{REPORT}bsc-recent-arbitrage-20261123/analysis.json"
    data = read(file)
    refs = [file, docs("RECENT_DEX_ARBITRAGE_20261123")]
    location = {"block": num(data["block"]["number"]), "block_hash": data["block"]["hash"]}
    first = data["findings"]
    row(
        first["tx"],
        "receipt_funding",
        refs,
        shape="serial",
        **location,
        index=first["tx_index"],
        swaps=len(first["raw_route"]),
        depth=4,
        closed_cycle_depths=[4],
        logical_pools=4,
        gross_raw_by_asset=gross((WBNB, first["gross_wbnb_raw"])),
        funding_note=(
            "WBNB payout and unwrap are visible. The follow-on native recipient is unknown."
        ),
        **receipt_cost(first["gas_used"], first["effective_gas_price_wei"]),
    )
    second = data["second_example"]
    row(
        second["tx"],
        "receipt_funding",
        refs,
        shape="mixed",
        **location,
        index=second["index"],
        swaps=second["recognized_swaps"],
        depth=4,
        closed_cycle_depths=[second["ring_legs"]],
        gross_raw_by_asset=gross((USDT, second["ring_return_raw"] - second["ring_input_raw"])),
        cashout_wbnb_wei=amount(second["converted_profit_wbnb_raw"]),
        receipt_gas_bnb_wei=amount(second["execution_fee_wei"]),
        funding_note="Three USDT swaps form the closed route. The fourth is the cashout.",
    )
    cohort(
        "five_recent_finalized_blocks",
        "receipt_screen",
        file,
        "$.preselected_receipts, $.findings, $.second_example",
        blocks=5,
        receipts=data["preselected_receipts"],
        individually_attributed=2,
    )


def indexed_tails() -> None:
    base = f"{REPORT}long-dex-20261123/"
    report = docs("LONG_SWAP_TAIL_20261123")
    for prefix in ("", "expanded/", "executor-lookback/"):
        path = f"{base}{prefix}binance-mainnet/result.json"
        result = read(path)
        through = result["verified_to"]
        hist = result["hist"]
        cohort(
            f"long_dex_{prefix.rstrip('/') or 'initial'}",
            "indexed_history",
            path,
            "$.hist and $.top (ranked by Swap shapes, no funding implication)",
            raw_index=source(f"{base}{prefix}binance-mainnet/response.ndjson"),
            first=result["from_block"],
            last=through,
            requested_last=result["to_block"],
            blocks=through - result["from_block"] + 1,
            matching_transactions=result["total_transactions"],
            matching_events=result["total_logs"],
            status=result["status"],
            address_filter=result["addresses_scope"],
            counts_ge={
                str(n): sum(v for k, v in hist.items() if int(k) >= n)
                for n in (5, 10, 20, 50, 100, 200)
            },
        )
        if prefix == "executor-lookback/":
            continue
        picks = {record["tx"]: record for record in result["top"]}
        file = f"{base}{prefix}binance-mainnet/receipts/result.json"
        for tx in read(file)["verified"]:
            selected = picks[tx["tx"]]
            row(
                tx["tx"],
                "receipt_swap_shapes",
                [file, path, report],
                block=selected["block"],
                index=selected["index"],
                block_hash=selected["block_hash"],
                swaps=selected["count"],
                **receipt_cost(tx["gas"], tx["price"]),
            )

    data = f"{base}binance-mainnet/closed-branch-proof.json"
    proof = read(data)
    pretrade = docs("MULTIHOP_PRETRADE_REPLAY_20261123")
    row(
        "0xa2c11e2c90a5d5fca43a6da0e349d5daf133bf1b654425adf3496fde020caf88",
        "trace_funding",
        [data, pretrade, report],
        shape="mixed",
        swaps=37,
        depth=6,
        closed_cycle_depths=[5],
        gross_raw_by_asset=gross((USDT, proof["core_profit_usdt_raw"])),
        cashout_wbnb_wei=amount(proof["converted_wbnb"]),
        observed_external_native_bnb_wei="357703882794830",
        funding_note=(
            "Five linked USDT swaps plus a separate WBNB cashout. Other branches remain "
            "outside the proof."
        ),
    )
    tail = f"{REPORT}pretrade-tail-20261123/"
    earlier_receipt_path = f"{tail}bsc-prefix-official/1.res"
    earlier = read(earlier_receipt_path)["result"]
    row(
        earlier["transactionHash"],
        "receipt_swap_shapes",
        [earlier_receipt_path, f"{tail}bsc-prefix-blast/1.res", pretrade],
        shape="control",
        block=num(earlier["blockNumber"]),
        index=num(earlier["transactionIndex"]),
        block_hash=earlier["blockHash"],
        status=num(earlier["status"]),
        funding_note=(
            "Verified prior landed transaction. two swaps touched pools in the later 5+1 witness."
        ),
        **receipt_cost(earlier["gasUsed"], earlier["effectiveGasPrice"]),
    )

    cohort(
        "pretrade_long_index",
        "indexed_history",
        f"{tail}bsc-1/response.ndjson",
        "Complete NDJSON lines only before the 48 MiB response cap; aggregate by transaction index",
        first=135_106_680,
        last=135_110_757,
        blocks=4_078,
        matching_transactions=84_223,
        counts_ge={"5": 1376, "10": 213, "20": 58, "50": 7, "100": 0},
        stop="response byte cap; two other BSC windows stopped at HTTP 529 and add no coverage",
    )
    file = f"{REPORT}bsc-alternate-long-tail-20261124/retained-verified.json"
    data = read(file)
    alt = docs("BSC_ALTERNATE_LONG_TAIL_20261124")
    for i, candidate in enumerate(data["receipt_only_controls"], 1):
        receipt_file = f"{tail}bsc-tail-{i}-official/receipt.res"
        receipt = read(receipt_file)["result"]
        if receipt["transactionHash"] != candidate["tx"]:
            raise ValueError(f"tail receipt does not match candidate {i}")
        witnesses = candidate.get("receipt_only_direct_witnesses", [])
        extra = (
            {
                "closed_cycle_depths": [w["legs"] for w in witnesses],
                "gross_raw_by_asset": gross((WBNB, sum(w["gross_wbnb_wei"] for w in witnesses))),
                "funding_note": (
                    "Two directly funded WBNB closures. The remainder and native payments "
                    "are unresolved."
                ),
            }
            if witnesses
            else {}
        )
        row(
            candidate["tx"],
            "receipt_funding" if witnesses else "receipt_swap_shapes",
            [file, receipt_file, pretrade, alt],
            shape="mixed" if witnesses else "unresolved",
            block=candidate["block"],
            index=candidate["tx_index"],
            block_hash=receipt["blockHash"],
            status=num(receipt["status"]),
            swaps=candidate["six_topic_event_count"],
            depth=max((w["legs"] for w in witnesses), default=None),
            **receipt_cost(receipt["gasUsed"], receipt["effectiveGasPrice"]),
            **extra,
        )

    row(
        data["tx"],
        "trace_funding",
        [file, alt],
        shape="batch",
        block=data["height"],
        index=data["index"],
        block_hash=data["block_hash"],
        swaps=data["swaps"],
        depth=4,
        closed_cycle_depths=[4] * len(data["groups"]),
        logical_pools=data["distinct_pools"],
        gross_raw_by_asset=gross(*data["token_net"].items()),
        receipt_gas_bnb_wei=amount(data["gas"]),
        observed_external_native_bnb_wei=amount(data["native_payment"][-1]),
        visible_bnb_component_after_costs_wei=amount(data["after_fees_bnb_wei"]),
        conditional_marked_net_bnb_wei=amount(
            data["same_transaction_forward_execution_mark"]["marked_net_bnb_wei"]
        ),
        funding_note=(
            "19 WBNB cycles and one USDT cycle. The separate BNB mark uses "
            "same-transaction observed conversion."
        ),
    )

    file = f"{REPORT}multichain-long-route-samples-20261123/bsc-verified.json"
    data = read(file)
    row(
        data["tx"],
        "trace_funding",
        [file, docs("MULTICHAIN_LONG_ROUTE_SAMPLES_20261123")],
        shape="control",
        block=data["block"],
        index=data["index"],
        block_hash=data["block_hash"],
        swaps=data["matching_swaps"],
        depth=max(map(int, data["route_swap_histogram"])),
        logical_pools=data["pool_count_matched"],
        funding_note=(
            "Fourteen branches fund a cross-asset output. No same-asset round trip or "
            "executable valuation was established."
        ),
        **receipt_cost(data["gas_used"], data["gas_price"]),
    )


def ordered_batches() -> None:
    file = f"{REPORT}bsc-batched-closures-20261123/verified.json"
    data = read(file)
    report = docs("BSC_BATCHED_CLOSURES_20261124")
    for batch in data["transactions"]:
        row(
            batch["tx"],
            "trace_funding",
            [file, report],
            shape="batch",
            block=batch["block"],
            index=batch["tx_index"],
            block_hash=batch["block_hash"],
            swaps=batch["swap_count"],
            depth=4,
            closed_cycle_depths=[4] * batch["route_count"],
            logical_pools=batch["logical_pool_count"],
            gross_raw_by_asset=gross(*batch["cycle_gross_by_asset"].items()),
            observed_external_native_bnb_wei=amount(batch["direct_native_payment"][-1]),
            visible_bnb_component_after_costs_wei=amount(
                batch["visible_payer_and_exec_wbnb_bnb_net_wei"]
            ),
            **receipt_cost(batch["gas_used"], batch["gas_price"]),
        )
        for side in batch["same_block_route_pool_overlap"].values():
            for other in side:
                row(
                    other["tx_hash"],
                    "rooted_receipt_swap_shapes",
                    [file, report],
                    block=batch["block"],
                    index=other["tx_index"],
                    block_hash=batch["block_hash"],
                    overlapping_pool_swaps=len(other["swap_logs"]),
                )

    report = docs("BSC_ORDERED_BATCH_MARGINALS_20261124")
    file = f"{REPORT}bsc-ordered-marginals-20261124/replayed.json"
    for compare in read(file)["older_receipt_cohort"]:
        for swaps, _, tx in compare["top_all_swaps"]:
            row(
                tx,
                "rooted_receipt_swap_shapes",
                [file, report],
                block=compare["height"],
                swaps=swaps,
            )
    cohort(
        "ordered_batch_other_receipts",
        "rooted_receipt_screen",
        file,
        "$.older_receipt_cohort[*].{height,counts,top_all_swaps}",
        blocks=2,
        receipts=236,
        focal_cases_excluded=2,
        funding_bound=(
            "Receipt amount links were screened. Only traced evidence proves 4+3 on the candidate."
        ),
    )

    file = f"{REPORT}bsc-ordered-marginals-20261124/candidate-triport-trace/verified.json"
    data = read(file)
    depths = [route["hops"] for route in data["routes"]]
    row(
        data["tx"],
        "trace_funding",
        [file, report],
        shape="batch",
        block=data["height"],
        index=data["index"],
        block_hash=data["block_hash"],
        swaps=sum(depths),
        depth=max(depths),
        closed_cycle_depths=depths,
        gross_raw_by_asset=gross((WBNB, data["gross_wbnb_wei"])),
        observed_external_native_bnb_wei=amount(data["direct_native_payment"][-1]),
        visible_bnb_component_after_costs_wei=amount(data["visible_wbnb_bnb_wei_after_both_costs"]),
        **receipt_cost(data["gas_used"], data["gas_price_wei"]),
    )


def format_proof(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "block": data["height"],
        "index": data["index"],
        "block_hash": data["block_hash"],
        "swaps": data["seen_swaps"],
        "logical_pools": data["logical_pools"],
        "observed_external_native_bnb_wei": str(
            sum(payment[-1] for payment in data["direct_third_party_native_payment_candidates"])
        ),
        "visible_bnb_component_after_costs_wei": amount(
            data["visible_anchor_pnl_after_exec_costs"]
        ),
        "visible_other_erc20_delta_raw": gross(*data["nonanchor_visible_deltas"].items()),
        **receipt_cost(data["gas_used"], data["effective_gas_price"]),
    }


def restricted_holdouts() -> None:
    file = f"{REPORT}bsc-66-swap-forensics-20261124/verified.json"
    data = read(file)[0]
    pages_doc = docs("BSC_PAGED_LONG_TAIL_20261124")
    row(
        data["tx"],
        "trace_funding",
        [file, pages_doc],
        shape="batch",
        depth=4,
        closed_cycle_depths=[3] * 3 + [4] * 20,
        gross_raw_by_asset=gross((WBNB, 295_812_089_777_379)),
        funding_note=(
            "One four-pool group returns native BNB and wraps it. StableSwap ABI adds 23 "
            "confirmed swaps to the earlier 66."
        ),
        **format_proof(data),
    )
    file = f"{REPORT}bsc-eight-address-holdout-20261124/verified.json"
    first, second = read(file)
    row(
        first["tx"],
        "trace_funding",
        [file, pages_doc],
        shape="batch",
        depth=5,
        closed_cycle_depths=[4, 4, 4, 5, 5],
        gross_raw_by_asset=gross((WBNB, 133_195_618_384_523)),
        funding_note=(
            "Twelve distinct swap subtrees and five fully linked WBNB cycles. The trace "
            "proves the aggregate payout. Other links remain open."
        ),
        **format_proof(first),
    )
    row(
        second["tx"],
        "trace_funding",
        [file, pages_doc],
        depth=2,
        funding_note=(
            "Three same-asset principals were fully returned. Two two-swap links are "
            "proven. A closed arbitrage route or valuation is unresolved."
        ),
        **format_proof(second),
    )
    cohort(
        "eight_address_holdout",
        "restricted_log_screen",
        f"{REPORT}bsc-eight-address-holdout-20261124/ranked.json",
        "Ranked restricted API log pages by matched Swap shape count and logical pools",
        first=135_377_727,
        last=135_378_110,
        blocks=384,
        blocks_with_matches=365,
        matching_events=4000,
        matching_transactions=2900,
        counts_ge={"10": 16, "20": 2, "50": 0, "100": 0, "200": 0},
        independently_traced_in_original_scope=2,
    )
    cohort(
        "twenty_address_first_stop",
        "stopped",
        f"{REPORT}bsc-paged-serial-holdout-20261124/STOP.json",
        "$.error, $.stage",
        requested_first=135_378_111,
        requested_last=135_378_494,
        blocks=0,
        stop="HTTP 403 / -32602 on page zero",
    )

    file = f"{REPORT}bsc-four-pool-tail-20261124/verified.json"
    data = read(file)
    four_doc = docs("BSC_FOUR_POOL_TAIL_20261124")
    row(
        data["tx"],
        "trace_funding",
        [file, four_doc],
        shape="batch",
        block=data["block_height"],
        index=data["index"],
        block_hash=data["block_hash"],
        swaps=len(data["legs"]),
        logical_pools=data["logical_pool_count"],
        visible_other_erc20_delta_raw=gross(*data["erc20_net"].items()),
        repaid_principal_raw_by_asset=gross(
            *((token, principal) for _, token, principal, _ in data["lending_repayments"])
        ),
        observed_external_native_bnb_wei="41080884987228",
        visible_bnb_component_after_costs_wei="0",
        funding_note=(
            "Four principals were repaid in a multi-asset financing batch. No swap "
            "output uniquely funds a later swap input under the inspected model."
        ),
        **receipt_cost(data["gas_used"], data["fee_price"]),
    )
    cohort(
        "four_pool_holdout",
        "restricted_log_screen",
        f"{REPORT}bsc-four-pool-tail-20261124/ranked.json",
        "$.{first,last,matching_transactions,matching_pool_events,bins,selected}",
        first=135_308_800,
        last=135_308_991,
        blocks=192,
        blocks_with_matches=170,
        matching_events=476,
        matching_transactions=459,
        counts_ge={"10": 0},
        independently_traced=1,
    )

    file = f"{REPORT}bsc-alternate-long-tail-20261124/single-pool-followup/verified.json"
    data = read(file)
    alt = docs("BSC_ALTERNATE_LONG_TAIL_20261124")
    row(
        data["tx"],
        "trace_funding",
        [file, alt],
        shape="control",
        block=data["block_height"],
        index=data["index"],
        block_hash=data["block_hash"],
        swaps=len(data["legs"]),
        depth=max(len(path["swaps"]) for path in data["paths_with_direct_evidence"]),
        logical_pools=data["logical_pool_count"],
        lp_token_id=data["mint"]["token_id"],
        lp_assets_raw=gross(*zip(data["mint"]["assets"], data["mint"]["amounts"], strict=True)),
        funding_note=(
            "LP mint receives the routed assets. The LP position and refund lack an "
            "execution-equivalent realized valuation."
        ),
        **receipt_cost(data["gas_used"], data["gas_price"]),
    )
    cohort(
        "single_pool_holdout",
        "restricted_log_screen",
        f"{REPORT}bsc-alternate-long-tail-20261124/single-pool-followup/ranked.json",
        "$.{first,last,matching_transactions,matching_pool_events,selected}",
        first=135_306_822,
        last=135_306_869,
        blocks=48,
        blocks_with_matches=14,
        matching_events=27,
        matching_transactions=18,
        independently_traced=1,
    )
    cohort(
        "single_pool_first_stop",
        "stopped",
        alt,
        "Independent 96-block window rejected",
        requested_first=135_306_726,
        requested_last=135_306_821,
        blocks=0,
        stop="HTTP 403 / -32701",
    )


def rooted_serial_screen() -> None:
    screen = f"{REPORT}bsc-robinhood-serial-search-20261127/screen.json"
    report = docs("BSC_ROBINHOOD_SERIAL_SEARCH_20261127")
    data = next(record for record in read(screen) if record["chain"] == 56)
    rooted_heights = {
        block["height"] for block in data["covered_blocks"] if block["header_hash_recomputed"]
    }
    for case in data["ranking"]:
        row(
            case["tx"],
            "rooted_receipt_swap_shapes"
            if case["height"] in rooted_heights
            else "receipt_swap_shapes",
            [screen, report],
            block=case["height"],
            index=case["index"],
            block_hash=case["block_hash"],
            status=case["status"],
            swaps=case["event_count"],
            **receipt_cost(case["gas_used"], case["effective_gas_price"]),
        )
    cohort(
        "mixed_historical_rooted_recheck",
        "mixed_receipt_screen",
        screen,
        "$[?(@.chain==56)].{covered_blocks,receipts,statuses,ranking,threshold_counts}",
        blocks=len(data["covered_blocks"]),
        blocks_with_verified_header_and_receipt_root=len(rooted_heights),
        receipts=data["receipts"],
        receipt_statuses=data["statuses"],
        counts_ge=data["threshold_counts"],
        additional_shape_counts_ge=data["new_threshold_counts"],
    )

    file = f"{REPORT}multichain-batch-control-20261127/scope.json"
    pick = read(file)["case"]
    controls = docs("MULTICHAIN_BATCH_CONTROL_20261127")
    row(
        pick["tx"],
        "indexed_swap_shapes",
        [file, controls],
        block=pick["height"],
        index=pick["index"],
        block_hash=pick["hash"],
        status=None,
        swaps=pick["matches"],
        funding_note=(
            "20 shapes in the restricted historical index. The first independent receipt "
            "request returned an empty list and stopped the scope."
        ),
    )
    file = f"{REPORT}multichain-batch-control-20261127/a578-trace/verified.json"
    data = read(file)
    extras = f"{REPORT}bsc-robinhood-serial-search-20261127/verified.json"
    algebra = read(extras)["candidate"]
    row(
        data["tx"],
        "trace_funding",
        [file, controls, extras],
        shape="batch",
        depth=4,
        closed_cycle_depths=[4, 3, 3, 3, 3, 3],
        supplemental_swap_shapes=algebra["event_count"] - data["seen_swaps"],
        funding_note=(
            "Five USDT closures and a three-swap WBNB-to-BNB path with a wrap. Two "
            "additional Algebra-shaped swaps are separate from the trace-proven count."
        ),
        **format_proof(data),
    )


def recent_windows() -> None:
    cases_path = f"{VERIFICATION}BSC_FROZEN_WINDOW_20261127.json"
    cases = read(cases_path)
    report = docs("BSC_FROZEN_WINDOW_20261127")
    ranking_path = f"{REPORT}bsc-window-audits-20261127/cohort/ranking.json"
    ranking = read(ranking_path)
    by_hash = {candidate["tx"]: candidate for candidate in ranking["candidates"]}
    for candidate in by_hash.values():
        row(
            candidate["tx"],
            "rooted_receipt_swap_shapes",
            [ranking_path, report],
            block=candidate["block"],
            index=candidate["index"],
            block_hash=candidate["hash"],
            swaps=candidate["events"],
        )
    for case in cases["landed"]["cases"]:
        candidate = by_hash[case["tx"]]
        depths = [len(route["swap_logs"]) for route in case["routes"]]
        row(
            case["tx"],
            "trace_funding",
            [cases_path, report, ranking_path],
            shape="batch",
            block=candidate["block"],
            index=case["index"],
            block_hash=case["block_hash"],
            swaps=case["events"],
            depth=max(depths),
            closed_cycle_depths=depths,
            gross_raw_by_asset=gross((WBNB, case["gross_wbnb_wei"])),
            visible_bnb_component_after_costs_wei=amount(case["visible_sender_executor_delta_wei"]),
            known_fee_on_transfer=bool(case["split_proofs"]),
            funding_note=(
                "The ring payouts reconcile for the measured roles. Beneficiary control "
                "and off-chain costs are unresolved."
            ),
            **receipt_cost(case["gas_used"], case["signed_price_wei"]),
        )
    cohort(
        "forty_block_frozen_landed",
        "rooted_receipt_screen",
        ranking_path,
        "$.totals, $.hist, $.candidates, $.selected",
        **ranking["totals"],
        counts_ge=cases["landed"]["candidate_thresholds"],
        independently_traced=2,
    )
    cohort(
        "frozen_115_pool_shadow",
        "forward_shadow",
        cases_path,
        "$.prospective, including the simulation-only control",
        pools=cases["prospective"]["pools"],
    )

    expanded_path = f"{VERIFICATION}BSC_HISTORICAL_POOL_EXPANSION_20261127.json"
    data = read(expanded_path)
    expanded_report = docs("BSC_HISTORICAL_POOL_EXPANSION_20261127")
    heads = data["forward"]["scored_heads"]
    seen = set()
    for head in heads:
        for candidate in head["receipt_observations"]["overlap_transactions"]:
            seen.add(candidate["tx"])
            row(
                candidate["tx"],
                "rooted_receipt_swap_shapes",
                [expanded_path, expanded_report],
                block=head["height"],
                index=candidate["i"],
                block_hash=head["hash"],
                swaps=candidate["event_shapes"],
                watch_pool_additional_events=candidate["added_events"],
                watch_pool_baseline_events=candidate["known_baseline_events"],
                effective_gas_price_wei=amount(candidate["actual_gas_price_wei"]),
            )
    for case in data["landed"]:
        verified = case["verified"]
        tax = data["landed_tax_reconciliation"]
        fields: dict[str, Any] = {}
        if tax["tx"] == verified["tx"]:
            depths = [len(path["swaps"]) for path in case["wbnb_closed"]] + [len(tax["swaps"])]
            shape = "batch"
            fields = {
                "depth": max(depths),
                "closed_cycle_depths": depths,
                "gross_raw_by_asset": gross((WBNB, tax["gross_all_four_closed_wbnb_wei"])),
                "known_fee_on_transfer": True,
                "funding_note": (
                    "Four WBNB closures. The additional four-leg closure proves a "
                    "scope-specific 3% token transfer deduction through V3→V2 evidence."
                ),
            }
        else:
            shape = "control"
            fields = {
                "depth": max((int(d) for d in verified["summary_paths"]), default=None),
                "funding_note": (
                    "Six open cross-asset funding paths to USDT. The input asset lacks an "
                    "execution-equivalent valuation."
                ),
            }
        row(
            verified["tx"],
            "trace_funding",
            [expanded_path, expanded_report],
            shape=shape,
            **fields,
            **format_proof(verified),
        )
    cohort(
        "expanded_138_pool_landed",
        "rooted_receipt_screen",
        expanded_path,
        (
            "$.forward.scored_heads[*].receipt_observations.{all_receipts,overlap_transactions}. "
            "$.landed"
        ),
        first=heads[0]["height"],
        last=heads[-1]["height"],
        blocks=len(heads),
        receipts=sum(head["receipt_observations"]["all_receipts"] for head in heads),
        unique_pool_overlap_transactions=len(seen),
        independently_traced=2,
    )
    cohort(
        "expanded_138_pool_shadow",
        "forward_shadow",
        expanded_path,
        "$.forward.scored_heads and $.whole_control (read-only simulated H/K/C)",
        blocks=len(heads),
        pools=138,
        baseline_pools=115,
        simulation_only=True,
    )


def state_only_windows() -> None:
    for ident, path, selector in (
        (
            "initial_multi_chain_snapshot",
            f"{VERIFICATION}MULTICHAIN_DISCOVERY_20261122.json",
            "BSC state-only row",
        ),
        (
            "twenty_head_triangle",
            f"{REPORT}multichain-forward-20261122/bsc/summary.json",
            "$.{block_count,economic_denominator,first_block,last_block,witness}",
        ),
        (
            "depth_snapshot",
            f"{REPORT}multichain-depth-20261123/bsc/result.json",
            "$.{head,index_pair_count,pools,pin_rechecked}; offline path checks have no receipts",
        ),
        (
            "wide_v2_window",
            f"{REPORT}bsc-wide-forward-20261123/summary.json",
            "$.{scored,pools,directional_routes,actual_receipts_collected,revert}",
        ),
        (
            "long_forward_window",
            f"{REPORT}bsc-long-forward-20261123/verified.json",
            "$.{outputs,received,source}",
        ),
        (
            "ordered_v2_batch_replay",
            docs("BSC_V2_BATCH_COMPARISON_20261123"),
            "Historical reserve snapshot and synthetic same-capital ordered evaluation",
        ),
    ):
        cohort(ident, "shadow_or_state_only", path, selector)


def main() -> None:
    public_and_recent()
    indexed_tails()
    ordered_batches()
    restricted_holdouts()
    rooted_serial_screen()
    recent_windows()
    state_only_windows()
    samples = sorted(
        SAMPLES.values(), key=lambda row: (row["block"], row.get("tx_index", -1), row["tx_hash"])
    )
    summary = {
        "distinct_individually_listed_landed_transactions": len(samples),
        "by_shape": Counter(item["shape"] for item in samples),
        "by_evidence_level": Counter(item["evidence_level"] for item in samples),
        "maximum_proven_funded_pool_swaps_in_one_path": max(
            row.get("max_proven_funded_pool_swaps", 0) for row in samples
        ),
        "maximum_proven_closed_cycle_swaps": max(
            max(row.get("closed_cycle_depths", [0])) for row in samples
        ),
        "full_owner_pnl_measured": sum(row["all_cost_profit"] is not None for row in samples),
    }
    print(
        json.dumps(
            {
                "chain_id": 56,
                "chain": "bsc",
                "schema": "oasis-bsc-multihop-sample-inventory-v1",
                "semantics": {
                    "serial": "A single funded serial cycle is established for the swaps observed.",
                    "batch": (
                        "Several paths or closed subgroups coexist, or the receipt and trace "
                        "prove multi-asset borrowing and repayment around the swaps."
                    ),
                    "mixed": (
                        "One or more closes coexist with a cashout or an unresolved remainder."
                    ),
                    "unresolved": "Landed/indexed logs exist, but organization remains unclear.",
                    "control": (
                        "A landed comparison, LP operation, or otherwise established nonclosure."
                    ),
                    "swaps": (
                        "Count from the strongest available source. It may be only shapes. "
                        "See swap_observations and evidence_level."
                    ),
                    "max_proven_funded_pool_swaps": (
                        "Largest evidenced linked path in this record. A cashout can "
                        "extend this beyond a closed cycle. Zero and absence have "
                        "different meanings."
                    ),
                    "costs": (
                        "Amounts are raw decimal integers keyed by asset or BNB wei. A "
                        "scoped visible zero does not establish owner PnL. Receipt gas is "
                        "actual inclusive charged gas only where recorded."
                    ),
                    "denominators": (
                        "Cohorts overlap and apply different filters. They cannot be "
                        "added. Absence from the individually listed samples conveys no "
                        "assessment."
                    ),
                    "rebuild": (
                        "python "
                        "docs/verification/fixtures/build_bsc_multihop_inventory_20261127.py "
                        "from a checkout with the listed retained evidence."
                    ),
                },
                "summary": summary,
                "samples": samples,
                "cohorts": COHORTS,
                "input_sha256": dict(sorted(INPUTS.items())),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
