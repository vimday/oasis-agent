"""Verify November 2026 BSC/Robinhood landed cohorts, funding receipts and native payouts."""

import csv
import gzip
import json
from collections import Counter, defaultdict
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path

from eth_hash.auto import keccak
from replay_bsc_pool_expansion import receipts_summary
from replay_y2026_eth_bsc_windows import (
    DEPOSIT,
    WBNB,
    WITHDRAWAL,
    dec_address,
    receipt_closures,
    swap_deltas,
    transfer_rows,
)
from replay_y2026_inventory import decoded, digest, read
from rlp import encode

from oasis_agent.arbitrage.cycle_protocols import abi, data_bytes, topic
from oasis_agent.evidence.nitro_block import nitro_header, verify_receipt_root
from oasis_agent.evidence.public_receipts import quantity
from oasis_agent.evidence.state_trie import PRAGUE_FIELDS
from oasis_agent.rpc.budget import BudgetPolicy, RpcBudget

ROOT = Path(__file__).resolve().parents[3]
D = ROOT / "reports/research/y2026-bsc-rh-landed-20261127"
SCOPE = "9f7de37b7e4a9fd085c662c1074e30b32dbed18f78529ee89ad0dabea2470ace"
NATIVE = "80326154df1b1415d474a3f6e3e8e6f3c1f6dc824043f2eba65d63ee88b64fb3"
RH_TX = "0xe5b5753af192d336a3b5bcec18f01d15c1f742599dc64b542080361a3518ff4b"
BSC_TX = "0xee5852aa2f4ca320f1b2adb34935883e27545e4f9cecde67b2f5963f4182753e"
SMALL = "0xceae39df38e513ea35ed0d6115e7693c1b2812cc58be18df1b1d84cafd852177"
ZERO = "0x" + "0" * 40
MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
V4 = topic("Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)")
FLASH = topic("FlashLoan(address,address,uint256)")
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
RH_WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
TIERS = (1, 2, 3, 5, 20, 50, 100, 200)


def keys(log):
    return [
        quantity(log["blockNumber"]),
        log["blockHash"],
        quantity(log["transactionIndex"]),
        log["transactionHash"],
        quantity(log["logIndex"]),
        log["address"],
        log["topics"],
        log["data"],
    ]


def matches(log, lookup):
    return bool(log["topics"] and log["topics"][0] in lookup)


def rooted(chain, head, rows):
    height = quantity(head["number"])
    if chain == 56:
        h = (
            "0x"
            + keccak(
                encode(
                    [
                        quantity(head[k]) if w < 0 else data_bytes(head[k])
                        for k, w in PRAGUE_FIELDS
                        if k in head
                    ]
                )
            ).hex()
        )
        assert h == head["hash"]
        got = receipts_summary(head, rows, {}, {"managers": {}, "pools": {}}, {})
        assert got["receipts_root"] == head["receiptsRoot"]
    else:
        h = nitro_header(head, height, head["hash"], chain_id=chain)
        assert len(verify_receipt_root(h, rows)) == len(rows)
    assert [r["transactionHash"] for r in rows] == head["transactions"]
    assert all(quantity(r["status"]) or r["logs"] == [] for r in rows)
    assert datetime.fromtimestamp(quantity(head["timestamp"]), UTC).year == 2026
    return {
        "height": height,
        "hash": head["hash"],
        "timestamp": quantity(head["timestamp"]),
        "receipts_root": head["receiptsRoot"],
        "transactions": len(rows),
        "reverts": sum(quantity(r["status"]) == 0 for r in rows),
    }


def summarize_fee(row):
    price, gas = quantity(row["effectiveGasPrice"]), quantity(row["gasUsed"])
    return {
        "transaction": row["transactionHash"],
        "block": quantity(row["blockNumber"]),
        "block_hash": row["blockHash"],
        "index": quantity(row["transactionIndex"]),
        "gas_used": gas,
        "effective_wei_per_gas": price,
        "receipt_fee_wei": price * gas,
        "gas_payer": row["from"],
        "executor": row["to"],
    }


