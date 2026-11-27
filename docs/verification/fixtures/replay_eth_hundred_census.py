"""Replay the remaining Ethereum 2026-11-23 eight-selector transactions with >=100 events."""

import argparse
import json
from collections import defaultdict
from dataclasses import asdict
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path

from replay_eth_hundred_holdout import _V4_BALANCER, digest, envelope_cost, source_bytes

from oasis_agent.evidence.nitro_block import indexed_trie_root
from oasis_agent.evidence.public_receipts import decode_receipt, quantity
from oasis_agent.evidence.research_traces import first_failure, nested_calls
from oasis_agent.evidence.state_trie import prague_header
from oasis_agent.evidence.swap_flow import profile_swap_receipt
from oasis_agent.evidence.swap_flow_trace import _receipt_positions, replay_trace_funding
from oasis_agent.rpc.budget import BudgetPolicy, RpcBudget
from scripts.replay_eth_tail_case import _archive_match, _settlement
from scripts.replay_weth_principal import _receipt_leaf


def receipts_proof(header, rows, number, hash_expected):
    block, _ = prague_header(header, number, hash_expected)
    previous_gas = next_log = 0
    for index, (row, transaction) in enumerate(zip(rows, header["transactions"], strict=True)):
        tx_hash = transaction["hash"] if isinstance(transaction, dict) else transaction
        assert (row["transactionHash"], row["blockHash"]) == (tx_hash, hash_expected)
        assert (quantity(row["blockNumber"]), quantity(row["transactionIndex"])) == (number, index)
        cumulative = quantity(row["cumulativeGasUsed"])
        assert cumulative - previous_gas == quantity(row["gasUsed"])
        previous_gas = cumulative
        for log in row["logs"]:
            assert (log["transactionHash"], log["blockHash"]) == (tx_hash, hash_expected)
            assert quantity(log["logIndex"]) == next_log
            next_log += 1
    assert previous_gas == quantity(header["gasUsed"])
    root = "0x" + indexed_trie_root([_receipt_leaf(row) for row in rows]).hex()
    assert root == header["receiptsRoot"]
    return block, root


def strict_paths(funding):
    proofs = {item.leg.event.log_index: item for item in funding.resolved_swaps}
    assert funding.profile.witnessed_depth is not None
    links = {(link.source, link.target): link for link in funding.links}
    following = dict(links.keys())
    entered = set(following.values())
    assert len(following) == len(entered) == len(funding.links)
    longest_close = []
    for start in sorted(proofs.keys() - entered):
        path = [start]
        while path[-1] in following:
            path.append(following[path[-1]])
        first_start_by_token = {}
        for j, number in enumerate(path):
            leg = proofs[number].leg
            first_start_by_token.setdefault(leg.token_in, j)
            known = first_start_by_token.get(leg.token_out)
            if known is not None and len(longest_close) < j - known + 1:
                longest_close = path[known : j + 1]
    exact = funding.profile.longest_witnessed_path
    return {
        "resolved_events": len(proofs),
        "links": len(funding.links),
        "strict_depth": funding.profile.witnessed_depth,
        "same_currency_depth": len(longest_close),
        "strict_legs": [asdict(proofs[n]) for n in exact],
        "strict_links": [asdict(links[pair]) for pair in pairwise(exact)],
        "same_currency_legs": [asdict(proofs[n]) for n in longest_close],
        "same_currency_links": [asdict(links[pair]) for pair in pairwise(longest_close)],
        "unresolved_events": funding.unresolved_events,
    }


