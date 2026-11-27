"""Replay the recent public multi-chain event census and verify receipt-level cash connections."""

import csv
import gzip
import json
from collections import Counter, defaultdict
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path

from measure_eth_nov21_window import amount, rank
from replay_y2026_era_window import BOOT, ETH, MAVERICK, rebuild_hash
from replay_y2026_op_arb import fee, header_hash, moves, receipt_commitment, sha_file, wire

from oasis_agent.arbitrage.cycle_protocols import abi, data_bytes, topic
from oasis_agent.evidence.public_receipts import decode_receipt
from oasis_agent.evidence.research_traces import first_failure, nested_calls
from oasis_agent.evidence.swap_flow_trace import _receipt_positions, replay_trace_funding
from oasis_agent.rpc.budget import BudgetPolicy, RpcBudget

ROOT = Path(__file__).resolve().parents[3] / "reports/research/y2026-open-tail-20261127"
SCOPE = "f3aca54b83bbf82eedc47d9e51cc84f931fa11f28ace04492f345c05f05e62e2"
TIERS = (1, 2, 3, 5, 10, 20, 50, 100, 200)
WRAP = {topic("Deposit(address,uint256)"), topic("Withdrawal(address,uint256)")}
MORPHO = topic("FlashLoan(address,address,uint256)")


def read(path):
    return json.loads(path.read_bytes())


def event_amounts(log, kind):
    if kind == "maverick_v1" and log["topics"] == [MAVERICK]:
        _, _, _, _, a, b, _ = abi(
            ("address", "address", "bool", "bool", "uint256", "uint256", "int32"),
            data_bytes(log["data"]),
        )
        return (a, b) if min(a, b) > 0 else None
    if kind in ("v2", "solidly") and len(log["topics"]) == 3:
        return amount("v2", log["data"][2:])
    if (
        kind in ("v3", "pancake_v3_shape")
        and len(log["topics"]) == 3
        and len(log["data"]) == 2 + 64 * (5 if kind == "v3" else 7)
    ):
        return amount(kind, log["data"][2:])
    return None


def receipt_paths(receipt, topics):
    transfer = moves(receipt)
    logs = receipt["logs"]
    legs = {}
    for event in logs:
        if not event["topics"]:
            continue
        kind = topics.get(event["topics"][0])
        amounts = event_amounts(event, kind)
        if amounts is None:
            continue
        pool = event["address"]
        ins = [
            i
            for i, (asset, src, dst, a) in transfer.items()
            if dst == pool and src != dst and a == amounts[0]
        ]
        outs = [
            i
            for i, (asset, src, dst, a) in transfer.items()
            if src == pool and src != dst and a == amounts[1]
        ]
        if len(ins) != 1 or len(outs) != 1:
            continue
        i, o = ins[0], outs[0]
        if transfer[i][0] == transfer[o][0]:
            continue
        # The indexed output receiver in the common V2/V3 signatures adds a direction check.
        if (
            kind in ("v2", "v3", "pancake_v3_shape")
            and "0x" + event["topics"][-1][-40:] != transfer[o][2]
        ):
            continue
        idx = int(event["logIndex"], 16)
        legs[idx] = {
            "event": idx,
            "kind": kind,
            "pool": pool,
            "in_log": i,
            "out_log": o,
            "in_token": transfer[i][0],
            "out_token": transfer[o][0],
            "payer": transfer[i][1],
            "receiver": transfer[o][2],
            "in_raw": transfer[i][3],
            "out_raw": transfer[o][3],
        }
    inputs = Counter(event["in_log"] for event in legs.values())
    outputs = Counter(event["out_log"] for event in legs.values())
    legs = {
        k: event
        for k, event in legs.items()
        if inputs[event["in_log"]] == outputs[event["out_log"]] == 1
    }
    pairs = []
    for i, a in legs.items():
        for j, b in legs.items():
            if (
                i == j
                or a["pool"] == b["pool"]
                or (a["out_token"], a["out_raw"]) != (b["in_token"], b["in_raw"])
            ):
                continue
            left, right = a["out_log"], b["in_log"]
            if left == right:
                pairs.append((i, j))
                continue
            if left >= right or a["receiver"] != b["payer"]:
                continue
            holder = a["receiver"]
            asset = a["out_token"]
            if any(
                left < n < right and t == asset and holder in (u, v)
                for n, (t, u, v, _) in transfer.items()
            ):
                continue
            if any(
                left < int(event["logIndex"], 16) < right
                and event["address"] == asset
                and len(event["topics"]) == 2
                and event["topics"][0] in WRAP
                and "0x" + event["topics"][1][-40:] == holder
                for event in logs
            ):
                continue
            pairs.append((i, j))
    starts = Counter(i for i, _ in pairs)
    ends = Counter(j for _, j in pairs)
    next_leg = {i: j for i, j in pairs if starts[i] == ends[j] == 1}
    paths = []
    closed = []
    for start in legs:
        i = start
        path = []
        while i not in path:
            path.append(i)
            a, b = legs[path[0]], legs[i]
            if len(path) > 1 and a["in_token"] == b["out_token"] and a["payer"] == b["receiver"]:
                closed.append(
                    {
                        "legs": list(path),
                        "holder": a["payer"],
                        "asset": a["in_token"],
                        "paid_raw": a["in_raw"],
                        "received_raw": b["out_raw"],
                        "gross_raw": b["out_raw"] - a["in_raw"],
                    }
                )
            if i not in next_leg:
                break
            i = next_leg[i]
        paths.append(path)
    return {
        "receipt_legs": list(legs.values()),
        "unique_edges": sorted(next_leg.items()),
        "longest_linked": max(paths, key=len, default=[]),
        "closed_at_same_holder": closed,
    }