def thresholds(occ):
    return {k: sum(len(v) >= k for v in occ.values()) for k in TIERS}


def read_cohort(lane, plan):
    here = D / lane
    head_src, rows_src, chain = ("bsc-h", "bsc-r", 56) if lane == "bsc" else ("rh-o", "rh-r", 4663)
    urls = plan["source"]

    def call(label, method, params, src):
        out, _ = decoded(here, label, method, params, urls[src])
        return out

    assert (
        quantity(call("chain-head", "eth_chainId", [], head_src))
        == quantity(call("chain-rows", "eth_chainId", [], rows_src))
        == chain
    )
    anchor = call("anchor", "eth_getBlockByNumber", ["latest", False], head_src)
    height = quantity(anchor["number"])
    occ = defaultdict(list)
    blocks = []
    audited = []
    background = []
    known = {}
    panels = []

    def fetch(n, hlabel, rlabel, bucket):
        h = call(hlabel, "eth_getBlockByNumber", [hex(n), False], head_src)
        assert quantity(h["number"]) == n
        rows = call(rlabel, "eth_getBlockReceipts", [h["hash"]], rows_src)
        bucket.append(rooted(chain, h, rows))
        known.update({r["transactionHash"]: (r, h) for r in rows})
        return h, rows

    if lane == "bsc":
        for gap in plan["bsc_gaps"]:
            prev = None
            for n in range(height - gap, height - gap + plan["bsc_width"]):
                h, rows = fetch(n, f"h{n}", f"r{n}", blocks)
                assert prev is None or h["parentHash"] == prev
                prev = h["hash"]
                for r in rows:
                    logs = [event for event in r["logs"] if matches(event, plan["topics"])]
                    if logs:
                        assert r["transactionHash"] not in occ
                        occ[r["transactionHash"]] = logs
    else:
        for delta in plan["rh_background_offsets"]:
            n = height - delta
            fetch(n, f"bg-h{n}", f"bg-r{n}", background)
        for gap in plan["rh_gaps"]:
            a = height - gap
            b = a + plan["rh_width"] - 1
            h = call(f"start{a}", "eth_getBlockByNumber", [hex(a), False], head_src)
            z = call(f"end{b}", "eth_getBlockByNumber", [hex(b), False], head_src)
            for hdr, pos in ((h, a), (z, b)):
                nitro_header(hdr, pos, hdr["hash"], chain_id=chain)
                assert datetime.fromtimestamp(quantity(hdr["timestamp"]), UTC).year == 2026
            ls = call(
                f"logs{a}",
                "eth_getLogs",
                [{"fromBlock": hex(a), "toBlock": hex(b), "topics": [plan["topics"]]}],
                head_src,
            )
            seen = set()
            hashes = {}
            stamp = []
            for event in ls:
                n, bh, idx, tx, i, *_ = keys(event)
                assert (
                    a <= n <= b
                    and event["removed"] is False
                    and (n, i) not in seen
                    and matches(event, plan["topics"])
                )
                assert hashes.setdefault(n, bh) == bh
                if n in (a, b):
                    assert bh == (h if n == a else z)["hash"]
                if occ[tx]:
                    assert keys(occ[tx][0])[:4] == [n, bh, idx, tx]
                occ[tx].append(event)
                seen.add((n, i))
                stamp.append((n, i))
            assert stamp == sorted(stamp)
            panels.append(
                {
                    "first": a,
                    "last": b,
                    "first_hash": h["hash"],
                    "last_hash": z["hash"],
                    "start_utc": datetime.fromtimestamp(quantity(h["timestamp"]), UTC).isoformat(),
                    "logs": len(ls),
                }
            )
    order = sorted(
        occ,
        key=lambda tx: (
            -len(occ[tx]),
            quantity(occ[tx][0]["blockNumber"]),
            quantity(occ[tx][0]["transactionIndex"]),
            tx,
        ),
    )
    low = next(tx for tx in order if len(occ[tx]) <= 2)
    assert read(here / "selected.json") == {"ranked": order[:2], "control": low}
    if lane == "rh":
        for i, tx in enumerate(order[:2]):
            n = quantity(occ[tx][0]["blockNumber"])
            _, rows = fetch(n, f"top-h{i}", f"top-r{i}", audited)
            r = rows[quantity(occ[tx][0]["transactionIndex"])]
            assert [keys(event) for event in r["logs"] if matches(event, plan["topics"])] == [
                keys(event) for event in occ[tx]
            ]
    else:
        assert quantity(call("chain-trace", "eth_chainId", [], "bsc-t")) == chain
    for logs in occ.values():
        assert len(logs) == len({quantity(event["logIndex"]) for event in logs})

    def row(tx):
        event = occ[tx][0]
        out = {
            "block": quantity(event["blockNumber"]),
            "index": quantity(event["transactionIndex"]),
            "tx": tx,
            "events": len(occ[tx]),
        }
        if tx in known:
            out["receipt_fee_wei"] = summarize_fee(known[tx][0])["receipt_fee_wei"]
        return out

    return {
        "chain_id": chain,
        "anchor_height": height,
        "anchor_hash": anchor["hash"],
        "thresholds": thresholds(occ),
        "hit_transactions": len(occ),
        "event_logs": sum(map(len, occ.values())),
        "full_sampling_blocks": blocks,
        "background_full_blocks": background,
        "target_verification_blocks": audited,
        "log_panels": panels,
        "all_3_or_more": [row(tx) for tx in order if len(occ[tx]) >= 3],
        "control": row(low),
        "ranked": [row(tx) for tx in order[:2]],
    }, known


