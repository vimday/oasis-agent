"""Reconcile every observed call to the three direct-cycle executors in a frozen Base panel."""

import json
from collections import Counter, defaultdict
from pathlib import Path

from eth_account import Account
from eth_hash.auto import keccak
from replay_multihop_second_shard_receipts import user_transaction
from replay_y2026_base_elysium_blocks import SCOPE as BLOCK_SCOPE
from replay_y2026_base_elysium_blocks import D as BLOCKS
from replay_y2026_base_elysium_blocks import cohort, fee
from replay_y2026_op_arb import budget_count, read, sha_file, wire

from oasis_agent.evidence.research_traces import nested_calls
from oasis_agent.rpc.budget import BudgetPolicy

D = BLOCKS.parent / "ci-anvil-margin-20261128"
PANEL = Path(__file__).resolve().parents[1] / "Y2026_BASE_ELYSIUM_BLOCKS_20261128.json"
SCOPE = "ca786ea2a53cfb3cba588de8abe2e7031ca7f0fe33e2400752905c5658e9eee5"
QUIET = "9bc73bfa89e10a50ffa525894a481409b269378500a42be8cfdcd8fbc0e31753"


def lane(name, plan, digest, utc):
    directory = D / name
    stop = read(directory / "STOP.json")
    calls = plan["calls"]
    assert stop["scope"] == digest
    observed = stop["counts"][name]
    assert observed == (12 if name == "quiet" else len(calls))
    assert (stop["failed"] is not None) == (name == "quiet")
    budget_count(directory, BudgetPolicy(name, len(calls), 350), {name: observed})
    results, provenance = {}, {}
    for i, (label, method, params) in enumerate(calls[:observed], 1):
        if name == "quiet" and i == observed:
            request = read(directory / (label + ".request.json"))
            assert request == {"id": i, "jsonrpc": "2.0", "method": method, "params": params}
            meta = read(directory / (label + ".meta.json"))
            assert meta["http"] == 408 and meta["transport_error"] == stop["failed"]
            assert meta["url"] == plan["url"] and meta["id"] == i
            assert utc <= meta["started_utc"] <= meta["finished_utc"] <= stop["utc"]
            assert sha_file(directory / (label + ".request.json")) == meta["request_sha256"]
            assert sha_file(directory / (label + ".response")) == meta["response_sha256"]
            provenance[label] = meta
            continue
        result, meta = wire(directory, label, method, params)
        assert label not in results and result is not None
        assert meta["url"] == plan["url"] and meta["id"] == i and meta["error"] is None
        assert utc <= meta["started_utc"] <= meta["finished_utc"] <= stop["utc"]
        results[label] = result
        provenance[label] = meta
    assert int(results.pop("chain"), 16) == 8453
    return results, provenance


def attempt(r, tx, traces, market):
    h = r["transactionHash"]
    signed = user_transaction(tx, 8453)
    sender = Account.recover_transaction(signed).lower()
    assert h == tx["hash"] == "0x" + keccak(signed).hex()
    assert (sender, r["to"], r["blockHash"], r["transactionIndex"], r["blockNumber"]) == (
        tx["from"],
        tx["to"],
        tx["blockHash"],
        tx["transactionIndex"],
        tx["blockNumber"],
    )
    data = bytes.fromhex(tx["input"][2:])
    nonce = int(tx["nonce"], 16)
    success = int(r["status"], 16) == 1
    assert int(tx["value"], 16) == 0
    receipt = fee(8453, r)
    outcome = "token_cycle" if market else "successful_without_logs_untraced"
    evidence = {
        "sender_eth_delta_wei": None,
        "frames": None,
        "first_level_read_probes": None,
        "error_selector": None,
    }
    if h + "-calls" in traces:
        calls = traces[h + "-calls"]
        diff = traces[h + "-balances"]
        frames = nested_calls(calls)
        top = frames[()]
        assert (sender, r["to"], 0, data, success) == (
            top.sender,
            top.recipient,
            top.value,
            top.data,
            top.error is None,
        )
        assert int(calls["gasUsed"], 16) == int(r["gasUsed"], 16)
        before, after = diff["pre"][sender], diff["post"][sender]
        assert before["nonce"] == nonce and after["nonce"] == nonce + 1
        delta = int(after["balance"], 16) - int(before["balance"], 16)
        assert delta == -receipt["l2_plus_l1_wei"]
        if not market:
            assert not r["logs"] and not any(f.value for f in frames.values())
            assert not any(row.get("storage") for side in diff.values() for row in side.values())
            # Children of a STATICCALL inherit its read-only context.
            assert all(not p or frames[(p[0],)].kind == "STATICCALL" for p in frames)
            outcome = "successful_no_swap" if success else "reverted"
        else:
            assert success
            for cycle in market:
                for leg in cycle["legs"]:
                    assert any(
                        f.kind == "CALL"
                        and f.recipient == leg["pool"]
                        and f.data[:4].hex() == "128acb08"
                        for f in frames.values()
                    )
        evidence = {
            "sender_eth_delta_wei": delta,
            "frames": len(frames),
            "first_level_read_probes": [
                {"target": f.recipient, "selector": "0x" + f.data[:4].hex()}
                for p, f in frames.items()
                if len(p) == 1 and f.kind == "STATICCALL"
            ],
            "error_selector": "0x" + top.output[:4].hex() if top.error and top.output else None,
        }
    else:
        assert success and (market or not r["logs"])
    return (
        receipt
        | evidence
        | {
            "nonce": nonce,
            "selector": "0x" + data[:4].hex(),
            "input_length": len(data),
            "input_keccak": "0x" + keccak(data).hex(),
            "receipt_success": success,
            "observed_outcome": outcome,
            "cycle": [
                {
                    "asset": c["asset"],
                    "paid": c["paid"],
                    "received": c["returned"],
                    "gross_raw": c["gross_raw"],
                    "holder": c["holder"],
                }
                for c in market
            ],
        }
    )


