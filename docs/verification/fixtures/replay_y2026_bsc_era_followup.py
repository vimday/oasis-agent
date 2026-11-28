"""Reconcile one retained BSC twelve-ring settlement and previously missing Era receipts."""

import argparse
import json
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from eth_hash.auto import keccak
from replay_y2026_bsc_rh_landed import keys, matches, rooted
from replay_y2026_era_window import BOOT, ETH, rebuild_hash
from replay_y2026_eth_bsc_fresh import reconcile
from replay_y2026_eth_bsc_untouched import PATH_TOPICS
from replay_y2026_eth_bsc_windows import WBNB, WITHDRAWAL
from replay_y2026_l2_fresh import FINANCE, FOLLOW_UP, Lane, digest, read
from replay_y2026_op_arb import moves, wire
from replay_y2026_open_tail import receipt_paths

from oasis_agent.arbitrage.cycle_protocols import data_bytes
from oasis_agent.evidence.public_receipts import decode_receipt
from oasis_agent.evidence.public_receipts import quantity as q
from oasis_agent.evidence.research_traces import first_failure, nested_calls
from oasis_agent.models.common import BlockRef, canonical_json, sha256_hex
from scripts.capture_public_rpc import durable_new
from scripts.replay_eth_tail_case import _receipt_positions

ROOT = Path(__file__).resolve().parents[3]
REPORTS = ROOT / "reports/research"
ERA_PRIOR = REPORTS / "y2026-l2-fresh-follow-20261128"
BSC_PRIOR = REPORTS / "y2026-eth-bsc-fresh-20261128"
BSC_SUMMARY = ROOT / "docs/verification/Y2026_ETH_BSC_FRESH_20261128.json"
BSC_HASH = "0xf79cff4482a38e6157510d25483a20f5e89d2c7c6be1ac8e754a9afac31f7abc"
BSC_HEIGHT = 120509259
ERA_WETH = FOLLOW_UP["era"][-1]
SOURCES = {
    "triport": (56, "https://triport.io/public/bsc"),
    "onfinality": (324, "https://zksync.api.onfinality.io/public"),
    "era_drpc": (324, "https://zksync.drpc.org"),
}
CORROBORATION = {"era_1rpc": (324, "https://public.1rpc.io/zksync2-era")}
BUDGET_INTERVAL_MS = 320


def era_index():
    scope = read(ERA_PRIOR / "scope.json")
    scan = ERA_PRIOR / "era/scan"
    width, offsets = scope["networks"]["era"][3:5]
    topics = scope["index_topics"]
    groups = defaultdict(list)

    def index(ask):
        assert q(ask("chain", "eth_chainId", [])) == 324
        anchor = ask("head", "eth_getBlockByNumber", ["latest", False])
        assert rebuild_hash(anchor) == anchor["hash"]
        for offset in offsets:
            last = q(anchor["number"]) - offset
            first = last - width + 1
            for n in (first, last):
                edge = ask(f"head-{n}", "eth_getBlockByNumber", [hex(n), False])
                assert rebuild_hash(edge) == edge["hash"] and q(edge["number"]) == n
            filters = {
                "fromBlock": hex(first),
                "toBlock": hex(last),
                "topics": [sorted(set(topics) | FINANCE)],
            }
            for log in ask(f"logs-{first}", "eth_getLogs", [filters]):
                if matches(log, topics):
                    assert first <= q(log["blockNumber"]) <= last and not log["removed"]
                    groups[log["transactionHash"]].append(log)

    status = Lane(scan, "scan", scope["networks"]["era"][1], False, scope).run(index)
    assert status["failure"] is None
    cases = sorted(
        (g for g in groups.values() if len(g) >= 2),
        key=lambda g: (-len(g), -len({x["address"] for x in g}), *keys(g[0])[0:3:2]),
    )
    assert len(cases) == 27
    return {logs[0]["transactionHash"]: logs for logs in cases}, topics


