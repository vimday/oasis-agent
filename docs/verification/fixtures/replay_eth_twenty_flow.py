"""Offline, single-transaction Ethereum funding audit."""

import argparse
import json
from collections import Counter
from dataclasses import replace
from hashlib import sha256
from itertools import pairwise
from pathlib import Path

import rlp
from eth_hash.auto import keccak

from oasis_agent.arbitrage.cycle_protocols import abi, data_bytes, topic
from oasis_agent.evidence.nitro_block import indexed_trie_root
from oasis_agent.evidence.public_receipts import decode_receipt, receipt_header
from oasis_agent.evidence.research_traces import first_failure, nested_calls
from oasis_agent.evidence.state_trie import prague_header
from oasis_agent.evidence.swap_flow import ObservedSwap, profile_swap_receipt
from oasis_agent.evidence.swap_flow_trace import (
    _indexed_address,
    _receipt_positions,
    _transfers,
    _v2,
    _v3,
    _v4,
    _v4_links,
)

HEIGHT = 26489188
INDEX = 266
TX = "0xe8fe407861f22fcfc39e2914de3ead6e0a8c28e87c7ce26236ce3f7b5e369b6e"
HASH = "0xfa491c993de11545d6b611df12658159534db904b4003620ec692a2c2e682118"
ETH = "0x" + "0" * 40
V4 = "0x000000000004444c5dc75cb358380d2e3de08a90"

# Event, action, input and output transfer logs. None for v4 internal credits.
ROWS = (
    (876, "mooniswap", 874, 875),
    (882, "v2", 879, 880),
    (885, "v2", 880, 883),
    (888, "v2", 883, 886),
    (894, "delta_pair", 891, 890),
    (898, "v2", 895, 896),
    (901, "v2", 896, 899),
    (904, "v4", 903, 905),
    (909, "v2", 906, 907),
    (912, "v3", 911, 910),
    (915, "v2", 910, 913),
    (918, "v2", 913, 916),
    (921, "v2", 916, 919),
    (924, "v2", 919, 922),
    (927, "v4", 926, None),
    (928, "v4", None, None),
    (929, "v4", None, 930),
    (934, "oneinch_order", 933, 932),
    (938, "v2", 935, 936),
    (941, "v2", 936, 939),
    (944, "v2", 939, 942),
    (947, "v2", 942, 945),
    (950, "v2", 945, 948),
    (953, "v2", 948, 951),
    (961, "v2_proxy", 955, 957),
    (966, "v2", 963, 964),
    (967, "balancer", 968, 969),
    (972, "pancake_v3", 971, 970),
    (975, "v3", 974, 973),
    (978, "v2", 973, 976),
    (981, "v3", 980, 979),
    (984, "v3", 983, 982),
    (987, "v3", 986, 985),
    (990, "v3", 989, 988),
    (993, "v2", 988, 991),
    (996, "v2", 991, 994),
    (999, "v2", 994, 997),
    (1002, "v2", 997, 1000),
    (1005, "v2", 1000, 1003),
    (1008, "v2", 1003, 1006),
    (1011, "v2", 1006, 1009),
    (1014, "bancor", 1012, 1013),
    (1020, "bancor", 1013, 1019),
    (1027, "v2", 1019, 1025),
    (1030, "v3", 1029, 1028),
    (1034, "curve_uint", 1031, 1033),
    (1037, "curve", 1035, 1036),
    (1040, "curve", 1038, 1039),
    (1044, "curve", 1041, 1043),
    (1047, "curve_lp_redeem", 1045, 1046),
    (1050, "v3", 1049, 1048),
    (1053, "v3", 1052, 1051),
    (1056, "v2", 1051, 1054),
    (1059, "v4", 1058, 1060),
    (1062, "uni_v1_cashout", 1061, None),
)


