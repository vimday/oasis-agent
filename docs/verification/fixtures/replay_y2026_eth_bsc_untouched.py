"""Recompute the two anonymous whole-block panels and exact receipt-level funding links."""

import csv
import gzip
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from eth_account import Account
from eth_hash.auto import keccak
from replay_multihop_second_shard_receipts import user_transaction
from replay_y2026_bsc_rh_landed import matches, rooted, summarize_fee
from replay_y2026_eth_bsc_windows import DEPOSIT, PC3, V2, V3, WBNB, WITHDRAWAL, dec_address
from replay_y2026_op_arb import header_hash, moves, receipt_commitment, sha_file, wire
from replay_y2026_open_tail import receipt_paths

from oasis_agent.arbitrage.cycle_protocols import topic
from oasis_agent.evidence.morpho_callback import morpho_callbacks
from oasis_agent.evidence.public_receipts import decode_receipt, quantity
from oasis_agent.evidence.research_traces import first_failure, nested_calls
from oasis_agent.evidence.swap_flow_trace import replay_trace_funding
from oasis_agent.rpc.budget import BudgetPolicy, RpcBudget
from scripts.replay_eth_tail_case import _settlement

ROOT = Path(__file__).resolve().parents[3] / "reports/research"
D = ROOT / "y2026-eth-bsc-untouched-20261128"
OLD = ROOT / "gold-book-20261127/all-hashes.tsv.gz"
OLD_SHA = "400147164406c06fc29ce20ed28f43a29b39cb331d12442e11db40ac60dc8648"
SCOPE = "275c9d34709c5dc1dfdfd6da8c21cf3be01f498a632a9cb41fafef3ce8a2a64d"
CASH_SCOPE = "3d4b423f34e226476a325c6d05e6c7ec82bf6b380735eba912b41fe9632de4f4"
WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
TIERS = (1, 2, 3, 5, 10, 20, 50, 100, 200)
PATH_TOPICS = {
    V2: "v2",
    V3: "v3",
    PC3: "pancake_v3_shape",
    topic("Swap(address,address,uint256,uint256,uint256,uint256)"): "solidly",
    topic("Swap(address,address,bool,bool,uint256,uint256,int32)"): "maverick_v1",
}
FLASH = {
    topic("FlashLoan(address,address,uint256)"),
    topic("FlashLoan(address,address,uint256,uint256)"),
    topic("FlashLoan(address,address,address,uint256,uint8,uint256,uint16)"),
}


