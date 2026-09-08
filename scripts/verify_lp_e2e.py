"""Offline independent replay of retained LP lifecycle receipts and audit links."""

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

from oasis_agent.models.chain import LogRecord
from oasis_agent.models.v4 import PoolKey
from oasis_agent.uniswap_v4.events import (
    MODIFY_LIQUIDITY_TOPIC,
    SWAP_TOPIC,
    decode_modify_liquidity,
    decode_swap,
)
from oasis_agent.uniswap_v4.pool_id import pool_id_from_key


def verify_run(path: Path) -> dict[str, Any]:
    previous = "0" * 64
    events = []
    for index, line in enumerate(path.read_bytes().splitlines(), 1):
        row = json.loads(line)
        event = row["event"]
        raw = json.dumps(event, sort_keys=True, separators=(",", ":")).encode()
        if (
            event["seq"] != index
            or event["previous"] != previous
            or hashlib.sha256(raw).hexdigest() != row["digest"]
        ):
            raise ValueError("E2E audit chain corrupt")
        previous = row["digest"]
        events.append(event)
    if not events or events[0]["kind"] != "start" or events[-1]["kind"] != "complete":
        raise ValueError("E2E did not complete")
    start, end = events[0], events[-1]
    if start["chain_id"] not in {31337, 46630} or end["execution_eligible"] is not False:
        raise ValueError("not isolated test evidence")
    contracts = end["contracts"]
    key = PoolKey(contracts["token0"], contracts["token1"], 0x800000, 60, contracts["hook"])
    pool_id = pool_id_from_key(key)
    manager = contracts["PoolManager"].lower()
    receipts = [event for event in events if event["kind"] == "receipt"]
    signed = [event for event in events if event["kind"] == "signed"]
    if len(receipts) != 35 or len(signed) != len(receipts):
        raise ValueError("incomplete lifecycle transactions")
    spent = 0
    modifications: dict[str, list[int]] = {}
    swaps = []
    labels = Counter(event["label"] for event in receipts)
    for name, count in {
        "mint": 1,
        "swap": 4,
        "collect": 1,
        "recenter": 1,
        "remove": 1,
        "revoke-whitelist": 1,
        "revoke-permit2": 2,
        "revoke-token": 2,
        "revoke-swap": 2,
    }.items():
        if labels[name] != count:
            raise ValueError("lifecycle action coverage mismatch")
    for index, (sent, event) in enumerate(zip(signed, receipts, strict=True)):
        receipt = event["raw_receipt"]
        if (
            sent["tx_hash"] != event["tx_hash"]
            or sent["nonce"] != event["nonce"]
            or event["nonce"] != signed[0]["nonce"] + index
            or sent["label"] != event["label"]
            or receipt["transactionHash"].lower() != event["tx_hash"].lower()
            or receipt["status"] != 1
            or receipt["from"].lower() != start["account"].lower()
            or receipt["blockHash"].lower() != event["block_hash"].lower()
            or receipt["blockNumber"] != event["block"]
        ):
            raise ValueError("receipt identity/status mismatch")
        spent += receipt["gasUsed"] * receipt["effectiveGasPrice"]
        for raw_log in receipt["logs"]:
            if raw_log["address"].lower() != manager:
                continue
            log = LogRecord(
                raw_log["address"],
                tuple(raw_log["topics"]),
                raw_log["data"],
                raw_log["blockNumber"],
                raw_log["transactionHash"],
                raw_log["logIndex"],
                raw_log["blockHash"],
                raw_log["transactionIndex"],
                raw_log.get("removed", False),
            )
            if log.topics[0] == SWAP_TOPIC:
                swap = decode_swap(log)
                if swap.pool_id != pool_id or swap.fee != 500:
                    raise ValueError("unexpected pool or effective fee")
                swaps.append((swap.amount0, swap.amount1))
            elif log.topics[0] == MODIFY_LIQUIDITY_TOPIC:
                change = decode_modify_liquidity(log)
                if (
                    change.pool_id != pool_id
                    or change.sender != contracts["PositionManager"].lower()
                ):
                    raise ValueError("position modification identity mismatch")
                modifications.setdefault(event["label"], []).append(change.liquidity_delta)
    if modifications != {
        "mint": [10**16],
        "collect": [0],
        "recenter": [-(10**16), 10**15],
        "remove": [-(10**15)],
    }:
        raise ValueError("liquidity lifecycle does not reconcile")
    if len(swaps) != 4:
        raise ValueError("missing bidirectional swap receipts")
    for (amount0, amount1), zero_for_one in zip(swaps, (True, False, True, False), strict=True):
        if (amount0 < 0 < amount1) != zero_for_one or amount0 * amount1 >= 0:
            raise ValueError("swap direction mismatch")
    checks = [event for event in events if event["kind"] == "conservation"]
    if len(checks) != 8:
        raise ValueError("missing conservation checks")
    for check in checks:
        before, after = check["before"], check["after"]
        if any(before[i] + before[i + 2] != after[i] + after[i + 2] for i in (0, 1)):
            raise ValueError("per-token conservation residual")
    if (
        spent != end["spent_wei"]
        or start["initial_balance"] - end["final_balance"] != spent
        or spent > start["max_gas_wei"]
    ):
        raise ValueError("native gas balance or budget mismatch")
    return {
        "chain_id": start["chain_id"],
        "transactions": len(receipts),
        "swaps": len(swaps),
        "conservation_checks": len(checks),
        "gas_wei": spent,
        "pool_id": pool_id,
        "audit_digest": previous,
        "execution_eligible": False,
        "economic_profitability": "unresolved",
        "trust": "single_provider_research",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("journal", type=Path)
    args = parser.parse_args()
    print(json.dumps(verify_run(args.journal), sort_keys=True))


if __name__ == "__main__":
    main()