def ranking(rows):
    chosen = []
    for keys in [("flash_shapes", "proxy", "events"), ("proxy", "events"), ("events", "pools")]:
        order = sorted(rows, key=lambda x: (tuple(-x[k] for k in keys), x["h"], x["idx"], x["tx"]))
        for row in order:
            if row not in chosen:
                chosen.append(row)
                break
    for row in sorted(rows, key=lambda x: (-x["proxy"], -x["events"], x["h"], x["idx"], x["tx"])):
        if len(chosen) >= 6:
            break
        if row not in chosen:
            chosen.append(row)
    return chosen


def run_scan(name, scope):
    cid, url, _, width = scope["nets"][name]
    d = ROOT / name / "scan"
    stop = read(d / "STOP.json")
    assert stop["scope_sha256"] == SCOPE
    with RpcBudget(
        d / "original.sqlite3", (BudgetPolicy("scan", scope["cap"]["scan"], 250),), read_only=True
    ) as b:
        assert (
            b.counts() == stop["counts"]
            and len(list(d.glob("*.request.json"))) == b.counts()["scan"]
        )
    sources = {}

    def get(label, m, p):
        out, meta = wire(d, label, m, p)
        assert meta["url"] == url
        sources[label] = meta
        return out

    assert int(get("chain", "eth_chainId", []), 16) == cid
    head = get("anchor", "eth_getBlockByNumber", ["latest", False])
    height = int(head["number"], 16)
    oldest_height = height - max(scope["offsets"]) - width + 1
    old = get("oldest", "eth_getBlockByNumber", [hex(oldest_height), False])
    assert int(old["number"], 16) == oldest_height and all(
        datetime.fromtimestamp(int(x["timestamp"], 16), UTC).year == 2026 for x in (old, head)
    )
    groups = defaultdict(list)
    windows = []
    sigs = scope["topics"]
    finance = scope["financing_topics"]
    for off in scope["offsets"]:
        last = height - off
        first = last - width + 1
        if not (d / (str(off) + ".meta.json")).is_file():
            break
        raw = get(
            str(off),
            "eth_getLogs",
            [{"fromBlock": hex(first), "toBlock": hex(last), "topics": [sorted(sigs | finance)]}],
        )
        if raw is None:
            break
        assert all(
            not event["removed"]
            and first <= int(event["blockNumber"], 16) <= last
            and event["topics"][0] in sigs | finance
            for event in raw
        )
        assert len({(event["blockHash"], event["logIndex"]) for event in raw}) == len(raw)
        windows.append({"first": first, "last": last, "offset": off, "matched_logs": len(raw)})
        for event in raw:
            groups[event["transactionHash"]].append(event)
    assert stop["complete_windows"] == [w["offset"] for w in windows]
    confirmed = (d / "anchor-confirm.response").exists()
    if confirmed:
        assert (
            get("anchor-confirm", "eth_getBlockByNumber", [hex(height), False])["hash"]
            == head["hash"]
        )
    candidates = []
    hist = Counter()
    for tx, xs in groups.items():
        assert len({(x["blockNumber"], x["blockHash"], x["transactionIndex"]) for x in xs}) == 1
        swaps = sorted(
            [x for x in xs if x["topics"][0] in sigs], key=lambda x: int(x["logIndex"], 16)
        )
        hist[len(swaps)] += 1
        if len(swaps) < 2:
            continue
        pools, path = rank(swaps, sigs)
        event = xs[0]
        candidates.append(
            {
                "tx": tx,
                "h": int(event["blockNumber"], 16),
                "bh": event["blockHash"],
                "idx": int(event["transactionIndex"], 16),
                "events": len(swaps),
                "pools": pools,
                "proxy": len(path),
                "flash_shapes": len(xs) - len(swaps),
            }
        )
    shortlist = ranking(candidates)
    if confirmed:
        assert shortlist == read(d / "shortlist.json")
    rows = []
    timestamp_disagreements = {}
    for x in shortlist:
        tx = x["tx"]
        if not (d / (tx + ".response")).exists():
            break
        rec = get(tx, "eth_getTransactionReceipt", [tx])
        if rec is None:
            break
        assert (
            rec["blockHash"] == x["bh"]
            and rec["transactionHash"] == tx
            and int(rec["transactionIndex"], 16) == x["idx"]
        )
        relevant = [
            event
            for event in rec["logs"]
            if event["topics"] and event["topics"][0] in sigs | finance
        ]
        index = sorted(groups[tx], key=lambda event: int(event["logIndex"], 16))
        for a, b in zip(relevant, index, strict=True):
            fields = (
                "address",
                "topics",
                "data",
                "logIndex",
                "transactionIndex",
                "transactionHash",
                "blockNumber",
                "blockHash",
                "removed",
            )
            assert [a[k] for k in fields] == [b[k] for k in fields]
        if relevant != index:
            assert all(
                {k: v for k, v in a.items() if k != "blockTimestamp"}
                == {k: v for k, v in b.items() if k != "blockTimestamp"}
                for a, b in zip(relevant, index, strict=True)
            )
            timestamp_disagreements[tx] = sorted(
                {
                    (a.get("blockTimestamp"), b.get("blockTimestamp"))
                    for a, b in zip(relevant, index, strict=True)
                }
            )
        rows.append(x | receipt_paths(rec, sigs))
    chosen = []
    if rows:
        strongest = max(
            rows,
            key=lambda r: (
                max((len(c["legs"]) for c in r["closed_at_same_holder"]), default=0),
                len(r["longest_linked"]),
                r["flash_shapes"],
                r["events"],
                -r["h"],
                -r["idx"],
            ),
        )
        chosen.append(strongest)
        others = [r for r in rows if r["tx"] != strongest["tx"]]
        if others:
            chosen.append(max(others, key=lambda r: (r["events"], r["pools"], -r["h"], -r["idx"])))
    return {
        "chain_id": cid,
        "anchor": head["hash"],
        "oldest": old["hash"],
        "windows": windows,
        "matched_tx": sum(hist.values()),
        "tiers": {k: sum(v for n, v in hist.items() if n >= k) for k in TIERS},
        "event_max": max(hist) if hist else None,
        "rows": rows,
        "stop": stop,
        "wire": sources,
        "timestamp_disagreements": timestamp_disagreements,
        "indexed_multihop": sorted(candidates, key=lambda x: (x["h"], x["idx"])),
    }, [{k: r[k] for k in shortlist[0]} for r in chosen]


