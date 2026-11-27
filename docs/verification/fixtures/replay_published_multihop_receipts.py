"""Audit amount-matched published loops using retained public receipts, fees and transfers."""

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from itertools import pairwise
from pathlib import Path

from replay_published_multihop_shards import SHARDS, closed_exact, published_rows, summarize

from oasis_agent.arbitrage.cycle_protocols import topic
from oasis_agent.rpc.budget import BudgetPolicy, RpcBudget
from oasis_agent.rpc.protocol import strict_json_object

TRANSFER = topic("Transfer(address,address,uint256)")
V2 = topic("Swap(address,uint256,uint256,uint256,uint256,address)")
V3 = topic("Swap(address,address,int256,int256,uint160,uint128,int24)")
BALANCER = topic("Swap(bytes32,address,address,uint256,uint256)")
ZKSYNC_VAULT = "0x621425a1ef6abe91058e9712575dcc4258f8d091"
ROOT_FIELDS = (
    "blockHash",
    "blockNumber",
    "transactionIndex",
    "transactionHash",
    "from",
    "to",
    "status",
    "gasUsed",
)
LOG_FIELDS = ("address", "data", "topics", "logIndex")


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def wire(directory, label, method, params):
    request = (directory / f"{label}.request.json").read_bytes()
    response = (directory / f"{label}.response.json").read_bytes()
    meta = json.loads((directory / f"{label}.meta.json").read_bytes())
    assert (digest(request), digest(response)) == (meta["request_sha256"], meta["response_sha256"])
    assert meta["started_ns"] <= meta["returned_ns"] and meta["http"] == 200
    assert strict_json_object(request) == {
        "method": method,
        "params": params,
        "jsonrpc": "2.0",
        "id": 1,
    }
    obj = strict_json_object(response)
    assert obj["id"] == 1 and obj["jsonrpc"] == "2.0"
    return obj.get("result"), digest(response)


def validate_scope(root, name, scope_name):
    scope = json.loads((root / scope_name).read_bytes())
    assert scope["usd"] == 0 and scope["selection_sha256"] == digest(
        (root / "selection.json").read_bytes()
    )
    stops = {}
    for key, spec in scope["requests_by_source"].items():
        path = root / name / key
        stop = json.loads((path / "stop.json").read_bytes())
        with RpcBudget(
            path / "original.sqlite3", (BudgetPolicy(key, len(spec["calls"]), 300),), read_only=True
        ) as budget:
            count = budget.counts()[key]
        assert count == stop["reservations"] == len(list(path.glob("*.request.json")))
        for label, method, args in spec["calls"][:count]:
            raw, _ = wire(path, label, method, args)
            if method == "eth_chainId":
                assert int(raw, 16) == spec["id"]
            elif raw is None:
                assert stop["stop"] and label == spec["calls"][count - 1][0]
        stops[key] = stop
    return stops


def indexed_address(word):
    assert len(word) == 66 and int(word[2:26], 16) == 0
    return "0x" + word[-40:].lower()


def leg_amounts(leg, log):
    sig, *topics = log["topics"]
    data = bytes.fromhex(log["data"][2:])
    assert leg["exchange"].lower() == log["address"].lower()
    if sig == BALANCER:
        assert len(data) == 64 and len(topics) == 3
        assert [indexed_address(w) for w in topics[1:]] == [
            leg[k].lower() for k in ("in_token", "out_token")
        ]
        incoming, outgoing = int.from_bytes(data[:32]), int.from_bytes(data[32:])
    elif sig == V3:
        assert len(data) == 160 and len(topics) == 2
        a, b = (int.from_bytes(data[i : i + 32], signed=True) for i in (0, 32))
        assert a * b < 0
        incoming, outgoing = max(a, b), -min(a, b)
    else:
        assert sig == V2 and len(data) == 128 and len(topics) == 2
        a, b, c, d = (int.from_bytes(data[i : i + 32]) for i in (0, 32, 64, 96))
        assert (a > 0 and d > 0 and b == c == 0) or (b > 0 and c > 0 and a == d == 0)
        incoming, outgoing = a + b, c + d
    assert (incoming, outgoing) == (int(leg["in_amount"]), int(leg["out_amount"]))
    return sig