def main():
    plan, quiet = read(D / "scope.json"), read(D / "quiet/scope.json")
    assert sha_file(D / "scope.json") == SCOPE == quiet["parent"]
    assert sha_file(D / "quiet/scope.json") == QUIET
    assert sha_file(D / "acquire.py") == plan["driver"]
    assert sha_file(D / "quiet/acquire.py") == quiet["driver"]
    assert (
        sha_file(D.parent / "y2026-op-arb-20261127/capture.py")
        == plan["transport"]
        == quiet["transport"]
    )
    assert sha_file(BLOCKS / "scope.json") == BLOCK_SCOPE and sha_file(PANEL) == plan["book"]
    fresh, receipts, classified = cohort("base", read(BLOCKS / "scope.json"))
    assert fresh["blocks"] == read(PANEL)["base"]["blocks"]
    targets = {r["target"] for r in classified.values() if r["direct_pool_cycles"]}
    sample = sorted(
        (r for r in receipts.values() if r["to"] in targets),
        key=lambda r: (int(r["blockNumber"], 16), int(r["transactionIndex"], 16)),
    )
    envelopes, w1 = lane("envelopes", plan["lanes"]["envelopes"], SCOPE, plan["utc"])
    traces, w2 = lane("traces", plan["lanes"]["traces"], SCOPE, plan["utc"])
    extra, w3 = lane("quiet", quiet, QUIET, quiet["utc"])
    assert not traces.keys() & extra.keys()
    traces.update(extra)
    hashes = {r["transactionHash"] for r in sample}
    assert envelopes.keys() == hashes and traces.keys() == {
        h + s for h in hashes for s in ("-calls", "-balances")
    } - {label for label, *_ in quiet["calls"][11:]}
    rows = [
        attempt(r, envelopes[h], traces, classified.get(h, {}).get("direct_pool_cycles", []))
        for r in sample
        if (h := r["transactionHash"])
    ]
    costs = Counter()
    groups = defaultdict(list)
    for row in rows:
        costs[row["observed_outcome"]] += row["l2_plus_l1_wei"]
        groups[(row["sender"], row["target"])].append(row)
    common = []
    for (sender, target), group in groups.items():
        if len(group) < 2 or not any(r["cycle"] for r in group):
            continue
        cycles = [c for r in group for c in r["cycle"]]
        assert (
            len(cycles) == 1 and cycles[0]["asset"] == "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
        )
        spend = sum(r["l2_plus_l1_wei"] for r in group)
        common.append(
            {
                "sender": sender,
                "target": target,
                "transactions": [r["tx"] for r in group],
                "receipt_l2_plus_l1_wei": spend,
                "executor_usdc_delta_raw": cycles[0]["gross_raw"],
                "conditional_break_even_usdc_per_eth_floor": cycles[0]["gross_raw"]
                * 10**12
                // spend,
            }
        )
    return {
        "schema": "oasis.base-executor-attempts.v1",
        "chain_id": 8453,
        "usd": 0,
        "scope_sha256": [SCOPE, QUIET],
        "previous_panel_sha256": plan["book"],
        "window_blocks": fresh["blocks"],
        "requests": len(w1) + len(w2) + len(w3),
        "wire": {"envelopes": w1, "traces": w2, "quiet": w3},
        "observed_outcomes": dict(Counter(r["observed_outcome"] for r in rows)),
        "receipt_l2_plus_l1_by_outcome_wei": dict(costs),
        "repeated_sender_target": common,
        "rows": rows,
    }


if __name__ == "__main__":
    print(json.dumps(main(), indent=2, sort_keys=True))
