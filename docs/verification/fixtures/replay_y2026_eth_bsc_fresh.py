"""Verify the fresh Ethereum and BSC whole-block receipts and selected settlements."""

import csv
import gzip
import json
from collections import Counter
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path

from eth_account import Account
from eth_account.typed_transactions import TypedTransaction
from eth_hash.auto import keccak
from replay_y2026_bsc_rh_landed import matches, rooted
from replay_y2026_eth_bsc_untouched import FLASH, PATH_TOPICS, WETH
from replay_y2026_eth_bsc_windows import WBNB, WITHDRAWAL, dec_address
from replay_y2026_multichain_expanded import eth_balance, signed_bytes
from replay_y2026_op_arb import header_hash, moves, receipt_commitment, sha_file, wire
from replay_y2026_open_tail import receipt_paths

from oasis_agent.arbitrage.cycle_protocols import data_bytes
from oasis_agent.evidence.public_receipts import decode_receipt
from oasis_agent.evidence.public_receipts import quantity as q
from oasis_agent.evidence.research_traces import first_failure, nested_calls
from oasis_agent.evidence.swap_flow_trace import replay_trace_funding
from oasis_agent.models.common import BlockRef
from oasis_agent.rpc.budget import BudgetPolicy, RpcBudget
from oasis_agent.rpc.protocol import response_envelope
from scripts.replay_eth_tail_case import _settlement

PROJECT = Path(__file__).resolve().parents[3]
RESEARCH = PROJECT / "reports/research"
D = RESEARCH / "y2026-eth-bsc-fresh-20261128"
OLD_CATALOG = RESEARCH / "gold-book-20261127/all-hashes.tsv.gz"
EARLIER = (
    PROJECT / "docs/verification/Y2026_ETH_BSC_UNTOUCHED_20261128.json",
    PROJECT / "docs/verification/Y2026_MULTICHAIN_EXPANDED_20261128.json",
)
BANDS = (2, 3, 5, 10, 20, 50, 100, 200)


def read(path):
    return json.loads(path.read_bytes())


def earlier_samples():
    known = {"ethereum": set(), "bsc": set()}
    with gzip.open(OLD_CATALOG, "rt") as fp:
        for row in csv.DictReader(fp, delimiter="\t"):
            if row["chain"] in known:
                known[row["chain"]].add(row["tx_hash"])
    prior, expanded = [read(path) for path in EARLIER]
    for chain, code_hashes in known.items():
        code_hashes.update(
            item["transaction"] for item in prior["evidence"][chain]["all_at_least_two"]
        )
        code_hashes.update(expanded["lanes"][chain]["all_multi_event"])
    blocks = {
        chain: {
            row["height"]
            for group in (prior["evidence"][chain]["blocks"], expanded["lanes"][chain]["blocks"])
            for row in group
        }
        for chain in known
    }
    return known, blocks


def required_wire(folder, label, method, args, url, frozen, stop, proofs):
    raw, proof = wire(folder, label, method, args)
    assert raw is not None and proof["error"] is None
    assert proof["url"] == url and frozen <= proof["started_utc"] <= proof["finished_utc"] <= stop
    assert label not in proofs
    proofs[label] = proof
    return raw


