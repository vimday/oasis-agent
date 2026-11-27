"""Verify preselected whole-block panels, direct pool funding and per-address settlement."""

import json
from collections import Counter
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path

from replay_y2026_bsc_rh_landed import TIERS, V4, ZERO, erc20_net, matches
from replay_y2026_eth_bsc_windows import PC3, V2, V3, dec_address, swap_deltas, transfer_rows
from replay_y2026_inventory import decoded, digest, read
from replay_y2026_op_arb import header_hash, receipt_commitment

from oasis_agent.arbitrage.cycle_protocols import abi, data_bytes
from oasis_agent.evidence.base_denim import validate_header_timestamp_ms
from oasis_agent.evidence.nitro_block import nitro_header
from oasis_agent.evidence.public_receipts import quantity
from oasis_agent.rpc.budget import BudgetPolicy, RpcBudget

ROOT = Path(__file__).resolve().parents[3]
D = ROOT / "reports/research/y2026-base-ely-background-20261128"
SCOPE = "61e3b3bd8edb68779adc76dbe3d6f76da8f0df38816e2cb1365d1f08478a4f99"
BASE_TX = "0x16f3c748b37ae594fd7817bbcc73762507f9c886ea25fdfe9cdf40f12dbf8cee"
ELY_TX = "0x423d6983929dd01e52bd1a1ff7bd153c0e9a091bea4c5c1f74dd6be066ae8d67"


def verified_block(chain, h, rows):
    if chain == 99901:
        nitro_header(h, quantity(h["number"]), h["hash"], chain_id=chain)
        kinds = [quantity(row["type"]) for row in rows]
        assert kinds[0] == 0x6A and set(kinds[1:]) <= {0, 2, 4, 0x68, 0x69}
    block, root = receipt_commitment(chain, h, rows)
    assert datetime.fromtimestamp(block.timestamp, UTC).year == 2026
    assert all(quantity(r["status"]) or r["logs"] == [] for r in rows)
    return {
        "height": block.number,
        "hash": block.block_hash,
        "timestamp": block.timestamp,
        "timestamp_ms": validate_header_timestamp_ms(h, chain_id=chain) if chain == 8453 else None,
        "receipts_root": root,
        "transactions": len(rows),
        "reverts": sum(quantity(r["status"]) == 0 for r in rows),
    }


def fee(chain, r):
    gas, price = quantity(r["gasUsed"]), quantity(r["effectiveGasPrice"])
    base = {
        "block": quantity(r["blockNumber"]),
        "block_hash": r["blockHash"],
        "tx": r["transactionHash"],
        "index": quantity(r["transactionIndex"]),
        "sender": r["from"],
        "target": r["to"],
        "gas": gas,
        "native_wei_per_gas": price,
        "gas_product_native_wei": gas * price,
    }
    if chain == 8453:
        l1 = quantity(r["l1Fee"])
        return base | {
            "l1_fee_wei": l1,
            "l2_plus_l1_wei": gas * price + l1,
            "operator_fee_scalar": r.get("operatorFeeScalar"),
            "operator_fee_constant": r.get("operatorFeeConstant"),
        }
    return base | {"l1_gas_inside_total": quantity(r["gasUsedForL1"])}


def direct_pool_paths(r):
    """Bind both sides of each event. Internal edges must spend the same single transfer."""
    legs = []
    moves = transfer_rows(r)
    for event in r["logs"]:
        topics = event["topics"]
        if len(topics) != 3 or topics[0] not in (V2, V3, PC3):
            continue
        delta = swap_deltas(event)
        if delta is None:
            continue
        paid, received, forward = delta
        pool, idx, to = event["address"], quantity(event["logIndex"]), dec_address(topics[2])
        ins = [m for m in moves if m["to"] == pool and m["amount"] == paid and m["log"] < idx]
        outs = [
            m
            for m in moves
            if (m["from"], m["to"], m["amount"]) == (pool, to, received) and m["log"] < idx
        ]
        if len(ins) != 1 or len(outs) != 1:
            continue
        i, o = ins[0], outs[0]
        if i["asset"] != o["asset"] and (i["asset"] < o["asset"]) == forward:
            legs.append({"swap": idx, "topic": topics[0], "pool": pool, "in": i, "out": o})
    a, b = Counter(x["in"]["log"] for x in legs), Counter(x["out"]["log"] for x in legs)
    unique = [x for x in legs if a[x["in"]["log"]] == b[x["out"]["log"]] == 1]
    by_pay = {x["in"]["log"]: x for x in unique}
    paths = []
    for start in unique:
        path, pools = [start], {start["pool"]}
        while (nxt := by_pay.get(path[-1]["out"]["log"])) is not None and nxt["pool"] not in pools:
            path.append(nxt)
            pools.add(nxt["pool"])
            i, o = start["in"], nxt["out"]
            if i["from"] == o["to"] == r["to"] and i["asset"] == o["asset"]:
                paths.append(
                    {
                        "asset": i["asset"],
                        "holder": i["from"],
                        "paid": i["amount"],
                        "returned": o["amount"],
                        "gross_raw": o["amount"] - i["amount"],
                        "legs": path.copy(),
                    }
                )
                break
    return len(unique), paths


