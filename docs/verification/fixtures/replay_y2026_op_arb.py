"""Rebuild frozen 2026 Optimism/Arbitrum coverage and repay-route evidence without I/O to RPCs."""

import argparse
import json
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path

from eth_abi import decode
from eth_hash.auto import keccak
from replay_multihop_second_shards import quantity, sha
from replay_published_multihop_receipts import indexed_address, leg_amounts
from rlp import encode

from oasis_agent.arbitrage.cycle_plan import address, hash32
from oasis_agent.arbitrage.cycle_protocols import topic
from oasis_agent.evidence.nitro_block import bloom_bits, indexed_trie_root
from oasis_agent.evidence.public_receipts import decode_receipt
from oasis_agent.evidence.research_traces import first_failure, nested_calls
from oasis_agent.evidence.state_trie import PRAGUE_FIELDS, hex_bytes
from oasis_agent.evidence.state_trie import quantity as bounded_quantity
from oasis_agent.evidence.swap_flow import TRANSFER
from oasis_agent.evidence.swap_flow_trace import replay_trace_funding
from oasis_agent.models.chain import BlockRef
from oasis_agent.rpc.budget import BudgetPolicy, RpcBudget
from oasis_agent.rpc.protocol import response_envelope

TIERS = (20, 50, 100, 200)
OP_WETH = "0x4200000000000000000000000000000000000006"
OP_USDC = "0x0b2c639c533813f4aa9d7837caf62653d097ff85"
OP_USDCE = "0x7f5c764cbc14f9669b88837ca1490cca17c31607"
AAVE_POOL = "0x794a61358d6845594f94dc1db02a252b5b4814ad"
AAVE_USER = "0xb32cb14f2a5b17d1f1343749e302316be5133321"
TOP_HASH = "0x86127198c8513464ec3a49c5c40c36c85b98e36dbe4cdc853ffccb1e9b3e7949"


def read(path):
    return json.loads(path.read_bytes())


def sha_file(path):
    return sha(path.read_bytes())


def utc(value):
    return datetime.fromtimestamp(value, UTC).isoformat()


def wire(folder, label, method=None, params=None):
    req = (folder / f"{label}.request.json").read_bytes()
    meta = read(folder / f"{label}.meta.json")
    raw = (folder / f"{label}.response").read_bytes()
    doc = json.loads(req)
    assert sha(req) == meta["request_sha256"] and sha(raw) == meta["response_sha256"]
    assert len(raw) == meta["bytes"] and meta["started_utc"] <= meta["finished_utc"]
    if method is not None:
        assert doc["method"] == method and doc["params"] == params and doc["id"] == meta["id"]
        payload = response_envelope(raw, meta["id"])
    else:
        payload = {"result": json.loads(raw)}
    result = payload.get("result")
    if meta["http"] != 200:
        result = None
    return result, {
        "method": method or "SQD stream",
        "label": label,
        "error": payload.get("error"),
        **meta,
    }


def budget_count(path, policy, expected):
    with RpcBudget(path / "original.sqlite3", (policy,), read_only=True) as ledger:
        assert ledger.counts() == expected
    assert len(list(path.glob("*.request.json"))) == expected[policy.source]
    return expected[policy.source]


def refused_scopes(root):
    anchor_plan = read(root / "anchor-plan.json")
    assert anchor_plan["driver_sha256"] == sha_file(root / "capture.py")
    anchors = {}
    for name, net in anchor_plan["nets"].items():
        p = root / "anchor" / name
        chain, chain_wire = wire(p, "chain", "eth_chainId", [])
        head, head_wire = wire(p, "finalized", "eth_getBlockByNumber", ["finalized", False])
        pin = read(p / "anchor.json")
        use = budget_count(p, BudgetPolicy(name, 2, 250), read(p / "STOP.json")["counts"])
        assert use == 2 and quantity(chain) == pin["chain_id"] == net["chain_id"]
        assert pin["hash"] == head["hash"] and pin["number"] == quantity(head["number"])
        assert pin["time"] == quantity(head["timestamp"])
        anchors[name] = pin | {
            "utc": utc(pin["time"]),
            "chain_wire": chain_wire,
            "head_wire": head_wire,
        }
    sqd = root / "index"
    plan = read(root / "index-plan.json")
    assert plan["driver_sha256"] == anchor_plan["driver_sha256"]
    old = read(sqd / "STOP.json")
    assert old["scope_sha256"] == sha_file(root / "index-plan.json")
    assert old["counts"] == {"sqd_public": 1} and not old["successes"]
    budget_count(sqd, BudgetPolicy("sqd_public", 4, 1000), old["counts"])
    attempted = plan["calls"][0]
    assert attempted["id"] == old["first_refusal"]["id"]
    _, sqd_wire = wire(sqd, attempted["id"])
    assert sqd_wire["http"] == 529
    assert read(sqd / f"{attempted['id']}.request.json") == attempted["body"]
    return (
        anchor_plan,
        anchors,
        {
            "planned_blocks": [
                v["body"]["toBlock"] - v["body"]["fromBlock"] + 1 for v in plan["calls"]
            ],
            "completed_blocks": 0,
            "stop": old,
            "failure_wire": sqd_wire,
        },
    )


