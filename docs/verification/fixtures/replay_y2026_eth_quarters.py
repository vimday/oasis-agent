"""Rebuild the quarterly Ethereum receipt denominators, select witnesses and account for traces."""

import argparse
import csv
import gzip
import json
from collections import Counter
from dataclasses import asdict
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path

from measure_eth_nov21_window import rank
from replay_eth_hundred_census import receipts_proof, strict_paths
from replay_eth_hundred_holdout import envelope_cost
from replay_y2026_inventory import decoded, digest, read

from oasis_agent.evidence.public_receipts import decode_receipt, quantity
from oasis_agent.evidence.research_traces import first_failure, nested_calls
from oasis_agent.evidence.swap_flow_trace import (
    _calldata,
    _receipt_positions,
    _static_then_bytes,
    _transfers,
    replay_trace_funding,
)
from oasis_agent.rpc.budget import BudgetPolicy, RpcBudget
from scripts.replay_eth_tail_case import _settlement

ROOT = Path(__file__).resolve().parents[3] / "reports/research/y2026-quarter-receipts-20261127"
SCOPE = "41b206e9cfec9751a2527859c81e388c62b1020d310d8a9dba3d6d63f4181353"
CUTS = (1, 2, 5, 20, 50, 100, 200)
JULY_WITNESS = "0x407603f5cc76266a81b2a120a43ac3969e86a421ddbe8918778d3c0b7b91b788"


def july_cash(receipt, frames, positions, funding, settlement):
    """Account for the callback holder, the repayment, and the separately liquidated gain."""
    assert receipt.transaction_hash == JULY_WITNESS
    loan, cb = frames[9, 0], frames[9, 0, 1]
    lender, holder = loan.recipient, loan.sender
    assert lender == "0xbbbbbbbbbb9cc5e90e3b3af64bdaf62c37eeffcb"
    assert (cb.sender, cb.recipient) == (lender, holder)
    types = ("address", "uint256", "bytes")
    token, principal, params = _static_then_bytes(types, _calldata(loan, "flashLoan", types))
    cb_types = ("uint256", "bytes")
    assert _static_then_bytes(cb_types, _calldata(cb, "onMorphoFlashLoan", cb_types)) == (
        principal,
        params,
    )
    transfers = _transfers(receipt, positions)
    by_index = {t.index: t for t in transfers}
    credit, repay, sweep = (by_index[i] for i in (267, 292, 293))
    assert (credit.sender, credit.recipient, credit.token, credit.amount, credit.path) == (
        lender,
        holder,
        token,
        principal,
        (9, 0, 0),
    )
    assert (repay.sender, repay.recipient, repay.token, repay.amount, repay.path) == (
        holder,
        lender,
        token,
        principal,
        (9, 0, 2),
    )
    route = funding.resolved_swaps
    assert [p.leg.event.log_index for p in route] == [271, 274, 279, 287, 296]
    assert len({p.leg.event.pool for p in route}) == 5
    assert all(p.call_path[:3] == (9, 0, 1) for p in route[:-1])
    for p in route[:-1]:
        assert (
            by_index[p.input_transfer_log].sender
            == by_index[p.output_transfer_log].recipient
            == holder
        )
    for previous, current in pairwise(route[:-1]):
        a, b = by_index[previous.output_transfer_log], by_index[current.input_transfer_log]
        assert (a.token, a.amount) == (b.token, b.amount)
        movements = [
            t.index for t in transfers if t.token == a.token and holder in (t.sender, t.recipient)
        ]
        assert movements == [a.index, b.index] and a.index < b.index
    first, last, sell = route[0], route[3], route[4]
    assert (
        (first.leg.token_in, first.leg.amount_in)
        == (last.leg.token_out, principal)
        == (token, principal)
    )
    gain = last.leg.amount_out - principal
    assert [
        t.index for t in transfers if t.token == token and holder in (t.sender, t.recipient)
    ] == [
        credit.index,
        first.input_transfer_log,
        last.output_transfer_log,
        repay.index,
        sweep.index,
    ]
    assert (sweep.sender, sweep.recipient, sweep.token, sweep.amount) == (
        holder,
        receipt.recipient,
        token,
        gain,
    )
    assert (sell.leg.token_in, sell.leg.amount_in) == (token, gain)
    assert (
        by_index[sell.input_transfer_log].sender
        == by_index[sell.output_transfer_log].recipient
        == receipt.recipient
    )
    assert sell.leg.token_out == "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
    assert frames[24,].recipient == sell.leg.token_out
    assert _calldata(frames[24,], "withdraw", ("uint256",)) == sell.leg.amount_out.to_bytes(
        32, "big"
    )
    native = frames[24, 0]
    assert (native.sender, native.recipient, native.value) == (
        sell.leg.token_out,
        receipt.recipient,
        sell.leg.amount_out,
    )
    pnl = (
        sell.leg.amount_out
        - settlement["direct_eth_to_header_fee_receiver_wei"]
        - settlement["paid_gas_wei"]
    )
    assert settlement["visible_role_deltas"] == [
        {"account": receipt.sender, "asset": "ETH", "delta_after_receipt_gas": pnl}
    ]
    return {
        "lender": lender,
        "callback_holder": holder,
        "loan_token": token,
        "loan_raw": principal,
        "repaid_raw": repay.amount,
        "round_trip_output_raw": last.leg.amount_out,
        "excess_sold_raw": gain,
        "weth_realized_wei": sell.leg.amount_out,
        "sender_visible_eth_after_gas_wei": pnl,
        "credit_repay_sweep_logs": [credit.index, repay.index, sweep.index],
        "path_including_liquidation": [asdict(p) for p in route],
    }