def erc20_net(moves, who):
    net = Counter()
    for m in moves:
        if who in (m["from"], m["to"]):
            net[m["asset"]] += m["amount"] * (int(m["to"] == who) - int(m["from"] == who))
    return net


def bsc_wide(r):
    assert r["transactionHash"] == BSC_TX
    moves = transfer_rows(r)
    by = {m["log"]: m for m in moves}
    logs = {quantity(event["logIndex"]): event for event in r["logs"]}
    holder = r["to"]
    loans = []
    lender = logs[541]["address"]
    for event_id, repay in [(541, 780), (543, 776), (545, 772), (547, 768)]:
        event = logs[event_id]
        assert event["topics"][0] == FLASH and event["address"] == lender
        who, asset = map(dec_address, event["topics"][1:])
        (amt,) = abi(("uint256",), data_bytes(event["data"]))
        assert who == holder
        outgoing, returned = by[event_id + 1], by[repay]
        assert (outgoing["from"], outgoing["to"], outgoing["asset"], outgoing["amount"]) == (
            lender,
            holder,
            asset,
            amt,
        )
        assert (returned["from"], returned["to"], returned["asset"], returned["amount"]) == (
            holder,
            lender,
            asset,
            amt,
        )
        assert [
            m["log"]
            for m in moves
            if m["asset"] == asset and {m["from"], m["to"]} == {lender, holder}
        ] == [event_id + 1, repay]
        loans.append(
            {
                "flash_event": event_id,
                "principal_transfer": event_id + 1,
                "repayment_transfer": repay,
                "asset": asset,
                "amount": amt,
            }
        )
    central = by[736]["asset"]
    history = [x for x in moves if x["asset"] == central and holder in (x["from"], x["to"])]
    assert [x["log"] for x in history] == [736, 739, 743, 747, 751, 755, 759, 763] and history[0][
        "to"
    ] == holder
    assert (
        all(x["from"] == holder for x in history[1:]) and len({x["to"] for x in history[1:]}) == 7
    )
    legs, _ = receipt_closures(r, moves, {})
    by_input = {event["in"]["log"]: event for event in legs}
    assert all(x["log"] in by_input for x in history[1:])
    assert any(event["out"]["log"] == 736 for event in legs)
    assert all(event["out"]["log"] != 736 or event["in"]["log"] == 737 for event in legs)
    wraps = [
        (
            i,
            1 if event["topics"][0] == DEPOSIT else -1,
            abi(("uint256",), data_bytes(event["data"]))[0],
        )
        for i, event in logs.items()
        if event["address"] == WBNB
        and event["topics"][0] in (DEPOSIT, WITHDRAWAL)
        and dec_address(event["topics"][1]) == holder
    ]
    net = erc20_net(moves, holder)
    net[WBNB] += sum(sign * q for _, sign, q in wraps)
    assert (
        net[WBNB] == 0
        and history[0]["amount"] - sum(m["amount"] for m in history[1:]) == net[central] > 0
    )
    return summarize_fee(r) | {
        "lender": lender,
        "loans": loans,
        "wrap_events": wraps,
        "holder_token_net": net,
        "split_input": history[0],
        "split_outgoing": [
            {
                "input": m,
                "swap": by_input[m["log"]]["swap_log"],
                "output": by_input[m["log"]]["out"],
            }
            for m in history[1:]
        ],
        "proven_exact_holder_market_legs": len(legs),
        "owner_net": None,
    }


