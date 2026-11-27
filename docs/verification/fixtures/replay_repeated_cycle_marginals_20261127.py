"""Rebuild fixed-principal round cash and conditional gas thresholds from retained receipts."""

import argparse
import hashlib
import json
from itertools import pairwise
from pathlib import Path

import rlp
from eth_hash.auto import keccak
from replay_published_multihop_receipts import TRANSFER, indexed_address, inspect, wire
from replay_published_multihop_shards import published_rows

from oasis_agent.rpc.budget import BudgetPolicy, RpcBudget

WEI = 10**18
ADDITIONAL_GAS_SCENARIOS = (50_000, 100_000, 200_000, 300_000, 400_000, 500_000)
IDS = {
    "optimism": "0x88ebc4488b690912980b05a1f3f2f6a94d41f12d6435927bc2b9fbacd37f443b",
    "arbitrum": "0x83ca4b8034a4eda21a724619c4e3c75c6b7ad01fb5cf024817e3dfa2b7e9b75c",
}
DOCS = Path(__file__).resolve().parents[1]


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def capture_outcomes(research, audited):
    folder = research / "repeated-cycle-marginals-20261127"
    scope_raw = (folder / "scope.json").read_bytes()
    scope = json.loads(scope_raw)
    assert scope["overall_usd"] == scope["source_usd"] == 0
    assert scope["scope_sha256_input"] == sha(audited)
    assert set(scope["per_lane_calls"]) == {f"{chain}-{i}" for chain in IDS for i in range(2)}
    limit = sum(len(spec["calls"]) for spec in scope["per_lane_calls"].values())
    assert limit == 12
    summary = {}
    for key, spec in scope["per_lane_calls"].items():
        chain = key.rsplit("-", 1)[0]
        assert spec["transaction"] == IDS[chain]
        lane = folder / key
        stop = json.loads((lane / "stop.json").read_bytes())
        calls = spec["calls"]
        with RpcBudget(
            lane / "original.sqlite3", (BudgetPolicy(key, len(calls), 300),), read_only=True
        ) as budget:
            n = budget.counts()[key]
        assert n == stop["reservations"] == len(list(lane.glob("*.request.json")))
        assert n > 0 and stop["reason"]
        wire_results = []
        for pos, (tag, method, params) in enumerate(calls[:n], 1):
            request = (lane / f"{tag}.request.json").read_bytes()
            response = (lane / f"{tag}.response.json").read_bytes()
            meta = json.loads((lane / f"{tag}.meta.json").read_bytes())
            assert (sha(request), sha(response)) == (
                meta["request_sha256"],
                meta["response_sha256"],
            )
            assert scope["frozen_ns"] <= meta["started_ns"] <= meta["returned_ns"]
            assert json.loads(request) == {
                "method": method,
                "params": params,
                "id": 1,
                "jsonrpc": "2.0",
            }
            body = json.loads(response)
            assert body["id"] == 1 and body["jsonrpc"] == "2.0"
            if pos == n:
                assert "error" in body or body.get("result") is None
            elif tag == "chain":
                assert int(body["result"], 16) == spec["chain_id"]
            else:
                assert tag == "before" and body["result"]["hash"] == spec["parent_hash"]
                assert int(body["result"]["number"], 16) == spec["parent_height"]
            wire_results.append(
                {
                    "method": method,
                    "http": meta["http"],
                    "result": "ok" if pos < n else body.get("error") or "null",
                    "request_sha256": sha(request),
                    "response_sha256": sha(response),
                }
            )
        summary[key] = {"endpoint": spec["endpoint"], "reservations": n, "checks": wire_results}
    return {
        "scope_sha256": sha(scope_raw),
        "cap": limit,
        "actual": sum(x["reservations"] for x in summary.values()),
        "lanes": summary,
    }