def shared_vault_groups(groups, logs, actor):
    """Receipt-level cash custody on chain 324. Internal vault pool balances remain unknown."""
    swap_logs = sorted(i for i, log in logs.items() if log["topics"][0] in (V2, V3, BALANCER))
    uses = Counter(leg["index"] for legs in groups for leg in legs)
    actor_moves = []
    for i, log in sorted(logs.items()):
        topics = log["topics"]
        if len(topics) != 3 or topics[0] != TRANSFER or len(log["data"]) != 66:
            continue
        sender, receiver = map(indexed_address, topics[1:])
        if actor in (sender, receiver):
            actor_moves.append((i, log["address"].lower(), sender, receiver, int(log["data"], 16)))
    evidence = []
    for number, legs in enumerate(groups, 1):
        if len(legs) < 2 or not closed_exact(legs):
            continue
        indices = [leg["index"] for leg in legs]
        first, last = indices[0], indices[-1]
        if indices != [i for i in swap_logs if first <= i <= last]:
            continue
        if any(
            uses[i] != 1
            or logs[i]["topics"][0] != V2
            or [indexed_address(w) for w in logs[i]["topics"][1:]] != [actor, actor]
            or logs[i]["address"].lower() in (ZKSYNC_VAULT, actor)
            for i in indices
        ):
            continue
        anchor = legs[0]["in_token"].lower()
        principal, payout = int(legs[0]["in_amount"]), int(legs[-1]["out_amount"])
        before = max((i for i in swap_logs if i < first), default=-1)
        after = min((i for i in swap_logs if i > last), default=max(logs) + 1)
        deposits = [m for m in actor_moves if before < m[0] < first and m[1] == anchor]
        returns = [m for m in actor_moves if last < m[0] < after and m[1] == anchor]
        funding = (anchor, actor, ZKSYNC_VAULT, principal)
        settlement = (anchor, ZKSYNC_VAULT, actor, payout)
        if [m[1:] for m in deposits].count(funding) != 1 or deposits[-1][1:] != funding:
            continue
        if [m[1:] for m in returns].count(settlement) != 1 or returns[0][1:] != settlement:
            continue
        edges = []
        for left, right in pairwise(legs):
            lo, hi = left["index"], right["index"]
            token, amount = left["out_token"].lower(), int(left["out_amount"])
            moves = [m for m in actor_moves if lo < m[0] < hi]
            if [m[1:] for m in moves] != [
                (token, ZKSYNC_VAULT, actor, amount),
                (token, actor, ZKSYNC_VAULT, amount),
            ]:
                break
            edges.append(
                {
                    "swap_log_indices": [lo, hi],
                    "vault_to_executor_log": moves[0][0],
                    "executor_to_vault_log": moves[1][0],
                    "token": token,
                    "amount_raw": amount,
                }
            )
        if len(edges) == len(legs) - 1:
            evidence.append(
                {
                    "group_number": number,
                    "executor": actor,
                    "vault": ZKSYNC_VAULT,
                    "anchor_token": anchor,
                    "principal_raw": principal,
                    "principal_transfer_log": deposits[-1][0],
                    "payout_raw": payout,
                    "payout_transfer_log": returns[0][0],
                    "edges": edges,
                }
            )
    return evidence


def audit_groups(groups, receipt, actor, chain_id):
    logs = {int(x["logIndex"], 16): x for x in receipt["logs"]}
    assert len(logs) == len(receipt["logs"])
    sent, received = defaultdict(list), defaultdict(list)
    touched = defaultdict(list)
    deltas = Counter()
    for i, x in logs.items():
        assert (
            x["transactionHash"] == receipt["transactionHash"]
            and x["blockHash"] == receipt["blockHash"]
            and not x.get("removed", False)
        )
        if len(x["topics"]) != 3 or x["topics"][0] != TRANSFER or len(x["data"]) != 66:
            continue
        a, b = map(indexed_address, x["topics"][1:])
        token = x["address"].lower()
        amount = int(x["data"], 16)
        sent[token, amount, a].append((i, b))
        received[token, amount, b].append((i, a))
        for acct in {a, b}:
            touched[token, acct].append(i)
        if actor in (a, b):
            deltas[token] += amount * ((actor == b) - (actor == a))
    counts = Counter()
    gross = Counter()
    seen = set()
    for legs in groups:
        edges_ok = True
        for leg in legs:
            idx = leg["index"]
            leg_amounts(leg, logs[idx])
            seen.add(idx)
        for a, b in pairwise(legs):
            token = a["out_token"].lower()
            value = int(a["out_amount"])
            if token != b["in_token"].lower() or value != int(b["in_amount"]):
                counts["amount_or_token_gap"] += 1
                edges_ok = False
                continue
            outgoing = sent[token, value, a["exchange"].lower()]
            incoming = received[token, value, b["exchange"].lower()]
            if len(outgoing) == len(incoming) == 1:
                (i, payee), (j, payer) = outgoing[0], incoming[0]
                if i == j:
                    counts["direct_unique_transfer"] += 1
                    continue
                if i < j and payee == payer and not any(i < t < j for t in touched[token, payee]):
                    counts["unique_transfer_pair_through_holder"] += 1
                    continue
            edges_ok = False
            counts["transfer_unresolved"] += 1
        if closed_exact(legs):
            anchor = legs[0]["in_token"].lower()
            gross[anchor] += int(legs[-1]["out_amount"]) - int(legs[0]["in_amount"])
            counts["closed_amount_groups"] += 1
            counts["closed_amount_groups_with_all_intermediate_transfers"] += int(edges_ok)
    custody = {}
    if chain_id == 324 and actor != ZKSYNC_VAULT:
        proofs = shared_vault_groups(groups, logs, actor)
        if proofs:
            counts["closed_groups_with_all_shared_vault_transfers"] = len(proofs)
            custody["shared_vault_custody_groups"] = proofs
    return {
        **custody,
        "distinct_events_amounts_matched": len(seen),
        "intermediate_evidence": dict(counts),
        "group_output_minus_input_raw_by_anchor": dict(gross),
        "top_level_recipient_transfer_log_net_raw": {k: v for k, v in deltas.items() if v},
    }


