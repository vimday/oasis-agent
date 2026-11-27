"""Recompute 2026 disjoint block panels, audit selected receipts and reconcile cash."""

import json
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

from eth_account import Account
from eth_account.typed_transactions import TypedTransaction
from eth_hash.auto import keccak
from replay_multihop_second_shard_receipts import user_transaction
from replay_y2026_bsc_rh_landed import keys, matches, rooted, summarize_fee
from replay_y2026_eth_bsc_untouched import PATH_TOPICS
from replay_y2026_eth_bsc_windows import WBNB, WITHDRAWAL
from replay_y2026_inventory import digest, read
from replay_y2026_l2_funding_holdout import TOPIC_NAMES, add_window, ranked, verify_header
from replay_y2026_op_arb import receipt_commitment, wire
from replay_y2026_open_tail import observed_fee, receipt_paths, row_roles

from oasis_agent.arbitrage.cycle_protocols import abi, data_bytes
from oasis_agent.evidence.public_receipts import decode_receipt
from oasis_agent.evidence.public_receipts import quantity as q
from oasis_agent.evidence.research_traces import first_failure, nested_calls
from oasis_agent.evidence.swap_flow_trace import _receipt_positions
from oasis_agent.models.common import BlockRef
from oasis_agent.rpc.budget import BudgetPolicy, RpcBudget
from scripts.replay_eth_tail_case import _settlement

ROOT = Path(__file__).resolve().parents[1]
D = ROOT.parents[1] / "reports/research/y2026-multipanel-expanded-20261128"
SCOPES = {
    "l1": "63d926ca24d414dff3bf2e85dd91d213652bf7deac9cabdbbb71884a37f6ebb8",
    "l2": "a4378ca0013faf974a4bb6c1ac749ad253601d1fa19a0983303671c15565462c",
    "follow": "03cb15073c6692187e044135bc8d60c871d2eb1fd1b6bba2093c3ccc9d3ad3d5",
}
TIERS = (1, 2, 3, 5, 10, 20, 50, 100, 200)


class Evidence:
    """Consume only the reserved requests and retain their original failure boundary."""

    def __init__(self, path, scope, time_field, policies):
        self.path = path
        self.stop = read(path / "STOP.json")
        self.scope = scope
        self.frozen = time_field
        self.items = {}
        assert self.stop["scope"] == SCOPES[scope]
        with RpcBudget(path / "original.sqlite3", policies, read_only=True) as ledger:
            assert ledger.counts() == self.stop["counts"]
        self.requests = sum(self.stop["counts"].values())
        assert self.requests == len(list(path.glob("*.request.json")))

    def get(self, label, method, args, url):
        name = self.path / label
        req = name.with_suffix(".request.json")
        if not req.is_file():
            assert self.stop["error"]
            return None
        meta = read(name.with_suffix(".meta.json"))
        assert self.frozen <= meta["started_utc"] <= meta["finished_utc"]
        assert meta["finished_utc"] <= (self.stop.get("utc") or self.stop["finished"])
        assert label not in self.items and meta["url"] == url
        assert read(req) == {
            "jsonrpc": "2.0",
            "id": meta["id"],
            "method": method,
            "params": args,
        }
        assert digest(req) == meta["request_sha256"]
        raw = name.with_suffix(".response")
        assert digest(raw) == meta["response_sha256"] and raw.stat().st_size == meta["bytes"]
        if meta["http"] == 200:
            result, meta = wire(self.path, label, method, args)
        else:
            result = None
        if result is None:
            assert self.stop["error"] and (meta["http"] != 200 or meta.get("error"))
        self.items[label] = meta
        return result

    def finish(self):
        assert len(self.items) == self.requests
        return {"stop": self.stop, "requests": self.items}