def rh_rounds(r):
    assert r["transactionHash"] == RH_TX
    rows = transfer_rows(r)
    moves = {m["log"]: m for m in rows}
    logs = {quantity(event["logIndex"]): event for event in r["logs"]}
    holder = r["to"]

    def leg(idx, i, o, v4=False):
        event, paid, out = logs[idx], moves[i], moves[o]
        assert paid["from"] == out["to"] == holder and paid["to"] == out["from"] == event["address"]
        if v4:
            assert (
                event["topics"][0] == V4
                and event["address"] == MANAGER
                and dec_address(event["topics"][2]) == holder
            )
            v = abi(
                ("int128", "int128", "uint160", "uint128", "int24", "uint24"),
                data_bytes(event["data"]),
            )
            a, b = v[:2]
            assert a * b < 0
            p, q, forward = -min(a, b), max(a, b), a < 0
        else:
            p, q, forward = swap_deltas(event)
            assert [dec_address(t) for t in event["topics"][1:]] == [holder, holder]
        assert (paid["asset"] < out["asset"]) == forward
        assert (paid["amount"], out["amount"]) == (p, q)
        return {
            "event": idx,
            "pool": event["address"],
            "pool_id": event["topics"][1] if v4 else None,
            "paid": paid,
            "received": out,
        }

    rounds = []
    for shift in (0, 11, 22):
        legs = [
            leg(10 + shift, 9 + shift, shift),
            leg(4 + shift, 2 + shift, 5 + shift, True),
            leg(8 + shift, 7 + shift, 6 + shift),
        ]
        for a, b in pairwise(legs):
            prev, nxt = a["received"], b["paid"]
            assert (prev["asset"], prev["amount"]) == (nxt["asset"], nxt["amount"]) and prev[
                "log"
            ] < nxt["log"]
            assert not any(
                prev["log"] < m["log"] < nxt["log"]
                and m["asset"] == prev["asset"]
                and holder in (m["from"], m["to"])
                for m in rows
            )
        p, q = legs[0]["paid"], legs[-1]["received"]
        assert p["asset"] == q["asset"] == USDG
        rounds.append(
            {
                "legs": legs,
                "advanced_usdg_raw": p["amount"],
                "returned_usdg_raw": q["amount"],
                "spread_usdg_raw": q["amount"] - p["amount"],
            }
        )
    sale = leg(35, 34, 33)
    burn = moves[36]
    assert (
        sale["paid"]["asset"] == USDG
        and sum(x["spread_usdg_raw"] for x in rounds) == sale["paid"]["amount"]
    )
    assert (burn["from"], burn["to"], burn["asset"], burn["amount"]) == (
        holder,
        ZERO,
        RH_WETH,
        sale["received"]["amount"],
    )
    assert all(net == 0 for net in erc20_net(rows, holder).values())
    fee = summarize_fee(r)
    l1 = quantity(r["gasUsedForL1"])
    return fee | {
        "rounds": rounds,
        "liquidation_leg": sale,
        "weth_burn_wei": burn["amount"],
        "l1_gas_inside_total": l1,
        "included_l1_fee_wei": l1 * fee["effective_wei_per_gas"],
        "all_ordinary_token_nets_zero": True,
        "conditional_native_wei_after_receipt_fee_at_parity": burn["amount"]
        - fee["receipt_fee_wei"],
        "proved_beneficiary_eth_net": None,
    }