def header_hash(header):
    fields = []
    ended = False
    for k, length in PRAGUE_FIELDS:
        if k not in header:
            ended = True
            continue
        assert not ended
        x = header[k]
        fields.append(
            bounded_quantity(x, -length)
            if length < 0
            else hex_bytes(x, length or 32, exact=length > 0)
        )
    return "0x" + keccak(encode(fields)).hex()


def receipt_commitment(chain, head, receipts):
    block = BlockRef(
        chain, quantity(head["number"]), hash32(head["hash"]), quantity(head["timestamp"])
    )
    assert header_hash(head) == block.block_hash
    raw = []
    cum = aggregate_bloom = next_log = 0
    for idx, (tx, r) in enumerate(zip(head["transactions"], receipts, strict=True)):
        result = decode_receipt(r, block, tx, idx)
        assert [log.log_index for log in result.logs] == list(
            range(next_log, next_log + len(result.logs))
        )
        next_log += len(result.logs)
        cum += result.gas_used
        assert cum == quantity(r["cumulativeGasUsed"])
        bloom = hex_bytes(r["logsBloom"], 256, exact=True)
        assert int.from_bytes(bloom) == bloom_bits(result.logs)
        aggregate_bloom |= int.from_bytes(bloom)
        logs = [
            [
                hex_bytes(log.address, 20, exact=True),
                [hex_bytes(t, 32, exact=True) for t in log.topics],
                hex_bytes(log.data, 512 * 1024),
            ]
            for log in result.logs
        ]
        fields = [result.status, cum, bloom, logs]
        kind = quantity(r["type"])
        if chain == 10 and kind == 0x7E:
            assert quantity(r["depositReceiptVersion"]) == 1
            fields += [quantity(r["depositNonce"]), quantity(r["depositReceiptVersion"])]
        raw.append((bytes([kind]) if kind else b"") + encode(fields))
    assert cum == quantity(head["gasUsed"]) and aggregate_bloom == int(head["logsBloom"], 16)
    root = "0x" + indexed_trie_root(raw).hex()
    assert root == head["receiptsRoot"]
    return block, root


def picks(found):
    return sorted(
        found,
        key=lambda r: (
            -r["events"],
            -r["distinct_logical_pools"],
            r["block_number"],
            r["transaction_index"],
            r["tx_hash"],
        ),
    )


def market_summary(logs, signatures):
    events = []
    for item in logs:
        topics = item["topics"]
        if not topics or topics[0] not in signatures:
            continue
        kind = signatures[topics[0]]
        pool_id = hash32(topics[1]) if kind in ("v4", "balancer_v2") else None
        events.append(
            {
                "log_index": quantity(item["logIndex"]),
                "kind": kind,
                "emitter": address(item["address"]),
                "pool_id": pool_id,
            }
        )
    assert len({r["log_index"] for r in events}) == len(events)
    return {
        "events": len(events),
        "distinct_emitters": len({e["emitter"] for e in events}),
        "distinct_logical_pools": len({(e["emitter"], e["pool_id"]) for e in events}),
        "types": dict(Counter(e["kind"] for e in events)),
        "event_locations": events,
    }


