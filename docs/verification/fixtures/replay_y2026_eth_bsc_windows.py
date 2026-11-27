"""Rebuild the two fresh 2026 windows and one rooted BSC receipt attribution."""

import json
from collections import Counter
from datetime import UTC, datetime
from hashlib import sha256
from itertools import pairwise
from pathlib import Path
from typing import Any

from docs.verification.fixtures.replay_bsc_pool_expansion import WBNB, receipts_summary
from oasis_agent.arbitrage.cycle_protocols import abi, data_bytes, topic
from oasis_agent.evidence.public_receipts import quantity
from oasis_agent.models.common import canonical_json

ROOT = Path(__file__).resolve().parents[3]
RESEARCH = ROOT / "reports/research"
DOCS = ROOT / "docs/verification"
TRANSFER = topic("Transfer(address,address,uint256)")
DEPOSIT = topic("Deposit(address,uint256)")
WITHDRAWAL = topic("Withdrawal(address,uint256)")
V2 = topic("Swap(address,uint256,uint256,uint256,uint256,address)")
V3 = topic("Swap(address,address,int256,int256,uint160,uint128,int24)")
PC3 = topic("Swap(address,address,int256,int256,uint160,uint128,int24,uint128,uint128)")


def read(path: Path) -> Any:
    return json.loads(path.read_bytes())


def digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def rel(path: Path) -> str:
    return str(path.relative_to(ROOT))


def amount(value: int) -> str:
    return str(value)


def receive_utc(wall_ns: int) -> str:
    seconds, nanos = divmod(wall_ns, 10**9)
    return datetime.fromtimestamp(seconds, UTC).isoformat(timespec="seconds")[:-6] + f".{nanos:09}Z"


def dec_address(value: str) -> str:
    word = data_bytes(value)
    if len(word) != 32 or any(word[:12]):
        raise ValueError("noncanonical transfer address")
    return "0x" + word[-20:].hex()


def transfer_rows(receipt: dict) -> list[dict]:
    return [
        {
            "log": quantity(log["logIndex"]),
            "asset": log["address"],
            "from": dec_address(log["topics"][1]),
            "to": dec_address(log["topics"][2]),
            "amount": abi(("uint256",), data_bytes(log["data"]))[0],
        }
        for log in receipt["logs"]
        if len(log["topics"]) == 3 and log["topics"][0] == TRANSFER
    ]


def swap_deltas(log: dict) -> tuple[int, int, bool] | None:
    parsed = log["topics"][0]
    body = data_bytes(log["data"])
    if parsed == V2:
        i0, i1, o0, o1 = abi(("uint256",) * 4, body)
        if i0 and o1 and not (i1 or o0):
            return i0, o1, True
        if i1 and o0 and not (i0 or o1):
            return i1, o0, False
        return None
    words = ("int256", "int256", "uint160", "uint128", "int24")
    if parsed == PC3:
        words += ("uint128", "uint128")
    d0, d1, *_ = abi(words, body)
    if d0 > 0 > d1:
        return d0, -d1, True
    if d1 > 0 > d0:
        return d1, -d0, False
    return None