def ids(log):
    return tuple(
        log[k]
        for k in (
            "blockNumber",
            "blockHash",
            "transactionHash",
            "transactionIndex",
            "logIndex",
            "address",
            "topics",
            "data",
        )
    )


def same_receipt(a, b):
    assert all(
        a[k] == b[k]
        for k in (
            "transactionHash",
            "blockNumber",
            "blockHash",
            "transactionIndex",
            "to",
            "from",
            "gasUsed",
            "effectiveGasPrice",
            "status",
            "logsBloom",
        )
    )
    assert list(map(ids, a["logs"])) == list(map(ids, b["logs"]))


def observed_fee(name, r):
    if name in ("arbitrum", "optimism"):
        return fee(name, r)
    assert name == "era"
    components = [
        (i, u, v, a)
        for i, (t, u, v, a) in moves(r).items()
        if t == ETH and {u, v} == {r["from"], BOOT}
    ]
    paid = sum(a if v == BOOT else -a for i, u, v, a in components)
    expected = int(r["gasUsed"], 16) * int(r["effectiveGasPrice"], 16)
    return {
        "receipt_network_wei": expected,
        "payer_system_fee_transfers": components,
        "payer_fee_reconciled": bool(components) and paid == expected,
    }


def row_roles(r):
    accounts = (r["from"], r["to"])
    bal = Counter()
    for asset, u, v, a in moves(r).values():
        for addr in set(accounts):
            bal[addr, asset] += a * ((v == addr) - (u == addr))
    return [
        {"address": k, "token": asset, "transfer_delta_raw": v}
        for (k, asset), v in sorted(bal.items())
        if v
    ]