def scan(chain, spec, scope, known_transactions, known_blocks):
    folder = D / chain
    stop = read(folder / "STOP.json")
    assert stop["scope"] == sha_file(D / "scope.json") and stop["error"] is None
    width = spec["width"]
    expected = width * len(spec["offsets"])
    limits = {"h": expected + 2, "r": expected + 1}
    with RpcBudget(
        folder / "original.sqlite3",
        tuple(BudgetPolicy(source, count, 250) for source, count in limits.items()),
        read_only=True,
    ) as budget:
        assert budget.counts() == stop["counts"] == limits
    proofs = {}
    cid = spec["chain"]

    def get(source, label, method, args):
        return required_wire(
            folder, label, method, args, spec[source], scope["utc"], stop["utc"], proofs
        )

    for name in limits:
        assert q(get(name, f"{name}-id", "eth_chainId", [])) == cid
    head = get("h", "anchor", "eth_getBlockByNumber", ["latest", False])
    anchor = q(head["number"])
    blocks, candidates, details = [], [], {}
    events, depths = Counter(), Counter()
    previous_txs = set()
    reverts, failure_fee = 0, 0
    for offset in spec["offsets"]:
        parent = None
        for height in range(anchor - offset, anchor - offset + width):
            assert height not in known_blocks
            block = get("h", f"h{height}", "eth_getBlockByNumber", [hex(height), False])
            assert q(block["number"]) == height and (
                parent is None or block["parentHash"] == parent
            )
            parent = block["hash"]
            rows = get("r", f"r{height}", "eth_getBlockReceipts", [parent])
            assert block["transactions"] == [r["transactionHash"] for r in rows]
            moment = datetime.fromtimestamp(q(block["timestamp"]), UTC)
            assert moment.year == 2026
            if cid == 1:
                assert parent == header_hash(block)
                _, receipts_root = receipt_commitment(cid, block, rows)
            else:
                receipts_root = rooted(cid, block, rows)["receipts_root"]
            blocks.append(
                {
                    "height": height,
                    "hash": parent,
                    "utc": moment.isoformat(),
                    "receipts_root": receipts_root,
                    "receipts": len(rows),
                }
            )
            for r in rows:
                tx = r["transactionHash"]
                assert tx not in previous_txs
                previous_txs.add(tx)
                fee = q(r["gasUsed"]) * q(r["effectiveGasPrice"])
                if not q(r["status"]):
                    assert not r["logs"]
                    reverts += 1
                    failure_fee += fee
                count = sum(matches(entry, scope["topics"]) for entry in r["logs"])
                events[count] += 1
                if count < 2:
                    continue
                paths = receipt_paths(r, PATH_TOPICS)
                max_path = len(paths["longest_linked"])
                depths[max_path] += 1
                close = paths["closed_at_same_holder"]
                native = WETH if cid == 1 else WBNB
                row = {
                    "tx": tx,
                    "block": height,
                    "index": q(r["transactionIndex"]),
                    "fee_wei": fee,
                    "fee_payer": r["from"],
                    "executor": r["to"],
                    "events": count,
                    "funding_depth": max_path,
                    "closures": close,
                    "financing_event_shapes": sum(matches(log, FLASH) for log in r["logs"]),
                    "best_native_close_minus_fee_wei": max(
                        (item["gross_raw"] - fee for item in close if item["asset"] == native),
                        default=None,
                    ),
                    "seen_before": tx in known_transactions,
                }
                candidates.append(row)
                details[tx] = (r, block, paths)
    assert [b["height"] for b in blocks] == stop["heights"]
    assert len(proofs) == sum(limits.values()) == len(list(folder.glob("*.request.json")))
    return {
        "chain_id": cid,
        "blocks": blocks,
        "proofs": proofs,
        "reverts": reverts,
        "failed_receipts_fee_wei": failure_fee,
        "requests": sum(limits.values()),
        "event_histogram": events,
        "funding_depth_histogram": depths,
        "at_least": {band: sum(v for k, v in events.items() if k >= band) for band in BANDS},
        "candidates": candidates,
    }, details


def panel():
    scope = read(D / "scope.json")
    assert sha_file(D / "acquire.py") == scope["driver"]
    assert sha_file(RESEARCH / "y2026-op-arb-20261127/capture.py") == scope["transport"]
    topics = RESEARCH / "y2026-bsc-rh-landed-20261127/scope.json"
    assert sha_file(topics) == scope["topic_source"] and read(topics)["topics"] == scope["topics"]
    assert scope["usd"] == 0
    known_transactions, known_blocks = earlier_samples()
    panels, details = {}, {}
    for name, spec in scope["specs"].items():
        panels[name], details[name] = scan(
            name, spec, scope, known_transactions[name], known_blocks[name]
        )
    return panels, details


def select(candidates):
    fresh = [row for row in candidates if not row["seen_before"]]

    def earliest(row):
        return -row["block"], -row["index"]

    event = max(fresh, key=lambda r: (r["events"], r["funding_depth"], *earliest(r)))
    serial = max(fresh, key=lambda r: (r["funding_depth"], r["events"], *earliest(r)))
    valued = [r for r in fresh if r["best_native_close_minus_fee_wei"] is not None]
    cash = max(valued, key=lambda r: (r["best_native_close_minus_fee_wei"], *earliest(r)))
    return list(dict.fromkeys(row["tx"] for row in (event, serial, cash)))