def receipt_closures(
    receipt: dict, moves: list[dict], named: dict
) -> tuple[list[dict], list[dict]]:
    """Link exact paid quantities at one holder, retaining each committed transfer index."""
    holder = receipt["to"]
    legs = []
    for log in receipt["logs"]:
        topics = log["topics"]
        if (
            len(topics) != 3
            or topics[0] not in (V2, V3, PC3)
            or [dec_address(word) for word in topics[1:]] != [holder, holder]
        ):
            continue
        pool, idx = log["address"], quantity(log["logIndex"])
        delta = swap_deltas(log)
        if delta is None:
            continue
        paid, received, forward = delta
        ins = [t for t in moves if (t["from"], t["to"], t["amount"]) == (holder, pool, paid)]
        outs = [t for t in moves if (t["from"], t["to"], t["amount"]) == (pool, holder, received)]
        if len(ins) != 1 or len(outs) != 1:
            continue
        incoming, outgoing = ins[0], outs[0]
        assets = (incoming["asset"], outgoing["asset"])
        if not (incoming["log"] < idx and outgoing["log"] < idx):
            continue
        if assets[0] == assets[1] or (assets[0] < assets[1]) != forward:
            continue
        tokens = named.get(pool)
        if tokens is not None and tuple(sorted(assets)) != tuple(tokens):
            continue
        legs.append(
            {
                "swap_log": idx,
                "pool": pool,
                "registry_identity": tokens is not None,
                "in": incoming,
                "out": outgoing,
            }
        )
    incoming_uses = Counter(leg["in"]["log"] for leg in legs)
    outgoing_uses = Counter(leg["out"]["log"] for leg in legs)
    legs = [
        leg
        for leg in legs
        if incoming_uses[leg["in"]["log"]] == outgoing_uses[leg["out"]["log"]] == 1
    ]
    linked = {}
    for source in legs:
        out = source["out"]
        targets = []
        for target in legs:
            pay = target["in"]
            if (
                target is source
                or out["asset"] == WBNB
                or (out["asset"], out["amount"]) != (pay["asset"], pay["amount"])
                or out["log"] >= pay["log"]
            ):
                continue
            if any(
                t["asset"] == out["asset"]
                and holder in (t["from"], t["to"])
                and out["log"] < t["log"] < pay["log"]
                for t in moves
            ):
                continue
            targets.append(target)
        if len(targets) == 1:
            linked[source["swap_log"]] = targets[0]
    target_uses = Counter(leg["swap_log"] for leg in linked.values())
    closures = []
    for leg in legs:
        if leg["in"]["asset"] != WBNB:
            continue
        path = [leg]
        while (next_leg := linked.get(path[-1]["swap_log"])) is not None:
            if target_uses[next_leg["swap_log"]] != 1 or next_leg["pool"] in {
                item["pool"] for item in path
            }:
                break
            path.append(next_leg)
            if next_leg["out"]["asset"] == WBNB:
                break
        if len(path) >= 2 and path[-1]["out"]["asset"] == WBNB:
            initial, final = path[0]["in"]["amount"], path[-1]["out"]["amount"]
            closures.append(
                {
                    "swap_logs": [item["swap_log"] for item in path],
                    "funding_transfer_pairs": [
                        [a["out"]["log"], b["in"]["log"]] for a, b in pairwise(path)
                    ],
                    "principal_wbnb_wei": amount(initial),
                    "output_wbnb_wei": amount(final),
                    "gross_wbnb_wei": amount(final - initial),
                    "all_pools_in_frozen_registry": all(item["registry_identity"] for item in path),
                    "legs": path,
                }
            )
    return legs, closures


def decimals(rows: list[dict]) -> list[dict]:
    return [{**t, "amount": amount(t["amount"])} for t in rows]


