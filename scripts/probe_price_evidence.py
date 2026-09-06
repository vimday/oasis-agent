"""Read-only USD feed evidence for a retained Base Sepolia transaction block.

Replays the retained transaction cost evidence, then collects Chainlink-style
feed observations (ETH/USD and USDC/USD) pinned to the exact same block.
Feed identity is a supplied research policy verified against on-chain runtime,
description, decimals and freshness; it is not an independent certification.
The resulting cost valuation is expressed in quote-token base units without any
stablecoin peg assumption.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from eth_hash.auto import keccak

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.farming.price_evidence import (
    UsdFeedPolicy,
    UsdPriceCapture,
    persist_cost_valuation,
    persist_price_evidence,
    value_cost_in_quote,
)
from oasis_agent.farming.transaction_costs import replay_transaction_cost
from oasis_agent.models.chain import BlockSnapshot
from oasis_agent.models.common import ZERO_ADDRESS, BlockRef, EvidenceRef, canonical_json
from oasis_agent.rpc.transport import JsonRpcTransport
from scripts.probe_v4_testnet import SOURCE, URL, ProbeRecorder, PublicTestnetPoster

# Research-sourced feed addresses; on-chain description and runtime are checked.
BASE_SEPOLIA_USDC = "0x036cbd53842c5426634e7929541ec2318f3dcf7e"
FEEDS = {
    "native": UsdFeedPolicy(
        84532,
        "0x4aDC67696bA383F43DD60A9e78F2C97Fbbfc7cb1",
        ZERO_ADDRESS,
        "ETH / USD",
        "0x" + "00" * 32,
        86_400,
    ),
    "quote": UsdFeedPolicy(
        84532,
        "0xd30e2101a97dcbAeBCBC04F14C3f624E67A35165",
        BASE_SEPOLIA_USDC,
        "USDC / USD",
        "0x" + "00" * 32,
        86_400,
    ),
}
SELECTORS = {
    "decimals()": "0x313ce567",
    "description()": "0x7284e416",
    "latestRoundData()": "0xfeaf968c",
}


def load_cost_evidence(
    input_dir: Path,
) -> tuple[EvidenceStore, EvidenceRef, BlockSnapshot, str]:
    summary = json.loads((input_dir / "summary.json").read_text())
    evidence = summary["evidence"]
    block = evidence["block"]
    ref = EvidenceRef(
        evidence["kind"],
        evidence["content_hash"],
        BlockRef(
            block["chain_id"],
            block["number"],
            block["block_hash"],
            block.get("timestamp"),
        ),
        evidence["source"],
        evidence.get("retrieved_at", ""),
        evidence.get("request_hash", ""),
        evidence.get("raw_response_hash", ""),
        evidence.get("interpretation_hash", ""),
        evidence.get("schema_version", 1),
    )
    parent_hash = None
    wire = input_dir / "cost-wire.jsonl"
    for line in wire.read_text().splitlines():
        row = json.loads(line)
        if row.get("method") == "eth_getBlockByNumber":
            result = json.loads(bytes.fromhex(row["response"]).decode())["result"]
            parent_hash = result.get("parentHash")
            break
    if parent_hash is None:
        raise ValueError("cost wire evidence missing block header parent hash")
    header = BlockSnapshot(
        BlockRef(block["chain_id"], block["number"], block["block_hash"], block["timestamp"]),
        parent_hash,
    )
    store = EvidenceStore(input_dir / "evidence.db")
    return store, ref, header, str(summary["cost"]["cost"]["transaction_hash"])


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    if not (args.input / "summary.json").exists() or not (args.input / "evidence.db").exists():
        p.error("input must be a retained transaction-cost evidence directory")
    if args.output.exists():
        p.error("new output directory required")
    args.output.mkdir(parents=True)

    store, cost_ref, cost_header, tx_hash = load_cost_evidence(args.input)
    try:
        cost = replay_transaction_cost(
            store, cost_ref, header=cost_header, transaction_hash=tx_hash
        )
    finally:
        store.close()

    out_store = EvidenceStore(args.output / "evidence.db")
    captures: dict[str, UsdPriceCapture] = {}
    refs: dict[str, object] = {}
    try:
        for name, policy in FEEDS.items():
            recorder = ProbeRecorder(args.output, f"price-{name}")
            rpc = JsonRpcTransport(URL, http=PublicTestnetPoster(), recorder=recorder, retries=0)
            pin = {"blockHash": cost.header.ref.block_hash, "requireCanonical": True}
            rpc.request("eth_chainId", [])
            rpc.request("eth_getBlockByNumber", [hex(cost.header.ref.number), False])
            code = rpc.request("eth_getCode", [policy.address, pin])
            if (
                not isinstance(code, str)
                or not code.startswith("0x")
                or not bytes.fromhex(code[2:])
            ):
                raise ValueError("feed runtime code missing at pinned block")
            policy = UsdFeedPolicy(
                policy.chain_id,
                policy.address,
                policy.base_token,
                policy.description,
                "0x" + keccak(bytes.fromhex(code[2:])).hex(),
                policy.max_age_seconds,
            )
            for selector in SELECTORS.values():
                rpc.request(
                    "eth_call",
                    [{"to": policy.address, "data": selector}, pin],
                )
            rpc.request("eth_getBlockByNumber", [hex(cost.header.ref.number), False])
            capture = UsdPriceCapture(cost.header, policy, SOURCE, recorder.exchanges)
            ref = persist_price_evidence(out_store, capture)
            refs[name] = asdict(ref)
            captures[name] = capture

        native = captures["native"]
        quote = captures["quote"]
        valuation = value_cost_in_quote(
            cost, native, quote, quote_token=BASE_SEPOLIA_USDC, quote_decimals=6
        )
        ref = persist_cost_valuation(
            out_store, valuation, block=cost.header.ref, source=cost.source
        )
        summary = {
            "schema": "oasis.price-closure.v1",
            "block": asdict(cost.header.ref),
            "transaction_hash": cost.transaction_hash,
            "cost_total_native": valuation.total_native,
            "feeds": {
                name: {
                    "address": capture.policy.address,
                    "base_token": capture.policy.base_token,
                    "description": capture.policy.description,
                    "runtime_keccak": capture.policy.runtime_keccak,
                    "answer": capture.reconstruct()[0],
                    "decimals": capture.reconstruct()[1],
                    "evidence": refs[name],
                }
                for name, capture in captures.items()
            },
            "valuation": {
                "value_quote_units": valuation.value_quote_units,
                "quote_token": valuation.quote_token,
                "quote_decimals": valuation.quote_decimals,
                "usd_nominal_indicative": valuation.usd_nominal,
                "peg_assumption": False,
                "evidence": asdict(ref),
            },
            "execution_eligible": False,
            "position_allocation": "unresolved",
        }
        (args.output / "summary.json").write_bytes(canonical_json(summary))
    finally:
        out_store.close()
    print(
        f"Retained pinned USD feed evidence; native cost {valuation.total_native} wei "
        f"valued at {valuation.value_quote_units} quote units "
        f"(indicative USD {valuation.usd_nominal})"
    )


if __name__ == "__main__":
    main()
