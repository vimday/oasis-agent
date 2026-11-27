"""Recompute the July borrower regression and the next three settled executor transactions."""

import csv
import gzip
import json
from collections import Counter
from dataclasses import asdict
from datetime import UTC, datetime

from replay_eth_hundred_census import receipts_proof
from replay_eth_hundred_holdout import envelope_cost
from replay_y2026_eth_quarters import JULY_WITNESS, ROOT, SCOPE
from replay_y2026_inventory import decoded, digest, read

from oasis_agent.arbitrage.cycle_protocols import abi, data_bytes, topic
from oasis_agent.evidence.morpho_callback import morpho_callbacks
from oasis_agent.evidence.public_receipts import decode_receipt, quantity
from oasis_agent.evidence.research_traces import first_failure, nested_calls
from oasis_agent.evidence.swap_flow_trace import (
    _calldata,
    _indexed_address,
    _receipt_positions,
    _transfers,
    replay_trace_funding,
)
from oasis_agent.rpc.budget import BudgetPolicy, RpcBudget
from scripts.replay_eth_tail_case import _settlement

NEW = ROOT.parent / "y2026-july-executor-cohort-20261127"
FAIL = ROOT.parent / "y2026-morpho-wbtc-family-20261127"
NEW_SHA = "3d1e61897683aaa4464bf07a01c39ed1d3f53678d74b799b74c7b9fdd8ca9e22"
FAIL_SHA = "c9d18cdc2d272cfef26e7e03ba221c6b52e96fb059e33e29279020630be45839"
WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
EXE = "0x781cdf8805e01f016fc39204a94e4382c48bcbb4"
NATIVE = "0x" + "ee" * 20
NETWORK = "0x2f9ec37d6ccfff1cab21733bdadede11c823ccb0"
TR = {"tracer": "callTracer", "tracerConfig": {"withLog": True}}


def scope_for(folder, hash_expected):
    scope = read(folder / "scope.json")
    stop = read(folder / "STOP.json")
    assert (
        digest(folder / "scope.json") == hash_expected == stop["scope"]
        and digest(folder / "capture.py") == scope["driver"]
    )
    with RpcBudget(
        folder / "original.sqlite3",
        tuple(BudgetPolicy(k, n, 150) for k, n in scope["caps"].items()),
        read_only=True,
    ) as b:
        assert b.counts() == stop["counts"]
    for key, url in scope["sources"].items():
        val, _ = decoded(folder, key + "-chain", "eth_chainId", [], url)
        assert quantity(val) == 1
    return scope, stop


def native_bancor(loan, rec, positions, frames, transfers, proofs):
    events = {e.log_index: e for e in rec.logs}
    by_index = {t.index: t for t in transfers}
    hold = loan.borrower
    assert [p.leg.event.log_index for p in proofs] == [191, 196]
    previous = proofs[-1].leg
    e = events[203]
    path = positions[203]
    f = frames[path]
    parent = frames[path[:-1]]
    assert (
        f.sender == parent.recipient == NETWORK
        and f.recipient == e.address
        and parent.sender == hold
    )
    types = ("address", "address", "uint256", "address", "address")
    token, final, sold, trader, beneficiary = abi(types, _calldata(f, "convert", types))
    assert (
        final == NATIVE
        and previous.token_out == token
        and previous.amount_out == sold
        and trader == beneficiary == hold
    )
    assert tuple(map(_indexed_address, e.topics[1:])) == (token, final, hold) and e.topics[
        0
    ] == topic("Conversion(address,address,address,uint256,uint256,int256)")
    amount_in, amount_out, _ = abi(("uint256", "uint256", "int256"), data_bytes(e.data))
    assert sold == amount_in and abi(("uint256",), f.output) == abi(
        ("uint256",), parent.output
    ) == (amount_out,)
    path_types = ("address[]", "uint256", "uint256", "address", "address", "uint256")
    route, supplied, minimum, dest, affiliate, fee = abi(
        path_types, _calldata(parent, "convertByPath", path_types)
    )
    assert len(route) == 3 and (route[0], route[-1], supplied) == (token, final, sold)
    assert (
        amount_out >= minimum
        and dest in (hold, "0x" + "00" * 20)
        and affiliate == "0x" + "00" * 20
        and fee == 0
    )
    pay = by_index[202]
    assert (pay.token, pay.sender, pay.recipient, pay.amount) == (token, hold, e.address, sold)
    assert [
        (t.index, t.amount)
        for t in transfers
        if t.token == token and hold in (t.sender, t.recipient)
    ] == [(195, sold), (202, sold)]
    native = [
        (p, f)
        for p, f in sorted(frames.items())
        if p[: len(loan.callback)] == loan.callback
        and f.kind == "CALL"
        and f.value
        and first_failure(frames, p) is None
    ]
    (credit, got), (wrap, paid) = native
    assert (
        credit[: len(path)] == path
        and got.sender == e.address
        and got.recipient == hold
        and got.value == amount_out
    )
    assert (
        wrap == positions[208]
        and paid.recipient == WETH
        and paid.sender == hold
        and paid.value == amount_out
        and _calldata(paid, "deposit", ()) == b""
    )
    assert events[208].address == WETH and events[208].topics[0] == topic(
        "Deposit(address,uint256)"
    )
    assert (
        _indexed_address(events[208].topics[1]) == hold and int(events[208].data, 16) == amount_out
    )
    assert (
        loan.asset == WETH
        and by_index[188].sender == hold
        and by_index[188].amount == proofs[0].leg.amount_in == loan.principal_raw
    )
    assert loan.callback_links[0].source == 191 and loan.callback_links[0].target == 196
    assert amount_out - loan.repaid_raw == by_index[210].amount and (
        by_index[210].sender,
        by_index[210].recipient,
        by_index[210].token,
    ) == (hold, EXE, WETH)
    return {
        "markets": [191, 196, 203],
        "conversion_input_log": 202,
        "native_payout_call": credit,
        "rewrap_log": 208,
        "principal": loan.principal_raw,
        "weth_returned": amount_out,
        "surplus_to_executor_log": 210,
        "surplus_raw": amount_out - loan.repaid_raw,
    }


