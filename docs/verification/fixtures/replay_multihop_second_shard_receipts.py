"""Verify the two preselected public CCS second-shard receipts and cash endpoints offline."""

import argparse
import json
from collections import Counter
from itertools import pairwise
from pathlib import Path

from eth_account import Account
from eth_hash.auto import keccak
from replay_multihop_second_shards import git_blob, quantity, sha
from replay_published_multihop_receipts import TRANSFER, audit_groups, indexed_address
from rlp import encode as rlp_encode

from oasis_agent.evidence.nitro_block import (
    bloom_bits,
    indexed_trie_root,
    nitro_header,
    verify_receipt_root,
)
from oasis_agent.evidence.public_receipts import decode_receipt
from oasis_agent.evidence.state_trie import PRAGUE_FIELDS, hex_bytes
from oasis_agent.evidence.state_trie import quantity as field_quantity
from oasis_agent.models.chain import BlockRef
from oasis_agent.rpc.budget import BudgetPolicy, RpcBudget
from oasis_agent.rpc.protocol import response_envelope


def digest(path):
    return sha(path.read_bytes())


def wire_evidence(root):
    plan, stop = (
        json.loads((root / name).read_bytes())
        for name in ("receipt-plan.json", "RECEIPT_STOP.json")
    )
    assert plan["max_usd"] == 0 and plan["max_methods"] == 14 and plan["retry"] is False
    assert digest(root / "capture-receipts.py") == plan["driver_sha256"]
    assert digest(root / "receipt-plan.json") == stop["plan_sha256"]
    counts = {}
    lanes = {}
    recovered = {}
    for row in stop["lanes"]:
        key = row["source"]
        scope = plan["sources"][key]
        lane = root / "rpc" / key
        assert row == json.loads((lane / "STOP.json").read_bytes())
        assert scope["sha256_selected_source"] == digest(root / "compiled.json")
        with RpcBudget(
            lane / "original.sqlite3",
            (BudgetPolicy(key, len(scope["calls"]), plan["source_min_pacing_ms"]),),
            read_only=True,
        ) as budget:
            counts[key] = count = budget.counts()[key]
        assert count == row["reservations"] == len(list(lane.glob("*.request.json")))
        recovered[key] = {}
        refs = []
        for i, (label, method, params) in enumerate(scope["calls"][:count], 1):
            request = (lane / f"{label}.request.json").read_bytes()
            response = (lane / f"{label}.response.json").read_bytes()
            meta = json.loads((lane / f"{label}.meta.json").read_bytes())
            assert json.loads(request) == {
                "jsonrpc": "2.0",
                "id": i,
                "method": method,
                "params": params,
            }
            assert (
                sha(request) == meta["request_sha256"] and sha(response) == meta["response_sha256"]
            )
            assert i == meta["id"] and meta["start_utc"] <= meta["end_utc"] <= row["utc"]
            assert plan["frozen_utc"] < meta["start_utc"]
            assert len(response) == meta["bytes"] <= plan["max_read_bytes_each"]
            res = response_envelope(response, i)
            value = res.get("result")
            if value is None:
                assert i == count and row["first_refusal"]
            else:
                assert meta["http"] == 200
            refs.append(
                {
                    "label": label,
                    "method": method,
                    **meta,
                    "rpc_error": res.get("error"),
                    "nonnull": value is not None,
                }
            )
            recovered[key][label] = value
        assert quantity(recovered[key]["chain"]) == scope["chain_id"]
        lanes[key] = row | {"endpoint": scope["endpoint"], "requests": refs}
    assert sum(counts.values()) == 13
    return recovered, lanes


