"""Verify three legacy Optimism hundred-event transactions and their ordered balance flow."""

import argparse
import json
from collections import Counter
from itertools import pairwise
from pathlib import Path

from eth_account import Account
from replay_multihop_second_shard_receipts import (
    group_edges,
    legacy_op_header_hash,
    op_single_receipt_root,
    transaction_root,
    transfers,
    user_transaction,
)
from replay_multihop_second_shards import git_blob, projected_row, quantity, sha
from replay_published_multihop_receipts import audit_groups, indexed_address

from oasis_agent.arbitrage.cycle_protocols import topic
from oasis_agent.rpc.budget import BudgetPolicy, RpcBudget
from oasis_agent.rpc.protocol import response_envelope

CHAIN_ID = 10
ACTOR = "0x89bedc6cc397562ca6e1f8355886162b269fdbfe"
PAYER = "0x7656a6df8cb25ca8082f961fae077f2ec082e6bc"
WETH = "0x4200000000000000000000000000000000000006"
USDC_E = "0x7f5c764cbc14f9669b88837ca1490cca17c31607"
DEPOSIT = topic("Deposit(address,uint256)")
WITHDRAWAL = topic("Withdrawal(address,uint256)")
FIXED = {
    "0x61c00451496dbf330f4a788bf2b40214662734732e40785e91c7827f00ffcbf9": (808, 50),
    "0x84dbfda2d50e482ff465a19fa9959426e21da7260efa5a3acb753361f3fa2f3b": (1675, 51),
    "0xb6f716079d44082e78c66f2d09ffd586ea891240ef691dafcad886c3456fe2ab": (997, 50),
}


def read(path):
    return json.loads(path.read_bytes())


def provenance(root):
    scope_path = root / "scope.json"
    scope = read(scope_path)
    stop = read(root / "STOP.json")
    assert scope["chain_id"] == CHAIN_ID and scope["max_usd"] == 0
    assert scope["retry"] is False and scope["first_failure_stops_lane"] is True
    assert sha(scope_path.read_bytes()) == stop["scope_sha256"]
    assert sha((root / "capture.py").read_bytes()) == scope["driver_sha256"]
    assert {x["source"] for x in stop["lanes"]} == scope["endpoints"].keys()
    lanes = {}
    values = {}
    for entry in stop["lanes"]:
        source = entry["source"]
        lane = root / source
        assert entry == read(lane / "STOP.json")
        policy = (BudgetPolicy(source, scope["source_caps"][source], scope["pacing_ms"]),)
        with RpcBudget(lane / "original.sqlite3", policy, read_only=True) as budget:
            count = budget.counts()[source]
        assert count == entry["reservations"] == len(list(lane.glob("*.request.json")))
        evidence = []
        records = {}
        for i, (label, method, params) in enumerate(scope["calls_for_each_source"][:count], 1):
            request = (lane / f"{label}.request.json").read_bytes()
            response = (lane / f"{label}.response.json").read_bytes()
            meta = read(lane / f"{label}.meta.json")
            assert json.loads(request) == {
                "jsonrpc": "2.0",
                "id": i,
                "method": method,
                "params": params,
            }
            assert (
                sha(request) == meta["request_sha256"] and sha(response) == meta["response_sha256"]
            )
            assert (
                i == meta["id"] and len(response) == meta["bytes"] <= scope["body_cap_per_response"]
            )
            assert scope["frozen_utc"] < meta["started_utc"] <= meta["finished_utc"] <= entry["utc"]
            result = response_envelope(response, i)
            if meta["http"] != 200 or result.get("result") is None:
                assert i == count and entry["first_refusal"]
            records[label] = result.get("result")
            evidence.append(
                {"label": label, "method": method, "error": result.get("error"), **meta}
            )
        assert quantity(records["chain"]) == CHAIN_ID
        lanes[source] = entry | {"endpoint": scope["endpoints"][source], "wires": evidence}
        values[source] = records
    assert sum(x["reservations"] for x in lanes.values()) == 9
    assert set(values["optimism_publicnode"]) == {"chain", "61c004-receipt"}
    assert values["optimism_publicnode"]["61c004-receipt"] is None
    return scope, stop["scope_sha256"], lanes, values["optimism_drpc"]


