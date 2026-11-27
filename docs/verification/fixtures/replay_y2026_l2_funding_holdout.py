"""Rebuild disjoint L2 samples and verify integer funding and fee evidence offline."""

import json
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

from eth_account import Account
from eth_hash.auto import keccak
from replay_multihop_second_shard_receipts import user_transaction
from replay_y2026_bsc_rh_landed import RH_WETH, USDG, V4, erc20_net, keys, matches, rooted
from replay_y2026_era_window import ETH, rebuild_hash
from replay_y2026_eth_bsc_windows import transfer_rows
from replay_y2026_inventory import digest, read
from replay_y2026_op_arb import budget_count, header_hash, receipt_commitment, wire
from replay_y2026_open_tail import observed_fee, receipt_paths, row_roles

from oasis_agent.arbitrage.cycle_protocols import abi, data_bytes, topic
from oasis_agent.evidence.nitro_block import nitro_header
from oasis_agent.rpc.budget import BudgetPolicy

D = Path(__file__).resolve().parents[3] / "reports/research/y2026-l2-heldout-20261128"
SCOPE = "36c7641c8154371e36f28bf2643f186a9a3e0682a52e51f1e282c7fbe98f9d5b"
FOLLOW = "7d6d04035f569bb4d78f10e64bbde9f2494e3e9377e53e4a16d327817dfcdf38"
RH_TRACE = "faf3d9a461cf06f97001e54e453e00f9aac2a3b75e3abe19e5e3d3c4af0e1c26"
WETH_ERA = "0x5aea5775959fbc2557cc8789bc1bf90a239d9a91"
WIDEST_RH = "0xe7f0ad686473fcd982ea1f038fbba99f093b2985ae20be7139ccf4413ea2edb0"
ERA_TWO_ROUNDS = "0x7aef1059efbce419afbd35ac9aa3115ced233a15b779b1e365ca478737c2271d"
RH_CLOSED = "0x01cac6f5612b22d2379d1bebf7e316825d7e2809c13e761e88ee8cdec9aecc81"
ARB_SPLIT = "0xc71634a223381b144614fd5cadeb17aea8447d33156266839ea2de284bdf929a"
BALANCER = topic("Swap(bytes32,address,address,uint256,uint256)")
DELTA_TYPES = ("int128", "int128", "uint160", "uint128", "int24", "uint24")
TIERS = (1, 2, 3, 5, 10, 20, 50, 100, 200)
TOPIC_NAMES = read(D.parent / "y2026-open-tail-20261127/scope.json")["topics"]


def q(s):
    return int(s, 16)


def verify_header(cid, h):
    if cid in (4663, 42161):
        nitro_header(h, q(h["number"]), h["hash"], chain_id=cid)
    elif cid == 324:
        assert rebuild_hash(h) == h["hash"]
    else:
        assert header_hash(h) == h["hash"]
    stamp = datetime.fromtimestamp(q(h["timestamp"]), UTC)
    assert stamp.year == 2026
    return stamp.isoformat()


class Lane:
    def __init__(self, folder, name, cap, plan, scope):
        self.folder = folder
        self.stop = read(folder / "STOP.json")
        assert self.stop["scope"] == scope
        self.count = budget_count(
            folder, BudgetPolicy(name, cap, plan["interval_ms"]), self.stop["counts"]
        )
        self.frozen = plan["frozen"]
        self.sources = {}

    def get(self, label, method, args, url):
        result, evidence = wire(self.folder, label, method, args)
        assert evidence["url"] == url and label not in self.sources
        assert (
            self.frozen
            <= evidence["started_utc"]
            <= evidence["finished_utc"]
            <= self.stop["finished"]
        )
        self.sources[label] = evidence
        if result is None:
            assert self.stop["error"] and (evidence["error"] or evidence["http"] != 200)
        return result

    def check(self):
        assert len(self.sources) == self.count
        assert sorted(e["id"] for e in self.sources.values()) == list(range(1, self.count + 1))
        return {"stop": self.stop, "request_evidence": self.sources}


