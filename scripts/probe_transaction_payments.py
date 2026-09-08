"""Read-only all-participant balances for the retained Base Sepolia transaction."""

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.farming.position_ledger import PositionLedgerEntry
from oasis_agent.farming.position_settlement import PositionSettlementCapture
from oasis_agent.farming.transaction_payments import (
    TransactionPaymentCapture,
    persist_transaction_payments,
    transaction_payment_requests,
)
from oasis_agent.models.chain import BlockSnapshot
from oasis_agent.models.common import BlockRef, canonical_json
from oasis_agent.models.v4 import ModifyLiquidityObservation, PoolKey
from oasis_agent.rpc.transport import JsonRpcTransport, RpcExchange
from scripts.probe_v4_testnet import URL, ProbeRecorder, PublicTestnetPoster


def load_core(directory: Path) -> PositionSettlementCapture:
    raw = json.loads((directory / "summary.json").read_text())["core_settlement"]
    h = raw["header"]
    header = BlockSnapshot(BlockRef(**h["ref"]), h["parent_hash"])
    e = raw["entry"]
    e["block"] = BlockRef(**e["block"])
    e["observation"] = ModifyLiquidityObservation(**e["observation"])
    rows = [json.loads(x) for x in (directory / "settlement-wire.jsonl").read_text().splitlines()]
    exchanges = tuple(
        RpcExchange(
            x["method"],
            x["request_id"],
            bytes.fromhex(x["request"]),
            bytes.fromhex(x["response"]),
            x["attempt"],
        )
        for x in rows
    )
    return PositionSettlementCapture(
        "base-sepolia-public",
        header,
        raw["pool_manager"],
        PoolKey(**raw["pool_key"]),
        PositionLedgerEntry(**e),
        exchanges,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--resume-wire", type=Path)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("new output directory required")
    args.output.mkdir(parents=True)
    core = load_core(args.input)
    if core.header.ref.chain_id != 84532:
        raise ValueError("probe restricted to Base Sepolia")
    recorder = ProbeRecorder(args.output, "payments")
    transport = JsonRpcTransport(URL, http=PublicTestnetPoster(), recorder=recorder, retries=0)
    specs = transaction_payment_requests(core)
    prefix: tuple[RpcExchange, ...] = ()
    if args.resume_wire:
        rows = [json.loads(x) for x in args.resume_wire.read_text().splitlines()]
        prefix = tuple(
            RpcExchange(
                x["method"],
                x["request_id"],
                bytes.fromhex(x["request"]),
                bytes.fromhex(x["response"]),
                x["attempt"],
            )
            for x in rows
        )
        if len(prefix) > len(specs):
            raise ValueError("resume prefix exceeds expected capture")
        for x, (method, params) in zip(prefix, specs, strict=False):
            request = json.loads(x.request_payload)
            response = json.loads(x.response_payload)
            if x.method != method or request["params"] != params or "error" in response:
                raise ValueError("resume prefix differs from expected requests")
        transport._request_id = max((x.request_id for x in prefix), default=0)
    for method, params in specs[len(prefix) :]:
        transport.request(method, params)
    capture = TransactionPaymentCapture(core, prefix + recorder.exchanges)
    store = EvidenceStore(args.output / "evidence.db")
    try:
        ref = persist_transaction_payments(store, capture)
        (args.output / "summary.json").write_bytes(
            canonical_json({"evidence": asdict(ref), "payments": json.loads(capture.payload)})
        )
    finally:
        store.close()
    print(
        "All-participant receipt/balance/supply reconciliation retained; NFT allocation not assumed"
    )


if __name__ == "__main__":
    main()
