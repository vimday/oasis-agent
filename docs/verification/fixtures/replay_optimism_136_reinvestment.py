"""Reconcile an Optimism reinvestment sequence including each WETH withdrawal and deposit."""

import argparse
import json
from itertools import groupby, pairwise
from pathlib import Path

from eth_account import Account
from replay_multihop_second_shard_receipts import (
    legacy_op_header_hash,
    op_single_receipt_root,
    transaction_root,
    transfers,
    user_transaction,
)
from replay_published_multihop_receipts import audit_groups, indexed_address
from replay_published_multihop_shards import REVISION, SHARDS, published_rows

from oasis_agent.arbitrage.cycle_protocols import topic
from oasis_agent.models.common import sha256_hex
from oasis_agent.rpc.budget import BudgetPolicy, RpcBudget

TX = "0x2f51f92b44dd05476c03dfc16b35e9f88d2bdf6c36db313af035d48912af1d30"
WETH = "0x4200000000000000000000000000000000000006"
USDC = "0x7f5c764cbc14f9669b88837ca1490cca17c31607"
DEPOSIT = topic("Deposit(address,uint256)")
WITHDRAW = topic("Withdrawal(address,uint256)")
DOCS = Path(__file__).resolve().parents[1]


def load(path):
    return json.loads(path.read_bytes())


def witnesses(root):
    scope, stop = load(root / "scope.json"), load(root / "STOP.json")
    assert scope["max_calls"] == 7 and scope["max_usd"] == 0 and scope["retry"] is False
    assert scope["tx"] == TX and scope["endpoint"] == "https://optimism.drpc.org"
    assert sha256_hex((root / "capture.py").read_bytes()) == scope["driver"]
    assert (
        sha256_hex((DOCS / "CROSS_GROUP_REINVESTMENT_SCREEN_20261127.json").read_bytes())
        == scope["selection"]
    )
    plan_hash = sha256_hex((root / "scope.json").read_bytes())
    assert stop["plan_sha256"] == plan_hash and stop["first_stop"] is None
    lane = root / "rpc"
    with RpcBudget(
        lane / "original.sqlite3", (BudgetPolicy("optimism-drpc", 7, 300),), read_only=True
    ) as book:
        assert book.counts()["optimism-drpc"] == stop["reservations"] == 7
    assert len(list(lane.glob("*.request.json"))) == 7
    hashes = {}

    def wire(tag, seq, method, params):
        request = (lane / (tag + ".request.json")).read_bytes()
        response = (lane / (tag + ".response.json")).read_bytes()
        meta = load(lane / (tag + ".meta.json"))
        assert json.loads(request) == {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": seq,
        }
        assert meta["http"] == 200 and meta["bytes"] == len(response) <= scope["clip_bytes"]
        assert scope["frozen_utc"] < meta["started_utc"] <= meta["end_utc"] <= stop["ended_utc"]
        assert meta["request_sha256"] == sha256_hex(request) and meta[
            "response_sha256"
        ] == sha256_hex(response)
        obj = json.loads(response)
        assert obj["id"] == seq and obj["jsonrpc"] == "2.0" and obj.get("result") is not None
        hashes[tag] = meta
        return obj["result"]

    assert int(wire("chain", 1, "eth_chainId", []), 16) == 10
    receipt = wire("receipt", 2, "eth_getTransactionReceipt", [TX])
    block = wire("block", 3, "eth_getBlockByNumber", [hex(scope["block"]), True])
    assert len(block["transactions"]) == 1 and block["hash"] == receipt["blockHash"]
    assert receipt["from"] == scope["sender"] and receipt["to"] == scope["executor"]
    seq = 3
    balances = {}
    for role, actor in (("sender", receipt["from"]), ("executor", receipt["to"])):
        balances[role] = {}
        for key, h in (("pre", block["parentHash"]), ("post", block["hash"])):
            seq += 1
            balances[role][key] = int(
                wire(
                    key + "-" + role,
                    seq,
                    "eth_getBalance",
                    [actor, {"blockHash": h, "requireCanonical": True}],
                ),
                16,
            )
    return (
        block,
        receipt,
        balances,
        {
            "endpoint": scope["endpoint"],
            "max_usd": 0,
            "reservations": 7,
            "scope_sha256": plan_hash,
            "wires": hashes,
        },
    )


