"""Replay the frozen Ethereum 109-event fingerprint without network calls."""

import argparse
import json
from collections import Counter
from dataclasses import asdict
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

import rlp
from eth_account import Account
from eth_hash.auto import keccak

from oasis_agent.arbitrage.cycle_protocols import abi, data_bytes, topic
from oasis_agent.evidence.nitro_block import indexed_trie_root
from oasis_agent.evidence.public_receipts import decode_receipt, quantity
from oasis_agent.evidence.research_traces import first_failure, nested_calls
from oasis_agent.evidence.rpc_validation import strict_json_object
from oasis_agent.evidence.state_trie import prague_header
from oasis_agent.evidence.swap_flow_trace import _receipt_positions, replay_trace_funding
from oasis_agent.rpc.budget import BudgetPolicy, RpcBudget

HEIGHT, INDEX = 26405358, 44
HASH = "0xf278ef51e04af84e397e8cc5bd6f2de0fe0d8487ab0ac175a8fe9094b261fb21"
TX = "0x1a52ee375bcdbe79ec67824cbb2a4d2cea35fd095fe5a93c79626fe02f5874f8"
PREVIOUS = "0xf96d87934b46a7d1d36dfe053d0071c04831f793950cc2cc1192555d0ced9829"
SENDER = "0x1d042c727d3f0b228a7438378e531773d1d8e353"
EXECUTOR = "0x96cb0b6dd66cbd3201681b9f9588b7107084f9ea"
PAYOUT = "0x610aa9ce9da1360852c3419f93a5f7d424379aa0"
MANAGER = "0x000000000004444c5dc75cb358380d2e3de08a90"
WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
USDT = "0xdac17f958d2ee523a2206206994597c13d831ec7"
USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
ORACLE = "0xe844701934d9c6085b292e428f41b911cc0b3872"
METRIC_POOL = "0xaef2ad7abb651945e6d24276715f25bded0ad41e"
METRIC_ADAPTER = "0x99ab7c1c5eab47801f7937075998527a550fba8a"
FEED = "0x8dfdc825dd0453e7c80b1c1d32ae910e35d6efe9000000000000000000010a00"
TRANSFER = topic("Transfer(address,address,uint256)")
DEPOSIT = topic("Deposit(address,uint256)")
WITHDRAWAL = topic("Withdrawal(address,uint256)")
SCOPE_SHA = "bfa688f07272b32595a75a7ee7c866721651beb2dd4b8e124cff703f438be373"
EARLIER_LOG_SHA = "6728d75514fae32bc14e7359f978ad7c60a5c635ecfd6a3300a4597456aa788b"
READS = [
    ["header", "https://ethereum.publicnode.com", "eth_getBlockByHash", [HASH, True]],
    ["receipts", "https://eth.nodely.dev", "eth_getBlockReceipts", [HASH]],
    [
        "trace",
        "https://eth.drpc.org",
        "debug_traceTransaction",
        [TX, {"tracer": "callTracer", "timeout": "10s", "tracerConfig": {"withLog": True}}],
    ],
]


def digest(data: bytes) -> str:
    return sha256(data).hexdigest()


def rpc(root: Path, lane: str, source: str, method: str, params: list) -> tuple[Any, dict]:
    meta = strict_json_object((root / f"{lane}.meta.json").read_bytes())
    request = (root / f"{lane}.request.json").read_bytes()
    reply = (root / f"{lane}.response.json").read_bytes()
    assert (meta["lane"], meta["source"], meta["method"]) == (lane, source, method)
    assert meta["http"] == "200" and meta["curl_returncode"] == 0 and meta["failure"] is None
    assert meta["request_sha256"] == digest(request)
    assert meta["response_sha256"] == digest(reply) and meta["response_bytes"] == len(reply)
    assert strict_json_object(request) == {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": params,
    }
    envelope = strict_json_object(reply)
    assert envelope["jsonrpc"] == "2.0" and envelope["id"] == 1 and "error" not in envelope
    return envelope["result"], dict(meta)