def archive_cash(r, h, scope):
    archive = read(D / "cash-bsc/archive.response")
    assert archive["header"] == {
        "number": quantity(h["number"]),
        "hash": h["hash"],
        "parentHash": h["parentHash"],
        "miner": h["miner"],
        "timestamp": quantity(h["timestamp"]),
    }
    assert (
        r["transactionHash"] == SMALL
        and quantity(r["blockNumber"])
        == scope["bsc_query"]["toBlock"]
        == scope["bsc_query"]["fromBlock"]
    )
    i = quantity(r["transactionIndex"])
    (a,) = [v for v in archive["transactions"] if v["transactionIndex"] == i]
    assert (
        a["hash"],
        a["from"],
        a["to"],
        quantity(a["gasUsed"]),
        quantity(a["gasPrice"]),
        a["status"],
    ) == (
        SMALL,
        r["from"],
        r["to"],
        quantity(r["gasUsed"]),
        quantity(r["effectiveGasPrice"]),
        quantity(r["status"]),
    )
    src = [v for v in archive["traces"] if v["transactionIndex"] == i]
    assert all(v["type"] == "call" and not v.get("error") for v in src)
    paths = {tuple(v["traceAddress"]): v["action"] for v in src}
    assert (
        len(paths) == len(src) == 20
        and paths[()]["from"] == paths[()]["to"] == r["from"] == r["to"]
    )
    for path, action in paths.items():
        ids = sorted(p[-1] for p in paths if p and p[:-1] == path)
        assert ids == list(range(len(ids)))
        assert not path or path[:-1] in paths
        assert action["type"] in ("call", "staticcall", "delegatecall")
        if path:
            parent = paths[path[:-1]]
            assert action["from"] == (
                parent["from"] if parent["type"] == "delegatecall" else parent["to"]
            )
    holder = r["from"]
    moves = transfer_rows(r)
    _, closures = receipt_closures(r, moves, {})
    (case,) = closures
    transfers = []
    for path, a in paths.items():
        if a["sighash"] == "0xa9059cbb":
            token = a["to"]
            recipient, value = abi(("address", "uint256"), data_bytes(a["input"])[4:])
            (seq,) = [
                m["log"]
                for m in moves
                if (m["from"], m["to"], m["asset"], m["amount"])
                == (a["from"], recipient, token, value)
            ]
            transfers.append({"frame": path, "log": seq})
    assert sorted(t["log"] for t in transfers) == sorted(m["log"] for m in moves)
    assert paths[0, 1, 2]["sighash"] == paths[0, 1, 2, 0, 2]["sighash"] == "0x23a69e75"
    assert [paths[p]["to"] for p in ((0, 1), (0, 1, 2, 0))] == [v["pool"] for v in case["legs"]]
    g = int(case["gross_wbnb_wei"])
    assert (paths[1,]["to"], paths[1,]["sighash"]) == (WBNB, "0x2e1a7d4d")
    assert abi(("uint256",), data_bytes(paths[1,]["input"])[4:]) == (g,)
    (w,) = [
        event
        for event in r["logs"]
        if event["address"] == WBNB and event["topics"][0] == WITHDRAWAL
    ]
    assert dec_address(w["topics"][1]) == holder and abi(("uint256",), data_bytes(w["data"])) == (
        g,
    )
    native = [
        (p, a["from"], a["to"], quantity(a["value"]))
        for p, a in paths.items()
        if a["type"] == "call" and quantity(a["value"]) > 0
    ]
    assert native[0] == ((1, 0), WBNB, holder, g)
    assert [v[0] for v in native[1:]] == [(2,), (3,)] and all(x[1] == holder for x in native[1:])
    assert sum(x[-1] for x in native[1:]) == g and h["miner"] not in [x[2] for x in native]
    tokens = erc20_net(moves, holder)
    tokens[WBNB] -= g
    assert all(v == 0 for v in tokens.values())
    f = summarize_fee(r)
    assert f["receipt_fee_wei"] == 0
    return f | {
        "cycle": case,
        "token_moves": transfers,
        "native_calls": native,
        "sender_executor_native_change_after_gas": 0,
        "sender_executor_token_net": tokens,
        "payout_beneficial_ownership": None,
        "block_fee_recipient": h["miner"],
        "archive_complete_successful_frames": len(src),
    }


