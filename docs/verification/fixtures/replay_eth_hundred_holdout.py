"""Replay the fixed Ethereum 2026-11-23 highest-event and widest-pool controls."""

import json
import sys
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import rlp
from eth_account import Account
from eth_hash.auto import keccak

from oasis_agent.arbitrage.cycle_protocols import abi, data_bytes, topic
from oasis_agent.evidence.public_receipts import decode_receipt, quantity
from oasis_agent.evidence.research_traces import first_failure, nested_calls
from oasis_agent.evidence.state_trie import prague_header
from oasis_agent.evidence.swap_flow import profile_swap_receipt
from oasis_agent.evidence.swap_flow_trace import (
    _receipt_positions,
    _transfers,
    replay_trace_funding,
)
from oasis_agent.evidence.weth_principal import WETH
from scripts.replay_eth_tail_case import _archive_match, _settlement
from scripts.replay_weth_principal import analyze

_MORPHO = "0xbbbbbbbbbb9cc5e90e3b3af64bdaf62c37eeffcb"
_FLASH = topic("FlashLoan(address,address,uint256)")
_WITHDRAW = topic("Withdrawal(address,uint256)")
_DEPOSIT = topic("Deposit(address,uint256)")
_V4_BALANCER = {
    "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f",
    "0x2170c741c41531aec20e7c107c24eecfdd15e69c9bb0a8dd37b1840b9e0b207b",
}


def digest(value):
    return sha256(value).hexdigest()


def source_bytes(root, label):
    raw = (root / f"{label}.response.json").read_bytes()
    request = (root / f"{label}.request.json").read_bytes()
    meta = json.loads((root / f"{label}.meta.json").read_bytes())
    assert meta["http"] == "200" and meta["failure"] is None
    assert meta["request_sha256"] == digest(request) and meta["response_sha256"] == digest(raw)
    assert len(raw) == meta["response_bytes"]
    assert json.loads(raw)["id"] == json.loads(request)["id"] == 1
    return raw, request, meta


def envelope_cost(tx, receipt, base_fee):
    assert quantity(tx["type"]) == 2 and quantity(tx["chainId"]) == 1
    tip, cap = (quantity(tx[k]) for k in ("maxPriorityFeePerGas", "maxFeePerGas"))
    parity = quantity(tx["yParity"] if "yParity" in tx else tx["v"])
    access = [
        [data_bytes(row["address"]), [data_bytes(k) for k in row["storageKeys"]]]
        for row in tx["accessList"]
    ]
    envelope = bytes([2]) + rlp.encode(
        [
            1,
            quantity(tx["nonce"]),
            tip,
            cap,
            quantity(tx["gas"]),
            data_bytes(tx["to"]),
            quantity(tx["value"]),
            data_bytes(tx["input"]),
            access,
            parity,
            quantity(tx["r"]),
            quantity(tx["s"]),
        ]
    )
    assert "0x" + keccak(envelope).hex() == receipt.transaction_hash
    assert Account.recover_transaction(envelope).lower() == receipt.sender == tx["from"]
    assert receipt.recipient == tx["to"] and cap >= base_fee
    price = min(cap, base_fee + tip)
    assert price == receipt.effective_gas_price == quantity(tx["gasPrice"])
    return {
        "gas_used": receipt.gas_used,
        "base_wei_per_gas": base_fee,
        "tip_wei_per_gas": price - base_fee,
        "effective_wei_per_gas": price,
        "burn_wei": base_fee * receipt.gas_used,
        "priority_wei": (price - base_fee) * receipt.gas_used,
        "gas_wei": price * receipt.gas_used,
    }


def morpho_principals(receipt, frames, positions, transfers):
    principals = []
    for log in receipt.logs:
        if log.address != _MORPHO or log.topics[0] != _FLASH:
            continue
        _, borrower_word, token_word = log.topics
        borrower = abi(("address",), data_bytes(borrower_word))[0]
        token = abi(("address",), data_bytes(token_word))[0]
        amount = abi(("uint256",), data_bytes(log.data))[0]
        path = positions[log.log_index]
        frame = frames[path]
        assert frame.sender == borrower == receipt.recipient and frame.recipient == _MORPHO
        assert frame.data[:4] == keccak(b"flashLoan(address,uint256,bytes)")[:4]
        # Solidity accepts the declared bytes without trailing word padding.
        token_in, requested, data_at = abi(("address", "uint256", "uint256"), frame.data[4:100])
        assert (token_in, requested, data_at) == (token, amount, 96)
        (payload_size,) = abi(("uint256",), frame.data[100:132])
        assert payload_size <= len(frame.data) - 132
        assert first_failure(frames, path) is None
        taken, repaid = [
            t
            for t in transfers
            if (
                t.token == token
                and {t.sender, t.recipient} == {borrower, _MORPHO}
                and t.path[: len(path)] == path
            )
        ]
        assert (taken.sender, taken.amount, repaid.sender, repaid.amount) == (
            _MORPHO,
            amount,
            borrower,
            amount,
        )
        assert log.log_index < taken.index < repaid.index
        assert taken.path[: len(path) + 1] == (*path, 0)
        assert repaid.path[: len(path) + 1] == (*path, 2)
        principals.append(
            {
                "token": token,
                "amount": amount,
                "fee_raw": repaid.amount - taken.amount,
                "event": log.log_index,
                "take": taken.index,
                "repay": repaid.index,
                "path": path,
            }
        )
    assert principals
    return principals