def add_window(grouped, events, first, last, endpoints, plan):
    all_topics = set(plan["topics"]) | set(plan["financing"])
    positions = [tuple(keys(event)[:1] + keys(event)[4:5]) for event in events]
    assert positions == sorted(set(positions))
    block_hashes = {q(h["number"]): h["hash"] for h in endpoints}
    for e in events:
        b, h, idx, tx, *_ = keys(e)
        assert first <= b <= last and e["removed"] is False
        assert e["topics"][0] in all_topics and block_hashes.setdefault(b, h) == h
        g = grouped[tx]
        assert not g or keys(g[0])[:4] == [b, h, idx, tx]
        g.append(e)
    return {
        "first": first,
        "last": last,
        "first_hash": block_hashes[first],
        "last_hash": block_hashes[last],
        "first_utc": datetime.fromtimestamp(q(endpoints[0]["timestamp"]), UTC).isoformat(),
        "last_utc": datetime.fromtimestamp(q(endpoints[1]["timestamp"]), UTC).isoformat(),
        "logs_including_financing": len(events),
    }


def markets(group, plan):
    return [e for e in group if matches(e, plan["topics"])]


def ranked(grouped, plan, limit):
    chosen = []
    seen = set()
    order = sorted(
        (markets(g, plan) for g in grouped.values()),
        key=lambda g: (
            (-len(g), -len({e["address"] for e in g}), *keys(g[0])[0:3:2]) if g else (0, 0, 0, 0)
        ),
    )
    for g in order:
        if len(g) < 2:
            break
        n, _, _, tx, *_ = keys(g[0])
        if n not in seen:
            chosen.append({"height": n, "tx": tx})
            seen.add(n)
        if len(chosen) == limit:
            break
    return chosen


def verify_case(name, net, selected, grouped, plan, detail, *, secondary_by_number=False):
    cid, hsrc, rsrc = net[:3]
    n, tx = selected["height"], selected["tx"]
    e = grouped[tx][0]
    h = detail.get(f"h{n}", "eth_getBlockByHash", [e["blockHash"], False], hsrc)
    assert q(h["number"]) == n and h["hash"] == e["blockHash"]
    stamp = verify_header(cid, h)
    number_mode = name == "era" or secondary_by_number
    rows = detail.get(f"r{n}", "eth_getBlockReceipts", [hex(n) if number_mode else h["hash"]], rsrc)
    assert [r["transactionHash"] for r in rows] == h["transactions"]
    receipt = rows[q(e["transactionIndex"])]
    assert receipt["transactionHash"] == tx and receipt["blockHash"] == h["hash"]
    if name in ("robinhood", "arbitrum"):
        block = rooted(cid, h, rows)
        proof = "ordered_receipt_trie_and_header"
    elif name == "optimism":
        b, root = receipt_commitment(cid, h, rows)
        block = {
            "height": b.number,
            "hash": b.block_hash,
            "receipts_root": root,
            "transactions": len(rows),
        }
        proof = "ordered_receipt_trie_and_header"
    else:
        independent = detail.get(f"t{n}", "eth_getTransactionReceipt", [tx], hsrc)
        assert independent == receipt
        block = {"height": n, "hash": h["hash"], "transactions": len(rows)}
        proof = "rolling_transaction_header_and_independent_receipt"
    expected = [keys(e) for e in grouped[tx]]
    actual = [
        keys(x) for x in receipt["logs"] if matches(x, set(plan["topics"]) | set(plan["financing"]))
    ]
    assert expected == actual
    market = markets(grouped[tx], plan)
    logical = {
        (
            e["address"],
            e["topics"][1] if e["topics"][0] in (V4, BALANCER) else None,
        )
        for e in market
    }
    funding = receipt_paths(receipt, TOPIC_NAMES)
    network = (
        {
            "receipt_network_wei": q(receipt["gasUsed"]) * q(receipt["effectiveGasPrice"]),
            "included_l1_gas": q(receipt["gasUsedForL1"]),
        }
        if name == "robinhood"
        else observed_fee(name, receipt)
    )
    return {
        "tx": tx,
        "index": q(receipt["transactionIndex"]),
        "utc": stamp,
        "block": block,
        "receipt_proof": proof,
        "fee_payer": receipt["from"],
        "target": receipt["to"],
        "market_events": len(market),
        "logical_market_keys": len(logical),
        "funding": funding,
        "receipt_transfer_deltas_for_roles": row_roles(receipt),
        "fee": network,
        "linked_cash": None,
    }, receipt