def summarize_selected(
    receipt: dict, moves: list[dict], legs: list[dict], paths: list[dict]
) -> dict:
    holder = receipt["to"]
    wrapped = []
    for log in receipt["logs"]:
        topics = log["topics"]
        if (
            log["address"] == WBNB
            and len(topics) == 2
            and topics[0] in (DEPOSIT, WITHDRAWAL)
            and dec_address(topics[1]) == holder
        ):
            wrapped.append(
                {
                    "log": quantity(log["logIndex"]),
                    "kind": "deposit" if topics[0] == DEPOSIT else "withdrawal",
                    "raw": amount(abi(("uint256",), data_bytes(log["data"]))[0]),
                }
            )
    transfer_delta = sum(
        row["amount"] * (int(row["to"] == holder) - int(row["from"] == holder))
        for row in moves
        if row["asset"] == WBNB
    )
    wrap_delta = sum(int(row["raw"]) * (1 if row["kind"] == "deposit" else -1) for row in wrapped)
    unmatched_wbnb = [
        row
        for row in moves
        if row["asset"] == WBNB
        and holder in (row["from"], row["to"])
        and row["log"] not in {leg[side]["log"] for leg in legs for side in ("in", "out")}
    ]
    # Grouped anchor differences are a receipt-side reconciliation. Individual
    # native legs and the ownership behind the external destinations stay open.
    for item in paths:
        for leg in item["legs"]:
            leg["in"], leg["out"] = decimals([leg["in"], leg["out"]])
    return {
        "matched_standard_swap_logs": [leg["swap_log"] for leg in legs],
        "receipt_funded_wbnb_paths": paths,
        "wbnb_ordinary_transfer_delta_wei": amount(transfer_delta),
        "wbnb_wrap_net_delta_wei": amount(wrap_delta),
        "visible_wbnb_balance_delta_wei": amount(transfer_delta + wrap_delta),
        "wrap_events": wrapped,
        "holder_wbnb_transfers": decimals(
            [row for row in moves if row["asset"] == WBNB and holder in (row["from"], row["to"])]
        ),
        "wbnb_transfers_outside_matched_standard_swaps": decimals(unmatched_wbnb),
        "sender": receipt["from"],
        "observed_holder": holder,
        "all_cost_owner_pnl": None,
    }