def arb_cash(r):
    # Both V4 legs use the canonical manager. Each input/output is actually settled to the caller.
    t = moves(r)
    actor = r["to"]
    manager = "0x360e68faccca8ca495c1b759fd9eee466db9fb32"
    log = {int(x["logIndex"], 16): x for x in r["logs"]}
    for i, pay, receive in [(33, 34, 35), (36, 37, 38)]:
        e = log[i]
        assert (
            e["address"] == manager
            and len(e["topics"]) == 3
            and e["topics"][2] == "0x" + ("0" * 24) + actor[2:]
        )
        assert e["topics"][0] == topic(
            "Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)"
        )
        a, b, *_ = abi(
            ("int128", "int128", "uint160", "uint128", "int24", "uint24"), data_bytes(e["data"])
        )
        assert ((a, b) if t[pay][0] < t[receive][0] else (b, a)) == (-t[pay][3], t[receive][3])
        assert t[pay][1:3] == (actor, manager) and t[receive][1:3] == (manager, actor)
    assert log[33]["topics"][1] != log[36]["topics"][1]
    # Opening V3 pool advances USDC; the other V3 pool pays out WETH, then opening pool is paid.
    pairs = receipt_paths(r, read(ROOT / "scope.json")["topics"])["receipt_legs"]
    assert [(p["event"], p["in_log"], p["out_log"]) for p in pairs] == [(41, 40, 39), (44, 42, 32)]
    for i, j in [(32, 34), (35, 37), (38, 40)]:
        assert t[i][0] == t[j][0] and t[i][3] == t[j][3] and t[i][2] == t[j][1] == actor
    w = t[39][0]
    assert w == t[42][0] == "0x82af49447d8a07e3bd95bd0d56f35241523fbab1"
    residue = t[39][3] - t[42][3]
    assert row_roles(r) == [{"address": actor, "token": w, "transfer_delta_raw": residue}]
    cost = observed_fee("arbitrum", r)["complete_network_fee_wei"]
    return {
        "funded_swap_log_order": [44, 33, 36, 41],
        "v4_pool_ids": [log[i]["topics"][1] for i in (33, 36)],
        "opening_pool": log[44]["address"],
        "advanced_usdc_raw": t[32][3],
        "returned_weth_raw": t[39][3],
        "paid_opening_pool_weth_raw": t[42][3],
        "executor_weth_delta": residue,
        "payer_network_wei": cost,
        "conditional_eth_weth_residue_after_network_fee": residue - cost,
        "owner_all_cost_pnl": None,
    }