def balance_effects(receipt, currency):
    moves = list(transfers(receipt, currency))
    account_delta = Counter()
    for _, sender, recipient, amount in moves:
        assert amount >= 0
        account_delta[sender] -= amount
        account_delta[recipient] += amount
    recorded = [row for row in moves if ACTOR in row[1:3]]
    raw = 0
    low = 0
    before = {}
    for i, sender, recipient, amount in recorded:
        before[i] = raw
        raw += amount * ((recipient == ACTOR) - (sender == ACTOR))
        low = min(low, raw)
    assert raw == account_delta[ACTOR]
    assert list(before) == sorted(before)
    return recorded, before, -low, account_delta


def native_provenance(root, parent_scope_hash, archive):
    folder = root / "native-state"
    scope_file = folder / "scope.json"
    scope, stop = read(scope_file), read(folder / "STOP.json")
    assert scope["prior_scope_sha256"] == parent_scope_hash
    assert sha(scope_file.read_bytes()) == stop["scope_sha256"]
    assert sha((folder / "capture.py").read_bytes()) == scope["driver_sha256"]
    assert scope["chain_id"] == CHAIN_ID and scope["max_usd"] == 0
    assert scope["no_retry"] and scope["first_refusal_stops"]
    assert len(scope["calls"]) == scope["max_requests"] == 6 and stop["first_refusal"] is None
    policy = (BudgetPolicy(scope["source"], scope["max_requests"], scope["pacing_ms"]),)
    with RpcBudget(folder / "original.sqlite3", policy, read_only=True) as ledger:
        assert ledger.counts() == stop["counts"] == {scope["source"]: 6}
    balances = {case: {} for case in FIXED}
    for i, (call, meta) in enumerate(zip(scope["calls"], stop["events"], strict=True), 1):
        tx = call["case"]
        stage = call["stage"]
        header_label = f"{tx[2:8]}-header"
        assert (
            sha((root / "optimism_drpc" / f"{header_label}.response.json").read_bytes())
            == call["header_sha256"]
        )
        assert stage in ("parent", "terminal") and stage not in balances[tx]
        block_hash = archive[header_label]["parentHash" if stage == "parent" else "hash"]
        params = [ACTOR, {"blockHash": block_hash, "requireCanonical": True}]
        assert call["method"] == "eth_getBalance" and call["params"] == params
        assert read(folder / f"{i:02}.meta.json") == meta
        req = (folder / f"{i:02}.request.json").read_bytes()
        raw = (folder / f"{i:02}.response.json").read_bytes()
        assert json.loads(req) == {
            "jsonrpc": "2.0",
            "id": i,
            "method": "eth_getBalance",
            "params": params,
        }
        assert sha(req) == meta["request_sha256"] and sha(raw) == meta["response_sha256"]
        assert len(raw) == meta["bytes"] <= scope["max_response_bytes"]
        assert scope["frozen_utc"] < meta["start_utc"] <= meta["end_utc"]
        assert meta["http"] == 200 and meta["failure"] is None and i == meta["id"]
        balance = quantity(response_envelope(raw, i)["result"])
        assert meta["balance_raw"] == balance == 0
        balances[tx][stage] = {"block_hash": block_hash, "balance_raw": balance, **meta}
    return balances, stop["scope_sha256"]