def user_transaction(raw, chain_id):
    kind = quantity(raw.get("type", "0x0"))
    tx_input = hex_bytes(raw["input"], 256 * 1024)
    if kind == 0x6A:
        assert chain_id == 42161 and quantity(raw["transactionIndex"]) == 0
        return bytes([kind]) + rlp_encode([chain_id, tx_input])

    def integer(key):
        return quantity(raw[key])

    dest = hex_bytes(raw["to"], 20, exact=True) if raw["to"] is not None else b""
    call = [integer("gas"), dest, integer("value"), tx_input]
    signature = [integer(k) for k in ("v", "r", "s")]
    if kind == 0:
        return rlp_encode([integer("nonce"), integer("gasPrice"), *call, *signature])
    assert kind == 2 and integer("chainId") == chain_id and signature[0] in (0, 1)
    access = [
        [
            hex_bytes(x["address"], 20, exact=True),
            [hex_bytes(k, 32, exact=True) for k in x["storageKeys"]],
        ]
        for x in raw["accessList"]
    ]
    return bytes([kind]) + rlp_encode(
        [
            chain_id,
            integer("nonce"),
            integer("maxPriorityFeePerGas"),
            integer("maxFeePerGas"),
            *call,
            access,
            *signature,
        ]
    )


def transaction_root(block, chain_id):
    values = []
    for i, tx in enumerate(block["transactions"]):
        payload = user_transaction(tx, chain_id)
        assert i == quantity(tx["transactionIndex"]) and tx["blockHash"] == block["hash"]
        assert quantity(tx["blockNumber"]) == quantity(block["number"])
        assert "0x" + keccak(payload).hex() == tx["hash"]
        values.append(payload)
    value = "0x" + indexed_trie_root(values).hex()
    assert value == block["transactionsRoot"]
    return value


def legacy_op_header_hash(block):
    assert all(k not in block for k, _ in PRAGUE_FIELDS[15:])
    fields = []
    for k, standard_width in PRAGUE_FIELDS[:15]:
        value = block[k]
        # This frozen legacy Optimism header carries Clique extra data.
        width = 97 if k == "extraData" else standard_width
        fields.append(
            field_quantity(value, -width) if width < 0 else hex_bytes(value, width, exact=True)
        )
    return "0x" + keccak(rlp_encode(fields)).hex()


def op_single_receipt_root(header, receipt, historic_gas_price):
    assert len(header["transactions"]) == 1 and receipt.get("type", "0x0") == "0x0"
    block = BlockRef(
        chain_id=10,
        number=quantity(header["number"]),
        block_hash=header["hash"],
        timestamp=quantity(header["timestamp"]),
    )
    observed = decode_receipt(
        receipt | {"effectiveGasPrice": hex(historic_gas_price)},
        block,
        receipt["transactionHash"],
        0,
    )
    gas = observed.gas_used
    assert gas == quantity(receipt["cumulativeGasUsed"]) == quantity(header["gasUsed"])
    bloom = hex_bytes(receipt["logsBloom"], 256, exact=True)
    assert receipt["logsBloom"] == header["logsBloom"] and int.from_bytes(bloom) == bloom_bits(
        observed.logs
    )
    assert [log.log_index for log in observed.logs] == list(range(len(observed.logs)))
    logs = [
        [
            hex_bytes(log.address, 20, exact=True),
            [hex_bytes(t, 32, exact=True) for t in log.topics],
            hex_bytes(log.data, 256 * 1024),
        ]
        for log in observed.logs
    ]
    return "0x" + indexed_trie_root([rlp_encode([observed.status, gas, bloom, logs])]).hex()


def transfers(receipt, currency):
    out = []
    for x in receipt["logs"]:
        if (
            x["address"].lower() == currency
            and len(x["topics"]) == 3
            and x["topics"][0] == TRANSFER
        ):
            sender, receiver = map(indexed_address, x["topics"][1:])
            amount = int.from_bytes(hex_bytes(x["data"], 32, exact=True))
            out.append((quantity(x["logIndex"]), sender, receiver, amount))
    return out