def index_initial(name, net, plan):
    cid, primary, secondary, width, offsets, minimum = net
    loc = D / name
    selection = read(loc / "selection.json")
    lane = Lane(loc / "index", "index", plan["index_limit"], plan, SCOPE)
    grouped = defaultdict(list)
    assert q(lane.get("chain", "eth_chainId", [], primary)) == cid
    anchor = lane.get("anchor", "eth_getBlockByNumber", ["latest", False], primary)
    verify_header(cid, anchor)
    height = q(anchor["number"])
    windows = []
    completed = []
    all_topics = sorted(set(plan["topics"]) | set(plan["financing"]))
    for offset in offsets:
        last = height - offset
        first = last - width + 1
        if first <= minimum:
            assert lane.stop["error"]
            break
        candidates = []
        for i, n in enumerate((first, last)):
            h = lane.get(f"head-{offset}-{i}", "eth_getBlockByNumber", [hex(n), False], primary)
            assert h and q(h["number"]) == n
            verify_header(cid, h)
            candidates.append(h)
        events = lane.get(
            f"logs-{offset}",
            "eth_getLogs",
            [{"fromBlock": hex(first), "toBlock": hex(last), "topics": [all_topics]}],
            primary,
        )
        if events is None:
            break
        windows.append(add_window(grouped, events, first, last, candidates, plan))
        completed.append(offset)
    assert completed == selection["completed_windows"]
    results = []
    proof = {}
    rs = {}
    if completed:
        detail = Lane(loc / "detail", "detail", plan["detail_limit"], plan, SCOPE)
        assert q(detail.get("chain", "eth_chainId", [], secondary)) == cid
        assert ranked(grouped, plan, 4) == selection["details"]
        for selected in selection["details"]:
            result, receipt = verify_case(name, net, selected, grouped, plan, detail)
            results.append(result)
            rs[result["tx"]] = receipt
        proof = detail.check()
    return grouped, windows, results, rs, {"index": lane.check(), "detail": proof}


def index_follow(initial, grouped):
    p = D / "follow"
    plan = read(p / "scope.json")
    assert digest(p / "scope.json") == FOLLOW and plan["parent"] == SCOPE
    assert digest(p / "capture.py") == plan["driver"] and plan["transport"] == initial["transport"]
    op = Lane(p / "op", "op", 19, plan, FOLLOW)
    net = plan["optimism"]
    cid, src, _ = net
    assert q(op.get("chain", "eth_chainId", [], src)) == cid
    windows = []
    groups = defaultdict(list)
    all_topics = sorted(set(initial["topics"]) | set(initial["financing"]))
    for i, (a, b) in enumerate(plan["op_spans"]):
        if not (op.folder / f"a{i}.meta.json").exists():
            break
        endpoints = []
        for label, n in (("a", a), ("b", b)):
            h = op.get(f"{label}{i}", "eth_getBlockByNumber", [hex(n), False], src)
            assert q(h["number"]) == n
            verify_header(cid, h)
            endpoints.append(h)
        events = op.get(
            f"logs{i}",
            "eth_getLogs",
            [{"fromBlock": hex(a), "toBlock": hex(b), "topics": [all_topics]}],
            src,
        )
        if events is None:
            break
        windows.append(add_window(groups, events, a, b, endpoints, initial))
    cases = []
    op_receipts = {}
    for entry in op.stop["result"] or []:
        data, rec = verify_case(
            "optimism", net, entry, groups, initial, op, secondary_by_number=True
        )
        cases.append(data)
        op_receipts[data["tx"]] = rec
    if cases:
        assert ranked(groups, initial, 3) == op.stop["result"]
    rh = Lane(p / "rh", "rh", 17, plan, FOLLOW)
    net = plan["robinhood"]
    assert q(rh.get("chain", "eth_chainId", [], net[2])) == net[0]
    assert rh.stop["result"] == plan["rh_candidates"]
    picked = []
    rh_receipts = {}
    for entry in plan["rh_candidates"]:
        case, rec = verify_case("robinhood", net, entry, grouped, initial, rh)
        picked.append(case)
        rh_receipts[case["tx"]] = rec
    return (groups, windows, cases, op_receipts, op.check()), (picked, rh_receipts, rh.check())