def weth_bridge(first, second, receipt, frames, positions, transfers):
    """Witness one exact WETH unwrap/native payout/re-wrap within a call group."""
    pool0, pool1 = first.pool[0], second.pool[0]
    refs = [log for log in receipt.logs if first.log_index - 2 <= log.log_index < second.log_index]
    events = {
        log.topics[0]: log
        for log in refs
        if log.address == WETH and log.topics[0] in {_WITHDRAW, _DEPOSIT}
    }
    assert len(events) == 2
    wlog, dlog = (events[key] for key in (_WITHDRAW, _DEPOSIT))
    output, incoming = [
        t for t in transfers if t.token == WETH and refs[0].log_index <= t.index < second.log_index
    ]
    router = output.recipient
    assert (output.sender, incoming.sender, incoming.recipient) == (pool0, router, pool1)
    assert output.index < first.log_index < wlog.log_index < dlog.log_index < incoming.index
    value = output.amount
    assert all(
        abi(("address",), data_bytes(event_log.topics[1])) == (router,)
        for event_log in (wlog, dlog)
    )
    assert all(
        abi(("uint256",), data_bytes(event_log.data)) == (value,) for event_log in (wlog, dlog)
    )
    assert incoming.amount == value > 0
    unwrap_path = positions[wlog.log_index]
    deposit_path = positions[dlog.log_index]
    sell, buy = unwrap_path[:-1], deposit_path[:-1]
    assert sell[:-1] == buy[:-1] and sell < buy
    intermediate = frames[sell].sender
    bridges = (
        ((*unwrap_path, 0), WETH, router),
        ((*sell, unwrap_path[-1] + 1), router, intermediate),
        (buy, intermediate, router),
        (deposit_path, router, WETH),
    )
    for path, payer, payee in bridges:
        frame = frames[path]
        assert first_failure(frames, path) is None
        assert (frame.kind, frame.sender, frame.recipient, frame.value) == (
            "CALL",
            payer,
            payee,
            value,
        )
    return {
        "swap_events": [first.log_index, second.log_index],
        "pool_addresses": [pool0, pool1],
        "weth_amount": value,
        "weth_bridge_logs": [output.index, wlog.log_index, dlog.log_index, incoming.index],
        "native_call_paths": [path for path, _, _ in bridges],
    }


def funding_components(funding):
    proofs = {proof.leg.event.log_index: proof for proof in funding.resolved_swaps}
    following = {link.source: link.target for link in funding.links}
    entered = set(following.values())
    assert len(following) == len(entered) == len(funding.links)
    components = []
    for start in sorted(proofs.keys() - entered):
        path = [start]
        while path[-1] in following:
            path.append(following[path[-1]])
        components.append(path)
    assert sum(map(len, components)) == len(proofs)
    cycles = [
        route
        for route in components
        if proofs[route[0]].leg.token_in == proofs[route[-1]].leg.token_out
    ]
    return {
        "component_sizes": dict(Counter(map(len, components))),
        "closed_component_sizes": dict(Counter(map(len, cycles))),
        "closed_four_leg_sequences": [
            [asdict(proofs[log_id]) for log_id in cycle] for cycle in cycles if len(cycle) == 4
        ],
    }