def transaction_leaf(tx: dict) -> bytes:
    # The observed block contains 49 EIP-1559 transactions. Reject other encodings here.
    assert tx["type"] == "0x2" and tx["chainId"] == "0x1" and tx["v"] == tx["yParity"]
    terms = [
        quantity(tx[name])
        for name in ("chainId", "nonce", "maxPriorityFeePerGas", "maxFeePerGas", "gas")
    ]
    access = [
        [data_bytes(entry["address"]), [data_bytes(key) for key in entry["storageKeys"]]]
        for entry in tx["accessList"]
    ]
    parsed = [
        *terms,
        data_bytes(tx["to"]) if tx["to"] is not None else b"",
        quantity(tx["value"]),
        data_bytes(tx["input"]),
        access,
        *(quantity(tx[field]) for field in ("yParity", "r", "s")),
    ]
    payload = b"\x02" + rlp.encode(parsed)
    assert "0x" + keccak(payload).hex() == tx["hash"]
    assert Account.recover_transaction(payload).lower() == tx["from"]
    return payload


def receipt_leaf(receipt: dict) -> bytes:
    payload = rlp.encode(
        [
            quantity(receipt["status"]),
            quantity(receipt["cumulativeGasUsed"]),
            data_bytes(receipt["logsBloom"]),
            [
                [
                    data_bytes(log["address"]),
                    list(map(data_bytes, log["topics"])),
                    data_bytes(log["data"]),
                ]
                for log in receipt["logs"]
            ],
        ]
    )
    assert receipt["type"] == "0x2"
    return b"\x02" + payload


def trie_root(leaves: list[bytes]) -> str:
    return "0x" + indexed_trie_root(leaves).hex()


def block_evidence(header: dict, receipts: list[dict]) -> None:
    txs = header["transactions"]
    assert len(txs) == len(receipts) == 49
    assert trie_root([transaction_leaf(tx) for tx in txs]) == header["transactionsRoot"]
    assert trie_root([receipt_leaf(r) for r in receipts]) == header["receiptsRoot"]
    cumulative = position = 0
    for index, (tx, receipt) in enumerate(zip(txs, receipts, strict=True)):
        assert (tx["blockHash"], quantity(tx["blockNumber"]), quantity(tx["transactionIndex"])) == (
            HASH,
            HEIGHT,
            index,
        )
        assert (receipt["transactionHash"], receipt["blockHash"]) == (tx["hash"], HASH)
        assert (quantity(receipt["blockNumber"]), quantity(receipt["transactionIndex"])) == (
            HEIGHT,
            index,
        )
        assert (receipt["from"], receipt["to"]) == (tx["from"], tx["to"])
        cumulative += quantity(receipt["gasUsed"])
        assert cumulative == quantity(receipt["cumulativeGasUsed"])
        for log in receipt["logs"]:
            assert (log["transactionHash"], log["blockHash"]) == (tx["hash"], HASH)
            assert quantity(log["logIndex"]) == position
            position += 1
    assert cumulative == quantity(header["gasUsed"])


def transfer(log) -> tuple[str, str, str, int]:
    assert len(log.topics) == 3 and log.topics[0] == TRANSFER and len(log.data) == 66
    assert all(t[:26] == "0x" + "0" * 24 for t in log.topics[1:])
    return log.address, "0x" + log.topics[1][-40:], "0x" + log.topics[2][-40:], int(log.data, 16)


def selector(signature: str) -> bytes:
    return keccak(signature.encode())[:4]