def era_finance(r):
    asset = "0x3355df6d4c9c3035724fd0e3914de96a5a83aaf4"
    t = moves(r)
    actor = r["to"]
    e = r["logs"][15]
    lender = e["address"]
    assert e["topics"] == [
        topic("FlashLoan(address,address,uint256,uint256)"),
        "0x" + "0" * 24 + actor[2:],
        "0x" + "0" * 24 + asset[2:],
    ]
    principal, premium = abi(("uint256", "uint256"), data_bytes(e["data"]))
    assert t[2] == (asset, lender, actor, principal) and t[12] == (
        asset,
        actor,
        lender,
        principal + premium,
    )
    assert t[13][0:2] == (asset, lender) and t[13][3] == premium
    (c,) = receipt_paths(r, read(ROOT / "scope.json")["topics"])["closed_at_same_holder"]
    assert (
        c["legs"] == [5, 8, 11]
        and c["paid_raw"] == principal
        and c["asset"] == asset
        and c["holder"] == actor
    )
    return {
        "principal_asset": asset,
        "principal_raw": principal,
        "return_from_markets_raw": c["received_raw"],
        "repaid_principal_plus_premium": t[12][3],
        "premium_raw": premium,
        "premium_recipient": t[13][2],
        "executor_after_loan_raw": c["gross_raw"] - premium,
        "owner_all_cost_pnl": None,
    }


def details(name, scope, selection):
    assert selection == read(ROOT / name / "selected.json")
    d = ROOT / name / "detail"
    stop = read(d / "STOP.json")
    assert stop["scope_sha256"] == SCOPE
    with RpcBudget(
        d / "original.sqlite3",
        (BudgetPolicy("detail", scope["cap"]["detail"], 250),),
        read_only=True,
    ) as b:
        assert (
            b.counts() == stop["counts"]
            and len(list(d.glob("*.request.json"))) == b.counts()["detail"]
        )
    cid, _, url, _ = scope["nets"][name]
    provenance = {}
    cases = []

    def get(label, method, params):
        out, meta = wire(d, label, method, params)
        assert meta["url"] == url
        provenance[label] = meta
        return out

    if selection:
        assert int(get("chain", "eth_chainId", []), 16) == cid
    for x in selection:
        tx = x["tx"]
        primary = read(ROOT / name / "scan" / (tx + ".response"))["result"]
        case = {
            "tx": tx,
            "roles": row_roles(primary),
            "fee": observed_fee(name, primary),
            "header_proof": None,
            "receipts_proof": None,
        }
        cases.append(case)
        if not (d / (tx + "-header.response")).exists():
            continue
        h = get(tx + "-header", "eth_getBlockByHash", [x["bh"], False])
        if h is None:
            continue
        identity = rebuild_hash(h) if name == "era" else header_hash(h)
        assert identity == h["hash"] == x["bh"] and h["transactions"][x["idx"]] == tx
        case["header_proof"] = {
            "hash": identity,
            "height": x["h"],
            "utc": datetime.fromtimestamp(int(h["timestamp"], 16), UTC).isoformat(),
        }
        rows = get(tx + "-receipts", "eth_getBlockReceipts", [hex(x["h"])])
        if rows is None:
            continue
        assert [r["transactionHash"] for r in rows] == h["transactions"]
        same_receipt(rows[x["idx"]], primary)
        if name == "era":
            assert int(h["receiptsRoot"], 16) == 0
            case["receipts_proof"] = {
                "ordered_count": len(rows),
                "comparison": "two matching providers; Era rolling tx hash verified",
                "root": None,
            }
            if tx.startswith("0x17ce58"):
                case["flash_loan"] = era_finance(primary)
        else:
            _, root = receipt_commitment(cid, h, rows)
            case["receipts_proof"] = {"ordered_count": len(rows), "root": root}
            if name == "arbitrum":
                assert all(
                    int(x["blockTimestamp"], 16) == int(h["timestamp"], 16) for x in primary["logs"]
                )
                case["four_market_cycle"] = arb_cash(primary)
        if (d / (tx + "-trace.response")).exists():
            result = get(
                tx + "-trace",
                "debug_traceTransaction",
                [tx, {"tracer": "callTracer", "tracerConfig": {"withLog": True}, "timeout": "10s"}],
            )
            assert result is None  # Retained RPC refusal for both selected chains.
    return {"wire": provenance, "stop": stop, "cases": cases}