def log_scope(root, dirname, filename, signatures):
    plan, stop = read(root / filename), read(root / dirname / "STOP.json")
    assert stop["scope_sha256"] == sha_file(root / filename)
    driver = "rpc_index.py" if dirname == "rpc-index" else "confirmed_logs.py"
    assert plan["driver_sha256"] == sha_file(root / driver)
    assert plan["helper_sha256"] == sha_file(root / "capture.py")
    coverage = {}
    for rec in stop["lanes"]:
        name = rec["source"]
        path = root / dirname / name
        assert rec == read(path / "STOP.json")
        count = budget_count(path, BudgetPolicy(name, 12, 330), rec["counts"])
        stats = []
        found = []
        selected = []
        hashes = {}
        for out in plan["nets"][name]["windows"]:
            key, lo, hi = (out[x] for x in ("window", "first", "last"))
            if (key - 1) * 3 >= count:
                continue
            one = f"{key}-first"
            start, start_meta = wire(path, one, "eth_getBlockByNumber", [hex(lo), False])
            selected.append(start_meta)
            assert start is not None and quantity(start["number"]) == lo
            endpoint = plan["nets"][name]["rpc"]
            assert start_meta["url"] == endpoint
            query = {"fromBlock": hex(lo), "toBlock": hex(hi), "topics": [plan["topic0"]]}
            if (key - 1) * 3 + 1 >= count:
                continue
            logs, log_meta = wire(path, f"{key}-logs", "eth_getLogs", [query])
            selected.append(log_meta)
            assert log_meta["url"] == endpoint
            if (key - 1) * 3 + 2 >= count:
                stats.append(
                    {
                        "from_block": lo,
                        "to_block": hi,
                        "first_boundary_utc": utc(quantity(start["timestamp"])),
                        "completed": False,
                    }
                )
                continue
            final, last_meta = wire(path, f"{key}-last", "eth_getBlockByNumber", [hex(hi), False])
            selected.append(last_meta)
            assert last_meta["url"] == endpoint and quantity(final["number"]) == hi
            assert header_hash(start) == start["hash"] and header_hash(final) == final["hash"]
            expected = next(x for x in rec["windows"] if x["window"] == key)
            assert (expected["first_hash"], expected["last_hash"], expected["logs"]) == (
                start["hash"],
                final["hash"],
                len(logs),
            )
            by_tx = defaultdict(list)
            keys = set()
            for item in logs:
                h, tx, b = (
                    hash32(item["blockHash"]),
                    hash32(item["transactionHash"]),
                    quantity(item["blockNumber"]),
                )
                assert lo <= b <= hi and hashes.setdefault(b, h) == h and item["removed"] is False
                idx = quantity(item["logIndex"])
                assert (b, idx) not in keys and item["topics"][0] in signatures
                keys.add((b, idx))
                by_tx[b, h, quantity(item["transactionIndex"]), tx].append(item)
            rows = [
                {
                    "block_number": b,
                    "block_hash": h,
                    "transaction_index": index,
                    "tx_hash": tx,
                    **market_summary(data, signatures),
                }
                for (b, h, index, tx), data in by_tx.items()
            ]
            found += rows
            stats.append(
                {
                    "from_block": lo,
                    "to_block": hi,
                    "blocks": hi - lo + 1,
                    "first_boundary_utc": utc(quantity(start["timestamp"])),
                    "last_boundary_utc": utc(quantity(final["timestamp"])),
                    "matched_logs": len(logs),
                    "matched_transactions": len(rows),
                    "at_least_event_counts": thresholds(rows),
                    "completed": True,
                }
            )
        assert len(selected) == count
        observed = [x for x in stats if x["completed"]]
        coverage[name] = {
            "first_refusal": rec["first_refusal"],
            "windows": stats,
            "frozen_windows": plan["nets"][name]["windows"],
            "completed_blocks": sum(x["blocks"] for x in observed),
            "matching_logs": sum(x["matched_logs"] for x in observed),
            "matching_transactions": len(found),
            "at_least_event_counts": thresholds(found) if observed else None,
            "top_three": picks(found)[:3],
            "request_count": count,
            "wires": selected,
        }
    return coverage


def thresholds(rows):
    return {str(t): sum(r["events"] >= t for r in rows) for t in TIERS}