def scan(name, plan, old):
    folder = D / name
    stop = json.loads((folder / "STOP.json").read_bytes())
    s = plan["specs"][name]
    chain = s["chain"]
    expected = s["width"] * len(s["offsets"])
    policies = (BudgetPolicy("h", expected + 2, 250), BudgetPolicy("r", expected + 1, 250))
    with RpcBudget(folder / "original.sqlite3", policies, read_only=True) as ledger:
        assert ledger.counts() == stop["counts"] == {"h": expected + 2, "r": expected + 1}
    assert stop["scope"] == SCOPE and stop["error"] is None
    records = {}

    def rpc(key, label, method, params):
        value, info = wire(folder, label, method, params)
        assert value is not None and info["error"] is None and info["url"] == s[key]
        assert plan["utc"] <= info["started_utc"] <= info["finished_utc"] <= stop["utc"]
        assert label not in records
        records[label] = info
        return value

    for key in ("h", "r"):
        assert quantity(rpc(key, key + "-id", "eth_chainId", [])) == chain
    head = rpc("h", "anchor", "eth_getBlockByNumber", ["latest", False])
    if chain == 1:
        assert header_hash(head) == head["hash"]
    heights = quantity(head["number"])
    blocks = []
    tally = Counter()
    candidates = []
    seen = set()
    failure_fees = 0
    fee_asset = WETH if chain == 1 else WBNB
    for delta in s["offsets"]:
        previous = None
        for n in range(heights - delta, heights - delta + s["width"]):
            h = rpc("h", f"h{n}", "eth_getBlockByNumber", [hex(n), False])
            assert quantity(h["number"]) == n
            assert previous is None or h["parentHash"] == previous
            previous = h["hash"]
            receipts = rpc("r", f"r{n}", "eth_getBlockReceipts", [previous])
            moment = datetime.fromtimestamp(quantity(h["timestamp"]), UTC)
            assert moment.year == 2026
            if chain == 56:
                block = rooted(chain, h, receipts)
            else:
                _, root = receipt_commitment(chain, h, receipts)
                block = {
                    "height": n,
                    "hash": previous,
                    "receipts_root": root,
                    "transactions": len(receipts),
                    "reverts": sum(not quantity(r["status"]) for r in receipts),
                }
            blocks.append(block | {"utc": moment.isoformat()})
            for r in receipts:
                hsh = r["transactionHash"]
                assert hsh not in seen
                seen.add(hsh)
                count = sum(matches(event, plan["topics"]) for event in r["logs"])
                tally[count] += 1
                if not quantity(r["status"]):
                    assert not r["logs"]
                    failure_fees += summarize_fee(r)["receipt_fee_wei"]
                if count < 2:
                    continue
                paths = receipt_paths(r, PATH_TOPICS)
                hint = sum(matches(event, FLASH) for event in r["logs"])
                gain_paths = paths["closed_at_same_holder"]
                candidate = summarize_fee(r) | {
                    "known_old_hash": hsh in old,
                    "market_events": count,
                    "financing_event_shapes": hint,
                    "receipt_bound_legs": len(paths["receipt_legs"]),
                    "linked_depth": len(paths["longest_linked"]),
                    "longest_linked": paths["longest_linked"],
                    "longest_same_holder_close": max(
                        (len(c["legs"]) for c in gain_paths), default=0
                    ),
                    "closures": gain_paths,
                }
                if gain_paths or candidate["linked_depth"] >= 4:
                    candidate["market_legs"] = paths["receipt_legs"]
                    candidate["links"] = paths["unique_edges"]
                positive = [c for c in gain_paths if c["asset"] == fee_asset and c["gross_raw"] > 0]
                if positive:
                    candidate["best_wrapped_native_gross_minus_network_fee_wei"] = (
                        max(c["gross_raw"] for c in positive) - candidate["receipt_fee_wei"]
                    )
                candidates.append(candidate)
    assert [b["height"] for b in blocks] == stop["heights"]
    assert len(records) == sum(stop["counts"].values()) == len(list(folder.glob("*.request.json")))
    return {
        "chain_id": chain,
        "blocks": blocks,
        "stop": stop,
        "requests": records,
        "event_histogram": tally,
        "thresholds": {
            tier: sum(cnt for events, cnt in tally.items() if events >= tier) for tier in TIERS
        },
        "context_failed_receipt_fee_wei": failure_fees,
        "new_at_least_two": sum(not r["known_old_hash"] for r in candidates),
        "all_at_least_two": candidates,
    }


def read_cash_lane(cash, label, lane, frozen):
    folder = cash / label
    stop = json.loads((folder / "STOP.json").read_bytes())
    assert stop["scope"] == CASH_SCOPE
    cap = len(lane["calls"])
    with RpcBudget(
        folder / "original.sqlite3", (BudgetPolicy(label, cap, 300),), read_only=True
    ) as ledger:
        assert ledger.counts() == stop["counts"]
    observed = stop["counts"][label]
    assert 0 < observed <= cap and len(list(folder.glob("*.request.json"))) == observed
    result, provenance = {}, {}
    for i, (key, method, params) in enumerate(lane["calls"][:observed], 1):
        meta_path = folder / (key + ".meta.json")
        meta = json.loads(meta_path.read_bytes())
        assert meta["id"] == i and meta["url"] == lane["url"]
        assert frozen["utc"] <= meta["started_utc"] <= meta["finished_utc"] <= stop["utc"]
        provenance[key] = meta
        if i == observed and stop["error"]:
            assert meta["http"] == 429 and meta["transport_error"] == stop["error"]
            request = folder / (key + ".request.json")
            assert json.loads(request.read_bytes()) == {
                "jsonrpc": "2.0",
                "id": i,
                "method": method,
                "params": params,
            }
            assert sha_file(request) == meta["request_sha256"]
            assert sha_file(folder / (key + ".response")) == meta["response_sha256"]
        else:
            value, info = wire(folder, key, method, params)
            assert value is not None and info["error"] is None
            result[key] = value
    assert stop["done"] == list(result)
    if result:
        assert quantity(result.pop("chain")) == lane["chain_id"]
    return result, {"stop": stop, "provenance": provenance}


