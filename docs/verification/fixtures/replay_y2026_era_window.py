"""Recompute one Era full-receipt / independent all-log sample from the retained bytes."""

import json
from collections import Counter
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from eth_hash.auto import keccak

from oasis_agent.arbitrage.cycle_protocols import abi, data_bytes, topic
from oasis_agent.rpc.budget import BudgetPolicy, RpcBudget
from oasis_agent.rpc.protocol import response_envelope

ROOT = Path(__file__).resolve().parents[3]
RAW = ROOT / "reports/research/y2026-era-20261127"
ETH = "0x000000000000000000000000000000000000800a"
BOOT = "0x0000000000000000000000000000000000008001"
TRANSFER = topic("Transfer(address,address,uint256)")
MAVERICK = topic("Swap(address,address,bool,bool,uint256,uint256,int32)")
ZERO = bytes(32)
CORE = {
    topic(sig)
    for sig in (
        "Swap(address,uint256,uint256,uint256,uint256,address)",
        "Swap(address,address,uint256,uint256,uint256,uint256)",
        "Swap(address,address,int256,int256,uint160,uint128,int24)",
        "Swap(address,address,int256,int256,uint160,uint128,int24,uint128,uint128)",
        "Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)",
        "Swap(bytes32,address,address,uint256,uint256)",
        "TokenExchange(address,int128,uint256,int128,uint256)",
        "TokenExchange(address,uint256,uint256,uint256,uint256)",
    )
}


def read(path):
    return json.loads(path.read_bytes())


def digest(path):
    return sha256(path.read_bytes()).hexdigest()


def wire(source, label, expected_id, method, params, provenance):
    lane = RAW / source
    request = lane / f"{label}.request.json"
    response = lane / f"{label}.response"
    meta = read(lane / f"{label}.meta.json")
    assert read(request) == {
        "jsonrpc": "2.0",
        "id": expected_id,
        "method": method,
        "params": params,
    }
    assert meta["id"] == expected_id and meta["request_sha256"] == digest(request)
    assert meta["http"] == 200 and meta["transport_error"] is None
    assert meta["response_sha256"] == digest(response) and meta["bytes"] == response.stat().st_size
    assert meta["url"] == read(RAW / "scope-valid.json")["sources"][source]
    assert meta["started_utc"] <= meta["finished_utc"]
    provenance[f"{source}/{label}"] = {
        k: meta[k] for k in ("request_sha256", "response_sha256", "started_utc", "finished_utc")
    }
    return response_envelope(response.read_bytes(), expected_id)["result"]


def rebuild_hash(header):
    rolling = ZERO
    for tx in header["transactions"]:
        word = data_bytes(tx)
        assert len(word) == 32
        rolling = keccak(rolling + word)
    preimage = (
        int(header["number"], 16).to_bytes(32)
        + int(header["timestamp"], 16).to_bytes(32)
        + data_bytes(header["parentHash"])
        + rolling
    )
    return "0x" + keccak(preimage).hex()


def summarize_tx(receipt):
    sender, executor = receipt["from"], receipt["to"]
    logs = receipt["logs"]
    moves = []
    for log in logs:
        if log["topics"][:1] == [TRANSFER] and len(log["topics"]) == 3:
            from_, to = [abi(("address",), data_bytes(t))[0] for t in log["topics"][1:]]
            (value,) = abi(("uint256",), data_bytes(log["data"]))
            moves.append(
                {
                    "index": int(log["logIndex"], 16),
                    "asset": log["address"],
                    "from": from_,
                    "to": to,
                    "raw": value,
                }
            )
    fees = [m for m in moves if m["asset"] == ETH and {m["from"], m["to"]} == {sender, BOOT}]
    assert fees and fees[0]["from"] == sender
    paid = sum(m["raw"] if m["from"] == sender else -m["raw"] for m in fees)
    gas, price = (int(receipt[k], 16) for k in ("gasUsed", "effectiveGasPrice"))
    assert gas * price == paid
    legs = []
    for log in logs:
        if log["topics"] != [MAVERICK]:
            continue
        pool = log["address"]
        who, recipient, token_a_in, exact_out, amount_in, amount_out, tick = abi(
            ("address", "address", "bool", "bool", "uint256", "uint256", "int32"),
            data_bytes(log["data"]),
        )
        assert who == recipient == executor and amount_in > 0 and amount_out > 0
        (inbound,) = [
            m for m in moves if (m["from"], m["to"], m["raw"]) == (executor, pool, amount_in)
        ]
        (outbound,) = [
            m for m in moves if (m["from"], m["to"], m["raw"]) == (pool, recipient, amount_out)
        ]
        log_index = int(log["logIndex"], 16)
        assert (
            inbound["asset"] != outbound["asset"]
            and max(inbound["index"], outbound["index"]) < log_index
        )
        legs.append(
            {
                "log_index": log_index,
                "pool": pool,
                "recipient": recipient,
                "token_a_in": token_a_in,
                "exact_output": exact_out,
                "active_tick": tick,
                "debit": inbound,
                "credit": outbound,
            }
        )
    return {
        "tx_hash": receipt["transactionHash"],
        "index": int(receipt["transactionIndex"], 16),
        "sender": sender,
        "executor": executor,
        "receipt_status": int(receipt["status"], 16),
        "log_count": len(logs),
        "gas_used": gas,
        "effective_gas_price_wei": price,
        "network_fee_wei": paid,
        "native_fee_transfers": fees,
        "core_8_signature_matches": sum(
            bool(log["topics"]) and log["topics"][0] in CORE for log in logs
        ),
        "maverick_v1_abi_amount_matched_legs": legs,
        "all_cost_pnl_wei": None,
        "unclassified_topics": sorted(
            {
                log["topics"][0]
                for log in logs
                if log["topics"]
                and log["topics"][0]
                not in (CORE | {MAVERICK, TRANSFER, topic("Approval(address,address,uint256)")})
            }
        ),
    }