def receipts_scope(root, signatures):
    plan = read(root / "block-sample-plan.json")
    assert plan["driver_sha256"] == sha_file(root / "block_sample.py")
    archive = root / "block-sample"
    stop = read(archive / "STOP.json")
    assert stop["scope_sha256"] == sha_file(root / "block-sample-plan.json")
    output = {}
    selected = {}
    for row in stop["lanes"]:
        name = row["name"]
        spec = plan["sources"][name]
        folder = archive / name
        assert row == read(folder / "STOP.json") and row["first_refusal"] is None
        requests = budget_count(folder, BudgetPolicy(name, 17, 350), row["counts"])
        cid, meta = wire(folder, "chain", "eth_chainId", [])
        chain = quantity(cid)
        assert chain == spec["chain_id"] and meta["url"] == spec["endpoint"]
        blocks = []
        times = {}
        complete = []
        for n, witness in zip(spec["targets"], row["complete"], strict=True):
            head, h = wire(folder, f"{n}-header", "eth_getBlockByNumber", [hex(n), False])
            receipts, r = wire(folder, f"{n}-receipts", "eth_getBlockReceipts", [hex(n)])
            block, committed = receipt_commitment(chain, head, receipts)
            assert h["url"] == r["url"] == spec["endpoint"]
            assert (witness["number"], witness["hash"], witness["time"], witness["receipts"]) == (
                n,
                block.block_hash,
                block.timestamp,
                len(receipts),
            )
            times[n] = (block.block_hash, head["parentHash"])
            opened = [x for x in receipts if quantity(x["status"]) == 0]
            blocks.append(
                {
                    "block_number": n,
                    "block_hash": block.block_hash,
                    "receipts_root": committed,
                    "utc": utc(block.timestamp),
                    "complete_receipts": len(receipts),
                    "failed_receipts": len(opened),
                    "header_wire": h,
                    "receipt_wire": r,
                }
            )
            for tx in receipts:
                sm = market_summary(tx["logs"], signatures)
                complete.append(
                    (
                        {
                            "block_number": n,
                            "block_hash": block.block_hash,
                            "transaction_index": quantity(tx["transactionIndex"]),
                            "tx_hash": tx["transactionHash"],
                            **sm,
                        },
                        tx,
                        head,
                    )
                )
        for n in spec["targets"]:
            if n + 1 in times:
                assert times[n + 1][1] == times[n][0]
        candidates = [p[0] for p in complete if p[0]["events"]]
        maximum = picks(candidates)[0] if candidates else None
        short = min(
            (
                p
                for p in candidates
                if p != maximum and p["events"] == p["distinct_logical_pools"] == 2
            ),
            key=lambda p: (p["block_number"], p["transaction_index"]),
            default=None,
        )
        for info, tx, head in complete:
            if info in (maximum, short):
                selected[info["tx_hash"]] = (name, info, tx, head)
        output[name] = {
            "blocks": blocks,
            "chain_id": chain,
            "request_count": requests,
            "complete_receipts": len(complete),
            "matched_transactions": len(candidates),
            "matched_events": sum(p["events"] for p in candidates),
            "at_least_event_counts": thresholds(candidates),
            "selected_cases": [x for x in (maximum, short) if x],
        }
    return output, selected


def fee(chain, receipt):
    gas, price = quantity(receipt["gasUsed"]), quantity(receipt["effectiveGasPrice"])
    all_l2 = gas * price
    operator = None
    if chain == "optimism":
        l1, l2 = quantity(receipt["l1Fee"]), all_l2
        if "operatorFeeScalar" in receipt or "operatorFeeConstant" in receipt:
            # Captured headers and receipts are Jovian (DA footprint fields are present).
            assert "daFootprintGasScalar" in receipt
            scalar, constant = (
                quantity(receipt.get(k, 0)) for k in ("operatorFeeScalar", "operatorFeeConstant")
            )
            operator = gas * scalar * 100 + constant
    else:
        parent = quantity(receipt["gasUsedForL1"])
        assert 0 <= parent <= gas
        l1, l2, operator = parent * price, (gas - parent) * price, 0
    return {
        "gas_units": gas,
        "effective_price_wei": price,
        "execution_l2_wei": l2,
        "l1_data_wei": l1,
        "operator_fee_wei": operator,
        "known_network_fee_wei": l1 + l2 + (operator or 0),
        "complete_network_fee_wei": None if operator is None else l1 + l2 + operator,
    }


