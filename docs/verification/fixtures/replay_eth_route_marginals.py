"""Rebuild registered same-state batch variants and inspect their simulated cash flows."""

import argparse
import json
from collections import Counter
from dataclasses import replace
from hashlib import sha256
from itertools import pairwise
from pathlib import Path

import rlp
from eth_abi import decode, encode
from eth_account import Account
from eth_account.typed_transactions import TypedTransaction
from eth_hash.auto import keccak
from replay_eth_109_motive import transaction_leaf as type_two_leaf
from replay_eth_hundred_census import receipts_proof
from replay_eth_hundred_holdout import envelope_cost, source_bytes

from oasis_agent.arbitrage.cycle_protocols import data_bytes
from oasis_agent.evidence.nitro_block import indexed_trie_root
from oasis_agent.evidence.public_receipts import decode_receipt, quantity
from oasis_agent.evidence.research_traces import nested_calls
from oasis_agent.evidence.swap_flow_trace import _receipt_positions, replay_trace_funding
from oasis_agent.evidence.weth_batch_cash import replay_weth_self_calls
from oasis_agent.evidence.weth_principal import WETH
from oasis_agent.models.chain import LogRecord
from oasis_agent.rpc.budget import BudgetPolicy, RpcBudget
from scripts.replay_eth_tail_case import _settlement

CASES = (
    "0xb3ef721461c5031a6c949dbd8ac28f58741156d286381038e03d2be7526c375f",
    "0xf99920390d463751870cc42cc8b67b89c05ccc406e55a3b6acc08d04f4df27a0",
    "0xfdc95125e665fcb4166c6935819d34189302336f8276568e228bffdf6143a1f9",
)
SOURCE = "https://eth.drpc.org"
ABI = ("bytes", "uint256", "uint256", "uint256", "uint256")
KEYS = (
    "from",
    "to",
    "gas",
    "value",
    "type",
    "nonce",
    "chainId",
    "accessList",
    "maxFeePerGas",
    "maxPriorityFeePerGas",
)


def digest(raw):
    return sha256(raw).hexdigest()


def fingerprint(value):
    return digest(json.dumps(value, separators=(",", ":"), sort_keys=True).encode())


def self_calls(tree):
    return [
        c
        for c in tree.get("calls", [])
        if c.get("type") == "CALL"
        and c["to"] == c["from"] == tree["to"]
        and c["input"].startswith("0xee6227b0")
    ]


def variants(env, tree, branch):
    assert env["data"] == tree["input"] and env["data"].startswith("0x96183ac4")
    raw = bytes.fromhex(env["data"][10:])
    blob, *params = decode(ABI, raw)
    assert encode(ABI, (blob, *params)) == raw
    # Only the two registered historical packets have been calibrated for this record boundary.
    starts = []
    for i in range(1, len(blob) - 24):
        size = int.from_bytes(blob[i : i + 2])
        hops = blob[i + 2]
        if (
            2 <= hops <= 30
            and size == 18 + 47 * hops
            and blob[i + 3] <= hops
            and blob[i + 4 : i + 24].hex() == WETH[2:]
            and i + 2 + size <= len(blob)
        ):
            starts.append(i)
    assert len(starts) == blob[0] and starts[0] == 1
    records = [blob[a:b] for a, b in pairwise([*starts, len(blob)])]
    identifiers = [r[2 : 2 + int.from_bytes(r[:2])] for r in records]
    assert len(set(identifiers)) == len(records)
    assert all(len(r) > 2 + len(v) for r, v in zip(records, identifiers, strict=True))

    def identity(call):
        (inner,) = decode(("bytes",), bytes.fromhex(call["input"][10:]))
        assert call["input"][10:] == encode(("bytes",), (inner,)).hex()
        return identifiers.index(inner)

    issued = list(map(identity, self_calls(tree)))
    assert issued == sorted(set(issued))
    target = identity(tree["calls"][branch])
    assert target in issued

    def encode_records(rows):
        return env["data"][:10] + encode(ABI, (bytes([len(rows)]) + b"".join(rows), *params)).hex()

    assert encode_records(records) == env["data"]
    return (
        {
            "drop": {**env, "data": encode_records(records[:target] + records[target + 1 :])},
            "only": {**env, "data": encode_records([records[target]])},
        },
        {
            "record_index": target,
            "record_count": len(records),
            "called": len(issued),
            "target_call_sha256": digest(bytes.fromhex(tree["calls"][branch]["input"][2:])),
        },
    )