def bsc_prior():
    scope = read(BSC_PRIOR / "scope.json")
    folder = BSC_PRIOR / "bsc"
    height = hex(BSC_HEIGHT)
    head, hp = wire(folder, f"h{BSC_HEIGHT}", "eth_getBlockByNumber", [height, False])
    receipts, rp = wire(folder, f"r{BSC_HEIGHT}", "eth_getBlockReceipts", [head["hash"]])
    assert hp["url"] == scope["specs"]["bsc"]["h"] and rp["url"] == scope["specs"]["bsc"]["r"]
    rooted(56, head, receipts)
    assert head["transactions"] == [r["transactionHash"] for r in receipts]
    [receipt] = [r for r in receipts if r["transactionHash"] == BSC_HASH]
    assert q(receipt["transactionIndex"]) == 1
    signed, proof = wire(
        BSC_PRIOR / "follow/bsc-envelopes", BSC_HASH, "eth_getTransactionByHash", [BSC_HASH]
    )
    assert proof["id"] == 2 and proof["url"] == scope["specs"]["bsc"]["h"]
    assert (signed["hash"], signed["from"], signed["to"], signed["blockHash"]) == (
        BSC_HASH,
        receipt["from"],
        receipt["to"],
        head["hash"],
    )
    return head, receipt, signed


def calls_for(name, groups):
    if name == "triport":
        return [
            ("chain", "eth_chainId", []),
            (
                "trace",
                "debug_traceTransaction",
                [
                    BSC_HASH,
                    {
                        "tracer": "callTracer",
                        "tracerConfig": {"withLog": True},
                        "timeout": "10s",
                    },
                ],
            ),
            (
                "diff",
                "debug_traceTransaction",
                [
                    BSC_HASH,
                    {
                        "tracer": "prestateTracer",
                        "tracerConfig": {"diffMode": True},
                        "timeout": "10s",
                    },
                ],
            ),
        ]
    calls, heads = [("chain", "eth_chainId", [])], set()
    for tx, logs in groups.items():
        height, hash_, *_ = keys(logs[0])
        if hash_ not in heads:
            calls.append((f"h{height}", "eth_getBlockByHash", [hash_, False]))
            heads.add(hash_)
        calls.append((f"r{tx[2:]}", "eth_getTransactionReceipt", [tx]))
    return calls


def freeze(root, groups, review_of=None):
    stamp = datetime.now(UTC).isoformat()
    sources = CORROBORATION if review_of is not None else SOURCES
    plan = {
        "schema": "oasis.bsc-era-followup.v1",
        "frozen_utc": stamp,
        "capture_sha256": digest(Path(__file__)),
        "previous_bsc_sha256": digest(BSC_SUMMARY),
        "previous_era_scope_sha256": digest(ERA_PRIOR / "scope.json"),
        "rpc_spending_usd": 0,
        "minimum_interval_ms": 900 if review_of is not None else BUDGET_INTERVAL_MS,
        "sources": {name: {"id": cid, "endpoint": url} for name, (cid, url) in sources.items()},
        "calls": {name: calls_for(name, groups) for name in sources},
    }
    if review_of is not None:
        plan["reviews_scope"] = digest(review_of / "scope.json")
    plan["budgets"] = {name: len(calls) for name, calls in plan["calls"].items()}
    root.mkdir()
    durable_new(root / "capture.py", Path(__file__).read_bytes())
    durable_new(root / "scope.json", canonical_json(plan))


def run_lanes(root, plan, groups, capture):
    sources = CORROBORATION if plan.get("reviews_scope") else SOURCES
    assert plan["sources"] == {
        name: {"id": cid, "endpoint": url} for name, (cid, url) in sources.items()
    }

    def one(name, endpoint, chain):
        result = {}

        def action(ask):
            for label, method, args in calls_for(name, groups):
                value = ask(label, method, args)
                if method == "eth_chainId" and q(value) != chain:
                    raise ValueError("wrong source chain")
                result[label] = value

        stop = Lane(root / name, name, endpoint, capture, plan).run(action)
        return result, stop

    assert plan["calls"] == json.loads(json.dumps({k: calls_for(k, groups) for k in sources}))
    with ThreadPoolExecutor() as pool:
        pending = {key: pool.submit(one, key, url, cid) for key, (cid, url) in sources.items()}
        return {key: f.result() for key, f in pending.items()}