def settlement(scope):
    p = ROOT / "settlement"
    fp = p / "plan.json"
    digest = "977875f6e37f3c1e9cfe502301e3558dfe5b41d188495b936f49d8bd364e5dfe"
    assert sha_file(fp) == digest
    spec = read(fp)
    assert sha_file(p / "probe.py") == spec["driver"] and spec["transport"] == scope["transport"]
    answer = {}
    for name, net in spec["nets"].items():
        folder = p / name
        sz = len(net["calls"])
        stop = read(folder / "STOP.json")
        with RpcBudget(
            folder / "original.sqlite3", (BudgetPolicy(name, sz, 350),), read_only=True
        ) as b:
            assert b.counts() == stop["counts"] == {name: sz}
        assert stop["scope"] == digest and stop["error"] is None
        data = {}
        provenance = {}
        for label, m, params in net["calls"]:
            data[label], provenance[label] = wire(folder, label, m, params)
            assert provenance[label]["url"] == net["url"]
        cid = scope["nets"][name][0]
        assert int(data["chain"], 16) == cid
        txfile = next(
            (ROOT / name / "scan").glob(("0xa40b" if name == "era" else "0x82e4") + "*.response")
        )
        assert sha_file(txfile) == spec["era_hash" if name == "era" else "op_hash"]
        r = read(txfile)["result"]
        h = data["head"]
        tx = r["transactionHash"]
        idx = int(r["transactionIndex"], 16)
        assert h["hash"] == r["blockHash"] and h["transactions"][idx] == tx
        accounting = {
            "tx": tx,
            "hash": h["hash"],
            "index": idx,
            "height": int(h["number"], 16),
            "utc": datetime.fromtimestamp(int(h["timestamp"], 16), UTC).isoformat(),
            "fee": observed_fee(name, r),
            "sender": r["from"],
            "executor": r["to"],
            "scope_sha256": digest,
            "wire": provenance,
        }
        if name == "era":
            assert h["hash"] == rebuild_hash(h)
            same_receipt(r, data["receipt"])
            assert list(map(ids, r["logs"])) == [
                ids(event) for event in data["logs"] if event["transactionHash"] == tx
            ]
            assert accounting["fee"]["payer_fee_reconciled"]
            (route,) = receipt_paths(r, scope["topics"])["closed_at_same_holder"]
            assert (
                route["asset"] == "0x5aea5775959fbc2557cc8789bc1bf90a239d9a91"
                and route["holder"] == r["to"]
            )
            assert sorted(row_roles(r), key=lambda entry: entry["token"]) == [
                {
                    "address": r["from"],
                    "token": ETH,
                    "transfer_delta_raw": -accounting["fee"]["receipt_network_wei"],
                },
                {
                    "address": r["to"],
                    "token": route["asset"],
                    "transfer_delta_raw": route["gross_raw"],
                },
            ]
            accounting.update(
                cycle=route,
                roles=row_roles(r),
                conditional_common_owner_after_network_wei=route["gross_raw"]
                - accounting["fee"]["receipt_network_wei"],
            )
        else:
            block, root = receipt_commitment(cid, h, data["receipts"])
            same_receipt(r, data["receipts"][idx])
            rec = decode_receipt(r, block, tx, idx)
            tree = data["cash"]
            fs = nested_calls(tree)
            _receipt_positions(tree, fs, rec)
            assert (
                fs[()].sender == rec.sender
                and fs[()].recipient == rec.recipient
                and int(tree["gasUsed"], 16) == rec.gas_used
            )
            funded = replay_trace_funding(rec, tree)
            transfers = moves(r)
            loan = r["logs"][0]
            lender = loan["address"]
            borrower = transfers[49][2]
            usdc = transfers[49][0]
            assert loan["topics"] == [
                MORPHO,
                "0x" + "0" * 24 + borrower[2:],
                "0x" + "0" * 24 + usdc[2:],
            ]
            (principal,) = abi(("uint256",), data_bytes(loan["data"]))
            assert transfers[49] == (usdc, lender, borrower, principal) and transfers[68] == (
                usdc,
                borrower,
                lender,
                principal,
            )
            proof = {v.leg.event.log_index: v for v in funded.resolved_swaps}
            ordered = [54, 59, 64, 67, 72]
            assert [proof[i].input_transfer_log for i in ordered] == [52, 57, 60, 66, 71]
            assert [proof[i].output_transfer_log for i in ordered] == [53, 58, 62, 65, 70]
            assert len({proof[i].leg.event.pool for i in ordered}) == len(ordered)
            for left, right in pairwise(ordered[:4]):
                a = transfers[proof[left].output_transfer_log]
                b = transfers[proof[right].input_transfer_log]
                assert (a[0], a[2], a[3]) == (b[0], b[1], b[3]) and b[1] == borrower
            assert transfers[52] == (usdc, borrower, proof[54].leg.event.pool[0], principal)
            returned = transfers[65][3]
            extra = returned - principal
            assert transfers[65][2] == borrower and transfers[69] == (
                usdc,
                borrower,
                r["to"],
                extra,
            )
            assert transfers[71] == (usdc, r["to"], proof[72].leg.event.pool[0], extra)
            native = [
                (f.sender, f.recipient, f.value)
                for path, f in fs.items()
                if f.kind == "CALL" and f.value and first_failure(fs, path) is None
            ]
            weth, pool, dest, converted = transfers[70]
            assert weth == "0x4200000000000000000000000000000000000006" and (pool, dest) == (
                proof[72].leg.event.pool[0],
                r["to"],
            )
            assert sorted(native) == sorted(
                [(weth, r["to"], converted), (r["to"], r["from"], converted)]
            )
            op = int(data["operator"], 16)
            assert len(data["operator"]) == 66
            raw = data["balances"]
            delta = int(raw["post"][r["from"]]["balance"], 16) - int(
                raw["pre"][r["from"]]["balance"], 16
            )
            assert converted - accounting["fee"]["known_network_fee_wei"] - op == delta
            accounting.update(
                receipts_root=root,
                receipts=len(data["receipts"]),
                frames=len(fs),
                borrower=borrower,
                lender=lender,
                principal_token=usdc,
                market_logs=ordered,
                borrowed_and_repaid_raw=principal,
                returned_raw=returned,
                excess_raw=extra,
                converted_weth_and_paid_sender_wei=converted,
                operator_fee_pinned_block_end_wei=op,
                network_charge_reconciled_wei=converted - delta,
                observed_sender_balance_difference_wei=delta,
                committed_native_calls=native,
            )
        answer[name] = accounting
    return answer