def tx_details(name, r, market_count, verified):
    network = (
        observed_fee(name, r)
        if name in ("optimism", "arbitrum", "era")
        else summarize_fee(r)["receipt_fee_wei"]
    )
    graph = receipt_paths(r, TOPIC_NAMES | PATH_TOPICS)
    return {
        "transaction": r["transactionHash"],
        "block": q(r["blockNumber"]),
        "block_hash": r["blockHash"],
        "index": q(r["transactionIndex"]),
        "fee_payer": r["from"],
        "recipient": r["to"],
        "market_events": market_count,
        "receipt_verification": verified,
        "funding": graph,
        "network_fee": network,
    }


def bands(tallies):
    return {edge: sum(v for k, v in tallies.items() if k >= edge) for edge in TIERS}


def l1_panel(name, spec, plan, prior):
    expected = spec["width"] * len(spec["offsets"])
    policies = (BudgetPolicy("h", expected + 2, 250), BudgetPolicy("r", expected + 1, 250))
    calls = Evidence(D / "l1" / name, "l1", plan["utc"], policies)
    cid = spec["chain"]
    for channel in ("h", "r"):
        assert q(calls.get(channel + "-id", "eth_chainId", [], spec[channel])) == cid
    latest = calls.get("anchor", "eth_getBlockByNumber", ["latest", False], spec["h"])
    blocks, cache, candidates = [], {}, {}
    heights = q(latest["number"])
    known_heights = {b["height"] for b in prior}
    all_tx = set()
    counts = Counter()
    for offset in spec["offsets"]:
        parent = None
        for height in range(heights - offset, heights - offset + spec["width"]):
            assert height not in known_heights
            known_heights.add(height)
            h = calls.get(f"h{height}", "eth_getBlockByNumber", [hex(height), False], spec["h"])
            assert q(h["number"]) == height and (parent is None or parent == h["parentHash"])
            parent = h["hash"]
            rows = calls.get(f"r{height}", "eth_getBlockReceipts", [parent], spec["r"])
            assert [r["transactionHash"] for r in rows] == h["transactions"]
            stamp = datetime.fromtimestamp(q(h["timestamp"]), UTC)
            assert stamp.year == 2026
            if cid == 1:
                _, root = receipt_commitment(cid, h, rows)
                block = {
                    "height": height,
                    "hash": parent,
                    "receipts_root": root,
                    "transactions": len(rows),
                    "reverts": sum(q(r["status"]) == 0 for r in rows),
                }
            else:
                block = rooted(cid, h, rows)
            blocks.append(block | {"utc": stamp.isoformat()})
            for r in rows:
                key = r["transactionHash"]
                assert key not in all_tx
                all_tx.add(key)
                n = sum(matches(e, plan["topics"]) for e in r["logs"])
                counts[n] += 1
                if n >= 2:
                    candidates[key] = tx_details(name, r, n, "full_receipt_root_and_header")
                    cache[key] = h, r
    assert [x["height"] for x in blocks] == calls.stop["heights"] and not calls.stop["error"]
    return {
        "blocks": blocks,
        "histogram": counts,
        "thresholds": bands(counts),
        "all_multi_event": candidates,
        "evidence": calls.finish(),
    }, cache