def verify_legacy_balance_positive_control(root):
    control = root.parent / "optimism-136-reinvestment-20261127"
    scope_file = control / "scope.json"
    scope = read(scope_file)
    assert scope["endpoint"] == "https://optimism.drpc.org" and scope["chain_id"] == CHAIN_ID
    assert scope["sender"] == PAYER and scope["executor"] == ACTOR
    wire = {}
    for label, i in (("receipt", 2), ("block", 3), ("pre-sender", 4)):
        lane = control / "rpc"
        req = (lane / f"{label}.request.json").read_bytes()
        raw = (lane / f"{label}.response.json").read_bytes()
        meta = read(lane / f"{label}.meta.json")
        assert sha(req) == meta["request_sha256"] and sha(raw) == meta["response_sha256"]
        assert meta["http"] == 200 and len(raw) == meta["bytes"]
        wire[label] = (
            json.loads(req),
            response_envelope(raw, i)["result"],
            meta["response_sha256"],
        )
    _, header, block_sha = wire["block"]
    _, receipt, receipt_sha = wire["receipt"]
    request, claimed, balance_sha = wire["pre-sender"]
    assert scope["tx"] == receipt["transactionHash"] and quantity(receipt["status"]) == 1
    assert len(header["transactions"]) == 1 and legacy_op_header_hash(header) == header["hash"]
    assert header["hash"] == receipt["blockHash"] and quantity(header["number"]) == scope["block"]
    tx = header["transactions"][0]
    assert tx["hash"] == scope["tx"] and tx["to"] == ACTOR
    assert transaction_root(header, CHAIN_ID) == header["transactionsRoot"]
    assert Account.recover_transaction(user_transaction(tx, CHAIN_ID)).lower() == PAYER
    assert (
        op_single_receipt_root(header, receipt, quantity(tx["gasPrice"])) == header["receiptsRoot"]
    )
    seed = quantity(tx["value"])
    assert request["method"] == "eth_getBalance" and request["params"] == [
        PAYER,
        {"blockHash": header["parentHash"], "requireCanonical": True},
    ]
    assert quantity(claimed) == 0 < seed
    return {
        "tx_hash": scope["tx"],
        "block_hash": header["hash"],
        "parent_hash": header["parentHash"],
        "sender": PAYER,
        "signed_native_seed_value_raw": seed,
        "reported_parent_balance_raw": 0,
        "scope_sha256": sha(scope_file.read_bytes()),
        "provider": scope["endpoint"],
        "wire_sha256": {"receipt": receipt_sha, "header": block_sha, "sender_pre": balance_sha},
    }