def era_case(tx, indexed, lanes, topics):
    h, block_hash, index, *_ = keys(indexed[0])
    headers, receipts = {}, {}
    for source in ("era_drpc", "onfinality", "era_1rpc"):
        if source not in lanes:
            continue
        data, _ = lanes[source]
        head = data.get(f"h{h}")
        receipt = data.get(f"r{tx[2:]}")
        if head is None or receipt is None:
            continue
        assert (q(head["number"]), head["hash"], rebuild_hash(head)) == (h, block_hash, block_hash)
        assert head["transactions"][index] == tx
        assert datetime.fromtimestamp(q(head["timestamp"]), UTC).year == 2026
        assert head["receiptsRoot"] == "0x" + "00" * 32
        assert (
            q(receipt["blockNumber"]),
            receipt["blockHash"],
            q(receipt["transactionIndex"]),
            receipt["transactionHash"],
            q(receipt["status"]),
        ) == (h, block_hash, index, tx, 1)
        logs = [keys(log) for log in receipt["logs"]]
        assert all(log[:4] == [h, block_hash, index, tx] for log in logs)
        assert [log[4] for log in logs] == sorted({log[4] for log in logs})
        assert all(not log["removed"] for log in receipt["logs"])
        assert [keys(x) for x in receipt["logs"] if matches(x, topics)] == [
            keys(x) for x in indexed
        ]
        headers[source] = head
        receipts[source] = receipt
    result = {
        "tx": tx,
        "height": h,
        "tx_index": index,
        "hash": block_hash,
        "market_events": len(indexed),
        "receipt_sources": sorted(receipts),
    }
    if not receipts:
        return result
    receipt = next(iter(receipts.values()))
    fingerprint = {
        key: receipt[key]
        for key in (
            "transactionHash",
            "blockHash",
            "blockNumber",
            "status",
            "type",
            "from",
            "to",
            "effectiveGasPrice",
            "gasUsed",
            "cumulativeGasUsed",
            "logsBloom",
            "logs",
        )
    }
    for second in receipts.values():
        assert all(
            list(map(keys, second[key])) == list(map(keys, value))
            if key == "logs"
            else second[key] == value
            for key, value in fingerprint.items()
        ), f"receipt sources disagree for {tx}"
    for head in headers.values():
        assert head["transactions"] == next(iter(headers.values()))["transactions"]
    fee_logs = [
        (i, src, dst, amount)
        for i, (asset, src, dst, amount) in moves(receipt).items()
        if asset == ETH and BOOT in (src, dst) and amount
    ]
    expected_fee = q(receipt["gasUsed"]) * q(receipt["effectiveGasPrice"])
    payer_balances = Counter()
    for _, src, dst, amount in fee_logs:
        if dst == BOOT:
            payer_balances[src] += amount
        else:
            payer_balances[dst] -= amount
    # A subsidy, fee refund, or system move without a matching total stays unassigned.
    actual_payers = {address: amount for address, amount in payer_balances.items() if amount}
    reconciled = sum(actual_payers.values()) == expected_fee and all(
        amount > 0 for amount in actual_payers.values()
    )
    funding = receipt_paths(receipt, topics)
    participants = {
        receipt["from"],
        receipt["to"],
        *(r["holder"] for r in funding["closed_at_same_holder"]),
    }
    deltas = Counter()
    for asset, src, dst, amount in moves(receipt).values():
        deltas[src, asset] -= amount
        deltas[dst, asset] += amount
    result.update(
        {
            "utc": datetime.fromtimestamp(
                q(next(iter(headers.values()))["timestamp"]), UTC
            ).isoformat(),
            "payer": receipt["from"],
            "recipient": receipt["to"],
            "receipt_log_count": len(receipt["logs"]),
            "receipt_fingerprint_sha256": sha256_hex(canonical_json(fingerprint)),
            "receipt_fee_wei": expected_fee,
            "fee_logs": fee_logs,
            "reconciled_fee_payers": actual_payers if reconciled else None,
            "longest_linked": funding["longest_linked"],
            "closed_cycles": funding["closed_at_same_holder"],
            "participant_transfer_deltas": [
                {"address": account, "token": asset, "delta_raw": delta}
                for (account, asset), delta in sorted(deltas.items())
                if account in participants and delta
            ],
            "weth_closure_minus_full_receipt_fee_wei": [
                r["gross_raw"] - expected_fee
                for r in funding["closed_at_same_holder"]
                if r["asset"] == ERA_WETH
            ],
            "all_cost_owner_net": None,
        }
    )
    return result