def bsc() -> dict:
    folder = RESEARCH / "bsc-y2026-window-20261127"
    result = read(folder / "result.json")
    plan = read(folder / "scope.json")
    out = {
        "scope_sha256": digest(folder / "scope.json"),
        "result_sha256": digest(folder / "result.json"),
        "stop_sha256": digest(folder / "STOP.json"),
        "original_ledger_sha256": digest(folder / "original.sqlite3"),
        "frozen_utc": plan["utc_frozen"],
        "source": plan["source"],
        "max_http": plan["max_http"],
        "http_attempts": result["requests"],
        "stop": result["stop"],
        "natural_proposals": result.get("natural_proposals"),
        "wire_folder": rel(folder / "wire"),
    }
    if result["stop"] is not None:
        return out
    out["ordinary_gas_price_wei"] = result["ordinary_gas_price_wei"]
    out["control_envelope_sha256"] = result["envelope_sha256"]
    out["baseline"] = result["baseline"]["counts"]
    out["added_only"] = result["additions"]["counts"]
    out["max_baseline_gross_wei"] = amount(result["baseline"]["best_grid"]["gross"])
    out["max_added_gross_wei"] = amount(result["additions"]["best_grid"]["gross"])
    out["phases"] = {}
    for name in "HKC":
        stage, head = result["stages"][name], result[name]
        proof = result["stages"][stage.get("reused_phase", name)]
        out["phases"][name] = {
            "block_number": quantity(head["number"]),
            "block_hash": head["hash"],
            "parent_hash": head["parentHash"],
            "head_receive_monotonic_ns": head["receive"]["mono_ns"],
            "head_receive_utc": receive_utc(head["receive"]["wall_ns"]),
            "head_rpc_sequence": head["rpc_seq"],
            "whole_return_monotonic_ns": proof["meta"]["received"]["mono_ns"],
            "gross_wei": amount(proof["gross_wei"]),
            "output_wei": amount(proof["output_wei"]),
            "gas_only_net_lower_wei": amount(proof["gas_only_net_lower_wei"]),
            "gas_only_net_upper_wei": amount(proof["gas_only_net_upper_wei"]),
            "actual_charged_gas": None,
        }
    earliest = result["H"]["receive"]["mono_ns"]

    def meta(seq: int) -> dict:
        return read(folder / "wire" / f"{seq:02}.meta.json")

    kth = meta(result["K"]["rpc_seq"])
    first = result["stages"]["H"]["meta"]["received"]["mono_ns"]
    out["timing_ns"] = {
        "H_to_state": meta(result["state_rpc"])["received"]["mono_ns"] - earliest,
        "H_to_rank": result["scoring_done_monotonic_ns"] - earliest,
        "H_to_whole_return": first - earliest,
        "H_return_to_K_send": kth["before"]["mono_ns"] - first,
        "H_return_to_K_receive": kth["received"]["mono_ns"] - first,
        "independent_child_timing": None,
    }
    receipts_path = folder / "wire" / f"{result['requests']:02}.response"
    request = read(receipts_path.with_suffix(".request"))
    assert request["method"] == "eth_getBlockReceipts" and request["params"] == [
        result["H"]["hash"]
    ]
    receipts = read(receipts_path)["result"]
    header = read(folder / "wire" / f"{result['H']['rpc_seq']:02}.response")["result"]
    assert header["hash"] == result["H"]["hash"]
    new = read(DOCS / "BSC_HISTORICAL_POOL_EXPANSION_20261127.json")["identity"]["state"]["pools"]
    old = read(RESEARCH / "bsc-wide-forward-20261123/universe.json")
    named = {r["pool"]: (r["token0"], r["token1"]) for r in old} | {
        r["pool_id"]: r["tokens"] for r in new
    }
    extension = {
        "pools": {r["pool_id"] for r in new},
        "managers": {r["contract"] for r in new if r["kind"] in {"infinity_cl", "v4"}},
    }
    topics = set(read(RESEARCH / "bsc-window-audits-20261127/cohort/scope.json")["signatures"])
    coverage = receipts_summary(header, receipts, {r["pool"] for r in old}, extension, topics)
    assert json.loads(canonical_json(coverage)) == result["H_receipts"]
    by_tx = {item["tx"]: item for item in coverage["overlap_transactions"]}
    screened = []
    choices = []
    for rc in receipts:
        swap_logs = [
            quantity(log["logIndex"])
            for log in rc["logs"]
            if log["topics"] and log["topics"][0] in topics
        ]
        if len(swap_logs) < 2 or quantity(rc["status"]) != 1:
            continue
        tx, index = rc["transactionHash"], quantity(rc["transactionIndex"])
        overlap = by_tx.get(tx, {})
        screened.append(
            {
                "tx_hash": tx,
                "transaction_index": index,
                "swap_log_indices": swap_logs,
                "added_pool_events": overlap.get("added_events", 0),
                "baseline_pool_events": overlap.get("known_baseline_events", 0),
                "gas_used": quantity(rc["gasUsed"]),
                "effective_gas_price_wei": amount(quantity(rc["effectiveGasPrice"])),
                "receipt_gas_bnb_wei": amount(
                    quantity(rc["gasUsed"]) * quantity(rc["effectiveGasPrice"])
                ),
            }
        )
        moves = transfer_rows(rc)
        legs, paths = receipt_closures(rc, moves, named)
        rank = (not paths, -overlap.get("added_events", 0), -len(swap_logs), index)
        choices.append((rank, rc, moves, legs, paths))
    winner = min(choices, key=lambda row: row[0]) if choices else None
    previous_files = [
        DOCS / name
        for name in (
            "BSC_MULTIHOP_SAMPLE_INVENTORY_20261127.json",
            "BSC_NEXT_WINDOW_SCREENED_20261127.json",
            "BSC_GOLD_WINDOW_SCREENED_20261127.json",
        )
    ]
    previous = {row["tx_hash"] for path in previous_files for row in read(path)["samples"]}
    previous_bsc = read(RESEARCH / "bsc-next-window-20261127/result.json")
    out["market_receipts"] = {
        "coverage": coverage,
        "raw_response_path": rel(receipts_path),
        "raw_response_sha256": digest(receipts_path),
        "screened": screened,
        "rows_also_in_three_prior_bsc_catalogues": sorted(
            {row["tx_hash"] for row in screened} & previous
        ),
        "prior_block_overlap": sorted(
            {result[name]["hash"] for name in "HKC"}
            & {previous_bsc[name]["hash"] for name in "HKC"}
        ),
        "selected": (
            {
                "tx_hash": winner[1]["transactionHash"],
                "selection_rank": winner[0],
                **summarize_selected(*winner[1:]),
            }
            if winner
            else None
        ),
    }
    return out