def main():
    scope = read(ROOT / "scope.json")
    assert (
        sha_file(ROOT / "scope.json") == SCOPE and sha_file(ROOT / "capture.py") == scope["driver"]
    )
    assert sha_file(ROOT.parent / "y2026-op-arb-20261127/capture.py") == scope["transport"]
    chains = {}
    for name in scope["nets"]:
        summary, selected = run_scan(name, scope)
        results = details(name, scope, selected)
        if not summary["windows"]:
            summary["tiers"] = None
            summary["matched_tx"] = None
        for r in summary["rows"]:
            receipt = read(ROOT / name / "scan" / (r["tx"] + ".response"))["result"]
            r["fee"] = observed_fee(name, receipt)
            r["transfer_only_role_deltas"] = row_roles(receipt)
        chains[name] = summary | {"deep": results}
    # Keep independent, date-limited winners separate from the previously frozen hash census.
    catalog = ROOT.parent / "gold-book-20261127/all-hashes.tsv.gz"
    catalog_digest = sha_file(catalog)
    assert catalog_digest == "400147164406c06fc29ce20ed28f43a29b39cb331d12442e11db40ac60dc8648"
    with gzip.open(catalog, "rt") as stream:
        previous = {
            (int(row["chain_id"]), row["tx_hash"]) for row in csv.DictReader(stream, delimiter="\t")
        }
    current = {
        (panel["chain_id"], row["tx"])
        for panel in chains.values()
        for row in panel["indexed_multihop"]
    }
    assert len(previous) == 118_083 and len(current) == 797 and not previous.intersection(current)
    return {
        "schema": "oasis.open-tail-cash.v1",
        "previous_catalog_sha256": catalog_digest,
        "new_multi_event_transaction_keys": len(current),
        "scope_sha256": SCOPE,
        "usd": 0,
        "chains": chains,
        "additional_cash_checks": settlement(scope),
    }


if __name__ == "__main__":
    if not __debug__:
        raise SystemExit("run without -O")
    print(json.dumps(main(), indent=2, sort_keys=True))