def transaction_commitment(header):
    leaves = []
    types = Counter()
    for index, tx in enumerate(header["transactions"]):
        assert (tx["blockHash"], quantity(tx["blockNumber"]), quantity(tx["transactionIndex"])) == (
            header["hash"],
            quantity(header["number"]),
            index,
        )
        kind = quantity(tx["type"])
        if kind == 2:
            # Existing type-2 verifier checks the hash, signature, chain and parity.
            leaf = type_two_leaf(tx)
        else:
            terms = (
                data_bytes(tx["to"]) if tx["to"] is not None else b"",
                quantity(tx["value"]),
                data_bytes(tx["input"]),
            )
            if kind == 0:
                assert quantity(tx["chainId"]) == 1 and quantity(tx["v"]) in (37, 38)
                leaf = rlp.encode(
                    [
                        *(quantity(tx[k]) for k in ("nonce", "gasPrice", "gas")),
                        *terms,
                        *(quantity(tx[k]) for k in ("v", "r", "s")),
                    ]
                )
            else:
                # These retained blocks also contain blob and set-code transactions.
                assert kind in (3, 4) and quantity(tx["chainId"]) == 1
                assert quantity(tx["v"]) == quantity(tx["yParity"]) in (0, 1)
                extras = (
                    ("maxFeePerBlobGas", "blobVersionedHashes")
                    if kind == 3
                    else ("authorizationList",)
                )
                fields = {
                    key: tx[key]
                    for key in (
                        "type",
                        "chainId",
                        "nonce",
                        "maxPriorityFeePerGas",
                        "maxFeePerGas",
                        "gas",
                        "accessList",
                        "v",
                        "r",
                        "s",
                        *extras,
                    )
                }
                fields["to"], fields["value"], fields["data"] = terms
                leaf = TypedTransaction.from_dict(fields).encode()
            assert "0x" + keccak(leaf).hex() == tx["hash"]
            assert Account.recover_transaction(leaf).lower() == tx["from"]
        leaves.append(leaf)
        types[kind] += 1
    root = "0x" + indexed_trie_root(leaves).hex()
    assert root == header["transactionsRoot"]
    return {
        "transactions_root": root,
        "signed_transactions": len(leaves),
        "count_by_type": dict(sorted(types.items())),
    }


def fixed_cases(research):
    root = research / "eth-batch-hundred-census-20261127"
    report_path = Path(__file__).parents[1] / "BATCH_HUNDRED_CENSUS_20261127.json"
    census = json.loads(report_path.read_bytes())
    for tx_hash in CASES:
        entry = next(
            c for c in census["additional_cases"] if c["target"]["transaction_hash"] == tx_hash
        )
        target = entry["target"]
        n, i, bh = (target[k] for k in ("block_number", "transaction_index", "block_hash"))
        header = json.loads(source_bytes(root, f"{n}-header")[0])["result"]
        receipts = json.loads(source_bytes(root, f"{n}-receipts")[0])["result"]
        b, _ = receipts_proof(header, receipts, n, bh)
        tx = header["transactions"][i]
        receipt = decode_receipt(receipts[i], b.ref, tx_hash, i)
        envelope_cost(tx, receipt, quantity(header["baseFeePerGas"]))
        env = {k: tx[k] for k in KEYS}
        env["data"] = tx["input"]
        trace = json.loads(source_bytes(root, tx_hash[2:14] + "-trace")[0])["result"]
        proof = replay_trace_funding(receipt, trace)
        by_idx = {p.leg.event.log_index: p for p in proof.resolved_swaps}
        route = [
            by_idx[row["leg"]["event"]["log_index"]]
            for row in entry["funding"]["same_currency_legs"]
        ]
        assert len(route) in (4, 6)
        links = {(edge.source, edge.target) for edge in proof.links}
        assert all(
            (a.leg.event.log_index, b.leg.event.log_index) in links for a, b in pairwise(route)
        )
        assert route[0].leg.token_in == route[-1].leg.token_out == WETH
        event_logs = {log.log_index: log for log in receipt.logs}
        signatures = [
            (p.leg.event.pool, event_logs[p.leg.event.log_index].topics[0]) for p in route
        ]

        def touches(log, signatures=signatures):
            return any(
                log["address"] == a
                and log["topics"][:1] == [sig]
                and (pid is None or log["topics"][1:2] == [pid])
                for (a, pid), sig in signatures
            )

        earlier = [j for j, r in enumerate(receipts[:i]) if any(touches(log) for log in r["logs"])]
        requests = {"landed": (env, i)}
        info = {}
        if tx_hash in CASES[:2]:
            branch = route[0].call_path[0]
            assert all(leg.call_path[0] == branch for leg in route)
            alternative, info = variants(env, trace, branch)
            requests.update({k: (v, i) for k, v in alternative.items()})
        # Same block / identical time and fee context. First observe the calibration.
        indices = {"opening": 0, "after_landed": i + 1}
        if earlier:
            indices.update(before_touch=earlier[-1], after_touch=earlier[-1] + 1)
        for label, pos in indices.items():
            requests[label] = (env, pos)
        yield (target, requests, receipt, trace, header, earlier, info, route)