def era_rounds(case, receipt):
    g = case["funding"]
    holder = receipt["to"]
    target = [14, 5, 8, 12]
    assert g["longest_linked"] == target and holder != receipt["from"]
    (a, b, total) = (
        next(c for c in g["closed_at_same_holder"] if c["legs"] == s)
        for s in (target[:2], target[2:], target)
    )
    assert (a["asset"], b["asset"], total["asset"]) == (WETH_ERA,) * 3
    assert (
        a["received_raw"] == b["paid_raw"]
        and a["gross_raw"] + b["gross_raw"] == total["gross_raw"] > 0
    )
    assert all(x["holder"] == holder for x in (a, b, total))
    by = {r["event"]: r for r in g["receipt_legs"]}
    assert len({by[i]["pool"] for i in target}) == 4
    assert [(by[i]["in_log"], by[i]["out_log"]) for i in target] == [
        (13, 2),
        (2, 3),
        (7, 6),
        (6, 9),
    ]
    movement = transfer_rows(receipt)
    net = erc20_net(movement, holder)
    assert {asset: amount for asset, amount in net.items() if amount} == {
        WETH_ERA: total["gross_raw"]
    }
    fee = case["fee"]
    assert fee["payer_fee_reconciled"]
    assert all(x["asset"] != ETH for x in movement if holder in (x["from"], x["to"]))
    return {
        "whole_funding_path": target,
        "amount_per_round": [a, b],
        "full_reinvestment": True,
        "whole_weth_gross_wei": total["gross_raw"],
        "visible_eth_fee_wei": fee["receipt_network_wei"],
        "hypothetical_net_wei_at_parity_and_common_owner": total["gross_raw"]
        - fee["receipt_network_wei"],
        "beneficiary_net": None,
    }


def widest_robinhood(case, receipt):
    movement = transfer_rows(receipt)
    t = {x["log"]: x for x in movement}
    trader = t[3]["from"]
    holder = receipt["to"]
    assert trader == t[97]["to"] != receipt["from"] and t[3]["to"] == t[97]["from"] == holder
    assert t[97]["asset"] == USDG and t[3]["asset"] != USDG
    assert {k: v for k, v in erc20_net(movement, trader).items() if v} == {
        t[3]["asset"]: -t[3]["amount"],
        USDG: t[97]["amount"],
    }
    assert t[67]["asset"] == t[86]["asset"] == RH_WETH
    assert case["market_events"] == 19 and not case["funding"]["closed_at_same_holder"]
    positions = {q(e["logIndex"]): e for e in receipt["logs"]}
    by_id = {k: e for k, e in positions.items() if matches(e, (V4,))}
    assert list(by_id) == [4, 43, 69, 77, 88] and len({e["topics"][1] for e in by_id.values()}) >= 2
    return {
        "input_recipient": holder,
        "end_recipient": trader,
        "input": t[3],
        "output": t[97],
        "identified_open_order": True,
        "payer": receipt["from"],
        "visible_network_fee_wei": case["fee"]["receipt_network_wei"],
        "proved_owner_pnl": None,
    }


def closed_robinhood(case, receipt):
    (cycle,) = case["funding"]["closed_at_same_holder"]
    holder = cycle["holder"]
    assert cycle["legs"] == [58, 50, 53, 56] and holder == receipt["to"] != receipt["from"]
    assert cycle["asset"] == RH_WETH
    legs = case["funding"]["receipt_legs"]
    assert len({leg["pool"] for leg in legs}) == len(legs) == 4
    assert all(leg["payer"] == leg["receiver"] == holder for leg in legs)
    net = erc20_net(transfer_rows(receipt), holder)
    assert {asset: amount for asset, amount in net.items() if amount} == {
        RH_WETH: cycle["gross_raw"]
    }
    fee = case["fee"]["receipt_network_wei"]
    assert 0 < cycle["gross_raw"] < fee
    return cycle | {
        "conditional_weth_eth_residue": cycle["gross_raw"] - fee,
        "visible_gas_wei": fee,
        "beneficiary_net": None,
    }