def cash_sequence(receipt, groups, seed, native):
    wraps = {DEPOSIT: [], WITHDRAWAL: []}
    for log in receipt["logs"]:
        if log["address"] != WETH or log["topics"][0] not in wraps:
            continue
        assert len(log["topics"]) == 2 and indexed_address(log["topics"][1]) == ACTOR
        assert len(log["data"]) == 66
        wraps[log["topics"][0]].append((quantity(log["logIndex"]), quantity(log["data"])))
    weth, _, _, weth_change = balance_effects(receipt, WETH)
    usdc, _, minimum_usdc, usdc_change = balance_effects(receipt, USDC_E)
    assert weth_change[PAYER] == usdc_change[PAYER] == usdc_change[ACTOR] == minimum_usdc == 0
    assert len(weth) == len(usdc) == 2 * len(groups)
    assert len(wraps[DEPOSIT]) == len(wraps[WITHDRAWAL]) == len(groups)
    starting_native = native["parent"]["balance_raw"]
    rounds = []
    for j, group in enumerate(groups):
        debit, credit = weth[2 * j : 2 * j + 2]
        market_a, market_b = group["markets"]
        principal, returned = (group[k] for k in ("principal_raw", "return_raw"))
        deposit, funding = wraps[DEPOSIT][j]
        withdrawal, native_credit = wraps[WITHDRAWAL][j]
        assert funding == principal and native_credit == returned
        assert debit[1:] == (ACTOR, market_a, principal) and credit[1:] == (
            market_b,
            ACTOR,
            returned,
        )
        assert deposit < debit[0] < credit[0] < withdrawal
        (edge,) = group_edges(group, receipt)
        assert edge["holder"] == ACTOR and edge["currency"] == USDC_E
        assert [x[0] for x in usdc[2 * j : 2 * j + 2]] == [
            edge["from_market_log"],
            edge["to_market_log"],
        ]
        assert debit[0] < edge["from_market_log"] < edge["to_market_log"]
        prev = None
        if j:
            prev = rounds[-1]
            assert prev["unwrap_log"] < deposit and prev["return_raw"] == principal
        else:
            assert seed == principal and deposit == 0
        rounds.append(
            {
                "round": j + 1,
                "pool_addresses": group["markets"],
                "swap_logs": group["log_indices"],
                "principal_raw": principal,
                "return_raw": returned,
                "gross_raw": returned - principal,
                "debit_transfer_log": debit[0],
                "return_transfer_log": credit[0],
                "usdc_e_transfer": edge,
                "wrap_log": deposit,
                "unwrap_log": withdrawal,
                "previous_round_native_credit_log": None if prev is None else prev["unwrap_log"],
                "previous_round_native_credit_matches_next_wrap": prev is not None,
                "minimum_opening_native_to_replace_prior_unwrap_raw": principal if prev else None,
                "opening_native_alone_could_replace_prior_unwrap_if_provider_read_is_valid": (
                    starting_native >= principal if prev else None
                ),
            }
        )
    first, last = rounds[0], rounds[-1]
    gross = weth_change[ACTOR]
    assert gross == last["return_raw"] - first["principal_raw"]
    assert gross == sum(r["gross_raw"] for r in rounds)
    wrapped_weth_delta = (
        gross + sum(x[1] for x in wraps[DEPOSIT]) - sum(x[1] for x in wraps[WITHDRAWAL])
    )
    assert wrapped_weth_delta == 0
    observed_native_delta = native["terminal"]["balance_raw"] - starting_native
    visible_native_net = seed + gross
    unexplained_native_net = observed_native_delta - visible_native_net
    turnover = sum(x["principal_raw"] for x in rounds)
    return {
        "rounds": rounds,
        "proved_reinvestment_transfer_links": len(rounds) - 1,
        "weth_opening_inventory_lower_bound_raw": 0,
        "observed_opening_weth_inventory_raw": None,
        "native_opening_inventory_lower_bound_under_visible_flows_raw": 0,
        "provider_reported_opening_native_balance_raw": starting_native,
        "usdc_e_opening_lower_bound_raw": minimum_usdc,
        "funder": PAYER,
        "signed_native_seed_value_raw": seed,
        "final_unwrap_recipient": ACTOR,
        "terminal_recipient": None,
        "gas_payer": PAYER,
        "gas_payer_weth_delta_raw": weth_change[PAYER],
        "actor_weth_transfer_only_delta_raw": gross,
        "actor_weth_delta_after_wrap_and_unwrap_raw": wrapped_weth_delta,
        "actor_usdc_e_delta_raw": usdc_change[ACTOR],
        "actor_native_visible_delta_before_unresolved_flows_raw": visible_native_net,
        "actor_native_provider_reported_delta_raw": observed_native_delta,
        "actor_native_unattributed_residual_if_provider_reads_valid_raw": unexplained_native_net,
        "native_balance_witnesses": native,
        "native_balance_quality": "single_source_legacy_archive_has_contradictory_positive_control",
        "turnover_weth_raw": turnover,
        "minimum_opening_native_without_any_withdrawal_reuse_raw": turnover - seed,
        "cycle_total_gross_raw": gross,
        "first_round_gross_raw": first["gross_raw"],
        "last_round_gross_raw": last["gross_raw"],
        "initial_principal_transfer_log": first["debit_transfer_log"],
        "terminal_credit_transfer_log": last["return_transfer_log"],
        "terminal_unwrap_log": last["unwrap_log"],
        "final_weth_return_raw": last["return_raw"],
        "deficit_threshold_if_excluding_previous_return_is_conditional": True,
        "unique_funding_attribution": None,
    }