def moves(receipt):
    return {
        quantity(log["logIndex"]): (
            address(log["address"]),
            *(indexed_address(w) for w in log["topics"][1:]),
            quantity(log["data"]),
        )
        for log in receipt["logs"]
        if len(log["topics"]) == 3 and log["topics"][0] == TRANSFER and len(log["data"]) == 66
    }


def short_cases(selected):
    paths = {
        "0x64d9": ("open_two_swap", [(95, 94, 93), (98, 97, 96)], (93, 97), (94, 96)),
        "0x95cb": ("open_two_swap", [(77, 76, 75), (81, 80, 79)], (75, 78, 80), (70, 85)),
        "0xf489": ("open_single_swap", [(4, 1, 2)], (), (0, 2)),
    }
    result = []
    for txid, (name, summary, receipt, head) in selected.items():
        kind, evidence, middle, endpoints = paths[txid[:6]]
        raw = moves(receipt)
        logs = {quantity(x["logIndex"]): x for x in receipt["logs"]}
        legs = []
        for idx, sent, payout in evidence:
            paid_token, _, pool, paid = raw[sent]
            received_token, other_pool, _, received = raw[payout]
            assert pool == other_pool == logs[idx]["address"]
            leg = {
                "exchange": pool,
                "in_token": paid_token,
                "in_amount": paid,
                "out_token": received_token,
                "out_amount": received,
            }
            assert leg_amounts(leg, logs[idx])
            legs.append(leg | {"log_index": idx, "funding_log": sent, "output_log": payout})
        assert legs[0]["in_token"] != legs[-1]["out_token"] and len(legs) == summary["events"]
        if middle:
            credits = [raw[x] for x in middle]
            group, *rest = credits
            assert all(x[0] == group[0] and x[-1] == group[-1] for x in rest)
            assert all(a[2] == b[1] for a, b in pairwise(credits))
            assert group[0] == legs[0]["out_token"] == legs[1]["in_token"]
        entering, leaving = (raw[loc] for loc in endpoints)
        assert (entering[0], entering[-1]) == (legs[0]["in_token"], legs[0]["in_amount"])
        assert (leaving[0], leaving[-1]) == (legs[-1]["out_token"], legs[-1]["out_amount"])
        result.append(
            summary
            | {
                "chain": name,
                "chain_id": 10 if name == "optimism" else 42161,
                "time_utc": utc(quantity(head["timestamp"])),
                "classification": kind,
                "input_asset": legs[0]["in_token"],
                "output_asset": legs[-1]["out_token"],
                "exact_path_legs": legs,
                "intermediate_transfer_logs": middle,
                "first_input_transfer_log": endpoints[0],
                "last_output_transfer_log": endpoints[1],
                "visible_asset_source": entering[1],
                "visible_asset_recipient": leaving[2],
                "observed_swap_funding_depth": len(legs),
                "fee": fee(name, receipt),
                "signer": receipt["from"],
                "top_target": receipt["to"],
                "full_net": None,
            }
        )
    return result