def l2_panel(name, spec, plan, prior_windows):
    cid, primary, secondary, width, offsets, _ = spec
    path = D / "l2" / name
    sc = Evidence(
        path / "index",
        "l2",
        plan["frozen"],
        (BudgetPolicy("index", plan["index_limit"], plan["interval_ms"]),),
    )
    assert q(sc.get("chain", "eth_chainId", [], primary)) == cid
    latest = sc.get("anchor", "eth_getBlockByNumber", ["latest", False], primary)
    verify_header(cid, latest)
    scans, groups, counts = [], defaultdict(list), []
    selected = read(path / "selection.json")
    for offset in offsets:
        hi = q(latest["number"]) - offset
        lo = hi - width + 1
        h0 = sc.get(f"head-{offset}-0", "eth_getBlockByNumber", [hex(lo), False], primary)
        if h0 is None:
            break
        h1 = sc.get(f"head-{offset}-1", "eth_getBlockByNumber", [hex(hi), False], primary)
        if h1 is None:
            break
        for h, number in ((h0, lo), (h1, hi)):
            assert q(h["number"]) == number
            verify_header(cid, h)
        filters = {
            "fromBlock": hex(lo),
            "toBlock": hex(hi),
            "topics": [sorted(set(plan["topics"]) | set(plan["financing"]))],
        }
        events = sc.get(f"logs-{offset}", "eth_getLogs", [filters], primary)
        if events is None:
            break
        assert all(hi < w["first"] or lo > w["last"] for w in scans + prior_windows)
        scans.append(add_window(groups, events, lo, hi, [h0, h1], plan))
        counts.append(offset)
    assert counts == selected["completed_windows"]
    found, cache, evidence = [], {}, {"index": sc.finish()}
    detail = Evidence(
        path / "detail",
        "l2",
        plan["frozen"],
        (BudgetPolicy("detail", plan["detail_limit"], plan["interval_ms"]),),
    )
    assert q(detail.get("chain", "eth_chainId", [], secondary)) == cid
    for row in ranked(groups, plan, 4):
        h, tx = row["height"], row["tx"]
        hash_ = groups[tx][0]["blockHash"]
        head = detail.get(f"h{h}", "eth_getBlockByHash", [hash_, False], primary)
        if head is None:
            break
        verify_header(cid, head)
        assert head["hash"] == hash_
        receipts = detail.get(
            f"r{h}", "eth_getBlockReceipts", [hex(h) if cid == 324 else hash_], secondary
        )
        if receipts is None:
            break
        if cid == 324:
            independent = detail.get(f"t{h}", "eth_getTransactionReceipt", [tx], primary)
            if independent is None:
                break
        else:
            independent = None
        entry, receipt = verify_l2_case(name, cid, head, receipts, tx, groups, plan, independent)
        found.append(entry)
        cache[tx] = head, receipt
    assert [(r["block"], r["transaction"]) for r in found] == [
        (x["height"], x["tx"]) for x in selected["details"]
    ]
    evidence["detail"] = detail.finish()
    tally = Counter(sum(matches(e, plan["topics"]) for e in g) for g in groups.values())
    candidates = [
        {
            "transaction": tx,
            "block": q(g[0]["blockNumber"]),
            "block_hash": g[0]["blockHash"],
            "index": q(g[0]["transactionIndex"]),
            "market_events": sum(matches(e, plan["topics"]) for e in g),
        }
        for tx, g in groups.items()
        if sum(matches(e, plan["topics"]) for e in g) >= 2
    ]
    return (
        {
            "windows": scans,
            "histogram": tally,
            "thresholds": bands(tally),
            "all_multi_event": candidates,
            "detail": found,
            "evidence": evidence,
        },
        cache,
        groups,
    )


def verify_l2_case(name, cid, block, rows, tx, groups, plan, independent=None):
    assert [r["transactionHash"] for r in rows] == block["transactions"]
    r = rows[q(groups[tx][0]["transactionIndex"])]
    assert r["transactionHash"] == tx and r["blockHash"] == block["hash"]
    if cid in (4663, 42161):
        rooted(cid, block, rows)
    elif cid == 10:
        receipt_commitment(cid, block, rows)
    else:
        assert cid == 324 and independent == r
    check_events(r, groups, plan)
    evidence = "ordered_receipt_root" if cid != 324 else "independent_individual_receipt"
    stats = tx_details(name, r, sum(matches(e, plan["topics"]) for e in r["logs"]), evidence)
    return stats, r


def check_events(r, groups, plan):
    topics = set(plan["topics"]) | set(plan["financing"])
    tx = r["transactionHash"]
    assert [keys(x) for x in r["logs"] if matches(x, topics)] == [keys(x) for x in groups[tx]]


def eth_balance(diff, actor):
    # prestateTracer diffMode may omit an unchanged field or the entire account.
    before = diff["pre"].get(actor, {}).get("balance")
    after = diff["post"].get(actor, {}).get("balance")
    if before is None:
        assert after is None
        return None
    return (q(after) if after is not None else q(before)) - q(before)