def dodo(loan, rec, positions, frames, transfers, proofs):
    events = {e.log_index: e for e in rec.logs}
    by_index = {t.index: t for t in transfers}
    e = events[235]
    assert [p.leg.event.log_index for p in proofs] == [230, 240]
    first, last = (p.leg for p in proofs)
    assert first.token_in == loan.asset == last.token_in and first.amount_in == loan.principal_raw
    assert len(e.topics) == 1 and e.topics[0] == topic(
        "DODOSwap(address,address,uint256,uint256,address,address)"
    )
    asset, token, sold, bought, trader, dest = abi(
        ("address", "address", "uint256", "uint256", "address", "address"), data_bytes(e.data)
    )
    path = positions[235]
    # The pool proxy forwards the trade calldata to the implementation emitting the event.
    f = frames[path]
    if f.kind == "DELEGATECALL":
        wrapped = frames[path[:-1]]
        assert f.data == wrapped.data and wrapped.output == f.output and f.sender == e.address
        path = path[:-1]
        f = wrapped
    assert (
        f.sender == trader == dest == loan.borrower
        and f.recipient == e.address
        and first_failure(frames, path) is None
    )
    assert abi(("address",), _calldata(f, "sellQuote", ("address",))) == (dest,) and abi(
        ("uint256",), f.output
    ) == (bought,)
    assert (asset, sold, token) == (first.token_out, first.amount_out, loan.asset)
    assert [
        (t.index, t.amount)
        for t in transfers
        if t.token == asset and dest in (t.sender, t.recipient)
    ] == [(231, sold), (233, sold)]
    assert (by_index[233].sender, by_index[233].recipient) == (dest, e.address)
    assert (
        by_index[234].sender,
        by_index[234].recipient,
        by_index[234].token,
        by_index[234].amount,
    ) == (e.address, dest, token, bought)
    assert by_index[234].path[: len(path)] == path
    transfer_path = by_index[233].path[:-1]
    pay_frame = frames[transfer_path]
    delegate = frames[by_index[233].path]
    assert (
        delegate.kind == "DELEGATECALL"
        and delegate.data == pay_frame.data
        and delegate.output == pay_frame.output
    )
    assert (pay_frame.sender, pay_frame.recipient) == (dest, asset) and abi(
        ("address", "uint256"), _calldata(pay_frame, "transfer", ("address", "uint256"))
    ) == (e.address, sold)
    assert transfer_path[:-1] == path[:-1] and transfer_path[-1] < path[-1]
    assert (
        by_index[237].token,
        by_index[237].sender,
        by_index[237].recipient,
        by_index[237].amount,
    ) == (token, dest, EXE, bought - loan.repaid_raw)
    assert last.amount_in == by_index[237].amount and last.token_out == WETH
    return {
        "markets": [230, 235, 240],
        "dodo_prepayment_log": 233,
        "dodo_output_log": 234,
        "dodo_observed_pool": e.address,
        "dodo_sold_raw": sold,
        "principal_asset_returned_raw": bought,
        "surplus_raw": bought - loan.repaid_raw,
        "surplus_to_executor_log": 237,
        "liquidation_weth": last.amount_out,
    }