def top_archive(root, top, signatures):
    folder = root / "top-case"
    plan = read(root / "top-case-plan.json")
    assert plan["driver_sha256"] == sha_file(root / "top_case.py")
    stop = read(folder / "STOP.json")
    assert stop["scope_sha256"] == sha_file(root / "top-case-plan.json")
    assert plan["scan_stop_sha256"] == sha_file(root / "log-confirmed-source/STOP.json")
    assert (
        plan["tx_hash"] == top["tx_hash"] == TOP_HASH and plan["index"] == top["transaction_index"]
    )
    assert plan["block_hash"] == top["block_hash"] and plan["block_number"] == top["block_number"]
    for n, h in plan["scan_responses_sha256"].items():
        assert sha_file(root / "log-confirmed-source" / "optimism" / f"{n}.response") == h
    values = {}
    for lane in stop["sources"]:
        key = lane["lane"]
        dr = folder / key
        assert lane == read(dr / "STOP.json") and lane["first_refusal"] is None
        ls = plan["lanes"][key]
        assert budget_count(dr, BudgetPolicy(key, len(ls["calls"]), 350), lane["counts"]) == len(
            ls["calls"]
        )
        values[key] = {label: wire(dr, label, method, args) for label, method, args in ls["calls"]}
        assert all(v[1]["url"] == ls["url"] for v in values[key].values())
    first, second = (values[f"optimism_{name}"] for name in ("drpc", "publicnode"))
    header, receipts, target, tracer = (
        first[k][0] for k in ("header", "full_receipts", "receipt", "trace")
    )
    header2 = second["header"][0]
    assert quantity(second["chain"][0]) == 10
    expected = (
        header["hash"],
        header["receiptsRoot"],
        header["transactionsRoot"],
        header["transactions"],
    )
    assert expected == (
        header2["hash"],
        header2["receiptsRoot"],
        header2["transactionsRoot"],
        header2["transactions"],
    )
    assert header_hash(header2) == header2["hash"]
    block, proof = receipt_commitment(10, header, receipts)
    position = plan["index"]
    assert receipts[position] == target and target["transactionHash"] == TOP_HASH
    observed = decode_receipt(target, block, TOP_HASH, position)
    assert market_summary(target["logs"], signatures)["event_locations"] == top["event_locations"]
    reconstructed = replay_trace_funding(observed, tracer)
    frames = nested_calls(tracer)
    cash = moves(target)
    funding_log, repayment_log = (cash[idx] for idx in (68, 165))
    assert funding_log[:2] == (OP_WETH, "0xe50fa9b3c56ffb159cb0fca61f5c9d750e8128c8")
    assert repayment_log[0] == OP_USDC and repayment_log[1] == funding_log[2] == AAVE_USER
    logs = {quantity(x["logIndex"]): x for x in target["logs"]}
    opening, ending = (logs[k] for k in (69, 166))
    assert opening["address"] == ending["address"] == AAVE_POOL
    assert opening["topics"][0] == topic("Withdraw(address,address,address,uint256)")
    assert ending["topics"][0] == topic("Repay(address,address,address,uint256,bool)")
    assert tuple(map(indexed_address, opening["topics"][1:])) == (OP_WETH, AAVE_USER, AAVE_USER)
    assert tuple(map(indexed_address, ending["topics"][1:])) == (OP_USDC, AAVE_USER, AAVE_USER)
    assert decode(["uint256"], bytes.fromhex(opening["data"][2:])) == (funding_log[-1],)
    assert decode(["uint256", "bool"], bytes.fromhex(ending["data"][2:])) == (
        repayment_log[-1],
        False,
    )
    assert cash[66][0] == funding_log[1] and cash[66][1] == AAVE_USER
    assert (
        cash[162][0] == "0x5d557b07776d12967914379c71a1310e917c7555" and cash[162][1] == AAVE_USER
    )
    proofs = {p.leg.event.log_index: p for p in reconstructed.resolved_swaps}
    legs = {index: entry.leg for index, entry in proofs.items()}
    joined = [81, 84]
    split = [132, 135, 138]
    assert sum(legs[x].amount_out for x in joined) == legs[126].amount_in
    assert all(legs[x].token_out == legs[126].token_in == OP_USDCE for x in joined)
    assert sum(legs[x].amount_in for x in split) == legs[87].amount_out
    assert all(legs[x].token_in == legs[87].token_out for x in split)
    split_in = [proofs[x].input_transfer_log for x in split]
    split_out = proofs[87].output_transfer_log
    join_in = proofs[126].input_transfer_log
    join_out = [proofs[x].output_transfer_log for x in joined]
    holder = cash[split_out][2]
    assert holder == cash[join_in][1]
    assert all(cash[x][2] == holder and x < join_in for x in join_out)
    assert all(cash[x][1] == holder and split_out < x for x in split_in)
    assert {k for k, v in cash.items() if v[0] == OP_USDCE and holder in v[1:3]} == {
        *join_out,
        join_in,
    }
    assert {k for k, v in cash.items() if v[0] == legs[87].token_out and holder in v[1:3]} == {
        split_out,
        *split_in,
    }
    for links, currency, amount in (
        ((68, 70, 72, 74), OP_WETH, funding_log[-1]),
        ((156, 160, 165), OP_USDC, repayment_log[-1]),
    ):
        allocations = [cash[k] for k in links]
        assert all(move[0] == currency and move[-1] == amount for move in allocations)
        assert all(a[2] == b[1] for a, b in pairwise(allocations))
    moves_native = [
        {"path": path, "from": f.sender, "to": f.recipient, "amount_wei": f.value}
        for path, f in frames.items()
        if f.kind in {"CALL", "CREATE", "CREATE2", "SELFDESTRUCT"}
        and f.value
        and first_failure(frames, path) is None
    ]
    return top | {
        "chain": "optimism",
        "chain_id": 10,
        "classification": "open_collateral_withdrawal_and_debt_repayment",
        "time_utc": utc(block.timestamp),
        "header_sources": 2,
        "receipt_root": proof,
        "ordered_block_receipts": len(receipts),
        "transaction_receipt_logs": len(target["logs"]),
        "transactions_root_field": header["transactionsRoot"],
        "transactions_root_reconstructed": False,
        "trace_frames": len(frames),
        "generic_trace_funding_profile": asdict(reconstructed.profile),
        "generic_trace_unresolved_events": reconstructed.unresolved_events,
        "generic_trace_resolved_legs": [asdict(p) for p in reconstructed.resolved_swaps],
        "evidenced_multi_input_and_output_amounts": {
            "join": {
                "token": OP_USDCE,
                "source_swaps": joined,
                "target_swap": 126,
                "amounts": [legs[x].amount_out for x in joined],
                "holder": holder,
                "incoming_transfer_logs": join_out,
                "outgoing_transfer_log": join_in,
            },
            "split": {
                "token": legs[87].token_out,
                "source_swap": 87,
                "target_swaps": split,
                "amounts": [legs[x].amount_in for x in split],
                "holder": holder,
                "incoming_transfer_log": split_out,
                "outgoing_transfer_logs": split_in,
            },
        },
        "collateral": {
            "user": AAVE_USER,
            "withdraw_log": 69,
            "withdraw_transfer_log": 68,
            "collateral_route_transfer_logs": [68, 70, 72, 74],
            "a_token_burn_log": 66,
            "asset": OP_WETH,
            "raw": funding_log[-1],
        },
        "debt_repayment": {
            "repay_log": 166,
            "transfer_log": 165,
            "proceeds_route_transfer_logs": [156, 160, 165],
            "debt_token_burn_log": 162,
            "debt_token_burn_raw": cash[162][-1],
            "asset": OP_USDC,
            "raw": repayment_log[-1],
        },
        "trace_native_movements": moves_native,
        "fee": fee("optimism", target),
        "network_fee_payer": target["from"],
        "full_net": None,
        "wires": {key: {k: v[1] for k, v in lanes.items()} for key, lanes in values.items()},
    }