def supplemental(folder, lane, plan):
    stop = read(folder / "STOP.json")
    source = folder.name
    assert stop["scope"] == sha_file(D / "follow/scope.json")
    limits = (BudgetPolicy(source, len(lane["calls"]), 300),)
    with RpcBudget(folder / "original.sqlite3", limits, read_only=True) as budget:
        assert stop["counts"] == budget.counts()
    calls = stop["counts"][source]
    assert 0 < calls <= len(lane["calls"])
    assert len(list(folder.glob("*.request.json"))) == calls
    values, metadata = {}, {}
    for i, (label, method, args) in enumerate(lane["calls"][:calls], 1):
        meta = read(folder / f"{label}.meta.json")
        assert meta["id"] == i and meta["url"] == lane["url"]
        assert plan["utc"] <= meta["started_utc"] <= meta["finished_utc"] <= stop["utc"]
        req = folder / f"{label}.request.json"
        raw = folder / f"{label}.response"
        assert read(req) == {"jsonrpc": "2.0", "id": i, "method": method, "params": args}
        assert sha_file(req) == meta["request_sha256"]
        if raw.exists():
            assert sha_file(raw) == meta["response_sha256"] and raw.stat().st_size == meta["bytes"]
        metadata[label] = meta
        if i == calls and stop["error"]:
            if meta["http"] == 200 and meta["transport_error"] is None:
                payload = response_envelope(raw.read_bytes(), i)
                assert "error" in payload or payload["result"] is None
            continue
        values[label], _ = wire(folder, label, method, args)
        assert values[label] is not None
    assert stop["done"] == list(values)
    if values:
        assert q(values.pop("chain")) == lane["chain_id"]
    return values, {"stop": stop, "proofs": metadata}


def trace_settlement(r, h, tx, cid, calls, diff):
    ref = BlockRef(
        chain_id=cid, number=q(h["number"]), block_hash=h["hash"], timestamp=q(h["timestamp"])
    )
    receipt = decode_receipt(r, ref, tx["hash"], q(r["transactionIndex"]))
    funding = replay_trace_funding(receipt, calls)
    frames = nested_calls(calls)
    assert frames[()].sender == tx["from"] and frames[()].recipient == tx["to"]
    assert frames[()].value == q(tx["value"]) and q(calls["gasUsed"]) == q(r["gasUsed"])
    native = [
        {"from": f.sender, "to": f.recipient, "value": f.value, "path": path}
        for path, f in sorted(frames.items())
        if f.kind == "CALL" and f.value and first_failure(frames, path) is None
    ]
    assert isinstance(diff["pre"], dict) and isinstance(diff["post"], dict)
    deltas = {actor: eth_balance(diff, actor) for actor in sorted({r["from"], r["to"]} - {None})}
    result = {
        "trace_funding_depth": funding.profile.witnessed_depth,
        "trace_resolved_swaps": len(funding.resolved_swaps),
        "native_calls": native,
        "native_balance_diff_wei": deltas,
    }
    if cid == 1:
        cash = _settlement(receipt, calls, h["miner"])
        for actor, actual in deltas.items():
            inferred = sum(
                item["delta_after_receipt_gas"]
                for item in cash["visible_role_deltas"]
                if item["account"] == actor and item["asset"] == "ETH"
            )
            assert actual == inferred or (actual is None and inferred == 0)
        result["settlement"] = cash
    return result