def load(root, stem, method, params):
    meta_path = root / (stem + ".meta.json")
    meta = json.loads(meta_path.read_bytes()) if meta_path.is_file() else None
    parsed = {}
    if meta is not None:
        assert meta["http"] == "200" and meta["failure"] is None
    for side in ("request", "response"):
        payload = (root / (stem + f".{side}.json")).read_bytes()
        if meta is not None:
            assert sha256(payload).hexdigest() == meta[side + "_sha256"]
        parsed[side] = json.loads(payload)
    assert parsed["request"] == {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    assert parsed["response"]["id"] == 1
    return parsed["response"]["result"]


def receipt_leaf(r):
    consensus = rlp.encode(
        [
            int(r["status"], 16),
            int(r["cumulativeGasUsed"], 16),
            data_bytes(r["logsBloom"]),
            [
                [
                    data_bytes(log["address"]),
                    [data_bytes(t) for t in log["topics"]],
                    data_bytes(log["data"]),
                ]
                for log in r["logs"]
            ],
        ]
    )
    typ = int(r["type"], 16)
    return bytes([typ]) + consensus if typ else consensus


def arguments(frame, signature, types):
    assert frame.data[:4] == keccak(signature.encode())[:4]
    return abi(types, frame.data[4:])


def inside(path, ancestor):
    return path[: len(ancestor)] == ancestor


def verify(root):
    if not __debug__:
        raise RuntimeError("forensic replay requires Python assertions enabled")
    header = load(root, f"{HEIGHT}-header", "eth_getBlockByNumber", [hex(HEIGHT), False])
    prague_header(header, HEIGHT, HASH)
    receipts = load(root, f"{HEIGHT}-receipts", "eth_getBlockReceipts", [HASH])
    assert len(receipts) == len(header["transactions"]) == 327
    assert (
        "0x" + indexed_trie_root([receipt_leaf(r) for r in receipts]).hex()
        == header["receiptsRoot"]
    )
    assert all(
        (r["transactionHash"], r["blockHash"], int(r["transactionIndex"], 16))
        == (header["transactions"][n], HASH, n)
        for n, r in enumerate(receipts)
    )
    assert header["transactions"][INDEX] == TX
    trace = load(
        root,
        "e8fe407861f2-trace",
        "debug_traceTransaction",
        [TX, {"timeout": "10s", "tracer": "callTracer", "tracerConfig": {"withLog": True}}],
    )
    bind = receipt_header(header, 1, HEIGHT, (TX,))
    receipt = decode_receipt(receipts[INDEX], bind.ref, TX, INDEX)
    assert receipt.status == 1 and len(receipt.logs) == 190
    assert receipt.gas_used == (
        int(receipts[INDEX]["cumulativeGasUsed"], 16)
        - int(receipts[INDEX - 1]["cumulativeGasUsed"], 16)
    )
    frames = nested_calls(trace)
    positions = _receipt_positions(trace, frames, receipt)
    logs = {entry.log_index: entry for entry in receipt.logs}
    transfers = _transfers(receipt, positions)
    movements = {t.index: t for t in transfers}
    known = {event.log_index: event for event in profile_swap_receipt(receipt).events}
    executor = receipt.recipient
    legs = []
    v4_proofs = []
    bancor = []
    for index, shape, incoming, outgoing in ROWS:
        log = logs[index]
        path = positions[index]
        f = frames[path]
        assert first_failure(frames, path) is None
        effective = frames[path[:-1]] if f.kind == "DELEGATECALL" else f
        assert effective.recipient == log.address
        if f.kind == "DELEGATECALL":
            assert f.sender == log.address and f.data == effective.data
        src = movements.get(incoming)
        dst = movements.get(outgoing)
        event = known.get(index, ObservedSwap(index, (log.address, None), shape))
        if shape == "v2":
            # Observed calldata has exactly one all-zero trailing word after canonical empty bytes.
            assert len(f.data) == 196 and f.data[-32:] == bytes(32)
            proof = _v2(event, log, path, replace(f, data=f.data[:-32]), transfers)
        elif shape in ("v3", "pancake_v3"):
            proof = _v3(event, log, path, f, transfers)
        elif shape == "v4":
            proof = _v4(receipt, event, log, path, f, transfers)
            v4_proofs.append(proof)
        else:
            proof = None
        if proof is not None:
            assert (incoming, outgoing) == (proof.input_transfer_log, proof.output_transfer_log)
            token_in, amount_in, token_out, amount_out = (
                proof.leg.token_in,
                proof.leg.amount_in,
                proof.leg.token_out,
                proof.leg.amount_out,
            )
        else:
            token_in, amount_in = src.token, src.amount
            token_out, amount_out = (
                (dst.token, dst.amount) if dst else (ETH, frames[(165, 0, 1)].value)
            )
            if shape not in ("oneinch_order", "curve_lp_redeem"):
                assert src.recipient == log.address
                assert dst is None or dst.sender == log.address
            assert (dst is None) or inside(dst.path, path)
            if shape == "mooniswap":
                assert arguments(
                    f,
                    "swap(address,address,uint256,uint256,address)",
                    ("address", "address", "uint256", "uint256", "address"),
                ) == (token_in, token_out, amount_in, 0, ETH)
                assert abi(("uint256",), f.output) == (amount_out,)
                assert tuple(map(_indexed_address, log.topics[1:])) == (
                    executor,
                    executor,
                    token_in,
                )
                assert abi(
                    ("address", "uint256", "uint256", "uint256", "uint256", "uint256"),
                    data_bytes(log.data),
                )[:3] == (token_out, amount_in, amount_out)
            elif shape == "delta_pair":
                recipient, direction, qty, callback = arguments(
                    f, "swap(address,bool,int256,bytes)", ("address", "bool", "int256", "bytes")
                )
                assert (recipient, direction, qty) == (executor, True, amount_in) and callback
                assert log.topics[0] == topic("Swap(address,address,int256,int256)")
                assert tuple(map(_indexed_address, log.topics[1:])) == (f.sender, executor)
                assert (
                    abi(("int256", "int256"), data_bytes(log.data))
                    == abi(("int256", "int256"), f.output)
                    == (amount_in, -amount_out)
                )
            elif shape == "oneinch_order":
                assert log.address == "0x111111125421ca6dc452d289314280a0f8842a65"
                assert log.topics == (topic("OrderFilled(bytes32,uint256)"),)
                assert (src.sender, src.recipient, dst.sender, dst.recipient) == (
                    executor,
                    "0x326bbd00c0bc17a284da81becedb61ded70e77ac",
                    "0x326bbd00c0bc17a284da81becedb61ded70e77ac",
                    executor,
                )
                assert src.path[:-1] == dst.path[:-1] == path
                order_type = "(" + ",".join(["uint256"] * 8) + ")"
                order, _, _, supplied, _ = arguments(
                    f,
                    f"fillOrder({order_type},bytes32,bytes32,uint256,uint256)",
                    (order_type, "bytes32", "bytes32", "uint256", "uint256"),
                )
                assert supplied == amount_in
                assert order[1:5] == tuple(
                    int(address, 16) for address in (src.recipient, ETH, token_out, token_in)
                )
                o, i, oid = abi(("uint256", "uint256", "bytes32"), f.output)
                assert (i, o, oid) == (amount_in, amount_out, data_bytes(log.data)[:32])
            elif shape == "v2_proxy":
                o0, o1, recipient, callback = arguments(
                    f,
                    "swap(uint256,uint256,address,bytes)",
                    ("uint256", "uint256", "address", "bytes"),
                )
                assert log.topics[0] == topic(
                    "Swap(address,address,uint256,uint256,uint256,uint256)"
                )
                assert abi(("uint256",) * 4, data_bytes(log.data)) == (0, amount_in, amount_out, 0)
                assert (o0, o1, recipient, callback) == (amount_out, 0, executor, b"")
                assert tuple(map(_indexed_address, log.topics[1:])) == (effective.sender, executor)
            elif shape == "balancer":
                assert log.topics[0] == topic("Swap(bytes32,address,address,uint256,uint256)")
                assert tuple(map(_indexed_address, log.topics[2:])) == (token_in, token_out)
                assert abi(("uint256", "uint256"), data_bytes(log.data)) == (amount_in, amount_out)
                # Decode the 14-word ABI and remove the extra zero word in this case.
                assert f.data[-32:] == bytes(32)
                swap, funds, limit, _ = arguments(
                    replace(f, data=f.data[:-32]),
                    "swap((bytes32,uint8,address,address,uint256,bytes),(address,bool,address,bool),uint256,uint256)",
                    (
                        "(bytes32,uint8,address,address,uint256,bytes)",
                        "(address,bool,address,bool)",
                        "uint256",
                        "uint256",
                    ),
                )
                assert swap == (data_bytes(log.topics[1]), 0, token_in, token_out, amount_in, b"")
                assert funds == (executor, False, executor, False) and limit <= amount_out
                assert abi(("uint256",), f.output) == (amount_out,)
                assert inside(src.path, path)
            elif shape == "bancor":
                tokens = ("address", "address", "uint256", "address", "address")
                assert arguments(f, "convert(address,address,uint256,address,address)", tokens) == (
                    token_in,
                    token_out,
                    amount_in,
                    executor,
                    dst.recipient,
                )
                assert tuple(map(_indexed_address, log.topics[1:])) == (
                    token_in,
                    token_out,
                    executor,
                )
                assert abi(("uint256", "uint256", "int256"), data_bytes(log.data))[:2] == (
                    amount_in,
                    amount_out,
                )
                assert abi(("uint256",), f.output) == (amount_out,)
                bancor.append((path, token_in, token_out, amount_in, amount_out, dst.recipient))
            elif shape in ("curve", "curve_uint"):
                integer = "uint256" if shape == "curve_uint" else "int128"
                types = (integer, integer, "uint256", "uint256")
                if shape == "curve":
                    types += ("address",)
                args = arguments(f, "exchange(" + ",".join(types) + ")", types)
                i, j, supplied, minimum = args[:4]
                assert supplied == amount_in and minimum <= amount_out
                assert shape == "curve_uint" or args[4] == executor
                assert log.topics == (
                    topic(f"TokenExchange(address,{integer},uint256,{integer},uint256)"),
                    "0x" + executor[2:].rjust(64, "0"),
                )
                assert abi((integer, "uint256", integer, "uint256"), data_bytes(log.data)) == (
                    i,
                    amount_in,
                    j,
                    amount_out,
                )
                assert abi(("uint256",), f.output) == (amount_out,)
            elif shape == "curve_lp_redeem":
                assert src.sender == executor and src.recipient == ETH
                assert log.topics[0] == topic("RemoveLiquidityOne(address,uint256,uint256)")
                assert _indexed_address(log.topics[1]) == executor
                assert arguments(
                    f,
                    "remove_liquidity_one_coin(uint256,int128,uint256)",
                    ("uint256", "int128", "uint256"),
                ) == (amount_in, 0, 0)
                assert abi(("uint256", "uint256"), data_bytes(log.data)) == (amount_in, amount_out)
                assert inside(src.path, path)
            elif shape == "uni_v1_cashout":
                assert log.topics[0] == topic("EthPurchase(address,uint256,uint256)")
                assert _indexed_address(log.topics[1]) == executor
                assert tuple(int(x, 16) for x in log.topics[2:]) == (amount_in, amount_out)
                assert (
                    arguments(f, "tokenToEthSwapInput(uint256,uint256,uint256)", ("uint256",) * 3)[
                        0
                    ]
                    == amount_in
                )
                assert abi(("uint256",), f.output) == (amount_out,)
                payment = frames[(165, 0, 1)]
                assert (payment.sender, payment.recipient, payment.value) == (
                    log.address,
                    executor,
                    amount_out,
                )
            else:
                raise ValueError(shape)
        assert token_in != token_out and min(amount_in, amount_out) > 0
        key = (log.address, log.topics[1] if shape in ("v4", "balancer") else None)
        legs.append(
            {
                "log": index,
                "shape": shape,
                "call_path": path,
                "pool": key,
                "token_in": token_in,
                "token_out": token_out,
                "amount_in": amount_in,
                "amount_out": amount_out,
                "input_transfer": incoming,
                "output_transfer": outgoing,
            }
        )

    assert len(bancor) == 2
    b0, b1 = bancor
    assert b0[0][:-1] == b1[0][:-1] and b1[0][-1] == b0[0][-1] + 1
    parent = frames[b0[0][:-1]]
    route, amount, minimum, recipient, _, _ = arguments(
        parent,
        "convertByPath(address[],uint256,uint256,address,address,uint256)",
        ("address[]", "uint256", "uint256", "address", "address", "uint256"),
    )
    assert len(route) == 5 and (route[0], route[2], route[4], amount, recipient) == (
        b0[1],
        b0[2],
        b1[2],
        b0[3],
        b1[5],
    )
    assert b0[2] == b1[1] and b0[4] == b1[3] and b0[5] == logs[1020].address
    assert abi(("uint256",), parent.output) == (b1[4],) and minimum <= b1[4]
    internal = {
        (link.source, link.target) for link in _v4_links(tuple(v4_proofs), transfers, frames)
    }
    links = []
    for a, b in pairwise(legs):
        assert (a["token_out"], a["amount_out"]) == (b["token_in"], b["amount_in"])
        out, incoming = a["output_transfer"], b["input_transfer"]
        ids = []
        if (a["log"], b["log"]) in internal:
            assert out is incoming is None
            mode = "v4_same_unlock_exact_deltas"
        elif out == incoming:
            assert out is not None and incoming is not None
            mode, ids = "direct", [out]
        else:
            assert out is not None and incoming is not None and out < incoming
            x, y = movements[out], movements[incoming]
            assert (x.token, x.amount) == (y.token, y.amount)
            ids = [out, incoming]
            if y.sender != executor:
                assert executor == x.recipient
                mid = [
                    t
                    for t in transfers
                    if out < t.index < incoming
                    and (t.token, t.sender, t.recipient, t.amount)
                    == (x.token, executor, y.sender, x.amount)
                ]
                assert len(mid) == 1 and y.recipient == V4
                ids.insert(1, mid[0].index)
            else:
                assert x.recipient == y.sender
            # No intervening same-asset custody movement at any linked receiving account.
            for left, right in pairwise(ids):
                left_t, right_t = movements[left], movements[right]
                assert left_t.recipient == right_t.sender
                assert not any(
                    left < t.index < right
                    and t.token == left_t.token
                    and left_t.recipient in (t.sender, t.recipient)
                    for t in transfers
                )
            mode = (
                "executor_and_v4_adapter" if len(ids) == 3 else "next_same_asset_executor_transfer"
            )
        links.append(
            {
                "start": a["log"],
                "end": b["log"],
                "token": a["token_out"],
                "amount": a["amount_out"],
                "mode": mode,
                "transfer_logs": ids,
            }
        )

    # Validate the signed-balance barriers for each successful v4 unlock.
    for proof in (v4_proofs[0], v4_proofs[1], v4_proofs[-1]):
        path = proof.call_path[:-1]
        callback, unlock = frames[path], frames[path[:-1]]
        assert (
            callback.sender == V4 and unlock.recipient == V4 and callback.recipient == unlock.sender
        )
        assert arguments(callback, "unlockCallback(bytes)", ("bytes",)) == arguments(
            unlock, "unlock(bytes)", ("bytes",)
        )
        swaps = [p for p in v4_proofs if p.call_path[:-1] == path]
        first, last = swaps[0], swaps[-1]
        assert all(p.hookless_v4_caller == callback.recipient for p in swaps)
        assert first.input_transfer_log and last.output_transfer_log
        deb = movements[first.input_transfer_log]
        cred = movements[last.output_transfer_log]
        assert (deb.sender, deb.recipient, deb.token, deb.amount) == (
            callback.recipient,
            V4,
            first.leg.token_in,
            first.leg.amount_in,
        )
        assert (cred.sender, cred.recipient, cred.token, cred.amount) == (
            V4,
            executor,
            last.leg.token_out,
            last.leg.amount_out,
        )
        calls = [
            f for k, f in frames.items() if k[:-1] == path and f.recipient == V4 and f.error is None
        ]
        assert any(
            c.data == keccak(b"settle()")[:4] and abi(("uint256",), c.output) == (deb.amount,)
            for c in calls
        )
        assert any(
            c.data[:4] == keccak(b"take(address,address,uint256)")[:4]
            and abi(("address", "address", "uint256"), c.data[4:])
            == (cred.token, executor, cred.amount)
            for c in calls
        )

    closed = legs[:-1]
    addresses = [closed[0]["token_in"], *[leg["token_out"] for leg in closed]]
    assert addresses[0] == addresses[-1] and addresses[0] == legs[-1]["token_in"]
    asset_simple_loops = []
    for i, a in enumerate(addresses):
        for j in range(i + 2, len(addresses)):
            path = addresses[i:j]
            if addresses[j] == a and len(path) == len(set(path)):
                asset_simple_loops.append((j - i, closed[i]["log"], closed[j - 1]["log"], a))
    failures = [(k, f.error) for k, f in frames.items() if f.error]
    assert all(first_failure(frames, positions[leg["log"]]) is None for leg in legs)
    assert len({idx for link in links for idx in link["transfer_logs"]}) == sum(
        len(link["transfer_logs"]) for link in links
    )
    fees = {
        "gas_used": receipt.gas_used,
        "effective_gas_price": receipt.effective_gas_price,
        "gas": receipt.gas_used * receipt.effective_gas_price,
        "fee_recipient_tip": frames[(167,)].value,
        "cashout_eth": frames[(165, 0, 1)].value,
        "eoa_payout": frames[(168,)].value,
    }
    assert (
        frames[(167,)].recipient == header["miner"] and frames[(168,)].recipient == receipt.sender
    )
    assert fees["eoa_payout"] == fees["cashout_eth"] - fees["fee_recipient_tip"]
    fees["observation_net_eth"] = fees["eoa_payout"] - fees["gas"]
    observed_accounts = {receipt.sender, executor}
    erc20_net = Counter()
    for movement in transfers:
        erc20_net[movement.token] += movement.amount * (
            int(movement.recipient in observed_accounts) - int(movement.sender in observed_accounts)
        )
    erc20_net = {token: amount for token, amount in erc20_net.items() if amount}
    assert erc20_net == {addresses[0]: -closed[0]["amount_in"]}
    fees["observation_net_usdc"] = erc20_net[addresses[0]]
    fees["base_fee_burn"] = receipt.gas_used * int(header["baseFeePerGas"], 16)
    fees["protocol_priority_fee"] = fees["gas"] - fees["base_fee_burn"]
    summary = {
        "schema": "oasis.ethereum-historical-hybrid-funding.v1",
        "chain_id": 1,
        "block": HEIGHT,
        "block_hash": HASH,
        "tx": TX,
        "tx_index": INDEX,
        "block_timestamp": int(header["timestamp"], 16),
        "receipt_root": header["receiptsRoot"],
        "rooted_receipts": len(receipts),
        "trace_frames": len(frames),
        "rooted_transaction_logs": len(receipt.logs),
        "executor": executor,
        "fee_payer": receipt.sender,
        "block_fee_recipient": header["miner"],
        "candidate_20": [leg["log"] for leg in legs if 966 <= leg["log"] <= 1030],
        "closed_hybrid_legs": len(closed),
        "cashout_hybrid_legs": len(legs),
        "closed_unique_venue_resolutions": len({tuple(leg["pool"]) for leg in closed}),
        "closed_asset_count": len(set(addresses)),
        "count_by_kind": dict(Counter(leg["shape"] for leg in closed)),
        "closed_start_amount": closed[0]["amount_in"],
        "closed_end_amount": closed[-1]["amount_out"],
        "closed_market_swap_legs": len(closed) - 2,
        "simple_asset_loops": sorted(asset_simple_loops, reverse=True),
        "fees": fees,
        "failures": failures,
        "legs": legs,
        "links": links,
    }
    cited_transfers = {
        log
        for leg in legs
        for log in (leg["input_transfer"], leg["output_transfer"])
        if log is not None
    }
    cited_transfers.update(log for link in links for log in link["transfer_logs"])
    # The input transfer at the proxy covered the quoted leg. Preserve its separate fee movement.
    cited_transfers.add(958)
    summary["cited_transfers"] = [
        {
            "log": movement.index,
            "call_path": movement.path,
            "token": movement.token,
            "from": movement.sender,
            "to": movement.recipient,
            "amount": str(movement.amount),
        }
        for movement in transfers
        if movement.index in cited_transfers
    ]
    for leg in legs:
        leg["amount_in"] = str(leg["amount_in"])
        leg["amount_out"] = str(leg["amount_out"])
    for link in links:
        link["amount"] = str(link["amount"])
    return summary


def main():
    if not __debug__:
        raise RuntimeError("this evidence verifier requires Python assertions")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("retained_directory", type=Path)
    args = parser.parse_args()
    report = verify(args.retained_directory)
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