def bsc_case(lane):
    head, receipt, signed = bsc_prior()
    parent = read(BSC_SUMMARY)["follow"]["selected"]["bsc"][BSC_HASH]
    prior = reconcile("bsc", 56, signed, (receipt, head, receipt_paths(receipt, PATH_TOPICS)), {})
    assert canonical_json(prior) == canonical_json(parent)
    roles = {receipt["from"], receipt["to"]}
    block = BlockRef(chain_id=56, number=BSC_HEIGHT, block_hash=head["hash"])
    observed = decode_receipt(receipt, block, BSC_HASH, 1)
    claim = parent["closures"]
    wrapped = sum(r["gross_raw"] for r in claim)
    assert len(claim) == 12 and sum(len(r["legs"]) for r in claim) == 36
    assert {(r["holder"], r["asset"]) for r in claim} == {(observed.recipient, WBNB)}
    movements = [
        [i, src, dst, amount]
        for i, (asset, src, dst, amount) in moves(receipt).items()
        if asset == WBNB and (src in roles or dst in roles)
    ]
    executor = observed.recipient
    assert (
        sum(n * ((dst == executor) - (src == executor)) for _, src, dst, n in movements) == wrapped
    )
    assert not any(r["address"] == WBNB and matches(r, {WITHDRAWAL}) for r in receipt["logs"])
    fee = observed.gas_used * observed.effective_gas_price
    result = {
        "tx": BSC_HASH,
        "height": BSC_HEIGHT,
        "canonical_hash": head["hash"],
        "receipt_root": head["receiptsRoot"],
        "fee_vault_or_miner": head["miner"],
        "sender": observed.sender,
        "executor": executor,
        "receipt_fee_wei": fee,
        "wrapped_delta_at_executor_wei": wrapped,
        "wrapped_role_transfers": movements,
        "direct_native_transfers": None,
        "native_role_state_deltas": None,
        "conditional_cash_after_visible_cost_if_same_owner_wei": wrapped - fee,
    }
    if (trace := lane.get("trace")) is None:
        return result
    frames = nested_calls(trace)
    root = frames[()]
    assert (root.sender, root.recipient, root.value, root.data, root.error) == (
        observed.sender,
        executor,
        q(signed["value"]),
        data_bytes(signed["input"]),
        None,
    )
    assert q(trace["gasUsed"]) == observed.gas_used
    positions = _receipt_positions(trace, frames, observed)
    assert len(positions) == len(observed.logs)
    groups = []
    for i, ring in enumerate(claim):
        paths = [positions[log] for log in ring["legs"]]
        assert all(path and path[0] == i for path in paths)
        groups.append(
            {
                "top_level_child_index": i,
                "events": ring["legs"],
                "event_trace_paths": paths,
                "principal_wei": ring["paid_raw"],
                "received_wei": ring["received_raw"],
            }
        )
    transfers = [
        {"path": list(path), "sender": f.sender, "recipient": f.recipient, "wei": f.value}
        for path, f in sorted(frames.items())
        if f.kind == "CALL" and f.value and first_failure(frames, path) is None
    ]
    balance = Counter({observed.sender: -fee})
    for item in transfers:
        balance[item["sender"]] -= item["wei"]
        balance[item["recipient"]] += item["wei"]
    beneficiaries = {item["recipient"] for item in transfers}
    expected = {a: balance[a] for a in sorted(roles | beneficiaries)}
    result |= {
        "trace_committed_logs": len(positions),
        "trace_frames": len(frames),
        "ring_trace_groups": groups,
        "direct_native_transfers": transfers,
        "native_role_trace_deltas": {a: expected[a] for a in sorted(roles)},
        "conditional_cash_after_visible_cost_if_same_owner_wei": wrapped
        + sum(expected[a] for a in roles),
    }
    if (diff := lane.get("diff")) is not None:
        balance_changes = {}
        for address in expected:
            pre = diff["pre"].get(address, {}).get("balance")
            post = diff["post"].get(address, {}).get("balance")
            assert pre is not None or post is None
            balance_changes[address] = q(post or pre) - q(pre) if pre is not None else None
        assert all(
            balance_changes[a] == delta or (balance_changes[a] is None and delta == 0)
            for a, delta in expected.items()
        )
        result["native_role_state_deltas"] = {a: balance_changes[a] for a in sorted(roles)}
        result["native_beneficiary_state_deltas"] = {
            a: balance_changes[a] for a in sorted(beneficiaries)
        }
        map_slot = "0x" + keccak(bytes(12) + data_bytes(executor) + (3).to_bytes(32, "big")).hex()
        before, after = (
            int(diff[stage][WBNB]["storage"][map_slot], 16) for stage in ("pre", "post")
        )
        assert after - before == wrapped
        result["executor_wrapped_storage_witness"] = {
            "slot": map_slot,
            "before_raw": before,
            "after_raw": after,
            "delta_raw": after - before,
        }
    return result