def eth() -> dict:
    folder = RESEARCH / "eth-y2026-window-20261127"
    result = read(folder / "result.json")
    plan = read(folder / "scope.json")
    out = {
        "scope_sha256": digest(folder / "scope.json"),
        "result_sha256": digest(folder / "result.json"),
        "stop_sha256": digest(folder / "STOP.json"),
        "original_http_ledger_sha256": digest(folder / "rpc-budget.sqlite3"),
        "original_ws_ledger_sha256": digest(folder / "ethereum-newheads/original-budget.sqlite3"),
        "frozen_utc": receive_utc(plan["locked_ms"] * 10**6),
        "rpc_counts": result["rpc_counts"],
        "witness": result["ethereum_newheads"],
        "stop": result["stop_reason"],
        "wire_folder": rel(folder),
    }
    if result["stop_reason"] is not None:
        return out
    out["control_envelope_sha256"] = result["envelope_sha256"]
    out["candidate_kind"] = result["candidate_kind"]
    out["scan"] = result["ranking"]["grid_counts"]
    out["route_grid_count"] = result["ranking"]["route_grid_count"]
    out["opportunity_summary"] = result["summary"]
    prior_eth = read(RESEARCH / "eth-independent-child-20261127/result.json")
    keys = ("discovery_head", "first_return_head", "direct_child_head")
    out["prior_block_overlap"] = sorted(
        {result[k]["block_hash"] for k in keys} & {prior_eth[k]["block_hash"] for k in keys}
    )
    raw_clock = {
        meta["received_ns"]: receive_utc(meta["wall_ms"] * 10**6)
        for path in sorted(folder.glob("publicnode-*.meta.json"))
        if (meta := read(path))["outcome"] == "ok"
    }
    out["phases"] = {}
    for label, head_name in zip("HKC", keys, strict=True):
        phase, head = result["phases"][label], result[head_name]
        raw_trace = result["trace_fee_audit"].get(phase.get("reused_phase", label))
        out["phases"][label] = {
            "block_number": head["height"],
            "block_hash": head["block_hash"],
            "parent_hash": head["parent_hash"],
            "head_receive_monotonic_ns": head["observed_ns"],
            "head_receive_utc_ms_resolution": raw_clock[head["observed_ns"]],
            "whole_quorum_return_monotonic_ns": max(
                phase[peer]["received_ns"] for peer in ("primary", "secondary")
            ),
            "gross_wei": amount(phase["gross_weth_wei"]),
            "policy_net_lower_wei": amount(phase["modeled_full_net_wei_lower"]),
            "traced_simulated_gas": raw_trace["gas_used"] if raw_trace else None,
            "simulated_net_after_gas_wei": amount(raw_trace["network_fee_net_wei"])
            if raw_trace
            else None,
            "actual_charged_gas": None,
        }
    h, c = out["phases"]["H"], out["phases"]["C"]
    witness = result["ethereum_newheads"]["witness"]
    origin = h["head_receive_monotonic_ns"]
    out["timing_ns"] = {
        "H_to_state": result["state_ready_ns"] - origin,
        "H_to_rank": result["ranking_finished_ns"] - origin,
        "H_to_whole_quorum": h["whole_quorum_return_monotonic_ns"] - origin,
        "H_to_C_independent_wss": witness["child_monotonic_ns"] - origin,
        "H_quorum_before_C_wss_margin": (
            witness["child_monotonic_ns"] - h["whole_quorum_return_monotonic_ns"]
        ),
        "C_http_minus_C_wss": c["head_receive_monotonic_ns"] - witness["child_monotonic_ns"],
    }
    return out


if __name__ == "__main__":
    print(
        json.dumps(
            {
                "schema": "oasis.y2026-eth-bsc-windows.v1",
                "fees_usd": 0,
                "execution_eligible": False,
                "ethereum": eth(),
                "bsc": bsc(),
            },
            indent=2,
            sort_keys=True,
        )
    )