def anchor_transfers(receipt, anchor, executor):
    moves = []
    for log in sorted(receipt["logs"], key=lambda row: int(row["logIndex"], 16)):
        topics = log["topics"]
        if log["address"].lower() != anchor or len(topics) != 3 or topics[0] != TRANSFER:
            continue
        assert len(log["data"]) == 66
        sender, recipient = map(indexed_address, topics[1:])
        if executor in (sender, recipient):
            moves.append((int(log["logIndex"], 16), sender, recipient, int(log["data"], 16)))
    return moves


def check_input(chain, base, row, tx):
    data = bytes.fromhex(tx["input"][2:])
    terms = {"bytes": len(data), "selector": "0x" + data[:4].hex(), "sha256": sha(data)}
    if chain == "optimism":
        raw = bytes.fromhex(tx["rawTransaction"][2:])
        assert "0x" + keccak(raw).hex() == row["tx_hash"] and rlp.decode(raw)[5] == data
        for i in range(2):
            header, _ = wire(
                base / "op-fee" / f"op-fee-{i}",
                row["tx_hash"] + "-block",
                "eth_getBlockByHash",
                [row["block_hash"], True],
            )
            candidate = header["transactions"][row["transaction_index"]]
            assert candidate["hash"] == row["tx_hash"] and candidate["input"] == tx["input"]
            assert int(candidate["gas"], 16) == tx["gas"]
        assert len(data) == 36
        terms.update(
            witness="signed_hash_and_two_full_blocks", single_word=int.from_bytes(data[4:])
        )
    else:
        words = [int.from_bytes(data[i : i + 32]) for i in range(4, len(data), 32)]
        assert (len(data) - 4) % 32 == 0
        terms.update(
            witness="pinned_paper_row_only",
            aligned_words=len(words),
            aligned_words_equal_round_count=words.count(row["groups"]),
        )
    return terms