def prove(root: Path, earlier_logs: Path) -> dict:
    scope_raw = (root / "scope.json").read_bytes()
    assert digest(scope_raw) == SCOPE_SHA and not (root / "STOP.json").exists()
    scope = strict_json_object(scope_raw)
    assert scope["reads"] == READS and scope["max_requests"] == 3 and scope["cost_usd"] == 0
    with RpcBudget(
        root / "original.sqlite3",
        tuple(BudgetPolicy(row[0], 1, 0) for row in READS),
        read_only=True,
    ) as budget:
        counts, reserved, sequence, ledger_digest = budget._read()
    assert counts == dict.fromkeys(("header", "receipts", "trace"), 1) and sequence == 3
    (header, hmeta), (rr, rmeta), (tree, tmeta) = [rpc(root, *row) for row in READS]
    previous = datetime.fromisoformat(scope["frozen_utc"])
    for meta in (hmeta, rmeta, tmeta):
        start = datetime.fromisoformat(meta["started_utc"])
        done = datetime.fromisoformat(meta["completed_utc"])
        assert previous <= start <= done and reserved[meta["lane"]] <= int(
            start.timestamp() * 1_000
        )
        previous = done
    block, _ = prague_header(header, HEIGHT, HASH)
    block_evidence(header, rr)
    tx = header["transactions"][INDEX]
    pre = header["transactions"][INDEX - 1]
    assert (tx["hash"], pre["hash"], tx["from"], tx["to"], pre["to"]) == (
        TX,
        PREVIOUS,
        SENDER,
        EXECUTOR,
        ORACLE,
    )
    receipt = decode_receipt(rr[INDEX], block.ref, TX, INDEX)
    assert receipt.status == 1 and len(receipt.logs) == 473
    earlier = earlier_logs.read_bytes()
    assert digest(earlier) == EARLIER_LOG_SHA
    for row, log in zip(json.loads(earlier), receipt.logs, strict=True):
        assert (row["log_index"], row["address"], tuple(row["topics"]), row["data"]) == (
            log.log_index,
            log.address,
            log.topics,
            log.data,
        )

    frames = nested_calls(tree)
    paths = _receipt_positions(tree, frames, receipt)
    assert frames[()].data == data_bytes(tx["input"]) and frames[()].value == quantity(tx["value"])
    logs = {log.log_index: log for log in receipt.logs}
    committed = [
        (path, frame)
        for path, frame in sorted(frames.items())
        if frame.kind == "CALL" and frame.value and first_failure(frames, path) is None
    ]
    failures = [
        {"path": path, "to": frame.recipient, "error": frame.error}
        for path, frame in sorted(frames.items())
        if frame.error
    ]
    receiver = header["miner"]
    direct_fee = frames[(0,)].value
    assert [
        (path, row.sender, row.value) for path, row in committed if row.recipient == receiver
    ] == [((0,), EXECUTOR, direct_fee)]
    assert not frames[(0,)].data and direct_fee == quantity(tx["value"])
    native_by_role = Counter()
    for _, movement in committed:
        native_by_role[movement.sender] -= movement.value
        native_by_role[movement.recipient] += movement.value
    assert [native_by_role[a] for a in (SENDER, EXECUTOR, PAYOUT, receiver)] == [
        -direct_fee,
        0,
        0,
        direct_fee,
    ]
    gas_used = receipt.gas_used
    base_fee = quantity(header["baseFeePerGas"])
    assert receipt.effective_gas_price == min(
        quantity(tx["maxFeePerGas"]), base_fee + quantity(tx["maxPriorityFeePerGas"])
    )

    batches = []
    balance_request = selector("balanceOf(address)") + bytes(12) + data_bytes(PAYOUT)
    for child, (first, repay, payout) in enumerate(
        ((474, 567, 568), (569, 708, 709), (710, 847, 848), (849, 945, 946)), start=1
    ):
        branch = frames[(child,)]
        assert branch.recipient == PAYOUT and branch.data[:4].hex() == "f740f328"
        callback = frames[(child, 1)]
        assert (callback.sender, callback.recipient, callback.data[:4].hex()) == (
            PAYOUT,
            EXECUTOR,
            "75870edb",
        )
        assert frames[(child, 1, 0)].recipient == MANAGER
        assert frames[(child, 1, 0)].data[:4] == selector("unlock(bytes)")
        before, after = (frames[(child, read)] for read in (0, 2))
        assert all(
            (f.kind, f.sender, f.recipient, f.data, f.error)
            == ("STATICCALL", PAYOUT, WETH, balance_request, None)
            and f.output is not None
            and len(f.output) == 32
            for f in (before, after)
        )
        start_weth, end_weth = (int.from_bytes(f.output or b"") for f in (before, after))
        _, _, _, principal = transfer(logs[first])
        _, _, _, proceeds = transfer(logs[payout])
        assert transfer(logs[first]) == (WETH, MANAGER, EXECUTOR, principal)
        assert transfer(logs[repay]) == (WETH, EXECUTOR, MANAGER, principal)
        assert transfer(logs[payout]) == (WETH, EXECUTOR, PAYOUT, proceeds)
        assert paths[payout] == (child, 1, 3) and end_weth - start_weth == proceeds
        assert [i for i in logs if paths[i][:1] == (child,)] == list(range(first, payout + 1))
        native, token = Counter(), Counter()
        for path, movement in committed:
            if path[:1] == (child,):
                native[movement.sender] -= movement.value
                native[movement.recipient] += movement.value
        for index in range(first, payout + 1):
            log = logs[index]
            if len(log.topics) == 3 and log.topics[0] == TRANSFER:
                asset, src, dst, amount = transfer(log)
                token[asset] += amount * ((dst == EXECUTOR) - (src == EXECUTOR))
            elif (
                log.address == WETH
                and len(log.topics) == 2
                and log.topics[0] in (DEPOSIT, WITHDRAWAL)
                and "0x" + log.topics[1][-40:] == EXECUTOR
            ):
                token[WETH] += int(log.data, 16) * (1 if log.topics[0] == DEPOSIT else -1)
        assert native[EXECUTOR] == native[PAYOUT] == 0
        batches.append(
            {
                "root_child": child,
                "log_span": [first, payout],
                "repay_log": repay,
                "principal_weth_wei": principal,
                "paid_weth_wei": proceeds,
                "payout_balance_before_weth_wei": start_weth,
                "payout_balance_after_weth_wei": end_weth,
                "executor_native_net_wei": native[EXECUTOR],
                "executor_tokens_nonzero": {
                    asset: amount for asset, amount in token.items() if amount
                },
                "root_child_gas_used": quantity(tree["calls"][child]["gasUsed"]),
            }
        )

    assert [b["executor_tokens_nonzero"] for b in batches] == [{}, {}, {USDC: 2}, {USDC: -2}]
    assert [b["payout_balance_before_weth_wei"] for b in batches[1:]] == [
        b["payout_balance_after_weth_wei"] for b in batches[:-1]
    ]
    priority = (receipt.effective_gas_price - base_fee) * gas_used
    gas_fee = receipt.effective_gas_price * gas_used
    proceeds = sum(b["paid_weth_wei"] for b in batches)

    updates = rr[INDEX - 1]["logs"]
    assert rr[INDEX - 1]["status"] == "0x1" and len(updates) == 20
    assert all(
        row["address"] == ORACLE
        and len(row["topics"]) == 2
        and row["topics"][0] == topic("FeedUpdated(bytes32)")
        and row["data"] == "0x"
        for row in updates
    )
    matching = [quantity(row["logIndex"]) for row in updates if row["topics"][1] == FEED]
    read = logs[491]
    assert matching == [461] and (read.address, read.topics, read.data) == (
        ORACLE,
        (topic("PriceRead(address,bytes32)"), "0x" + "0" * 24 + METRIC_POOL[2:], FEED),
        "0x",
    )
    read_path = paths[491]
    pricing = frames[read_path]
    assert pricing.recipient == ORACLE and pricing.data[:4] == selector("price(bytes32,address)")
    assert abi(("bytes32", "address"), pricing.data[4:]) == (data_bytes(FEED), METRIC_POOL)
    quote = abi(("uint256", "uint256", "uint16", "uint256"), pricing.output or b"")
    adapter_path, pool_path = read_path[:-3], read_path[:-2]
    assert (frames[adapter_path].recipient, frames[pool_path].recipient) == (
        METRIC_ADAPTER,
        METRIC_POOL,
    )
    assert paths[493][: len(pool_path)] == pool_path and paths[494][: len(pool_path)] == pool_path
    sold = transfer(logs[494])
    bought = transfer(logs[493])
    assert sold[:3] == (USDT, METRIC_ADAPTER, METRIC_POOL)
    assert bought[:3] == (USDC, METRIC_POOL, EXECUTOR)

    last_actions = (853, 858, 880, 893, 914, 917)
    assert all(paths[index][:1] == (4,) for index in last_actions)
    intermediate = transfer(logs[915])
    amount = intermediate[3]
    assert intermediate[1:3] == (MANAGER, EXECUTOR)
    assert transfer(logs[916]) == (intermediate[0], EXECUTOR, MANAGER, amount)
    v4_args = ("int128", "int128", "uint160", "uint128", "int24", "uint24")
    assert abi(v4_args, data_bytes(logs[914].data))[:2] == (-482799, amount)
    assert abi(v4_args, data_bytes(logs[917].data))[:2] == (171309857100380850, -amount)

    strict = replay_trace_funding(receipt, tree)
    return {
        "schema": "oasis-eth-109-motive-v1",
        "scope_sha256": SCOPE_SHA,
        "original_budget": {"requests": sequence, "digest": ledger_digest},
        "rpc": {m["lane"]: m for m in (hmeta, rmeta, tmeta)},
        "chain_id": 1,
        "block_height": HEIGHT,
        "block_hash": HASH,
        "block_utc": datetime.fromtimestamp(block.ref.timestamp, UTC).isoformat(),
        "transactions_root": header["transactionsRoot"],
        "receipts_root": header["receiptsRoot"],
        "transactions_verified": len(rr),
        "receipts_verified": len(rr),
        "target_tx": TX,
        "target_index": INDEX,
        "target_selector": tx["input"][:10],
        "sender": SENDER,
        "executor": EXECUTOR,
        "weth_recipient": PAYOUT,
        "fee_recipient": receiver,
        "raw_log_source_sha256": EARLIER_LOG_SHA,
        "matched_call_logs": len(paths),
        "trace_frames": len(frames),
        "caught_failures": failures,
        "funding_batches": batches,
        "gas_used": gas_used,
        "gas_limit": quantity(tx["gas"]),
        "base_fee_per_gas_wei": base_fee,
        "effective_gas_price_wei": receipt.effective_gas_price,
        "gas_paid_wei": gas_fee,
        "gas_base_wei": base_fee * gas_used,
        "gas_priority_wei": priority,
        "direct_fee_wei": direct_fee,
        "direct_fee_trace_path": [0],
        "tx_value_wei": quantity(tx["value"]),
        "native_call_deltas_before_gas_wei": {
            "sender": native_by_role[SENDER],
            "executor": native_by_role[EXECUTOR],
            "weth_recipient": native_by_role[PAYOUT],
            "block_fee_recipient": native_by_role[receiver],
        },
        "visible_weth_proceeds_wei": proceeds,
        "visible_after_execution_and_direct_fee_if_one_owner_wei": proceeds - gas_fee - direct_fee,
        "strict_funded_subgraph": asdict(strict.profile),
        "strict_unresolved": strict.unresolved_events,
        "fourth_batch_layer_log_paths": [
            {"log_index": index, "trace_path": paths[index]} for index in last_actions
        ],
        "oracle_join": {
            "previous_tx": PREVIOUS,
            "previous_tx_index": INDEX - 1,
            "previous_tx_sender": pre["from"],
            "previous_tx_selector": pre["input"][:10],
            "update_event_count": len(updates),
            "update_log": matching[0],
            "read_log": read.log_index,
            "read_trace_path": read_path,
            "oracle": ORACLE,
            "reader_pool": METRIC_POOL,
            "feed_id": FEED,
            "read_values": quote,
            "pool_usdt_in_raw": sold[3],
            "pool_usdc_out_raw": bought[3],
            "pool_input_output_transfer_logs": [494, 493],
        },
    }


def main() -> None:
    if not __debug__:
        raise RuntimeError("forensic replay requires Python assertions enabled")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    parser.add_argument("earlier_logs", type=Path)
    args = parser.parse_args()
    print(json.dumps(prove(args.evidence, args.earlier_logs), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