def trace_case(case, registry):
    h, b, row = registry[case["tx_hash"]]
    tx = case["tx_hash"]
    env = h["transactions"][case["index"]]
    receipt = decode_receipt(row, b.ref, tx, case["index"])
    fees = {"gas_used": receipt.gas_used, "gas_wei": receipt.gas_used * receipt.effective_gas_price}
    if quantity(env["type"]) == 2:
        fees = envelope_cost(env, receipt, quantity(h["baseFeePerGas"]))
    out = {
        "fees": fees,
        "transaction_type": quantity(env["type"]),
        "trace_funding": None,
        "settlement": None,
    }
    loc = ROOT / "trace"
    method = "debug_traceTransaction"
    if not (loc / (tx + ".response")).exists() or "result" not in read(loc / (tx + ".response")):
        return out
    traced, meta = decoded(
        loc, tx, method, [tx, {"tracer": "callTracer", "tracerConfig": {"withLog": True}}], "trace"
    )
    frames = nested_calls(traced)
    assert frames[()].sender == receipt.sender and frames[()].recipient == receipt.recipient
    assert quantity(env["value"]) == frames[()].value
    positions = _receipt_positions(traced, frames, receipt)
    funding = replay_trace_funding(receipt, traced)
    funds = strict_paths(funding)
    settled = _settlement(receipt, traced, h["miner"])
    del settled["native_calls_touching_roles"]
    if tx == JULY_WITNESS:
        out["callback_borrower_accounting"] = july_cash(
            receipt, frames, positions, funding, settled
        )
    out.update(
        trace_wire=meta,
        trace_funding=funds,
        settlement=settled,
        frame_count=len(frames),
        native_builder_calls=[
            {"call": p, "value": f.value}
            for p, f in frames.items()
            if f.kind == "CALL"
            and f.value
            and f.recipient == h["miner"]
            and first_failure(frames, p) is None
        ],
    )
    return out