def base_payout(r, cases):
    (case,) = cases
    moves = transfer_rows(r)
    legs, holder = case["legs"], r["to"]
    assert [event["swap"] for event in legs] == [139, 140, 142]
    assert [event["out"]["log"] for event in legs] == [137, 136, 135]
    assert [event["in"]["log"] for event in legs] == [138, 137, 136]
    assert all(
        dec_address(event["topics"][1]) == holder
        for event in r["logs"]
        if event["topics"][0] in (PC3, V3)
    )
    assert len(moves) == 5
    payout = moves[-1]
    assert (payout["from"], payout["asset"], payout["amount"]) == (
        holder,
        case["asset"],
        case["gross_raw"],
    )
    assert payout["to"] not in (holder, r["from"]) and all(
        v == 0 for v in erc20_net(moves, holder).values()
    )
    charge = fee(8453, r)
    wei_mark = case["gross_raw"] * legs[0]["out"]["amount"] // case["paid"]
    return {
        "tx": r["transactionHash"],
        "case": case,
        "payout": payout,
        "executor_token_net": erc20_net(moves, holder),
        "sender_receipt_fee": charge,
        "at_first_leg_average_wei": wei_mark,
        "conditional_average_mark_less_l2_l1_wei": wei_mark - charge["l2_plus_l1_wei"],
        "beneficiary_owner_net_native_wei": None,
    }


def elysium_output(r):
    moves = transfer_rows(r)
    by = {m["log"]: m for m in moves}
    logs = {quantity(event["logIndex"]): event for event in r["logs"]}
    assert list(by) == [5, 9, 12, 13, 14, 17, 21, 23, 28, 31]

    def transfers(indices):
        ordered = [by[i] for i in indices]
        assert all(
            u["to"] == v["from"] and (u["asset"], u["amount"]) == (v["asset"], v["amount"])
            for u, v in pairwise(ordered)
        )
        return ordered

    principal, burn, payout = (
        transfers([5, 9, 12, 14]),
        transfers([13, 17]),
        transfers([21, 23, 28, 31]),
    )
    event, manager = logs[15], logs[20]
    assert event["topics"][0] == V3 and manager["topics"][0] == V4
    paid, output, forward = swap_deltas(event)
    assert forward and (paid, output) == (principal[-1]["amount"], burn[0]["amount"])
    assert event["address"] == principal[-1]["to"] == burn[0]["from"] and burn[-1]["to"] == ZERO
    a, b, *_ = abi(
        ("int128", "int128", "uint160", "uint128", "int24", "uint24"), data_bytes(manager["data"])
    )
    assert a < 0 < b and manager["address"] == payout[0]["from"] and b == payout[0]["amount"]
    assert principal[0]["asset"] != payout[-1]["asset"] and principal[0]["from"] != payout[-1]["to"]
    assert logs[19]["address"] == manager["address"]
    extra = int.from_bytes(data_bytes(logs[19]["data"])[32:64])
    assert burn[-1]["amount"] + a == extra
    return {
        "tx": r["transactionHash"],
        "input_movements": principal,
        "intermediate_burn": burn,
        "v4_pool_id": manager["topics"][1],
        "v4_delta0": a,
        "v4_delta1": b,
        "output_movements": payout,
        "unwrapped_minus_v4_input": extra,
        "native_recipient_for_difference": None,
        "sender_fee": fee(99901, r),
        "closed_asset_return": False,
    }