def summarize(root, lanes, groups, topics, additional=None):
    era = [era_case(tx, events, lanes, topics) for tx, events in groups.items()]
    return {
        "scope_sha256": digest(root / "scope.json"),
        "reserved": sum(sum(stop["counts"].values()) for _, stop in lanes.values()),
        "rpc_spending_usd": 0,
        "lanes": {key: stop for key, (_, stop) in lanes.items()},
        "era": era,
        "bsc": bsc_case(lanes["triport"][0]) if "triport" in lanes else None,
    } | ({"corroboration_scope_sha256": digest(additional / "scope.json")} if additional else {})


def load_scope(root, capture):
    plan = read(root / "scope.json")
    assert digest(root / "capture.py") == plan["capture_sha256"]
    if capture:
        assert digest(Path(__file__)) == plan["capture_sha256"]
    assert digest(BSC_SUMMARY) == plan["previous_bsc_sha256"]
    assert digest(ERA_PRIOR / "scope.json") == plan["previous_era_scope_sha256"]
    return plan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("freeze", "capture", "replay"))
    parser.add_argument("root", type=Path)
    parser.add_argument("--also", type=Path)
    args = parser.parse_args()
    groups, topics = era_index()
    if args.mode == "freeze":
        freeze(args.root, groups, args.also)
        return
    capture = args.mode == "capture"
    lanes = run_lanes(args.root, load_scope(args.root, capture), groups, capture)
    if args.also is not None:
        assert not capture
        extra = load_scope(args.also, False)
        assert extra["reviews_scope"] == digest(args.root / "scope.json")
        lanes.update(run_lanes(args.also, extra, groups, False))
    print(
        json.dumps(summarize(args.root, lanes, groups, topics, args.also), indent=2, sort_keys=True)
    )


if __name__ == "__main__":
    main()
