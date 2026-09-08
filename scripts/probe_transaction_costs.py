"""Read-only cost evidence for a previously retained Base Sepolia transaction."""

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from eth_abi.abi import encode

from oasis_agent.evidence.rpc_validation import rpc_quantity
from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.farming.transaction_costs import (
    OP_ORACLE,
    OP_SELECTOR,
    TransactionCostCapture,
    persist_transaction_cost,
)
from oasis_agent.models.common import canonical_json
from oasis_agent.rpc.transport import JsonRpcTransport
from scripts.probe_transaction_payments import load_core
from scripts.probe_v4_testnet import URL, ProbeRecorder, PublicTestnetPoster


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    if args.output.exists():
        p.error("new output directory required")
    args.output.mkdir(parents=True)
    core = load_core(args.input)
    b = core.header.ref
    if b.chain_id != 84532:
        raise ValueError("probe restricted to Base Sepolia")
    recorder = ProbeRecorder(args.output, "cost")
    rpc = JsonRpcTransport(URL, http=PublicTestnetPoster(), recorder=recorder, retries=0)
    rpc.request("eth_chainId", [])
    rpc.request("eth_getBlockByNumber", [hex(b.number), False])
    receipt = rpc.request("eth_getTransactionReceipt", [core.entry.transaction_hash])
    if not isinstance(receipt, dict):
        raise TypeError("receipt missing")
    gas = rpc_quantity(receipt.get("gasUsed"))
    pin = {"blockHash": b.block_hash, "requireCanonical": True}
    rpc.request("eth_getCode", [OP_ORACLE, pin])
    rpc.request(
        "eth_call", [{"to": OP_ORACLE, "data": OP_SELECTOR + encode(["uint256"], [gas]).hex()}, pin]
    )
    rpc.request("eth_getBlockByNumber", [hex(b.number), False])
    capture = TransactionCostCapture(
        core.header, core.entry.transaction_hash, core.source, recorder.exchanges
    )
    store = EvidenceStore(args.output / "evidence.db")
    try:
        ref = persist_transaction_cost(store, capture)
        (args.output / "summary.json").write_bytes(
            canonical_json({"evidence": asdict(ref), "cost": json.loads(capture.payload)})
        )
    finally:
        store.close()
    print("Retained whole-transaction native costs including L1 and pinned operator fee")


if __name__ == "__main__":
    main()