def record_case(root, target, scope, archive, selectors):
    n, block_hash, index, tx_hash = (
        target[k] for k in ("block_number", "block_hash", "transaction_index", "transaction_hash")
    )
    report = {"target": target}
    dirs = scope["reused_witnesses"].get(str(n), {})
    witnesses = {}
    for kind, label in (("header", str(n)), ("receipts", str(n)), ("trace", tx_hash[2:14])):
        reused = dirs.get(kind)
        directory = Path(reused["directory"]) if reused else root
        info = directory / f"{label}-{kind}.meta.json"
        if not info.exists():
            continue
        observed = json.loads(info.read_bytes())
        if observed["failure"] is not None:
            report["first_source_error"] = observed
            continue
        raw, request, meta = source_bytes(directory, f"{label}-{kind}")
        assert meta["source"] == scope["sources"][kind]
        if reused:
            assert meta["response_sha256"] == reused["response_sha256"]
        witnesses[kind] = (raw, json.loads(request), meta)

    report["sources"] = {kind: row[2] for kind, row in witnesses.items()}
    if "header" not in witnesses or "receipts" not in witnesses:
        return dict(report, status="unverified_block_receipts")
    header = json.loads(witnesses["header"][0])["result"]
    rows = json.loads(witnesses["receipts"][0])["result"]
    expected_header = witnesses["header"][1]
    assert expected_header["method"] == "eth_getBlockByNumber"
    assert expected_header["params"][0] == hex(n)
    assert witnesses["receipts"][1]["method"] == "eth_getBlockReceipts"
    assert witnesses["receipts"][1]["params"] == [block_hash]
    block, receipt_root = receipts_proof(header, rows, n, block_hash)
    receipt = decode_receipt(rows[index], block.ref, tx_hash, index)
    _archive_match(archive, receipt)
    counted = [row for row in receipt.logs if row.topics and row.topics[0] in selectors]
    pool_keys = {
        (row.address, row.topics[1] if row.topics[0] in _V4_BALANCER else None) for row in counted
    }
    assert (len(counted), len(pool_keys)) == (target["events"], target["pool_keys"])
    profile = profile_swap_receipt(receipt)
    report.update(
        utc=datetime.fromtimestamp(block.ref.timestamp, UTC).isoformat(),
        receipts_root=receipt_root,
        verified_block_receipts=len(rows),
        archive_logs=len(receipt.logs),
        compatible_shape_events=len(profile.events),
        fee_payer=receipt.sender,
        executor=receipt.recipient,
        fee_recipient=header["miner"],
    )
    tx = header["transactions"][index]
    base_fee = quantity(header["baseFeePerGas"])
    kind = quantity(rows[index]["type"])
    if isinstance(tx, dict) and kind == 2:
        envelope = envelope_cost(tx, receipt, base_fee)
        fee_validation = "rooted gas, transaction type, transaction hash and signature verified"
    else:
        assert receipt.effective_gas_price >= base_fee
        envelope = {
            "gas_used": receipt.gas_used,
            "effective_wei_per_gas": receipt.effective_gas_price,
            "base_wei_per_gas": base_fee,
            "burn_wei": receipt.gas_used * base_fee,
            "priority_wei": receipt.gas_used * (receipt.effective_gas_price - base_fee),
            "gas_wei": receipt.gas_used * receipt.effective_gas_price,
        }
        fee_validation = (
            "rooted gas and transaction type; effective gas price reported by receipt source"
        )
    report.update(fees=envelope, fee_validation=fee_validation, transaction_type=kind)
    if "trace" not in witnesses:
        return dict(report, status="trace_missing")
    trace_raw, trace_request, _ = witnesses["trace"]
    assert trace_request["method"] == "debug_traceTransaction"
    assert trace_request["params"][0] == tx_hash
    assert trace_request["params"][1]["tracer"] == "callTracer"
    assert trace_request["params"][1]["tracerConfig"] == {"withLog": True}
    trace = json.loads(trace_raw)["result"]
    try:
        frames = nested_calls(trace)
        _receipt_positions(trace, frames, receipt)
    except ValueError as exc:
        return dict(report, status="trace_unsupported", reason=str(exc))
    assert frames[()].sender == receipt.sender and frames[()].recipient == receipt.recipient
    if isinstance(tx, dict):
        assert frames[()].value == quantity(tx["value"])
        assert "0x" + frames[()].data.hex() == tx["input"]
    payment = [
        {"path": path, "from": frame.sender, "value": frame.value}
        for path, frame in sorted(frames.items())
        if (
            frame.kind == "CALL"
            and frame.recipient == header["miner"]
            and frame.value
            and first_failure(frames, path) is None
        )
    ]
    settlement = _settlement(receipt, trace, header["miner"])
    settlement.pop("native_calls_touching_roles")
    assert settlement["direct_eth_to_header_fee_receiver_wei"] <= sum(
        row["value"] for row in payment
    )
    report.update(
        status="verified",
        funding=strict_paths(replay_trace_funding(receipt, trace)),
        call_frames=len(frames),
        explicit_native_fee_calls=payment,
        settlement=settlement,
    )
    return report


def main():
    if not __debug__:
        raise SystemExit("run this evidence verifier without Python optimization")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    root = args.root
    previous = Path("docs/verification/BATCH_HUNDRED_HOLDOUT_20261127.json").read_bytes()
    fixed = json.loads(previous)
    scope_raw = (root / "scope.json").read_bytes()
    scope = json.loads(scope_raw)
    assert digest(previous) == scope["previous_report_sha256"]
    chosen = fixed["cases"]
    originals = {item["target"]["transaction_hash"] for item in chosen}
    correct = [r for r in fixed["eligible_tail"] if r["transaction_hash"] not in originals]
    assert {json.dumps(r, sort_keys=True) for r in scope["targets"]} == {
        json.dumps(r, sort_keys=True) for r in correct
    }
    selectors = set(chosen[0]["selector_histogram"]) | set(chosen[1]["selector_histogram"])
    sql = (root.parent / "eth-batch-hundred-holdout-20261127/select.sql").read_bytes()
    assert digest(sql) == fixed["selection"]["selection_sql_sha256"]
    selectors |= {row for row in sql.decode().split("'") if row.startswith("0x") and len(row) == 66}
    assert len(selectors) == 8
    batches = defaultdict(list)
    archive_raw = (root / "archive-logs.json").read_bytes()
    for row in json.loads(archive_raw):
        batches[row["transaction_hash"]].append(row)
    assert set(batches) == {r["transaction_hash"] for r in correct}
    policies = tuple(BudgetPolicy(key, cap, 0) for key, cap in scope["caps"].items())
    with RpcBudget(root / "original.sqlite3", policies, read_only=True) as ledger:
        counts = ledger.counts()
    stop = json.loads((root / "STOP.json").read_bytes())
    results = [
        record_case(
            root, target, scope, json.dumps(batches[target["transaction_hash"]]).encode(), selectors
        )
        for target in scope["targets"]
    ]
    print(
        json.dumps(
            {
                "schema": "oasis-eth-hundred-event-census-v1",
                "sources_by_lane": scope["sources"],
                "scope_sha256": digest(scope_raw),
                "prior_two_cases_sha256": digest(previous),
                "archived_logs_sha256": digest(archive_raw),
                "eligible": len(fixed["eligible_tail"]),
                "prior_cases": len(chosen),
                "attempts": counts,
                "capture": stop,
                "additional_cases": results,
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