def trace_query(env, bh, idx):
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "debug_traceCall",
        "params": [
            env,
            bh,
            {
                "tracer": "callTracer",
                "tracerConfig": {"withLog": True},
                "txIndex": hex(idx),
                "timeout": "15s",
            },
        ],
    }


def stage_logs(tree):
    if tree.get("error") is not None:
        return
    children = tree.get("calls", [])
    positions = {}
    for log in tree.get("logs", []):
        at = quantity(log["position"])
        assert at <= len(children)
        positions.setdefault(at, []).append(log)
    for pos in range(len(children) + 1):
        yield from positions.get(pos, [])
        if pos < len(children):
            yield from stage_logs(children[pos])


def outcome(anchor, tree, env, miner, route, target_sha, single_record):
    assert (
        tree["type"],
        tree["from"],
        tree["to"],
        tree["input"],
        quantity(tree["value"]),
        quantity(tree["gas"]),
    ) == ("CALL", env["from"], env["to"], env["data"], quantity(env["value"]), quantity(env["gas"]))
    nodes = nested_calls(tree)
    assert all(c.kind not in {"CREATE", "CREATE2", "SELFDESTRUCT"} for c in nodes.values())
    ordered = tuple(
        LogRecord(
            log["address"],
            tuple(log["topics"]),
            log["data"],
            anchor.block.number,
            anchor.transaction_hash,
            i,
        )
        for i, log in enumerate(stage_logs(tree))
    )
    sim = replace(
        anchor, logs=ordered, gas_used=quantity(tree["gasUsed"]), status=int("error" not in tree)
    )
    settlement = _settlement(sim, tree, miner)
    calls = self_calls(tree)
    identities = [digest(bytes.fromhex(c["input"][2:])) for c in calls]
    position = identities.index(target_sha) if target_sha in identities else None
    funding = replay_trace_funding(sim, tree)
    matches = []
    relevant = []
    if position is not None:
        branch_idx = next(i for i, c in enumerate(tree["calls"]) if c is calls[position])
        relevant = [p for p in funding.resolved_swaps if p.call_path[:1] == (branch_idx,)]
    elif single_record and len(funding.resolved_swaps) == len(route):
        relevant = list(funding.resolved_swaps)
    for leg in route:
        hits = [
            p
            for p in relevant
            if (p.leg.event.pool, p.leg.token_in, p.leg.token_out)
            == (leg.leg.event.pool, leg.leg.token_in, leg.leg.token_out)
        ]
        if len(hits) != 1:
            break
        matches.append(hits[0])
    links = {(edge.source, edge.target) for edge in funding.links}
    closed = len(matches) == len(route) and all(
        (a.leg.event.log_index, b.leg.event.log_index) in links for a, b in pairwise(matches)
    )
    effects = {
        name: fingerprint(
            [
                row.get("error"),
                row.get("output"),
                [[event["address"], event["topics"], event["data"]] for event in stage_logs(row)],
            ]
        )
        for name, row in zip(identities, calls, strict=True)
    }
    assert len(effects) == len(calls)
    branch_cash = replay_weth_self_calls(sim, tree)
    cash_by_call = {
        digest(bytes.fromhex(tree["calls"][item.root_child]["input"][2:])): item.weth_delta_wei
        for item in branch_cash
    }
    out = tree.get("output", "")
    reason = None
    if not sim.status and out[:10] == "0x08c379a0":
        reason = decode(("string",), bytes.fromhex(out[10:]))[0]
        assert reason == "profit"
    return {
        "status": "success" if sim.status else "revert",
        "revert_string": reason,
        "remaining_call_effects": effects,
        "cash_by_call": cash_by_call,
        "self_call_cash_reconciled": sum(item.status == "reconciled" for item in branch_cash),
        "self_call_cash_weth_wei": sum(
            item.weth_delta_wei for item in branch_cash if item.status == "reconciled"
        ),
        "gas": sim.gas_used,
        "gas_cost_wei": settlement["paid_gas_wei"],
        "direct_payment_wei": settlement["direct_eth_to_header_fee_receiver_wei"],
        "role_deltas": settlement["visible_role_deltas"],
        "conditional_net_wei": settlement["conditional_common_role_eth_weth_delta_wei"],
        "logs": len(ordered),
        "self_calls": len(calls),
        "self_order": identities,
        "target_called": position is not None if target_sha else None,
        "target_six_hop_io": (
            [matches[0].leg.amount_in, matches[-1].leg.amount_out] if closed else None
        ),
        "failed_calls": sum(c.error is not None for c in nodes.values()),
    }