def cohort(name, plan):
    source = plan["lanes"][name]
    chain = source["chain"]
    folder = D / name
    wire = {}

    def call(label, src, method, params):
        out, m = decoded(folder, label, method, params, source[src])
        assert plan["frozen"] <= m["started"] <= m["finished"]
        wire[label] = m
        return out

    for src in ("headers", "receipts"):
        assert quantity(call(src + "-id", src, "eth_chainId", [])) == chain
    anchor = call("anchor", "headers", "eth_getBlockByNumber", ["latest", False])
    height = quantity(anchor["number"])
    if chain == 8453:
        assert header_hash(anchor) == anchor["hash"]
    else:
        nitro_header(anchor, height, anchor["hash"], chain_id=chain)
    blocks, headers, candidates, records = [], {}, {}, {}
    counts = Counter()
    for gap in plan["offsets"]:
        n = height - gap
        h = call(f"h{n}", "headers", "eth_getBlockByNumber", [hex(n), False])
        assert quantity(h["number"]) == n
        rows = call(f"r{n}", "receipts", "eth_getBlockReceipts", [h["hash"]])
        blocks.append(verified_block(chain, h, rows))
        headers[n] = h
        for r in rows:
            tx = r["transactionHash"]
            assert tx not in records
            records[tx] = r
            e = [event for event in r["logs"] if matches(event, plan["topics"])]
            counts[len(e)] += 1
            if e:
                candidates[tx] = (len(e), n, quantity(r["transactionIndex"]))
    for n, h in headers.items():
        if n + 1 in headers:
            assert headers[n + 1]["parentHash"] == h["hash"]
    order = sorted(candidates, key=lambda tx: (-candidates[tx][0], *candidates[tx][1:], tx))
    audited = {}
    for tx in order:
        count = candidates[tx][0]
        if count < 2:
            continue
        n_legs, cycles = direct_pool_paths(records[tx])
        audited[tx] = fee(chain, records[tx]) | {
            "events": count,
            "amount_bound_legs": n_legs,
            "direct_pool_cycles": cycles,
        }
    low = min((tx for tx in order if candidates[tx][0] == 1), key=lambda tx: candidates[tx][1:])
    stop = read(folder / "STOP.json")
    assert (
        stop["scope"] == SCOPE
        and stop["error"] is None
        and stop["counts"] == {"headers": 14, "receipts": 13}
    )
    with RpcBudget(
        folder / "original.sqlite3",
        tuple(BudgetPolicy(src, plan["limit_" + src], 250) for src in ("headers", "receipts")),
        read_only=True,
    ) as b:
        assert b.counts() == stop["counts"] and sum(b.counts().values()) == len(wire)
    return (
        {
            "chain": chain,
            "anchor": height,
            "anchor_hash": anchor["hash"],
            "blocks": blocks,
            "stop": stop,
            "wire": wire,
            "thresholds": {
                i: sum(count for size, count in counts.items() if size >= i) for i in TIERS
            },
            "events": sum(size * count for size, count in counts.items()),
            "histogram": counts,
            "all_at_least_two": list(audited.values()),
            "earliest_one_event_control": fee(chain, records[low]),
            "widest_event_ties": [
                tx for tx in order if candidates[tx][0] == candidates[order[0]][0]
            ],
        },
        records,
        audited,
    )


def main():
    plan = read(D / "scope.json")
    assert digest(D / "scope.json") == SCOPE and digest(D / "capture.py") == plan["driver_sha"]
    assert list(TIERS) == plan["tiers"] and len(plan["topics"]) == len(set(plan["topics"])) == 16
    base, rb, ab = cohort("base", plan)
    ely, re, _ = cohort("elysium", plan)
    assert BASE_TX in base["widest_event_ties"] and ely["widest_event_ties"] == [ELY_TX]
    base["cbbtc_payout"] = base_payout(rb[BASE_TX], ab[BASE_TX]["direct_pool_cycles"])
    short = []
    for tx, candidate in ab.items():
        for case in candidate["direct_pool_cycles"]:
            if len(case["legs"]) != 2:
                continue
            assert case["asset"] == "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
            moves = transfer_rows(rb[tx])
            assert len(moves) == 3 and len(rb[tx]["logs"]) == 5
            assert erc20_net(moves, case["holder"]) == {case["asset"]: case["gross_raw"]}
            assert case["holder"] != candidate["sender"]
            short.append(
                {
                    "transaction": tx,
                    "executor_usdc_change_raw": case["gross_raw"],
                    "l2_l1_fee_wei": candidate["l2_plus_l1_wei"],
                    "fee_break_even_usdc_per_eth_floor": case["gross_raw"]
                    * 10**12
                    // candidate["l2_plus_l1_wei"],
                    "beneficiary_owner_net": None,
                }
            )
    assert len(short) == 3
    base["two_pool_usdc_checks"] = short
    ely["two_event_open_path"] = elysium_output(re[ELY_TX])
    return {
        "schema": "oasis.base-elysium-full-blocks.v1",
        "scope_sha256": SCOPE,
        "scope": plan,
        "cost_usd": 0,
        "requests": len(base["wire"]) + len(ely["wire"]),
        "base": base,
        "elysium": ely,
    }


if __name__ == "__main__":
    if not __debug__:
        raise SystemExit("run without -O")
    print(json.dumps(main(), sort_keys=True, indent=2))