def check_wires(plan, native):
    evidence = {}
    for lane in ("rh", "bsc", "ely", "cash-bsc", "cash-rh"):
        folder = D / lane
        cash = lane.startswith("cash-")
        policy = (
            (BudgetPolicy(lane[5:], native["limits"][lane[5:]], 250),)
            if cash
            else tuple(
                BudgetPolicy(n, c, 240)
                for n, c in plan["cap"].items()
                if n == lane or n.startswith(lane + "-")
            )
        )
        stop = read(folder / "STOP.json")
        assert stop["scope"] == (NATIVE if cash else SCOPE)
        with RpcBudget(folder / "original.sqlite3", policy, read_only=True) as b:
            assert b.counts() == stop["counts"]
        raw = {}
        for q in sorted(folder.glob("*.request")):
            label = q.stem
            res = folder / (label + ".response")
            m = read(folder / (label + ".meta"))
            assert m["request_sha256"] == digest(q) and m["response_sha256"] == digest(res)
            assert m["utc"] <= m["done"] if cash else m["started"] <= m["finished"]
            if cash:
                req = read(q)
                assert req["url"] == m["source"]
                if lane == "cash-bsc":
                    assert (
                        req == {"method": "POST", "url": native["bsc"], "body": native["bsc_query"]}
                        and m["http"] == 200
                    )
                else:
                    assert (
                        req
                        == {"method": "GET", "url": native["rh"] + "/state-changes", "body": None}
                        and m["http"] == 403
                    )
            else:
                req = read(q)
                reply = read(res)
                assert (
                    req["jsonrpc"] == reply["jsonrpc"] == "2.0"
                    and req["id"] == m["seq"] == reply["id"]
                )
                assert m["source"] in {plan["source"][p.source] for p in policy}
            raw[label] = m
        assert len(raw) == sum(stop["counts"].values())
        evidence[lane] = {"stop": stop, "wire": raw}
    assert (
        read(D / "bsc/trace.response")["error"]["code"] == 15
        and evidence["bsc"]["wire"]["trace"]["http"] == 429
    )
    assert read(D / "bsc/trace.request")["params"] == [
        BSC_TX,
        {"tracer": "callTracer", "tracerConfig": {"withLog": True}, "timeout": "15s"},
    ]
    assert (
        evidence["rh"]["stop"]["error"] == "ValueError: RPC request budget exhausted"
        and evidence["rh"]["stop"]["counts"]["rh-t"] == 0
    )
    return evidence