def main():
    if not __debug__:
        raise RuntimeError("run with assertions enabled")
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("root", type=Path)
    root = p.parse_args().root
    plan, anchors, sqd = refused_scopes(root)
    sig = {v: k for k, v in plan["swap_topics"].items()}
    original_logs = log_scope(root, "rpc-index", "rpc-index-plan.json", sig)
    complete, selections = receipts_scope(root, sig)
    verified_logs = log_scope(root, "log-confirmed-source", "confirmed-log-plan.json", sig)
    case = top_archive(root, verified_logs["optimism"]["top_three"][0], sig)
    document = {
        "schema": "oasis.y2026-optimism-arbitrum-cohort.v1",
        "cost_usd": 0,
        "event_topics": plan["swap_topics"],
        "finalized_anchors": anchors,
        "initial_sqd": sqd,
        "original_official_log_scope": original_logs,
        "fully_covered_blocks": complete,
        "separate_small_log_scope": verified_logs,
        "cases": [case, *short_cases(selections)],
        "receipt_read_requests": sum(x["request_count"] for x in complete.values()),
        "all_capture_read_requests": 4
        + 1
        + sum(x["request_count"] for x in original_logs.values())
        + sum(x["request_count"] for x in complete.values())
        + sum(x["request_count"] for x in verified_logs.values())
        + sum(len(x) for x in case["wires"].values()),
    }
    print(json.dumps(document, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