def callback_free(rec, frames, transfers, proofs):
    by_index = {t.index: t for t in transfers}
    a, b = proofs
    assert [a.leg.event.log_index, b.leg.event.log_index] == [611, 612]
    assert a.call_path[: len(b.call_path)] == b.call_path and (
        a.output_transfer_log,
        b.input_transfer_log,
    ) == (609, 609)
    assert (a.leg.token_out, a.leg.amount_out) == (b.leg.token_in, b.leg.amount_in)
    assert a.leg.token_in == b.leg.token_out == WETH
    assert (by_index[608].recipient, by_index[608].amount) == (
        by_index[610].sender,
        b.leg.amount_out,
    )
    excess = b.leg.amount_out - a.leg.amount_in
    assert (
        by_index[613].token,
        by_index[613].amount,
        by_index[613].sender,
        by_index[613].recipient,
    ) == (WETH, excess, by_index[610].sender, rec.recipient)
    return {
        "markets": [611, 612],
        "prefunded_weth_receipt_log": 608,
        "pool_direct_usdt_log": 609,
        "weth_actual_debit_log": 610,
        "surplus_to_executor_log": 613,
        "surplus_raw": excess,
    }


def measure(root, height, idx, tx, baseline=False):
    urls = read(root / "scope.json")["urls" if baseline else "sources"]
    hlabel = f"{height}-head" if baseline else tx + "-head"
    loc = root / "panels" if baseline else root
    h, hm = decoded(
        loc,
        hlabel,
        "eth_getBlockByNumber",
        [hex(height), True],
        "header" if baseline else urls["logs"],
    )
    rows, rm = decoded(
        loc,
        f"{height}-rows" if baseline else tx + "-rows",
        "eth_getBlockReceipts",
        [h["hash"]],
        "receipts" if baseline else urls["receipts"],
    )
    tree, tm = decoded(
        root / "trace" if baseline else root,
        tx if baseline else tx + "-trace",
        "debug_traceTransaction",
        [tx, TR],
        "trace" if baseline else urls["trace"],
    )
    block, rt = receipts_proof(h, rows, height, h["hash"])
    r = decode_receipt(rows[idx], block.ref, tx, idx)
    env = h["transactions"][idx]
    fees = envelope_cost(env, r, quantity(h["baseFeePerGas"]))
    assert env["hash"] == tx and (tree["input"], tree["from"], tree["to"], tree["value"]) == (
        env["input"],
        env["from"],
        EXE,
        env["value"],
    )
    fs = nested_calls(tree)
    positions = _receipt_positions(tree, fs, r)
    tr = _transfers(r, positions)
    fund = replay_trace_funding(r, tree)
    cb = morpho_callbacks(r, tree, fund)
    assert not cb.unresolved_loan_logs
    settlement = _settlement(r, tree, h["miner"])
    cash = settlement["native_calls_touching_roles"]
    assert len(cash) == 4
    incoming, unwrap, payment, payout = cash
    assert (incoming["from"], incoming["to"]) == (r.sender, EXE) and (
        payout["from"],
        payout["to"],
    ) == (EXE, r.sender)
    assert (payment["from"], payment["to"]) == (EXE, h["miner"]) and (
        unwrap["from"],
        unwrap["to"],
    ) == (WETH, EXE)
    assert payout["amount"] + payment["amount"] == unwrap["amount"] + incoming["amount"]
    assert settlement["visible_role_deltas"] == [
        {
            "account": r.sender,
            "asset": "ETH",
            "delta_after_receipt_gas": payout["amount"] - incoming["amount"] - fees["gas_wei"],
        }
    ]
    if baseline:
        (loan,) = cb.loans
        assert loan.principal_cycle is not None and loan.principal_cycle.swap_logs == (
            271,
            274,
            279,
            287,
        )
        assert (
            loan.principal_cycle.surplus_raw == fund.resolved_swaps[-1].leg.amount_in
            and fund.resolved_swaps[-1].leg.amount_out == unwrap["amount"]
        )
        special = {
            "markets": [271, 274, 279, 287, 296],
            "surplus_raw": loan.principal_cycle.surplus_raw,
        }
    elif tx.startswith("0xc720"):
        special = native_bancor(cb.loans[0], r, positions, fs, tr, fund.resolved_swaps)
        assert special["surplus_raw"] == unwrap["amount"]
    elif tx.startswith("0xdc2a"):
        special = dodo(cb.loans[0], r, positions, fs, tr, fund.resolved_swaps)
        assert special["liquidation_weth"] == unwrap["amount"]
    else:
        assert not cb.loans
        special = callback_free(r, fs, tr, fund.resolved_swaps)
        assert special["surplus_raw"] == unwrap["amount"]
    return {
        "tx": tx,
        "height": height,
        "index": idx,
        "hash": h["hash"],
        "timestamp_utc": datetime.fromtimestamp(quantity(h["timestamp"]), UTC).isoformat(),
        "ordered_receipts": len(rows),
        "receipts_root": rt,
        "chain_id": 1,
        "original_wires": [hm, rm, tm],
        "settlement": settlement,
        "eip1559_fees": fees,
        "generic_depth": fund.profile.witnessed_depth,
        "callback": asdict(cb),
        "funded_case": special,
        "frames": len(fs),
        "logs": len(r.logs),
        "unresolved_generic_events": fund.unresolved_events,
        "resolved_market_legs": [asdict(p) for p in fund.resolved_swaps],
    }


