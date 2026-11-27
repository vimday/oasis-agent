"""Export the ten receipt-backed two-shape candidates from one frozen BSC block."""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Any

from docs.verification.fixtures.replay_bsc_pool_expansion import receipts_summary
from oasis_agent.evidence.public_receipts import quantity

ROOT = Path(__file__).resolve().parents[3]
RESEARCH = "reports/research/"
WINDOW = f"{RESEARCH}bsc-next-window-20261127/"
HEADER = f"{WINDOW}wire/03.response"
RECEIPTS = f"{WINDOW}wire/16.response"
FROZEN_RESULT = f"{WINDOW}result.json"
EXPANSION = "docs/verification/BSC_HISTORICAL_POOL_EXPANSION_20261127.json"
BASELINE = f"{RESEARCH}bsc-wide-forward-20261123/universe.json"
TOPICS = f"{RESEARCH}bsc-window-audits-20261127/cohort/scope.json"


def raw(path: str) -> bytes:
    return (ROOT / path).read_bytes()


def read(path: str) -> Any:
    return json.loads(raw(path))


def main() -> None:
    receipt_bytes = raw(RECEIPTS)
    receipts = json.loads(receipt_bytes)["result"]
    header = read(HEADER)["result"]
    summary = read(FROZEN_RESULT)
    if summary["H"]["hash"] != header["hash"] or summary["H"]["number"] != header["number"]:
        raise ValueError("receipt block header differs from the frozen discovery head")
    topics = set(read(TOPICS)["signatures"])
    pools = read(EXPANSION)["identity"]["state"]["pools"]
    baseline = {row["pool"] for row in read(BASELINE)}
    added = {row["pool_id"] for row in pools}
    managers = {row["contract"] for row in pools if row["kind"] in {"v4", "infinity_cl"}}
    checked = receipts_summary(
        header, receipts, baseline, {"pools": added, "managers": managers}, topics
    )
    if json.loads(json.dumps(checked)) != summary["H_receipts"]:
        raise ValueError("raw receipts do not reproduce the original coverage and receipt root")

    samples = []
    for receipt in receipts:
        matched = [log for log in receipt["logs"] if log["topics"] and log["topics"][0] in topics]
        if len(matched) < 2:
            continue
        logical = [
            (log, log["topics"][1] if log["address"] in managers else log["address"])
            for log in matched
        ]
        gas = quantity(receipt["gasUsed"])
        price = quantity(receipt["effectiveGasPrice"])
        overlaps = [
            {
                "log_index": quantity(log["logIndex"]),
                "logical_pool_id": pool,
                "emitter": log["address"],
                "roster": "added" if pool in added else "baseline",
            }
            for log, pool in logical
            if pool in added or pool in baseline
        ]
        samples.append(
            {
                "tx_hash": receipt["transactionHash"],
                "transaction_index": quantity(receipt["transactionIndex"]),
                "shape": "unresolved",
                "receipt_status": quantity(receipt["status"]),
                "recognized_swap_shapes": len(matched),
                "swap_log_indices": [quantity(log["logIndex"]) for log in matched],
                "receipt_gas_used": gas,
                "effective_gas_price_wei": str(price),
                "receipt_gas_bnb_wei": str(gas * price),
                "roster_overlap": overlaps,
            }
        )
    observed = {row["tx"] for row in checked["overlap_transactions"]}
    if (
        len(samples) != 10
        or {row["tx_hash"] for row in samples if row["roster_overlap"]} != observed
    ):
        raise ValueError("frozen candidate or roster overlap denominator differs")
    result = {
        "schema": "oasis-bsc-next-window-screened-v1",
        "chain_id": 56,
        "chain": "bsc",
        "block_number": quantity(header["number"]),
        "block_hash": header["hash"],
        "receipts_root": checked["receipts_root"],
        "raw_response": RECEIPTS,
        "raw_response_sha256": sha256(receipt_bytes).hexdigest(),
        "raw_header": HEADER,
        "raw_header_sha256": sha256(raw(HEADER)).hexdigest(),
        "all_receipts": len(receipts),
        "matched_log_topic0": sorted(topics),
        "pool_roster_overlap_transactions": len(observed),
        "samples": samples,
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