def cash_evidence(groups, r):
    actor = r["to"]
    wrapped = transfers(r, WETH)
    stable = transfers(r, USDC)
    actor_weth = [row for row in wrapped if actor in row[1:3]]
    assert len(actor_weth) == len(stable) == 2 * len(groups)
    wraps = []
    for log in r["logs"]:
        if log["address"] != WETH or log["topics"][0] not in (DEPOSIT, WITHDRAW):
            continue
        assert (
            len(log["topics"]) == 2
            and indexed_address(log["topics"][1]) == actor
            and len(log["data"]) == 66
        )
        wraps.append((int(log["logIndex"], 16), log["topics"][0], int(log["data"], 16)))
    assert len(wraps) == 2 * len(groups)
    rows = []
    for i, (first, second) in enumerate(groups):
        first_pool, second_pool = first["exchange"].lower(), second["exchange"].lower()
        assert first["in_token"].lower() == second["out_token"].lower() == WETH
        assert first["out_token"].lower() == second["in_token"].lower() == USDC
        principal, bridge, payout = (
            int(first["in_amount"]),
            int(first["out_amount"]),
            int(second["out_amount"]),
        )
        assert bridge == int(second["in_amount"]) and payout > principal > 0
        debit, credit = actor_weth[2 * i : 2 * i + 2]
        out, inn = stable[2 * i : 2 * i + 2]
        dep, wd = wraps[2 * i : 2 * i + 2]
        assert dep[1:] == (DEPOSIT, principal) and wd[1:] == (WITHDRAW, payout)
        assert debit[1:] == (actor, first_pool, principal) and credit[1:] == (
            second_pool,
            actor,
            payout,
        )
        assert out[1:] == (first_pool, actor, bridge) and inn[1:] == (actor, second_pool, bridge)
        assert dep[0] < min(debit[0], out[0]) <= max(debit[0], out[0]) < first["index"]
        assert (
            first["index"]
            < min(inn[0], credit[0])
            <= max(inn[0], credit[0])
            < second["index"]
            < wd[0]
        )
        rows.append(
            {
                "round": i + 1,
                "markets": [first_pool, second_pool],
                "swap_logs": [first["index"], second["index"]],
                "deposit_log": dep[0],
                "weth_to_first_pool_log": debit[0],
                "usdc_transfers": [out[0], inn[0]],
                "weth_from_second_pool_log": credit[0],
                "withdrawal_log": wd[0],
                "principal_wei": principal,
                "intermediate_usdc_raw": bridge,
                "returned_wei": payout,
                "gain_wei": payout - principal,
            }
        )
    bridges = []
    for left, right in pairwise(rows):
        assert left["returned_wei"] == right["principal_wei"]
        assert (
            left["weth_from_second_pool_log"]
            < left["withdrawal_log"]
            < right["deposit_log"]
            < right["weth_to_first_pool_log"]
        )
        # Every log in this single-transaction block has a contiguous, root-checked index.
        assert left["withdrawal_log"] + 1 == right["deposit_log"]
        bridges.append(
            {
                "from_round": left["round"],
                "to_round": right["round"],
                "amount_wei": right["principal_wei"],
                "receive_weth": left["weth_from_second_pool_log"],
                "unwrap": left["withdrawal_log"],
                "rewrap": right["deposit_log"],
                "pay_weth": right["weth_to_first_pool_log"],
            }
        )
    pool_debits = [x for x in wrapped if actor not in x[1:3]]
    assert len({(x[1], x[2]) for x in pool_debits}) == 1
    pool_fee = {
        "from_market": pool_debits[0][1],
        "to": pool_debits[0][2],
        "count": len(pool_debits),
        "amount_wei": sum(x[3] for x in pool_debits),
    }
    return rows, bridges, pool_fee