def ethereum_cash(header, block, r, tx, traces):
    h = tx["hash"]
    tree = traces[h + "-calls"]
    delta = traces[h + "-diff"]
    frames = nested_calls(tree)
    payer = r["from"]
    before, after = [delta[side][payer] for side in ("pre", "post")]
    assert before["nonce"] == quantity(tx["nonce"]) and after["nonce"] == before["nonce"] + 1
    actual_wei = quantity(after["balance"]) - quantity(before["balance"])
    ref = decode_receipt(r, block, h, quantity(tx["transactionIndex"]))
    funding = replay_trace_funding(ref, tree)
    result = _settlement(ref, tree, header["miner"])
    (role,) = [
        q for q in result["visible_role_deltas"] if q["account"] == payer and q["asset"] == "ETH"
    ]
    assert role["delta_after_receipt_gas"] == actual_wei
    assert frames[()].data.hex() == tx["input"][2:]
    assert frames[()].value == quantity(tx["value"])
    morpho = morpho_callbacks(ref, tree, funding)
    result.update(
        actual_sender_eth_delta_wei=actual_wei,
        trace_frames=len(frames),
        resolved_swaps=len(funding.resolved_swaps),
        exact_links=len(funding.links),
        trace_funding_depth=funding.profile.witnessed_depth,
        loans=[
            {
                "asset": loan.asset,
                "borrower": loan.borrower,
                "principal_raw": loan.principal_raw,
                "repaid_raw": loan.repaid_raw,
                "event": loan.loan_log,
                "credit": loan.credit_log,
                "repayment": loan.repayment_log,
                "depth": loan.callback_profile.witnessed_depth,
            }
            for loan in morpho.loans
        ],
        unresolved_loans=morpho.unresolved_loan_logs,
        successful_native_calls=[
            {"path": list(path), "from": f.sender, "to": f.recipient, "value": f.value}
            for path, f in frames.items()
            if f.kind == "CALL" and f.value and first_failure(frames, path) is None
        ],
    )
    if payer == r["to"]:
        code = before["code"]
        assert code.startswith("0xef0100") and len(code) == 48
        payer_word = int(payer, 16).to_bytes(32, "big")
        selector = keccak(b"balanceOf(address)")[:4]
        exact = [
            int.from_bytes(f.output, "big")
            for path, f in sorted(frames.items())
            if len(path) == 1
            and f.kind == "STATICCALL"
            and f.recipient == WETH
            and f.data == selector + payer_word
            and f.output is not None
            and len(f.output) == 32
            and first_failure(frames, path) is None
        ]
        assert len(exact) == 2
        key = "0x" + keccak(payer_word + (3).to_bytes(32, "big")).hex()
        assert exact == [int(delta[k][WETH]["storage"][key], 16) for k in ("pre", "post")]
        net_weth = exact[1] - exact[0]
        assert any(
            q["account"] == payer
            and q["asset"] == WETH
            and q["delta_after_receipt_gas"] == net_weth
            for q in result["visible_role_deltas"]
        )
        assert net_weth + actual_wei == result["conditional_common_role_eth_weth_delta_wei"]
        result.update(
            delegation_designator=code, weth_balance_boundary=exact, weth_delta_wei=net_weth
        )
    return result


def bsc_cash(r, tx, case):
    assert (
        quantity(r["effectiveGasPrice"]) == quantity(tx["gasPrice"]) == quantity(tx["value"]) == 0
    )
    if quantity(tx["type"]) == 2:
        assert quantity(tx["maxFeePerGas"]) == quantity(tx["maxPriorityFeePerGas"]) == 0
    else:
        assert (quantity(tx["v"]) - 35) // 2 == 56
    sender, executor = r["from"], r["to"]
    balance = Counter()
    for asset, src, dst, amt in moves(r).values():
        balance[src, asset] -= amt
        balance[dst, asset] += amt
    wrap_changes = []
    for event in r["logs"]:
        if event["address"] != WBNB or len(event["topics"]) != 2:
            continue
        kind, actor = event["topics"]
        if kind not in (WITHDRAWAL, DEPOSIT):
            continue
        assert len(event["data"]) == 66
        address = dec_address(actor)
        amount = int(event["data"], 16) * (1 if kind == DEPOSIT else -1)
        balance[address, WBNB] += amount
        wrap_changes.append(
            {"address": address, "delta": amount, "index": quantity(event["logIndex"])}
        )
    covered_legs = set()
    cycle_total = 0
    for cycle in case["closures"]:
        assert cycle["asset"] == WBNB and cycle["holder"] == executor
        assert covered_legs.isdisjoint(cycle["legs"])
        covered_legs.update(cycle["legs"])
        cycle_total += cycle["gross_raw"]
    out = {
        "wrap_changes": wrap_changes,
        "closed_legs": len(covered_legs),
        "closed_gross_wbnb_wei": cycle_total,
        "role_transfer_residuals": [
            {"account": account, "asset": asset, "delta": v}
            for (account, asset), v in sorted(balance.items())
            if account in (sender, executor) and v
        ],
        "executor_other_wbnb_credit": [
            {"log": i, "to": u, "amount": amt}
            for i, (asset, v, u, amt) in moves(r).items()
            if asset == WBNB
            and v == executor
            and i not in {leg["in_log"] for leg in case["market_legs"]}
        ],
        "actual_sender_bnb_delta_wei": None,
        "final_native_recipient": None,
    }
    return out