def case(root, target, archive, selectors, sources):
    n, block_hash, index, tx_hash = (
        target[k] for k in ("block_number", "block_hash", "transaction_index", "transaction_hash")
    )
    header_raw, header_request, header_meta = source_bytes(root, f"{n}-header")
    receipts_raw, receipts_request, receipt_meta = source_bytes(root, f"{n}-receipts")
    trace_raw, trace_request, trace_meta = source_bytes(root, f"{tx_hash[2:14]}-trace")
    assert (header_meta["source"], receipt_meta["source"], trace_meta["source"]) == (
        sources["header"],
        sources["receipts"],
        sources["trace"],
    )
    assert json.loads(header_request)["params"] == [hex(n), True]
    assert json.loads(receipts_request)["params"] == [block_hash]
    assert json.loads(trace_request)["params"][0] == tx_hash
    principal = analyze(
        block_raw=header_raw,
        receipts_raw=receipts_raw,
        call_trace_request=trace_request,
        call_trace_raw=trace_raw,
    )
    assert principal["transaction_index"] == index and principal["transaction_hash"] == tx_hash
    header = json.loads(header_raw)["result"]
    block, _ = prague_header(header, n, block_hash)
    receipt = decode_receipt(json.loads(receipts_raw)["result"][index], block.ref, tx_hash, index)
    _archive_match(archive, receipt)
    assert receipt.status == 1
    counted = [
        event_log
        for event_log in receipt.logs
        if event_log.topics and event_log.topics[0] in selectors
    ]
    pool_keys = {
        (event_log.address, event_log.topics[1] if event_log.topics[0] in _V4_BALANCER else None)
        for event_log in counted
    }
    assert len(counted) == target["events"] and len(pool_keys) == target["pool_keys"]
    trace = json.loads(trace_raw)["result"]
    frames = nested_calls(trace)
    positions = _receipt_positions(trace, frames, receipt)
    transfers = _transfers(receipt, positions)
    tx = header["transactions"][index]
    envelope = envelope_cost(tx, receipt, quantity(header["baseFeePerGas"]))
    assert frames[()].value == quantity(tx["value"]) and frames[()].data == data_bytes(tx["input"])
    assert frames[()].recipient == receipt.recipient and frames[()].sender == receipt.sender
    swaps = profile_swap_receipt(receipt)
    funding = replay_trace_funding(receipt, trace)
    scope = (0, 1) if target["events"] == 153 else (0, 1, 0, 1, 0, 1, 0, 1)
    branches = defaultdict(list)
    for log in counted:
        path = positions[log.log_index]
        assert path[: len(scope)] == scope
        branches[path[len(scope)]].append(log.log_index)
    payout = _settlement(receipt, trace, header["miner"])
    payout.pop("native_calls_touching_roles")
    native_fee = [
        {"path": p, "from": f.sender, "value": f.value}
        for p, f in sorted(frames.items())
        if f.recipient == header["miner"]
        and f.kind == "CALL"
        and f.value
        and not first_failure(frames, p)
    ]
    assert sum(p["value"] for p in native_fee) == payout["direct_eth_to_header_fee_receiver_wei"]
    report = {
        "target": target,
        "timestamp_utc": datetime.fromtimestamp(block.ref.timestamp, UTC).isoformat(),
        "receipts_root": principal["receipts_root"],
        "block_receipts": principal["receipts_verified"],
        "matched_archived_logs": len(receipt.logs),
        "selector_histogram": dict(Counter(event_log.topics[0] for event_log in counted)),
        "call_trace_frames": len(frames),
        "caught_reverts": [p for p, f in sorted(frames.items()) if f.error],
        "morpho_principals": morpho_principals(receipt, frames, positions, transfers),
        "swap_branches": sorted(branches.items()),
        "generic_leg_shapes": dict(Counter(s.shape for s in swaps.events)),
        "generic_depth": funding.profile.witnessed_depth,
        "generic_exact_links": len(funding.links),
        "generic_supported_events": len(swaps.events),
        "generic_resolved_events": len(funding.resolved_swaps),
        "generic_pool_keys": swaps.distinct_pools,
        "v4_weth_unlocks": principal["observed_unlocks"],
        "v4_weth_groups": principal["verified_principal_groups"],
        "envelope_fees": envelope,
        "direct_native_fee_calls": native_fee,
        "observed_payout": payout,
        "sources": [header_meta, receipt_meta, trace_meta],
    }
    if target["events"] == 153:
        by_index = {event.log_index: event for event in swaps.events}
        bridged = [
            weth_bridge(by_index[ids[0]], by_index[ids[1]], receipt, frames, positions, transfers)
            for ids in branches.values()
            if len(ids) >= 2
        ]
        report["separate_two_swap_weth_bridges"] = bridged
    else:
        assert all(
            positions[link.source][: len(scope) + 1] == positions[link.target][: len(scope) + 1]
            for link in funding.links
        )
        report["strict_components"] = funding_components(funding)
    return report


def main():
    if not __debug__:
        raise SystemExit("run this evidence verifier without Python optimization")
    root = Path(sys.argv[1])
    scope = json.loads((root / "rpc-scope.json").read_bytes())
    selection = (root / "selection-plan.json").read_bytes()
    pool_ranks = (root / "tail.json").read_bytes()
    assert digest(selection) == scope["selection_sha256"]
    assert digest(pool_ranks) == scope["ranked_sha256"]
    assert (
        digest((root / "select.sql").read_bytes()) == json.loads(selection)["selection_sql_sha256"]
    )
    selectors = set()
    for fragment in (root / "select.sql").read_text().split("'"):
        if fragment.startswith("0x") and len(fragment) == 66:
            selectors.add(fragment)
    assert len(selectors) == 8
    archive = (root / "selected-archive-logs.json").read_bytes()
    ranks = json.loads(pool_ranks)
    print(
        json.dumps(
            {
                "schema": "eth-hundred-event-holdout-v1",
                "selection": json.loads(selection),
                "scope_sha256": digest((root / "rpc-scope.json").read_bytes()),
                "archive_logs_sha256": digest(archive),
                "ranked_tail_sha256": digest(pool_ranks),
                "eligible_tail": ranks,
                "cases": [
                    case(root, target, archive, selectors, scope["sources"])
                    for target in scope["targets"].values()
                ],
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