def arbitrum_split(case, receipt):
    t = {x["log"]: x for x in transfer_rows(receipt)}
    e = {q(x["logIndex"]): x for x in receipt["logs"]}
    holder = receipt["to"]

    def amounts(pay, receive, venue):
        assert t[pay]["from"] == t[receive]["to"] == holder
        assert t[pay]["to"] == t[receive]["from"] == venue
        return t[pay]["amount"], t[receive]["amount"]

    curve_types = [("int128", "uint256", "int128", "uint256"), ("uint256",) * 4]
    for log, pay, receive, types in zip((15, 23), (12, 24), (14, 26), curve_types, strict=True):
        assert e[log]["topics"][0] == topic("TokenExchange(address," + ",".join(types) + ")")
        assert "0x" + e[log]["topics"][1][-40:] == holder
        assert abi(types, data_bytes(e[log]["data"]))[1::2] == amounts(
            pay, receive, e[log]["address"]
        )
    assert "0x" + e[22]["topics"][1][-40:] == "0x" + e[22]["topics"][2][-40:] == holder
    amounts(21, 20, e[22]["address"])
    manager = e[35]["address"]
    assert manager == "0x360e68faccca8ca495c1b759fd9eee466db9fb32"
    assert e[35]["topics"][0] == V4 and "0x" + e[35]["topics"][2][-40:] == holder
    assert t[37]["asset"] < t[36]["asset"]
    amt_in, amt_out = amounts(36, 37, manager)
    assert abi(DELTA_TYPES, data_bytes(e[35]["data"]))[:2] == (amt_out, -amt_in)
    assert case["funding"]["longest_linked"] == [31, 34]
    assert t[32]["asset"] == t[36]["asset"] and t[32]["amount"] == t[36]["amount"]
    assert t[20]["asset"] == t[24]["asset"] == t[29]["asset"]
    assert t[14]["asset"] == t[21]["asset"] == t[37]["asset"]
    assert t[12]["asset"] == t[26]["asset"]
    net = {
        t[20]["asset"]: t[20]["amount"] - t[24]["amount"] - t[29]["amount"],
        t[14]["asset"]: t[14]["amount"] + t[37]["amount"] - t[21]["amount"],
        t[12]["asset"]: t[26]["amount"] - t[12]["amount"],
    }
    assert {k: v for k, v in erc20_net(list(t.values()), holder).items() if v} == net
    assert all(v > 0 for v in net.values())
    return {
        "all_roles_net_raw_for_executor": net,
        "paired_market_logs_by_cash_flow": [15, 22, [23, [31, 34, 35]]],
        "weth_split": [t[i] for i in (20, 24, 29)],
        "usdc_settlement": [t[i] for i in (14, 21, 37)],
        "v4_pool_id": e[35]["topics"][1],
        "owner_all_cost_net": None,
    }