def collect(select_only):
    assert digest(ROOT / "scope.json") == SCOPE
    scope = read(ROOT / "scope.json")
    assert digest(ROOT / "capture.py") == scope["driver_sha256"]
    loc = ROOT / "panels"
    stop = read(loc / "STOP.json")
    assert stop["scope_sha256"] == SCOPE and stop["error"] is None
    for src in ("header", "receipts"):
        cid, _ = decoded(loc, src + "-chain", "eth_chainId", [], src)
        assert quantity(cid) == 1
    panels = []
    candidates = []
    registry = {}
    all_hashes = set()
    topics = scope["topics"]
    for qi, start in enumerate(scope["starts"], 1):
        hist = Counter()
        blocks = []
        failed = 0
        for height in range(start, start + scope["width"]):
            h, hm = decoded(
                loc, f"{height}-head", "eth_getBlockByNumber", [hex(height), True], "header"
            )
            rows, rm = decoded(
                loc, f"{height}-rows", "eth_getBlockReceipts", [h["hash"]], "receipts"
            )
            b, root = receipts_proof(h, rows, height, h["hash"])
            stamp = datetime.fromtimestamp(quantity(h["timestamp"]), UTC)
            assert stamp.year == 2026 and (stamp.month - 1) // 3 + 1 == qi
            if blocks:
                assert h["parentHash"] == blocks[-1]["hash"]
            blocks.append(
                {
                    "height": height,
                    "hash": h["hash"],
                    "parent": h["parentHash"],
                    "timestamp": stamp.isoformat(),
                    "receipts_root": root,
                    "transactions": len(rows),
                    "header_wire": hm,
                    "receipt_wire": rm,
                }
            )
            for i, row in enumerate(rows):
                tx = row["transactionHash"]
                assert tx not in all_hashes
                all_hashes.add(tx)
                failed += not quantity(row["status"])
                logs = [log for log in row["logs"] if log["topics"] and log["topics"][0] in topics]
                hist[len(logs)] += 1
                if len(logs) < 2 or not quantity(row["status"]):
                    continue
                pools, path = rank(logs, topics)
                candidates.append(
                    {
                        "tx_hash": tx,
                        "index": i,
                        "height": height,
                        "timestamp_utc": stamp.isoformat(),
                        "events": len(logs),
                        "pools": pools,
                        "amount_proxy": len(path),
                    }
                )
                registry[tx] = h, b, row
        panels.append(
            {
                "quarter": qi,
                "blocks": blocks,
                "transactions": sum(hist.values()),
                "reverted": failed,
                "matches": {k: sum(v for n, v in hist.items() if n >= k) for k in CUTS},
                "max_events": max(hist),
            }
        )
    assert sum(len(p["blocks"]) for p in panels) == 24
    first = min(
        candidates,
        key=lambda c: (-c["amount_proxy"], -c["events"], c["height"], c["index"], c["tx_hash"]),
    )
    wide = min(
        (c for c in candidates if c != first),
        key=lambda c: (-c["events"], c["height"], c["index"], c["tx_hash"]),
    )
    chosen = [first, wide]
    if select_only:
        return chosen
    assert chosen == read(ROOT / "selected.json")
    trace_stop = read(ROOT / "trace/STOP.json")
    assert trace_stop["scope_sha256"] == SCOPE
    policies = tuple(
        BudgetPolicy(s, c, 120) for s, c in [("header", 25), ("receipts", 25), ("trace", 3)]
    )
    with RpcBudget(ROOT / "original.sqlite3", policies, read_only=True) as budget:
        assert budget.counts() == trace_stop["counts"]
    chain, _ = decoded(ROOT / "trace", "trace-chain", "eth_chainId", [], "trace")
    assert quantity(chain) == 1
    old_path = ROOT.parent / "gold-book-20261127/all-hashes.tsv.gz"
    assert digest(old_path) == "400147164406c06fc29ce20ed28f43a29b39cb331d12442e11db40ac60dc8648"
    with gzip.open(old_path, "rt") as f:
        old = {r["tx_hash"] for r in csv.DictReader(f, delimiter="\t") if r["chain"] == "ethereum"}
    assert not old.intersection(all_hashes)
    return {
        "schema": "oasis.y2026.ethereum-receipt-panels.v1",
        "scope_sha256": SCOPE,
        "sources": scope["urls"],
        "panels": panels,
        "thresholds": {k: sum(p["matches"][k] for p in panels) for k in CUTS},
        "new_multi_event_candidates": candidates,
        "selected": [c | trace_case(c, registry) for c in chosen],
        "capture": stop,
        "trace": trace_stop,
    }


if __name__ == "__main__":
    if not __debug__:
        raise SystemExit("run without -O")
    p = argparse.ArgumentParser()
    p.add_argument("--select", action="store_true")
    print(json.dumps(collect(p.parse_args().select), indent=2, sort_keys=True))