def inspect(raw_paper, row, archive, balances):
    identity = row["tx_hash"]
    row_number, rounds_observed = FIXED[identity]
    assert row["row"] == row_number
    paper = json.loads(raw_paper[row_number - 1])
    projected, groups = projected_row("optimism", row_number, paper)
    assert projected["tx_hash"] == identity and projected["block_number"] == row["block_number"]
    assert projected["published_indices"] == 2 * rounds_observed and len(groups) == rounds_observed
    assert all(g["route_tokens"] == (WETH, USDC_E, WETH) for g in groups)
    prefix = identity[2:8]
    receipt, hdr = (archive[f"{prefix}-{kind}"] for kind in ("receipt", "header"))
    assert len(hdr["transactions"]) == 1 and hdr["hash"] == legacy_op_header_hash(hdr)
    assert receipt["blockHash"] == hdr["hash"] == hdr["transactions"][0]["blockHash"]
    assert quantity(hdr["number"]) == quantity(receipt["blockNumber"]) == row["block_number"]
    assert quantity(hdr["timestamp"]) == projected["block_utc"]
    tx = hdr["transactions"][0]
    assert tx["hash"] == receipt["transactionHash"] == projected["tx_hash"]
    assert quantity(tx.get("type", "0x0")) == 0
    seed = quantity(tx["value"])
    assert seed > 0
    assert receipt["from"] == PAYER == paper["transaction"]["from"].lower()
    assert receipt["to"] == ACTOR == paper["transaction"]["to"].lower()
    assert quantity(receipt["transactionIndex"]) == row["tx_index"] == 0
    assert quantity(receipt["status"]) == 1
    assert Account.recover_transaction(user_transaction(tx, CHAIN_ID)).lower() == PAYER
    tx_root = transaction_root(hdr, CHAIN_ID)
    price = quantity(tx["gasPrice"])
    root = op_single_receipt_root(hdr, receipt, price)
    assert hdr["receiptsRoot"] == root
    funds = audit_groups([g["swaps"] for g in paper["arbitrages"]], receipt, ACTOR, CHAIN_ID)
    assert funds["distinct_events_amounts_matched"] == rounds_observed * 2
    assert funds["intermediate_evidence"]["unique_transfer_pair_through_holder"] == rounds_observed
    cash = cash_sequence(receipt, groups, seed, balances[identity])
    pool_counts = Counter(pool for group in groups for pool in group["markets"])
    assert sum(pool_counts.values()) == 2 * rounds_observed
    fees = quantity(receipt["gasUsed"]) * price + quantity(receipt["l1Fee"])
    return {
        "chain_id": CHAIN_ID,
        "chain": "optimism",
        "tx_hash": identity,
        "shape": "mixed",
        "block_number": row["block_number"],
        "block_hash": hdr["hash"],
        "transaction_index": 0,
        "timestamp_utc_unix": projected["block_utc"],
        "transactions_root": tx_root,
        "receipts_root": root,
        "fully_covered_block_receipts": 1,
        "agreeing_receipt_sources": 1,
        "source_row": row_number,
        "distinct_swap_events": rounds_observed * 2,
        "verified_two_hop_rounds": rounds_observed,
        "funded_market_swaps_lb": 2,
        "receipt_serial_reinvestment_compatible_hops": rounds_observed * 2,
        "funding_identity_note": (
            "Signed seed and ordered same-account unwrap/wrap support the full path. "
            "Independent opening balance and internal transfer traces are unavailable."
        ),
        "distinct_pools": len(pool_counts),
        "swap_count_by_pool": dict(pool_counts),
        "pool_pair_switches": [
            left["group_number"]
            for left, right in pairwise(groups)
            if left["markets"] != right["markets"]
        ],
        "receipt_logs": len(receipt["logs"]),
        "gas_price_wei": price,
        "gas_units": quantity(receipt["gasUsed"]),
        "l2_fee_wei": quantity(receipt["gasUsed"]) * price,
        "l1_fee_wei": quantity(receipt["l1Fee"]),
        "network_cost_wei": fees,
        "conditional_route_gross_minus_observed_network_fee_wei": (
            cash["cycle_total_gross_raw"] - fees
        ),
        "full_economic_net": None,
        "beneficial_common_control": None,
        "cash": cash,
    }


def main():
    if not __debug__:
        raise RuntimeError("run without optimization")
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("root", type=Path)
    p.add_argument("--shard", type=Path, required=True)
    args = p.parse_args()
    scope, scope_hash, lanes, archive = provenance(args.root)
    native, native_scope_hash = native_provenance(args.root, scope_hash, archive)
    source = args.shard.read_bytes()
    assert git_blob(source) == scope["author_blob"] and sha(source) == scope["author_sha256"]
    assert {row["tx_hash"] for row in scope["anchor_rows"]} == FIXED.keys()
    lines = source.splitlines()
    result = {
        "schema": "oasis.optimism-hundred-cycle-family.v1",
        "chain_id": CHAIN_ID,
        "rpc_scope_sha256": scope_hash,
        "native_state_scope_sha256": native_scope_hash,
        "legacy_native_balance_positive_control": verify_legacy_balance_positive_control(args.root),
        "author_blob": scope["author_blob"],
        "author_sha256": scope["author_sha256"],
        "same_executor_address": ACTOR,
        "same_recovered_transaction_sender": PAYER,
        "cases": [inspect(lines, row, archive, native) for row in scope["anchor_rows"]],
        "research_rpc_cost_usd": 0,
        "rpc_lanes": lanes,
    }
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