def main():
    scope, stop = scope_for(NEW, NEW_SHA)
    failed, failed_stop = scope_for(FAIL, FAIL_SHA)
    tag = str(failed["starts"][0]) + "-logs"
    m = read(FAIL / (tag + ".meta"))
    assert (
        m["http"] == 403
        and read(FAIL / (tag + ".response"))["error"]["code"] == -32602
        and failed_stop["error"]
    )
    assert (m["request_sha256"], m["response_sha256"]) == (
        digest(FAIL / (tag + ".request")),
        digest(FAIL / (tag + ".response")),
    )
    assert failed_stop["counts"] == {"logs": 2, "receipts": 1, "trace": 1}
    assert stop["error"] is None and stop["counts"] == {
        "index": 1,
        "logs": 4,
        "receipts": 4,
        "trace": 4,
    }
    page = read(NEW / "page.response")["items"]
    meta = read(NEW / "page.meta")
    url = scope["index_api"]
    assert read(NEW / "page.request") == {"method": "GET", "url": url}
    assert (meta["source"], meta["http"], meta["request_sha256"], meta["response_sha256"]) == (
        url,
        200,
        digest(NEW / "page.request"),
        digest(NEW / "page.response"),
    )
    ordered = [(r["block_number"], r["position"]) for r in page]
    assert ordered == sorted(set(ordered), reverse=True)
    assert all(r["to"]["hash"].lower() == EXE for r in page)
    cat = ROOT.parent / "gold-book-20261127/all-hashes.tsv.gz"
    assert (
        digest(cat) == "400147164406c06fc29ce20ed28f43a29b39cb331d12442e11db40ac60dc8648"
        and digest(ROOT / "scope.json") == SCOPE
    )
    with gzip.open(cat, "rt") as f:
        old = {z["tx_hash"] for z in csv.DictReader(f, delimiter="\t") if z["chain"] == "ethereum"}
    qs = read(ROOT / "scope.json")
    exclude = [range(v, v + qs["width"]) for v in qs["starts"]] + [
        range(v, v + failed["width"]) for v in failed["starts"]
    ]
    seen = set()
    selected = []
    for row in page:
        n = row["block_number"]
        tx = row["hash"]
        if (
            n in seen
            or tx in old
            or not row["timestamp"].startswith("2026-")
            or row["status"] != "ok"
            or any(n in r for r in exclude)
        ):
            continue
        seen.add(n)
        selected.append(
            {"blockNumber": n, "transactionHash": tx, "transactionIndex": row["position"]}
        )
        if len(selected) == 3:
            break
    assert selected == read(NEW / "selected.json")
    cases = [
        measure(NEW, r["blockNumber"], r["transactionIndex"], r["transactionHash"])
        for r in selected
    ]
    return {
        "schema": "oasis.ethereum.2026-morpho-callback-cohort.v1",
        "cost_usd": 0,
        "scope_sha256": NEW_SHA,
        "sources": scope["sources"],
        "stop": stop,
        "failed_archive": {"scope_sha256": FAIL_SHA, "wire": m, "stop": failed_stop},
        "index": {
            "source": url,
            "wire": meta,
            "items": len(page),
            "status": Counter(z["status"] for z in page),
            "listed_hashes": [z["hash"] for z in page],
            "old_catalog_matches": len(old & {z["hash"] for z in page}),
            "first_last_utc": [page[i]["timestamp"] for i in (0, -1)],
        },
        "july_calibration": measure(ROOT, 25584104, 122, JULY_WITNESS, True),
        "new_cases": cases,
    }


if __name__ == "__main__":
    if not __debug__:
        raise SystemExit("run without -O")
    print(json.dumps(main(), indent=2, sort_keys=True))
