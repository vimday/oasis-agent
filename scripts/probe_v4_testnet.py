"""Read-only Base Sepolia probe with retained wire evidence; no signing capability."""

import argparse
import json
import subprocess
from dataclasses import asdict
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from eth_hash.auto import keccak

from oasis_agent.evidence.rpc_capture import BoundedExchangeRecorder
from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.farming.position_capture import (
    PositionSnapshotCapture,
    collect_position_snapshot,
    persist_position_snapshot_evidence,
)
from oasis_agent.farming.position_ledger import PositionLedgerEntry
from oasis_agent.farming.position_settlement import (
    collect_position_settlement,
    persist_position_settlement_evidence,
)
from oasis_agent.models.common import canonical_json
from oasis_agent.rpc.client import RpcClient
from oasis_agent.rpc.transport import JsonRpcTransport, RpcExchange
from oasis_agent.uniswap_v4.events import MODIFY_LIQUIDITY_TOPIC, decode_modify_liquidity

# Public testnet only. Address discovery is not a deployment identity attestation.
# https://developers.uniswap.org/docs/protocols/v4/deployments
URL = "https://sepolia.base.org"
CHAIN = 84532
CORE = "0x05e73354cfdd6745c338b50bcfdfa3aa6fa03408"
POSM = "0x4b2c77d209d3405f41a037ec6c77f7f5b8e2ca80"
VIEW = "0x571291b572ed32ce6751a2cb2486ebee8defb9b4"
SOURCE = "base-sepolia-public"


class PublicTestnetPoster:
    """Use system curl/TLS for this public endpoint, with no credential material."""

    def post(self, payload: bytes) -> bytes:
        return subprocess.run(
            [
                "/usr/bin/curl",
                "--http1.1",
                "--fail",
                "--silent",
                "--show-error",
                "--max-time",
                "20",
                URL,
                "-H",
                "Content-Type: application/json",
                "--data-binary",
                "@-",
            ],
            input=payload,
            capture_output=True,
            check=True,
            timeout=25,
        ).stdout


class ProbeRecorder(BoundedExchangeRecorder):
    def __init__(self, output: Path, name: str) -> None:
        super().__init__(max_exchanges=100, max_bytes=5_000_000)
        self.path = output / f"{name}-wire.jsonl"

    def record(self, exchange: RpcExchange) -> None:
        super().record(exchange)
        # Append immediately, including RPC errors, so a later failure cannot
        # discard earlier observations. Files contain public RPC payloads only.
        with self.path.open("ab") as handle:
            handle.write(
                canonical_json(
                    {
                        "source": SOURCE,
                        "retrieved_at": datetime.now(UTC).isoformat(),
                        "method": exchange.method,
                        "request_id": exchange.request_id,
                        "attempt": exchange.attempt,
                        "request": exchange.request_payload.hex(),
                        "response": exchange.response_payload.hex(),
                    }
                )
                + b"\n"
            )