def main():
    plan = read(D / "scope.json")
    native = read(D / "native-scope.json")
    assert (
        digest(D / "scope.json") == native["previous_scope"] == SCOPE
        and digest(D / "native-scope.json") == NATIVE
    )
    assert (
        digest(D / "capture.py") == plan["driver"]
        and digest(D / "native.py") == native["source_file"]
    )
    wires = check_wires(plan, native)
    bsc, b = read_cohort("bsc", plan)
    rh, r = read_cohort("rh", plan)
    old_catalog = D.parent / "gold-book-20261127/all-hashes.tsv.gz"
    assert digest(old_catalog) == "400147164406c06fc29ce20ed28f43a29b39cb331d12442e11db40ac60dc8648"
    with gzip.open(old_catalog, "rt") as stream:
        prior = {(v["chain"], v["tx_hash"]) for v in csv.DictReader(stream, delimiter="\t")}
    for name, cohort in (("bsc", bsc), ("robinhood", rh)):
        cohort["already_in_frozen_catalog_ge3"] = sum(
            (name, candidate["tx"]) in prior for candidate in cohort["all_3_or_more"]
        )
    assert bsc["ranked"][0]["tx"] == BSC_TX and rh["ranked"][0]["tx"] == RH_TX
    bsc["widest_funding"] = bsc_wide(b[BSC_TX][0])
    bsc["two_pool_final_cash"] = archive_cash(*b[SMALL], native)
    rh["widest_funding"] = rh_rounds(r[RH_TX][0])
    for c, known in [(bsc, b), (rh, r)]:
        row = c["control"]
        row["receipt"] = summarize_fee(known[row["tx"]][0]) if row["tx"] in known else None
    # Keep every successful same-holder WBNB closure in the complete BSC background,
    # giving two-leg paths the same membership test as the longer routes.
    digest_rows = []
    for rec, _ in b.values():
        count = sum(matches(log, plan["topics"]) for log in rec["logs"])
        if count >= 2:
            _, cycles = receipt_closures(rec, transfer_rows(rec), {})
            if cycles:
                digest_rows.append(
                    summarize_fee(rec)
                    | {
                        "initial_screen_events": count,
                        "cycles": [{k: v for k, v in c.items() if k != "legs"} for c in cycles],
                    }
                )
    assert [
        x["transaction"]
        for x in digest_rows
        if x["initial_screen_events"] == 2
        and any(int(v["gross_wbnb_wei"]) > 0 for v in x["cycles"])
    ] == [SMALL]
    bsc["all_receipt_linked_wbnb_closures"] = digest_rows
    etx = "0x4c88853a130363551076e2c66ae13bcd2fcb049b43494d615fc4aeb76d478ef7"
    er = read(D / "ely/receipt.response")["result"]
    assert (
        er["transactionHash"] == etx
        and er["blockHash"] == "0x8b315ceedff3d2846fdf14e9fdef2ad06af65736c49b8375ac6df302e55be101"
    )
    assert decoded(D / "ely", "chain", "eth_chainId", [], plan["source"]["ely"])[0] == hex(99901)
    assert (
        decoded(D / "ely", "receipt", "eth_getTransactionReceipt", [etx], plan["source"]["ely"])[0]
        == er
    )
    assert read(D / "ely/state.request")["params"] == [
        etx,
        {"tracer": "prestateTracer", "tracerConfig": {"diffMode": True}, "timeout": "15s"},
    ]
    assert read(D / "ely/state.response")["error"]["code"] == -32601
    return {
        "schema": "oasis.y2026.bsc-robinhood-landed.v1",
        "scope_sha256": SCOPE,
        "cash_scope_sha256": NATIVE,
        "cost_usd": 0,
        "request_count": sum(sum(x["stop"]["counts"].values()) for x in wires.values()),
        "bsc": bsc,
        "robinhood": rh,
        "elysium": {
            "retained_winner": summarize_fee(er),
            "state_diff": None,
            "denial": read(D / "ely/state.response")["error"],
        },
        "evidence": wires,
    }


if __name__ == "__main__":
    if not __debug__:
        raise SystemExit("run without -O")
    print(json.dumps(main(), indent=2, sort_keys=True))