def read_cash(evidence):
    folder = D / "cash"
    frozen = json.loads((folder / "scope.json").read_bytes())
    assert sha_file(folder / "scope.json") == CASH_SCOPE
    assert sha_file(folder / "acquire.py") == frozen["driver"]
    assert sha_file(D / "panel.json") == frozen["panel_sha"]
    assert sha_file(ROOT / "y2026-op-arb-20261127/capture.py") == frozen["source"]
    values, notes = {}, {}
    for label, lane in frozen["lanes"].items():
        values[label], notes[label] = read_cash_lane(folder, label, lane, frozen)
    by_chain = {}
    for chain, targets in frozen["picked"].items():
        rows = {r["transaction"]: r for r in evidence[chain]["all_at_least_two"]}
        transactions = values[chain + "-envelopes"]
        assert list(transactions) == targets
        checked = {}
        for h, tx in transactions.items():
            case = rows[h]
            n = case["block"]
            br = next(r for r in evidence[chain]["blocks"] if r["height"] == n)
            network = evidence[chain]["chain_id"]
            wires = D / chain
            header, _ = wire(wires, f"h{n}", "eth_getBlockByNumber", [hex(n), False])
            rr, _ = wire(wires, f"r{n}", "eth_getBlockReceipts", [br["hash"]])
            r = rr[case["index"]]
            signed = user_transaction(tx, network)
            assert h == tx["hash"] == "0x" + keccak(signed).hex() == r["transactionHash"]
            assert Account.recover_transaction(signed).lower() == tx["from"] == r["from"]
            assert all(
                tx[k] == r[k]
                for k in ("to", "type", "blockNumber", "blockHash", "transactionIndex")
            )
            assert quantity(tx["gasPrice"]) == quantity(r["effectiveGasPrice"])
            if chain == "ethereum":
                block, _ = receipt_commitment(network, header, rr)
                details = ethereum_cash(header, block, r, tx, values[chain + "-trace"])
            else:
                details = bsc_cash(r, tx, case)
            checked[h] = details | {
                "signed_envelope_hash_rebuilt": True,
                "sender_recovered": True,
                "nonce": quantity(tx["nonce"]),
                "receipt_gas_wei": case["receipt_fee_wei"],
            }
        by_chain[chain] = checked
    return {
        "scope_sha256": CASH_SCOPE,
        "freeze": frozen["utc"],
        "cases": by_chain,
        "sources": notes,
    }, sum(len(r["provenance"]) for r in notes.values())


def main():
    plan = json.loads((D / "scope.json").read_bytes())
    assert sha_file(D / "scope.json") == SCOPE and sha_file(D / "acquire.py") == plan["driver"]
    assert sha_file(ROOT / "y2026-op-arb-20261127/capture.py") == plan["transport"]
    old_topics = ROOT / "y2026-bsc-rh-landed-20261127/scope.json"
    assert sha_file(old_topics) == plan["topic_source"]
    assert json.loads(old_topics.read_bytes())["topics"] == plan["topics"]
    assert sha_file(OLD) == OLD_SHA
    indexed = {k: set() for k in plan["specs"]}
    with gzip.open(OLD, "rt") as fp:
        for row in csv.DictReader(fp, delimiter="\t"):
            if row["chain"] in indexed:
                indexed[row["chain"]].add(row["tx_hash"])
    evidence = {name: scan(name, plan, indexed[name]) for name in sorted(indexed)}
    result = {
        "schema": "oasis.y2026-eth-bsc-untouched.v1",
        "scope_sha256": SCOPE,
        "prior_catalog_sha256": OLD_SHA,
        "requests": sum(len(row["requests"]) for row in evidence.values()),
        "usd": 0,
        "evidence": evidence,
    }
    panel = (D / "panel.json").read_text()
    assert json.dumps(result, sort_keys=True, indent=2) + "\n" == panel
    settlement, count = read_cash(evidence)
    return result | {"requests": result["requests"] + count, "cash": settlement}


if __name__ == "__main__":
    print(json.dumps(main(), sort_keys=True, indent=2))