def signed_bytes(tx, cid):
    kind = q(tx["type"])
    if kind in (0, 2):
        return user_transaction(tx, cid)
    assert kind == 4 and q(tx["chainId"]) == cid and q(tx["v"]) == q(tx["yParity"]) in (0, 1)
    fields = {
        key: tx[key]
        for key in (
            "type",
            "chainId",
            "nonce",
            "maxPriorityFeePerGas",
            "maxFeePerGas",
            "gas",
            "accessList",
            "v",
            "r",
            "s",
            "authorizationList",
        )
    }
    fields.update(to=data_bytes(tx["to"]), value=q(tx["value"]), data=data_bytes(tx["input"]))
    return TypedTransaction.from_dict(fields).encode()


def bsc_self_settlement(tx, r):
    actor = r["from"]
    assert actor == r["to"] == tx["from"] == tx["to"]
    assert q(tx["type"]) == 4 and q(r["effectiveGasPrice"]) == q(tx["maxFeePerGas"]) == 0
    (token,) = row_roles(r)
    (withdraw,) = [
        log
        for log in r["logs"]
        if log["address"] == WBNB
        and log["topics"][0] == WITHDRAWAL
        and log["topics"][1][-40:] == actor[2:]
    ]
    (amount,) = abi(("uint256",), data_bytes(withdraw["data"]))
    assert token == {"address": actor, "token": WBNB, "transfer_delta_raw": amount}
    return {
        "wrapped_native_before_unwrap_wei": amount,
        "unwrap_log": q(withdraw["logIndex"]),
        "network_fee_wei": 0,
        "verified_native_delta_wei": None,
    }


def trace_cash(name, block, r, tx, trace, balance):
    h = r["transactionHash"]
    cid = 1 if name == "ethereum" else 10
    assert tx["hash"] == h and tx["blockHash"] == r["blockHash"]
    kind = q(tx["type"])
    raw = signed_bytes(tx, cid)
    assert "0x" + keccak(raw).hex() == h
    assert Account.recover_transaction(raw).lower() == r["from"] == tx["from"]
    assert tx["to"] == r["to"] and q(tx["transactionIndex"]) == q(r["transactionIndex"])
    ref = BlockRef(
        chain_id=cid,
        number=q(block["number"]),
        block_hash=block["hash"],
        timestamp=q(block["timestamp"]),
    )
    receipt = decode_receipt(r, ref, h, q(r["transactionIndex"]))
    frames = nested_calls(trace)
    _receipt_positions(trace, frames, receipt)
    assert frames[()].sender == r["from"] and frames[()].recipient == r["to"]
    assert frames[()].value == q(tx["value"]) and q(trace["gasUsed"]) == q(r["gasUsed"])
    native = [
        {"from": f.sender, "to": f.recipient, "amount": f.value, "path": path}
        for path, f in sorted(frames.items())
        if f.kind == "CALL" and f.value and first_failure(frames, path) is None
    ]
    balances = {a: eth_balance(balance, a) for a in {r["from"], r["to"]} if a}
    out = {
        "sender": r["from"],
        "recipient": r["to"],
        "tx_type": kind,
        "signature_recovered": True,
        "visible_native_delta_wei": balances,
        "verified_log_count": len(r["logs"]),
        "native_value_transfers": native,
        "role_token_transfers": row_roles(r),
    }
    if cid == 1:
        out["settlement"] = _settlement(receipt, trace, block["miner"])
        for actor, delta in balances.items():
            net = sum(
                d["delta_after_receipt_gas"]
                for d in out["settlement"]["visible_role_deltas"]
                if d["account"] == actor and d["asset"] == "ETH"
            )
            assert (delta is None and net == 0) or delta == net
    return out