def replay(research):
    folder = research / "eth-route-marginals-20261127"
    plan = json.loads((folder / "scope.json").read_bytes())
    cases = []
    count = 0
    for target, variants_by_stage, receipt, actual, header, prior, info, route in fixed_cases(
        research
    ):
        tx = target["transaction_hash"]
        record = {
            "tx": tx,
            "block": receipt.block.number,
            "hash": receipt.block.block_hash,
            "index": receipt.transaction_index,
            "transactions_commitment": transaction_commitment(header),
            "receipts_root": header["receiptsRoot"],
            "prior_route_swap_indices": prior,
            "batch": info,
            "stages": {},
            "requests": {},
        }
        visited = {}
        for label, (env, index) in variants_by_stage.items():
            req = trace_query(env, receipt.block.block_hash, index)
            key = fingerprint(req)
            filename = visited.setdefault(key, tx[2:14] + "-" + label)
            raw, request, meta = source_bytes(folder, filename)
            assert json.loads(request) == req and meta["source"] == SOURCE
            assert [filename, key] in plan["requests"]
            count += int(filename == tx[2:14] + "-" + label)
            tree = json.loads(raw)["result"]
            if label == "landed":
                _receipt_positions(tree, nested_calls(tree), receipt)
                assert tree == actual
            metrics = outcome(
                receipt,
                tree,
                env,
                header["miner"],
                route,
                info.get("target_call_sha256"),
                label == "only",
            )
            order = metrics.pop("self_order")
            effects = metrics.pop("remaining_call_effects")
            cash = metrics.pop("cash_by_call")
            if label == "landed":
                full = order
                full_effects = effects
                full_cash = cash
            if label == "drop":
                assert order == [h for h in full if h != info["target_call_sha256"]]
                assert set(order) == set(cash)
                metrics["other_calls_with_changed_weth_delta"] = sum(
                    cash[h] != full_cash[h] for h in order
                )
                metrics["other_calls_with_changed_logs_status_or_output"] = sum(
                    effects[h] != full_effects[h] for h in order
                )
            if label == "only" and order:
                assert order == [info["target_call_sha256"]]
            record["stages"][label] = metrics
            record["requests"][label] = {
                "wire": filename,
                "tx_index_before": index,
                "request_sha256": meta["request_sha256"],
                "response_sha256": meta["response_sha256"],
                "envelope_sha256": fingerprint(env),
            }
        if info:
            full, drop, only = (record["stages"][k] for k in ("landed", "drop", "only"))
            assert full["target_six_hop_io"] == [route[0].leg.amount_in, route[-1].leg.amount_out]
            if only["status"] == "success":
                assert only["target_six_hop_io"] == full["target_six_hop_io"]
            assert all(isinstance(x["conditional_net_wei"], int) for x in (full, drop, only))
            record["full_minus_drop"] = {
                k: full[k] - drop[k]
                for k in ("gas", "gas_cost_wei", "direct_payment_wei", "conditional_net_wei")
            }
        cases.append(record)
    assert count == len(plan["requests"])
    with RpcBudget(
        folder / "original.sqlite3", (BudgetPolicy("trace", count, 0),), read_only=True
    ) as b:
        assert b.counts() == {"trace": count}
    return {
        "schema": "oasis-ethereum-batch-entry-marginals-v2",
        "chain_id": 1,
        "source": SOURCE,
        "scope_sha256": digest((folder / "scope.json").read_bytes()),
        "anonymous_reads": count,
        "cases": cases,
    }


if __name__ == "__main__":
    if not __debug__:
        raise RuntimeError("assertions required for evidence verification")
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("research", type=Path)
    print(json.dumps(replay(p.parse_args().research), indent=2, sort_keys=True))