def session(output: Path, name: str, *, retries: int = 0):
    recorder = ProbeRecorder(output, name)
    transport = JsonRpcTransport(
        URL,
        http=PublicTestnetPoster(),
        recorder=recorder,
        retries=retries,
        timeout=20,
        total_deadline_seconds=45,
    )
    return recorder, transport


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--lookback", type=int, default=2000)
    args = parser.parse_args()
    if not 1 <= args.lookback <= 50_000:
        parser.error("lookback must be between 1 and 50000 blocks")
    if args.output.exists() and any(args.output.iterdir()):
        parser.error("use a fresh output directory to preserve evidence")
    args.output.mkdir(parents=True, exist_ok=True)
    recorder, transport = session(args.output, "discovery", retries=1)
    rpc = RpcClient(transport, expected_chain_id=CHAIN)
    head = rpc.get_block_snapshot()
    # Observation lag only, not a finality proof.
    header = rpc.get_block_snapshot(head.ref.number - 32)
    code_hashes = {}
    for role, address in [("pool_manager", CORE), ("position_manager", POSM), ("state_view", VIEW)]:
        code = bytes.fromhex(rpc.get_code(address, header.ref)[2:])
        if not code:
            raise ValueError("testnet contract code missing")
        code_hashes[role] = {
            "address": address,
            "bytes": len(code),
            "keccak": "0x" + keccak(code).hex(),
        }
    logs = []
    for start in range(header.ref.number - args.lookback, header.ref.number + 1, 2000):
        logs.extend(
            rpc.get_logs(
                {
                    "address": CORE,
                    "fromBlock": hex(start),
                    "toBlock": hex(min(start + 1999, header.ref.number)),
                    "topics": [MODIFY_LIQUIDITY_TOPIC, None, "0x" + "0" * 24 + POSM[2:]],
                }
            )
        )
    if rpc.get_block_snapshot(header.ref.number) != header:
        raise ValueError("testnet block changed")
    summary = {
        "retrieved_at": datetime.now(UTC).isoformat(),
        "lookback_blocks": args.lookback,
        "chain_id": CHAIN,
        "header": asdict(header),
        "source": SOURCE,
        "contracts": code_hashes,
        "recent_modification_count": len(logs),
        "execution_eligible": False,
        "net_pnl": None,
        "trust": "single_provider_research",
    }
    # Retain the discovery session as observation, not canonical full-range proof.
    (args.output / "discovery.json").write_bytes(
        canonical_json(
            {
                "summary": summary,
                "exchanges": [
                    {
                        "method": x.method,
                        "request": x.request_payload.hex(),
                        "response": x.response_payload.hex(),
                    }
                    for x in recorder.exchanges
                ],
            }
        )
    )
    summary["discovery_sha256"] = sha256((args.output / "discovery.json").read_bytes()).hexdigest()
    candidates = [log for log in logs if decode_modify_liquidity(log).liquidity_delta > 0]
    if candidates:
        log = candidates[-1]
        token_id = int(decode_modify_liquidity(log).salt, 16)
        snapshot_recorder, snapshot_transport = session(args.output, "snapshot")
        snapshot_rpc = RpcClient(snapshot_transport, expected_chain_id=CHAIN)
        pinned, snapshot = collect_position_snapshot(
            snapshot_rpc,
            position_manager=POSM,
            state_view=VIEW,
            token_id=token_id,
            block_number=log.block_number,
        )
        capture = PositionSnapshotCapture(
            SOURCE, pinned, VIEW, snapshot, snapshot_recorder.exchanges
        )
        if log.block_hash != pinned.ref.block_hash or log.transaction_index is None:
            raise ValueError("discovered log is not pinned to snapshot")
        entry = PositionLedgerEntry(
            pinned.ref,
            log.transaction_hash,
            log.transaction_index,
            log.log_index,
            decode_modify_liquidity(log),
        )
        store = EvidenceStore(args.output / "evidence.db")
        try:
            summary["snapshot_evidence"] = asdict(
                persist_position_snapshot_evidence(store, capture)
            )
            (args.output / "snapshot-evidence.json").write_bytes(
                canonical_json(summary["snapshot_evidence"])
            )
            settlement_recorder, settlement_transport = session(args.output, "settlement")
            settlement = collect_position_settlement(
                settlement_transport,
                settlement_recorder,
                source=SOURCE,
                header=pinned,
                pool_manager=CORE,
                pool_key=snapshot.pool_key,
                entry=entry,
            )
            summary["core_settlement_evidence"] = asdict(
                persist_position_settlement_evidence(store, settlement)
            )
            summary["core_settlement"] = json.loads(settlement.payload)
        finally:
            store.close()
    else:
        summary["settlement_probe"] = "unresolved_no_positive_modification_in_bounded_window"
    (args.output / "summary.json").write_bytes(canonical_json(summary))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