def build_case(chain, base, published):
    txid = IDS[chain]
    info, record = next((i, rec) for i, rec in published_rows(base, chain) if i["tx_hash"] == txid)
    row = inspect(base, chain, info, record)
    assert row in published["audited"][chain]
    receipt, digest = wire(
        base / "rpc" / f"{chain}-0", txid + "-receipt", "eth_getTransactionReceipt", [txid]
    )
    groups = [g["swaps"] for g in record["arbitrages"]]
    executor, sender = row["transfer_log_account"], receipt["from"].lower()
    anchor = groups[0][0]["in_token"].lower()
    venues = [swap["exchange"].lower() for swap in groups[0]]
    moves = anchor_transfers(receipt, anchor, executor)
    trading = [m for m in moves if (m[1], m[2]) in [(executor, venues[0]), (venues[-1], executor)]]
    assert row["intermediate_evidence"][
        "closed_amount_groups_with_all_intermediate_transfers"
    ] == len(groups)
    assert len(trading) == 2 * len(groups)
    tx = record["transaction"]
    terms = check_input(chain, base, row, tx)
    gas_price, full_fee = row["gas_price_wei"], row["observed_network_fee_wei"]
    gas_cap_price = int(tx.get("maxFeePerGas") or tx["gasPrice"])
    ceiling = tx["gas"] * gas_cap_price + row["separately_added_op_l1_fee_wei"]
    assert ceiling >= full_fee
    scenarios = (
        {"1000_USDC_per_ETH": 10**9, "2000_USDC_per_ETH": 2 * 10**9}
        if chain == "optimism"
        else {"ETH_WETH_at_par": WEI}
    )
    rounds, gross, previous_log = [], 0, -1
    for idx, group in enumerate(groups):
        events = [leg["index"] for leg in group]
        assert previous_log < events[0] and events == sorted(events)
        assert [x["exchange"].lower() for x in group] == venues
        assert group[0]["in_token"].lower() == group[-1]["out_token"].lower() == anchor
        principal, payout = int(group[0]["in_amount"]), int(group[-1]["out_amount"])
        assert principal == int(groups[0][0]["in_amount"])
        debit, credit = trading[2 * idx : 2 * idx + 2]
        assert debit[1:] == (executor, venues[0], principal)
        assert credit[1:] == (venues[-1], executor, payout)
        assert previous_log < debit[0] < events[0] <= events[-1] < credit[0]
        previous_log = credit[0]
        delta = payout - principal
        assert delta > 0
        gross += delta
        rounds.append(
            {
                "round": idx + 1,
                "swap_logs": events,
                "principal_raw": principal,
                "executor_principal_log": debit[0],
                "payout_raw": payout,
                "executor_payout_log": credit[0],
                "gross_raw": delta,
                "cumulative_gross_raw": gross,
                "measured_prefix_network_fee_wei": full_fee if idx + 1 == len(groups) else None,
                "measured_incremental_network_fee_wei": None,
                "strict_incremental_gas_max_by_scenario": {
                    k: (delta * WEI - 1) // (rate * gas_price) for k, rate in scenarios.items()
                },
            }
        )
    assert gross == row["group_output_minus_input_raw_by_anchor"][anchor]
    assert all(left["gross_raw"] > right["gross_raw"] for left, right in pairwise(rounds))
    other = [m for m in moves if m not in trading]
    if chain == "optimism":
        assert len(other) == 2 * len(groups) + 1 and other[-1][1:] == (executor, sender, gross)
        for idx, step in enumerate(rounds):
            front, back = other[2 * idx : 2 * idx + 2]
            assert front[1:] == (sender, executor, step["principal_raw"])
            assert back[1:] == (executor, sender, step["principal_raw"])
            assert (
                front[0]
                < step["executor_principal_log"]
                < step["executor_payout_log"]
                < back[0]
                < other[-1][0]
            )
            if idx:
                assert rounds[idx - 1]["principal_return_to_sender_log"] < front[0]
            step.update(sender_funding_log=front[0], principal_return_to_sender_log=back[0])
    else:
        assert not other
    summary = {}
    for k, rate in scenarios.items():

        def first_cover(fee_wei, rate=rate):
            return next(
                (s["round"] for s in rounds if s["cumulative_gross_raw"] * WEI > fee_wei * rate),
                None,
            )

        summary[k] = {
            "anchor_raw_per_ETH": rate,
            "first_round_covering_full_observed_network_fee": first_cover(full_fee),
            "first_round_covering_conditional_fee_ceiling": first_cover(ceiling),
            "rounds_individually_positive_at_hypothetical_incremental_gas": {
                str(u): sum(s["strict_incremental_gas_max_by_scenario"][k] >= u for s in rounds)
                for u in ADDITIONAL_GAS_SCENARIOS
            },
        }
    return {
        "chain": chain,
        "chain_id": row["chain_id"],
        "tx_hash": txid,
        "block": row["block"],
        "block_hash": row["block_hash"],
        "transaction_index": row["transaction_index"],
        "receipt_sha256": digest,
        "source_row": row["source_row"],
        "executor": executor,
        "sender": sender,
        "anchor": anchor,
        "anchor_decimals": 6 if chain == "optimism" else 18,
        "observed_network_fee_wei": full_fee,
        "observed_gas_price_wei": gas_price,
        "gas_limit": tx["gas"],
        "gas_cap_price_wei": gas_cap_price,
        "l1_fee_added_to_observed_wei": row["separately_added_op_l1_fee_wei"],
        "conditionally_bounded_full_fee_wei": ceiling,
        "calldata": terms,
        "last_distribution_to_sender_log": other[-1][0] if other else None,
        "prefix_envelope_proven": False,
        "entity_net": None,
        "scenarios": summary,
        "rounds": rounds,
    }


def main():
    if not __debug__:
        raise SystemExit("run without optimization")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("research", type=Path)
    research = parser.parse_args().research
    catalog = (DOCS / "PUBLISHED_MULTIHOP_RECEIPTS_20261127.json").read_bytes()
    base = research / "multichain-inventory-20261127"
    print(
        json.dumps(
            {
                "capture": capture_outcomes(research, catalog),
                "cases": [build_case(ch, base, json.loads(catalog)) for ch in IDS],
            },
            sort_keys=True,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