def group_edges(group, receipt):
    rows = []
    for i, (source, target) in enumerate(pairwise(group["markets"])):
        currency = group["route_tokens"][i + 1]
        amount = group["leg_out_amounts_raw"][i]
        assert amount == group["leg_in_amounts_raw"][i + 1]
        moves = transfers(receipt, currency)
        ((left_index, _, payee, _),) = [x for x in moves if x[1] == source and x[3] == amount]
        ((right_index, payer, _, _),) = [x for x in moves if x[2] == target and x[3] == amount]
        if left_index != right_index:
            assert left_index < right_index and payee == payer
            assert not any(left_index < x[0] < right_index and payer in x[1:3] for x in moves)
        rows.append(
            {
                "currency": currency,
                "amount_raw": amount,
                "from_market_log": left_index,
                "to_market_log": right_index,
                "holder": payer if left_index != right_index else None,
            }
        )
    return rows


def group_cash(case, receipt):
    actor = receipt["to"] or receipt["contractAddress"]
    payer = receipt["from"]
    ((token, gross),) = case["gross_sum_by_closing_token_raw"].items()
    all_transfers = transfers(receipt, token)
    group_transfers = []
    prev = -1
    op = case["chain_id"] == 10
    for group in case["exact_closed_groups"]:
        window = [e for e in all_transfers if prev < e[0] <= max(group["log_indices"])]
        prev = max(group["log_indices"])
        (pay,), (ret,) = (
            [e[0] for e in window if e[1:] == (actor, group["markets"][0], group["principal_raw"])],
            [e[0] for e in window if e[1:] == (group["markets"][-1], actor, group["return_raw"])],
        )
        if op:
            assert ret < pay  # Pool transfer precedes executor spending within this observed group.
        group_transfers.append(
            {
                "group": group["group_number"],
                "funding_transfer": pay,
                "return_transfer": ret,
                "principal_raw": group["principal_raw"],
                "return_raw": group["return_raw"],
                "intermediate_transfers": group_edges(group, receipt),
            }
        )
    deltas = Counter()
    for _, sender, receiver, amount in all_transfers:
        deltas[sender] -= amount
        deltas[receiver] += amount
    assert deltas[actor] == 0
    if op:
        ((log, _, recipient, _),) = [e for e in all_transfers if e[1] == actor and e[3] == gross]
        assert (
            log > prev and deltas[recipient] == gross and deltas[payer] == 0 and payer != recipient
        )
        assert len([e for e in all_transfers if recipient in e[1:3]]) == 1
        endpoint = {"payout_log": log, "closing_recipient": recipient, "payer_common_control": None}
    else:
        (group,) = case["exact_closed_groups"]
        (fund,), (finish,) = (
            [e[0] for e in all_transfers if e[1:] == (payer, actor, group["principal_raw"])],
            [e[0] for e in all_transfers if e[1:] == (actor, payer, group["return_raw"])],
        )
        assert deltas[payer] == gross and fund < group_transfers[0]["funding_transfer"]
        assert finish > group_transfers[0]["return_transfer"]
        endpoint = {"sender_principal_log": fund, "payout_log": finish, "closing_recipient": payer}
    return {
        "currency": token,
        "gross_raw": gross,
        "payer": payer,
        "executor": actor,
        "executor_delta_raw": deltas[actor],
        "group_transfers": group_transfers,
        **endpoint,
    }