def follow(plan, l2_plan, caches, groups):
    result = {}
    for name, spec in sorted(plan["lanes"].items()):
        folder = D / "follow" / name
        cid = spec["chain_id"]
        lane = Evidence(
            folder,
            "follow",
            plan["utc"],
            (BudgetPolicy(name, len(spec["calls"]), plan["interval_ms"]),),
        )
        replies = {}
        for label, method, args, endpoint in spec["calls"]:
            value = lane.get(label, method, args, endpoint)
            if value is None:
                break
            replies[label] = value
        assert q(replies.pop("chain")) == cid
        with_cash = {}
        for label, body in replies.items():
            if name == "era":
                assert (
                    body["transactionHash"] == label
                    and body["blockHash"] == groups[name][label][0]["blockHash"]
                )
                check_events(body, groups[name], l2_plan)
                with_cash[label] = tx_details(
                    name,
                    body,
                    sum(matches(e, l2_plan["topics"]) for e in body["logs"]),
                    "single_source_receipt_cross_checked_with_index",
                )
            elif name == "robinhood":
                if not label.endswith("-header"):
                    continue
                tx = label.removesuffix("-header")
                rows = replies.get(tx + "-receipts")
                if rows is not None:
                    verify_header(cid, body)
                    entry, _ = verify_l2_case(name, cid, body, rows, tx, groups[name], l2_plan)
                    with_cash[tx] = entry
            elif "-" not in label:
                block, receipt = caches[name][label]
                assert body["hash"] == label and body["blockHash"] == receipt["blockHash"]
                if label + "-balance" in replies:
                    with_cash[label] = trace_cash(
                        name,
                        block,
                        receipt,
                        body,
                        replies[label + "-cash"],
                        replies[label + "-balance"],
                    )
                else:
                    raw = signed_bytes(body, cid)
                    assert "0x" + keccak(raw).hex() == label
                    assert Account.recover_transaction(raw).lower() == receipt["from"]
                    with_cash[label] = {
                        "sender": receipt["from"],
                        "recipient": receipt["to"],
                        "signature_recovered": True,
                        "visible_native_delta_wei": None,
                    }
                    if name == "bsc":
                        with_cash[label]["wrapped_settlement"] = bsc_self_settlement(body, receipt)

        result[name] = {"detail": with_cash, "evidence": lane.finish()}
    return result


def main():
    plans = {}
    for name, sig in SCOPES.items():
        p = D / name / "scope.json"
        assert digest(p) == sig
        plans[name] = read(p)
        assert digest(D / name / "capture.py") == plans[name]["driver"]
        assert digest(D.parent / "y2026-op-arb-20261127/capture.py") == plans[name]["transport"]
    assert plans["follow"]["dependencies"] == {k: SCOPES[k] for k in ("l1", "l2")}
    l1_plan, l2_plan = plans["l1"], plans["l2"]
    prev_l1 = read(ROOT / "Y2026_ETH_BSC_UNTOUCHED_20261128.json")["evidence"]
    prev_l2 = read(ROOT / "Y2026_L2_FUNDING_HOLDOUT_20261128.json")["chains"]
    orig = read(ROOT / "Y2026_OPEN_TAIL_CASH_20261127.json")["chains"]
    panels, caches, groups = {}, {}, {}
    for name, spec in l1_plan["specs"].items():
        panels[name], caches[name] = l1_panel(name, spec, l1_plan, prev_l1[name]["blocks"])
    for name, spec in l2_plan["networks"].items():
        old = prev_l2[name]["windows"] + orig.get(name, {}).get("windows", [])
        panels[name], caches[name], groups[name] = l2_panel(name, spec, l2_plan, old)
    selected = follow(plans["follow"], l2_plan, caches, groups)
    return {
        "schema": "oasis.y2026-historical-expanded.v1",
        "scope_hashes": SCOPES,
        "lanes": panels,
        "followup": selected,
        "rpc_spend_usd": 0,
    }


if __name__ == "__main__":
    print(json.dumps(main(), indent=2, sort_keys=True))