def main():
    scope = read(RAW / "scope-valid.json")
    stop = read(RAW / "STOP.json")
    scope_hash = digest(RAW / "scope-valid.json")
    assert (
        scope_hash
        == read(RAW / "START.json")["scope_sha256"]
        == "cd4b348f11aaedf9d46c8d665784bdf4ef674a3d8a561c8ce5dd3aa4b17ad246"
    )
    assert scope["driver_sha256"] == digest(RAW / "capture.py")
    assert scope["wire_driver_sha256"] == digest(RAW.parent / "y2026-op-arb-20261127/capture.py")
    wire_hashes, anchors, ledgers = {}, {}, {}
    for source in scope["sources"]:
        ledger = RAW / source / "original.sqlite3"
        with RpcBudget(
            ledger,
            (BudgetPolicy(source, scope["per_source_cap"], scope["pacing_ms"]),),
            read_only=True,
        ) as b:
            assert b.counts() == stop["stops"][source]["counts"] == {source: 10}
        ledgers[source] = digest(ledger)
        assert wire(source, "chain", 1, "eth_chainId", [], wire_hashes) == hex(324)
        a = wire(source, "latest", 2, "eth_getBlockByNumber", ["latest", False], wire_hashes)
        anchors[source] = {"height": int(a["number"], 16), "hash": a["hash"]}
    anchor = min(a["height"] for a in anchors.values())
    targets = [anchor - d for d in scope["offsets_below_anchor"]]
    assert {"anchor": anchor, "heights": targets} == read(RAW / "targets.json")
    rows = []
    headers = {}
    for i, h in enumerate(targets):
        hs = [
            wire(s, f"{h}-header", 3 + i, "eth_getBlockByNumber", [hex(h), False], wire_hashes)
            for s in scope["sources"]
        ]
        header, other = hs
        assert (
            header == other
            and rebuild_hash(header) == header["hash"]
            and int(header["number"], 16) == h
        )
        headers[h] = header
        assert all(
            data_bytes(header[k]) == ZERO for k in ("receiptsRoot", "stateRoot", "transactionsRoot")
        )
        receipts = wire(
            "official", f"{h}-receipts", 7 + i, "eth_getBlockReceipts", [hex(h)], wire_hashes
        )
        remote_logs = wire(
            "drpc",
            f"{h}-logs",
            7 + i,
            "eth_getLogs",
            [{"fromBlock": hex(h), "toBlock": hex(h)}],
            wire_hashes,
        )
        logs = [log for tx in receipts for log in tx["logs"]]
        assert logs == remote_logs and len(receipts) == len(header["transactions"])
        for j, (tx, receipt) in enumerate(zip(header["transactions"], receipts, strict=True)):
            for item in [receipt, *receipt["logs"]]:
                assert item["blockHash"] == header["hash"] and int(item["blockNumber"], 16) == h
                assert (
                    item["transactionHash"] == tx
                    and int(item["transactionIndex"], 16) == j
                    and not item.get("removed", False)
                )
        assert [int(log["logIndex"], 16) for log in logs] == list(range(len(logs)))
        assert sum(int(tx["gasUsed"], 16) for tx in receipts) == int(header["gasUsed"], 16)
        rows.append(
            {
                "height": h,
                "hash": header["hash"],
                "parent_hash": header["parentHash"],
                "offset": anchor - h,
                "block_utc": datetime.fromtimestamp(int(header["timestamp"], 16), UTC).isoformat(),
                "log_count": len(logs),
                "rollup_block_hash_recomputed": True,
                "eth_header_trie_roots_zero": True,
                "ordered_logs_agree": True,
                "ordered_transactions": [summarize_tx(r) for r in receipts],
            }
        )
    for h in headers:
        if h - 1 in headers:
            assert headers[h]["parentHash"] == headers[h - 1]["hash"]
    cases = [tx for block in rows for tx in block["ordered_transactions"]]
    result = {
        "chain": "zkSync Era",
        "chain_id": 324,
        "source_scope": scope,
        "scope_sha256": scope_hash,
        "raw_directory": str(RAW.relative_to(ROOT)),
        "source_anchors": anchors,
        "receipts_authenticated_by_header": False,
        "source_stops": stop,
        "original_ledger_sha256": ledgers,
        "requests": wire_hashes,
        "blocks": rows,
        "complete_receipts": len(cases),
        "log_count": sum(tx["log_count"] for tx in cases),
        "by_receipt_status": dict(Counter(t["receipt_status"] for t in cases)),
        "fee_logs_reconciled": len(cases),
        "confirmed_serial_or_batched_closed_arbitrages": 0,
    }
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