def verify_case(root, corpus, case, lanes):
    chain = case["chain"]
    primary, secondary = (lanes[f"{chain}-{i}"] for i in (0, 1))
    header, receipt = primary["header"], primary["receipt"]
    pos = case["transaction_index"]
    tx = header["transactions"][pos]
    assert tx["hash"] == case["tx_hash"] == receipt["transactionHash"]
    assert (
        Account.recover_transaction(user_transaction(tx, case["chain_id"])).lower()
        == receipt["from"]
    )
    assert quantity(receipt["status"]) == 1 and quantity(receipt["transactionIndex"]) == pos
    assert header["hash"] == receipt["blockHash"] == tx["blockHash"]
    assert quantity(receipt["blockNumber"]) == quantity(header["number"]) == case["block_number"]
    assert quantity(header["timestamp"]) == case["block_utc"]
    source = (root / f"{chain}-paper-2.json").read_bytes()
    assert git_blob(source) == corpus["chains"][chain]["source"]["blob"]
    source_row = json.loads(source.splitlines()[case["source_row"] - 1])
    assert source_row["transaction"]["hash"].lower() == case["tx_hash"]
    assert source_row["transaction"]["from"].lower() == receipt["from"]
    assert (source_row["transaction"]["to"] or "").lower() == (receipt["to"] or "")
    groups = [g["swaps"] for g in source_row["arbitrages"]]
    cash = group_cash(case, receipt)
    funding = audit_groups(groups, receipt, cash["executor"], case["chain_id"])
    assert funding["distinct_events_amounts_matched"] == case["published_indices"]
    assert funding["intermediate_evidence"][
        "closed_amount_groups_with_all_intermediate_transfers"
    ] == len(groups)
    price = quantity(receipt.get("effectiveGasPrice", tx["gasPrice"]))
    tx_root = transaction_root(header, case["chain_id"])
    op = chain == "optimism"
    if op:
        assert tx.get("type", "0x0") == "0x0"
        assert secondary["receipt"] == receipt
        second_header = secondary["header"]
        assert (
            legacy_op_header_hash(header) == legacy_op_header_hash(second_header) == header["hash"]
        )
        assert transaction_root(second_header, 10) == tx_root
        assert quantity(second_header["transactions"][pos]["gasPrice"]) == price
        receipt_root = op_single_receipt_root(header, receipt, price)
        assert receipt_root == header["receiptsRoot"] == second_header["receiptsRoot"]
        count = 1
        l1 = quantity(receipt["l1Fee"])
    else:
        assert secondary["receipt"] is None and "header" not in secondary
        reduced = header | {"transactions": [t["hash"] for t in header["transactions"]]}
        bound = nitro_header(reduced, case["block_number"], receipt["blockHash"], chain_id=42161)
        full = primary["full_receipts"]
        assert full[pos] == receipt
        count = len(verify_receipt_root(bound, full))
        receipt_root, l1 = bound.receipts_root, 0
    gas = quantity(receipt["gasUsed"])
    fees = gas * price + l1
    return {
        "chain": chain,
        "chain_id": case["chain_id"],
        "tx_hash": case["tx_hash"],
        "block_number": case["block_number"],
        "transaction_index": pos,
        "block_hash": header["hash"],
        "receipts_root": receipt_root,
        "transactions_root": tx_root,
        "verified_block_receipt_count": count,
        "agreeing_receipt_sources": 2 if op else 1,
        "receipt_logs": len(receipt["logs"]),
        "funding": funding,
        "cash": cash,
        "network_cost_wei": fees,
        "receipt_gas_units": gas,
        "effective_gas_price_wei": price,
        "separately_added_l1_fee_wei": l1 if op else None,
        "conditional_combined_eth_weth_after_network_fee_wei": cash["gross_raw"] - fees
        if op
        else None,
        "unknown": ["direct native transfers", "financing and outside fees", "full economic net"],
    }


def main():
    if not __debug__:
        raise RuntimeError("run without optimization")
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("root", type=Path)
    root = p.parse_args().root
    corpus = json.loads((root / "compiled.json").read_bytes())
    closed, stops = wire_evidence(root)
    results = [
        verify_case(root, corpus, case, closed) for case in corpus["independent_receipt_queue"]
    ]
    out = {
        "schema": "oasis.ccs24-second-shard-receipts.v1",
        "cases": results,
        "lanes": stops,
        "compiled_sha256": digest(root / "compiled.json"),
        "receipt_scope_sha256": digest(root / "receipt-plan.json"),
        "paid_rpc_usd": 0,
    }
    print(json.dumps(out, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