def verify(research):
    source = research / "multichain-inventory-20261127"
    info, record = next((i, r) for i, r in published_rows(source, "optimism") if i["tx_hash"] == TX)
    block, receipt, balances, proof = witnesses(research / "optimism-136-reinvestment-20261127")
    tx = block["transactions"][0]
    signer = Account.recover_transaction(user_transaction(tx, 10)).lower()
    assert (int(tx["v"], 16) - 35) // 2 == 10 and tx["to"] == record["transaction"][
        "to"
    ].lower() == receipt["to"]
    assert signer == tx["from"] == receipt["from"] == record["transaction"]["from"].lower()
    assert tx["hash"] == TX == receipt["transactionHash"] and int(receipt["status"], 16) == 1
    assert int(receipt["transactionIndex"], 16) == info["transaction_index"] == 0
    assert (
        legacy_op_header_hash(block) == block["hash"] and int(block["number"], 16) == info["block"]
    )
    assert (
        tx["input"] == record["transaction"]["input"]
        and int(block["timestamp"], 16) == record["block_timestamp"]
    )
    price = int(tx["gasPrice"], 16)
    gas = int(receipt["gasUsed"], 16)
    tx_root = transaction_root(block, 10)
    root = op_single_receipt_root(block, receipt, price)
    assert root == block["receiptsRoot"]
    l1 = int(receipt["l1Fee"], 16)
    assert receipt["l1FeeScalar"] == "1" and l1 == int(receipt["l1GasUsed"], 16) * int(
        receipt["l1GasPrice"], 16
    )
    network = gas * price + l1
    groups = [g["swaps"] for g in record["arbitrages"]]
    funding = audit_groups(groups, receipt, receipt["to"], 10)
    assert funding["distinct_events_amounts_matched"] == 2 * len(groups) == 136
    assert funding["intermediate_evidence"][
        "closed_amount_groups_with_all_intermediate_transfers"
    ] == len(groups)
    rows, bridges, pool_fee = cash_evidence(groups, receipt)
    principal = rows[0]["principal_wei"]
    payout = rows[-1]["returned_wei"]
    gross = payout - principal
    assert int(tx["value"], 16) == int(record["transaction"]["value"]) == principal
    assert sum(r["gain_wei"] for r in rows) == gross
    assert funding["top_level_recipient_transfer_log_net_raw"] == {WETH: gross}
    assert rows[0]["deposit_log"] == 0 and rows[-1]["withdrawal_log"] == len(receipt["logs"]) - 1
    assert balances["sender"]["pre"] < principal and all(
        v == 0 for b in balances.values() for v in b.values()
    )
    phases = []
    for pair, it in groupby(rows, key=lambda v: v["markets"]):
        phase = list(it)
        gains = [r["gain_wei"] for r in phase]
        phases.append(
            {
                "first_round": phase[0]["round"],
                "last_round": phase[-1]["round"],
                "markets": pair,
                "first_gain_wei": gains[0],
                "last_gain_wei": gains[-1],
                "all_marginals_fell": all(a > b for a, b in pairwise(gains)),
                "gross_wei": sum(gains),
            }
        )
    row = {
        "tx_hash": TX,
        "chain": "optimism",
        "chain_id": 10,
        "block_number": info["block"],
        "block_hash": block["hash"],
        "timestamp": int(block["timestamp"], 16),
        "transaction_index": 0,
        "source_row": info["source_row"],
        "signer": signer,
        "executor": receipt["to"],
        "shape": "serial",
        "case_kind": "reinvested_two_swap_cycles",
        "market_swaps": 2 * len(groups),
        "verified_market_legs": 2 * len(groups),
        "rounds": len(groups),
        "unique_markets": len({p for x in rows for p in x["markets"]}),
        "within_group_transfer_edges": len(rows),
        "cross_group_equal_reinvestment_edges": len(bridges),
        "selector": tx["input"][:10],
        "initial_tx_value_wei": principal,
        "weth_trade_turnover_wei": sum(r["principal_wei"] for r in rows),
        "gross_wei": gross,
        "final_native_unwrap_wei": payout,
        "last_receipt_visible_recipient": receipt["to"],
        "ultimate_native_recipient": None,
        "pool_side_weth_transfers": pool_fee,
        "l2_gas_units": gas,
        "l2_price_wei": price,
        "l2_network_fee_wei": gas * price,
        "l1_network_fee_wei": l1,
        "network_fee_wei": network,
        "known_cash_less_network_fee_wei": gross - network,
        "entity_net_wei": None,
        "rpc_native_balance_claim_wei": balances,
        "balance_claim_usable": False,
        "balance_claim_rejection": (
            "sole transaction's signed native value exceeds the reported pre-sender balance; "
            "no account proof"
        ),
        "market_phases": phases,
        "round_evidence": rows,
        "cross_group_edges": bridges,
    }
    return {
        "cases": [row],
        "source": {
            "git_revision": REVISION,
            "git_blob": SHARDS["optimism"][1],
            "sha256_paper_bytes": sha256_hex((source / "optimism-paper-1.json").read_bytes()),
        },
        "rpc": proof,
        "commitments": {
            "header_hash": block["hash"],
            "transaction_root": tx_root,
            "receipt_root": root,
            "full_block_receipts": 1,
            "receipt_logs": len(receipt["logs"]),
            "independent_live_sources": 1,
        },
    }


if __name__ == "__main__":
    if not __debug__:
        raise SystemExit("run without optimization")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("research", type=Path)
    print(json.dumps(verify(parser.parse_args().research), indent=2, sort_keys=True))