def rh_trace_audit(network, receipts, prior):
    folder = D / "rh-trace"
    plan = read(folder / "scope.json")
    assert digest(folder / "scope.json") == RH_TRACE and plan["parent"] == FOLLOW
    assert (
        digest(folder / "capture.py") == plan["driver"] and plan["transport"] == prior["transport"]
    )
    lane = Lane(folder, "trace", plan["caps"], plan, RH_TRACE)
    assert lane.count == 3
    sampled = {}
    for label, method, args in plan["calls"][: lane.count]:
        sampled[label] = lane.get(label, method, args, plan["source"])
    assert q(sampled["chain"]) == 4663 and sampled["calls"] is None
    assert lane.sources["calls"]["error"]["code"] == -32601
    raw = sampled["tx"]
    receipt = receipts[plan["chosen"]["tx"]]
    signed = user_transaction(raw, 4663)
    assert raw["hash"] == receipt["transactionHash"] == "0x" + keccak(signed).hex()
    assert Account.recover_transaction(signed).lower() == raw["from"] == receipt["from"]
    assert all(raw[k] == receipt[k] for k in ("from", "to", "blockHash", "transactionIndex"))
    positions = {q(x["logIndex"]): x for x in receipt["logs"]}
    manager = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
    v4 = [
        (i, positions[i]["topics"][1], abi(DELTA_TYPES, data_bytes(positions[i]["data"]))[:2])
        for i in (26, 27, 28, 29, 35, 36)
    ]
    assert all(
        positions[i]["address"] == manager and positions[i]["topics"][0] == V4 for i, _, _ in v4
    )
    assert len({positions[i]["topics"][2] for i, _, _ in v4}) == 1
    t = {x["log"]: x for x in transfer_rows(receipt)}
    assert t[30]["to"] == t[32]["from"] == manager and t[30]["from"] == t[32]["to"]
    assert t[32]["amount"] == v4[3][2][1] and t[30]["amount"] == -v4[4][2][0]
    case = next(c for c in network if c["tx"] == raw["hash"])
    assert case["funding"]["longest_linked"] == [34]
    case["linked_cash"] = {
        "signature_hash_and_sender_recovered": True,
        "v4_raw_delta_logs": v4,
        "explicit_v4_to_v3_v4_transfers": [t[32], t[30]],
        "beneficiary_net": None,
        "full_serial_depth": None,
    }
    return lane.check()


def summarize(grouped, windows, cases, plan):
    relevant = [(tx, markets(g, plan)) for tx, g in grouped.items()]
    counts = Counter(len(g) for _, g in relevant if g)
    return {
        "windows": windows,
        "complete_blocks": sum(w["last"] - w["first"] + 1 for w in windows),
        "market_hits": sum(counts.values()),
        "market_event_tail": max(counts, default=None),
        "thresholds": {
            k: sum(v for n, v in counts.items() if n >= k) if windows else None for k in TIERS
        },
        "multi_event_keys": [
            [tx, len(g), q(g[0]["blockNumber"]), q(g[0]["transactionIndex"])]
            for tx, g in relevant
            if len(g) >= 2
        ],
        "cases": cases,
    }


def main():
    p = read(D / "scope.json")
    assert digest(D / "scope.json") == SCOPE and digest(D / "capture.py") == p["driver"]
    assert digest(D.parent / "y2026-op-arb-20261127/capture.py") == p["transport"]
    scans = {k: index_initial(k, net, p) for k, net in p["networks"].items()}
    (og, ow, oc, or_, op), (rh_cases, rh_receipts, rh) = index_follow(p, scans["robinhood"][0])
    output = {}
    evidence = {}
    for name, (groups, windows, cases, receipts, refs) in scans.items():
        if name == "optimism":
            groups.update(og)
            windows.extend(ow)
            cases.extend(oc)
            receipts.update(or_)
            refs["new_disjoint_window"] = op
        if name == "robinhood":
            cases.extend(rh_cases)
            receipts.update(rh_receipts)
            refs["amount_link_holdout"] = rh
        for case in cases:
            if case["tx"] == ERA_TWO_ROUNDS:
                case["linked_cash"] = era_rounds(case, receipts[case["tx"]])
            if case["tx"] == WIDEST_RH:
                case["linked_cash"] = widest_robinhood(case, receipts[case["tx"]])
            if case["tx"] == RH_CLOSED:
                case["linked_cash"] = closed_robinhood(case, receipts[case["tx"]])
            if case["tx"] == ARB_SPLIT:
                case["linked_cash"] = arbitrum_split(case, receipts[case["tx"]])
        if name == "robinhood":
            refs["amount_rank_first_trace"] = rh_trace_audit(cases, receipts, p)
        output[name] = summarize(groups, windows, cases, p)
        evidence[name] = refs
    requests = sum(
        len(x["request_evidence"]) for lane in evidence.values() for x in lane.values() if x
    )
    return {
        "schema": "oasis.y2026-l2-funding-holdout.v1",
        "sources": evidence,
        "chains": output,
        "scope_hashes": [SCOPE, FOLLOW, RH_TRACE],
        "rpc": requests,
        "usd": 0,
    }


if __name__ == "__main__":
    print(json.dumps(main(), indent=2, sort_keys=True))