def reconcile(chain, cid, tx, details, traces):
    r, h, graph = details
    if q(tx["type"]) == 1:
        assert q(tx["chainId"]) == cid and q(tx["yParity"]) == q(tx["v"]) in (0, 1)
        fields = {
            name: tx[name]
            for name in ("type", "chainId", "nonce", "gasPrice", "gas", "accessList", "v", "r", "s")
        }
        fields.update(to=data_bytes(tx["to"]), value=q(tx["value"]), data=data_bytes(tx["input"]))
        signed = TypedTransaction.from_dict(fields).encode()
    else:
        signed = signed_bytes(tx, cid)
    assert "0x" + keccak(signed).hex() == tx["hash"] == r["transactionHash"]
    assert Account.recover_transaction(signed).lower() == tx["from"] == r["from"]
    assert all(
        tx[key] == r[key] for key in ("to", "blockHash", "blockNumber", "transactionIndex", "type")
    )
    assert q(tx["gasPrice"]) == q(r["effectiveGasPrice"])
    roles = {r["from"], r["to"]}
    balance = Counter()
    for token, sender, receiver, amount in moves(r).values():
        balance[sender, token] -= amount
        balance[receiver, token] += amount
    unwraps = [
        {
            "beneficiary": dec_address(log["topics"][1]),
            "wei": int(log["data"], 16),
            "index": q(log["logIndex"]),
        }
        for log in r["logs"]
        if log["address"] == (WBNB if chain == "bsc" else WETH)
        and len(log["topics"]) == 2
        and log["topics"][0] == WITHDRAWAL
    ]
    legs = {leg["event"]: leg for leg in graph["receipt_legs"]}
    closures = sorted(
        graph["closed_at_same_holder"],
        key=lambda cycle: min(legs[event]["in_log"] for event in cycle["legs"]),
    )
    covered = [event for cycle in closures for event in cycle["legs"]]
    assert len(covered) == len(set(covered))
    comparable = [
        (left, right)
        for left, right in pairwise(closures)
        if (left["asset"], left["holder"]) == (right["asset"], right["holder"])
    ]
    key = tx["hash"]
    result = {
        "receipt_bound_legs": graph["receipt_legs"],
        "unique_edges": graph["unique_edges"],
        "longest_funding_path": graph["longest_linked"],
        "closures": closures,
        "market_events_in_disjoint_closures": len(covered),
        "adjacent_same_holder_asset_closures": len(comparable),
        "adjacent_full_proceeds_reused": sum(
            left["received_raw"] == right["paid_raw"] for left, right in comparable
        ),
        "wrapped_native_unwrap_events": unwraps,
        "role_token_transfer_deltas": [
            {"account": account, "token": token, "delta": amount}
            for (account, token), amount in sorted(balance.items())
            if account in roles and amount
        ],
        "signed_envelope_and_sender_verified": True,
        "receipt_fee_wei": q(r["gasUsed"]) * q(r["effectiveGasPrice"]),
        "trace": None,
    }
    if (key + "-calls") in traces and (key + "-diff") in traces:
        result["trace"] = trace_settlement(
            r, h, tx, cid, traces[key + "-calls"], traces[key + "-diff"]
        )
    return result


def main():
    panels, details = panel()
    follow = D / "follow"
    plan = read(follow / "scope.json")
    assert sha_file(follow / "acquire.py") == plan["driver"]
    assert plan["parent_scope"] == sha_file(D / "scope.json") and plan["usd"] == 0
    looked_up, proof = {}, {}
    for name, lane in plan["lanes"].items():
        looked_up[name], proof[name] = supplemental(follow / name, lane, plan)
    selected = {}
    for chain, evidence in panels.items():
        picked = select(evidence["candidates"])
        assert plan["picked"][chain] == picked
        selected[chain] = {
            tx: reconcile(
                chain, evidence["chain_id"], raw, details[chain][tx], looked_up[chain + "-trace"]
            )
            for tx, raw in looked_up[chain + "-envelopes"].items()
            if tx in picked
        }
    return {
        "schema": "oasis.y2026-eth-bsc-fresh.v1",
        "scope_sha256": sha_file(D / "scope.json"),
        "follow_scope_sha256": sha_file(follow / "scope.json"),
        "catalog_sha256": sha_file(OLD_CATALOG),
        "prior_reports": {p.name: sha_file(p) for p in EARLIER},
        "usd": 0,
        "requests": sum(p["requests"] for p in panels.values())
        + sum(len(p["proofs"]) for p in proof.values()),
        "chains": panels,
        "follow": {"proof": proof, "selected": selected},
    }


if __name__ == "__main__":
    print(json.dumps(main(), sort_keys=True, indent=2))
