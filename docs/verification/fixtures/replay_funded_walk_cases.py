"""Rebuild three Ethereum transaction witnesses from their retained public RPC responses."""

import argparse
import json
from collections import Counter
from pathlib import Path

from replay_eth_twenty_flow import load, receipt_leaf
from replay_weth_walk_lifetime import verify_lifetime

from oasis_agent.arbitrage.cycle_protocols import abi, data_bytes, topic
from oasis_agent.evidence.nitro_block import indexed_trie_root
from oasis_agent.evidence.public_receipts import decode_receipt, receipt_header
from oasis_agent.evidence.research_traces import first_failure, nested_calls
from oasis_agent.evidence.state_trie import prague_header
from oasis_agent.evidence.swap_flow import TRANSFER
from oasis_agent.evidence.swap_flow_trace import replay_trace_funding

CASES = (
    (26403845, 184, "0x765cf607fdea388a9da9c65965c18521a4182c1ea3e27979c60ab939f9d8b701", ""),
    (26406955, 105, "0x252b5fd176f05fdc4e9dad9cb0621041f3aa5227268c913312c8fafff0121723", ""),
    (26406955, 99, "0x7e1b1a0393b59a114f4cb6bb223059c7b6fc377c94eb0a9525ae3af847b497ab", "blind"),
)
WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"


def examine(root, height, index, tx, trace_folder):
    header = load(root, f"{height}-header", "eth_getBlockByNumber", [hex(height), False])
    block_hash = header["hash"]
    prague_header(header, height, block_hash)
    receipts = load(root, f"{height}-receipts", "eth_getBlockReceipts", [block_hash])
    assert (
        "0x" + indexed_trie_root([receipt_leaf(r) for r in receipts]).hex()
        == header["receiptsRoot"]
    )
    assert [
        (r["blockHash"], r["transactionHash"], int(r["transactionIndex"], 16)) for r in receipts
    ] == [(block_hash, tx_hash, i) for i, tx_hash in enumerate(header["transactions"])]
    assert receipts[index]["transactionHash"] == tx
    receipt = decode_receipt(
        receipts[index], receipt_header(header, 1, height, (tx,)).ref, tx, index
    )
    gas_delta = int(receipts[index]["cumulativeGasUsed"], 16)
    if index:
        gas_delta -= int(receipts[index - 1]["cumulativeGasUsed"], 16)
    assert receipt.status == 1 and receipt.gas_used == gas_delta
    trace = load(
        root / trace_folder,
        tx[2:14] + "-trace",
        "debug_traceTransaction",
        [tx, {"tracer": "callTracer", "tracerConfig": {"withLog": True}, "timeout": "10s"}],
    )
    proof = replay_trace_funding(receipt, trace)
    frames = nested_calls(trace)
    roles = {receipt.sender, receipt.recipient}
    assets = Counter()
    native = builder = 0
    for path, frame in frames.items():
        if frame.kind == "CALL" and first_failure(frames, path) is None:
            native += frame.value * (int(frame.recipient in roles) - int(frame.sender in roles))
            if (
                frame.sender in roles
                and frame.recipient == header["miner"]
                and frame.recipient not in roles
            ):
                builder += frame.value
    deposit, withdraw = topic("Deposit(address,uint256)"), topic("Withdrawal(address,uint256)")
    for log in receipt.logs:
        if not log.topics or len(log.data) != 66:
            continue
        if len(log.topics) == 3 and log.topics[0] == TRANSFER:
            sender, to = (abi(("address",), data_bytes(t))[0] for t in log.topics[1:])
            assets[log.address] += int(log.data, 16) * (int(to in roles) - int(sender in roles))
        elif len(log.topics) == 2 and log.address == WETH and log.topics[0] in (deposit, withdraw):
            who = abi(("address",), data_bytes(log.topics[1]))[0]
            if who in roles:
                assets[WETH] += int(log.data, 16) * (1 if log.topics[0] == deposit else -1)
    path = proof.profile.longest_witnessed_path
    swaps = {point.leg.event.log_index: point for point in proof.resolved_swaps}
    connections = {(link.source, link.target): link for link in proof.links}
    legs = []
    for position, idx in enumerate(path):
        point = swaps[idx]
        leg = point.leg
        link = connections[path[position - 1], idx] if position else None
        if link is not None:
            before = swaps[path[position - 1]].leg
            assert (
                before.token_out == link.token == leg.token_in
                and before.amount_out == link.amount == leg.amount_in
            )
        legs.append(
            {
                "log": idx,
                "shape": leg.event.shape,
                "venue": leg.event.pool,
                "frame": point.call_path,
                "input_asset": leg.token_in,
                "input": str(leg.amount_in),
                "input_transfer": point.input_transfer_log,
                "output_asset": leg.token_out,
                "output": str(leg.amount_out),
                "output_transfer": point.output_transfer_log,
                "previous_funding": link.evidence_id if link else None,
            }
        )
    gas = receipt.gas_used * receipt.effective_gas_price
    wires = {}
    for directory, stem in (
        (root, f"{height}-header"),
        (root, f"{height}-receipts"),
        (root / trace_folder, tx[2:14] + "-trace"),
    ):
        raw = json.loads((directory / (stem + ".meta.json")).read_bytes())
        wires[stem] = {
            key: raw[key]
            for key in ("source", "request_sha256", "response_sha256", "completed_utc")
        }
    return {
        "chain_id": 1,
        "height": height,
        "hash": block_hash,
        "receipt_root": header["receiptsRoot"],
        "receipt_count": len(receipts),
        "tx": tx,
        "index": index,
        "executor": receipt.recipient,
        "sender": receipt.sender,
        "logs": len(receipt.logs),
        "frames": len(frames),
        "resolved": len(swaps),
        "unresolved": proof.unresolved_events,
        "gas_used": receipt.gas_used,
        "gas_price_wei": str(receipt.effective_gas_price),
        "gas_paid_wei": str(gas),
        "direct_fee_recipient_wei": str(builder),
        "combined_visible_eth_weth_after_gas_wei": str(native + assets.pop(WETH, 0) - gas),
        "other_erc20_visible_deltas": {key: str(v) for key, v in sorted(assets.items()) if v},
        "path": legs,
        "wires": wires,
    }


def main():
    if not __debug__:
        raise RuntimeError("evidence verification requires Python assertions")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("retained_directory", type=Path)
    root = parser.parse_args().retained_directory
    rows = [examine(root, *case) for case in CASES]
    rows[1]["same_envelope_lifetime"] = verify_lifetime(root, rows[1])
    print(json.dumps(rows, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