def inspect(root, chain, info, record):
    tx = info["tx_hash"]
    recs = []
    wire_hashes = {}
    headers = []
    for lane in range(2):
        path = root / "rpc" / f"{chain}-{lane}"
        label = f"{tx}-receipt"
        if not (path / f"{label}.response.json").exists():
            continue
        result, wire_hashes[f"receipt_{lane}"] = wire(
            path, label, "eth_getTransactionReceipt", [tx]
        )
        if result is None:
            continue
        recs.append(result)
        if lane == 0:
            hdr, wire_hashes["header"] = wire(
                path, f"{tx}-header", "eth_getBlockByNumber", [hex(info["block"]), False]
            )
            assert (
                hdr["transactions"][info["transaction_index"]] == tx
                and hdr["hash"] == result["blockHash"]
            )
            headers.append(hdr["hash"])
    receipt = recs[0]
    assert all(receipt[k] == r[k] for r in recs for k in ROOT_FIELDS)
    for r in recs[1:]:
        assert [[x[k] for k in LOG_FIELDS] for x in receipt["logs"]] == [
            [x[k] for k in LOG_FIELDS] for x in r["logs"]
        ]
    assert (tx, info["block"], info["transaction_index"], 1) == (
        receipt["transactionHash"],
        *(int(receipt[k], 16) for k in ("blockNumber", "transactionIndex", "status")),
    )
    assert receipt["from"].lower() == record["transaction"]["from"].lower()
    assert (receipt["to"] or "").lower() == (record["transaction"]["to"] or "").lower()
    actor = (receipt["to"] or receipt["contractAddress"]).lower()
    gas = int(receipt["gasUsed"], 16)
    if chain == "optimism":
        prices = []
        for lane in range(2):
            block, wire_hashes[f"legacy_fee_block_{lane}"] = wire(
                root / "op-fee" / f"op-fee-{lane}",
                f"{tx}-block",
                "eth_getBlockByHash",
                [receipt["blockHash"], True],
            )
            t = block["transactions"][info["transaction_index"]]
            assert block["hash"] == t["blockHash"] == receipt["blockHash"] and t["hash"] == tx
            assert t.get("type", "0x0") == "0x0" and t["from"].lower() == receipt["from"].lower()
            prices.append(int(t["gasPrice"], 16))
        assert len(set(prices)) == 1
        price = prices[0]
        l1 = int(receipt["l1Fee"], 16)
        assert all(int(r["l1Fee"], 16) == l1 for r in recs)
    else:
        price = int(receipt["effectiveGasPrice"], 16)
        l1 = 0
    return {
        **info,
        "block_hash": receipt["blockHash"],
        "matching_receipts": len(recs),
        "header_tx_position_seen": bool(headers),
        "logs": len(receipt["logs"]),
        "gas_used": gas,
        "gas_price_wei": price,
        "separately_added_op_l1_fee_wei": l1,
        "observed_network_fee_wei": gas * price + l1,
        "currency": "ETH",
        "entity_net": None,
        "transfer_log_account": actor,
        **audit_groups(
            [g["swaps"] for g in record["arbitrages"]], receipt, actor, info["chain_id"]
        ),
        "wire_sha256": wire_hashes,
    }


def main():
    if not __debug__:
        raise SystemExit("run this verifier without Python optimization")
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("research_root", type=Path)
    root = p.parse_args().research_root
    selected = summarize(root)
    assert json.loads((root / "selection.json").read_bytes()) == json.loads(json.dumps(selected))
    stops = validate_scope(root, "rpc", "verification-scope.json") | validate_scope(
        root, "op-fee", "op-fee-scope.json"
    )
    audited = {}
    for chain in SHARDS:
        v = selected["shards"][chain]
        ids = {
            v[k]["tx_hash"] for k in ("highest_event_count", "longest_amount_matched_closed_group")
        }
        audited[chain] = [
            inspect(root, chain, r, record)
            for r, record in published_rows(root, chain)
            if r["tx_hash"] in ids
        ]
        assert len(audited[chain]) == len(ids)
    print(
        json.dumps(
            {**selected, "audited": audited, "source_stops": stops}, indent=2, sort_keys=True
        )
    )


if __name__ == "__main__":
    main()
