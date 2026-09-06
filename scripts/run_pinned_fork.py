"""Verify public chain/runtime pins, then execute a strictly local Foundry fork."""

from __future__ import annotations

import argparse
import os
import subprocess
from dataclasses import asdict
from pathlib import Path

from eth_hash.auto import keccak

from oasis_agent.evidence.rpc_capture import BoundedExchangeRecorder
from oasis_agent.models.common import canonical_json
from oasis_agent.rpc.client import RpcClient
from oasis_agent.rpc.transport import JsonRpcTransport

NETWORKS = {
    "base-sepolia": (
        84532,
        "https://sepolia.base.org",
        "0x05e73354cfdd6745c338b50bcfdfa3aa6fa03408",
        "0x4b2c77d209d3405f41a037ec6c77f7f5b8e2ca80",
    ),
    "robinhood": (
        4663,
        "https://rpc.mainnet.chain.robinhood.com",
        "0x8366a39cc670b4001a1121b8f6a443a643e40951",
        "0x58daec3116aae6d93017baaea7749052e8a04fa7",
    ),
}
PERMIT2 = "0x000000000022d473030f116ddee9f6b43ac78ba3"


class PublicPoster:
    def __init__(self, url: str):
        self.url = url

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
                self.url,
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


def main() -> None:
    from datetime import UTC, datetime

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--network", choices=NETWORKS, required=True)
    parser.add_argument("--block", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.block <= 0 or args.output.exists():
        parser.error("positive explicit block and new output directory required")
    args.output.mkdir(parents=True)
    chain, url, manager, positions = NETWORKS[args.network]
    recorder = BoundedExchangeRecorder()
    rpc = RpcClient(
        JsonRpcTransport(url, http=PublicPoster(url), recorder=recorder, retries=1),
        expected_chain_id=chain,
    )
    header = rpc.get_block_snapshot(args.block)
    targets = {"POOL_MANAGER": manager, "POSITION_MANAGER": positions, "PERMIT2": PERMIT2}
    env = os.environ.copy()
    env.update(
        FOUNDRY_PROFILE="fork",
        OASIS_FORK_RPC=url,
        OASIS_FORK_BLOCK=str(args.block),
        OASIS_FORK_CHAIN_ID=str(chain),
        OASIS_FORK_PARENT_HASH=header.parent_hash,
    )
    contracts = {}
    for role, address in targets.items():
        code = bytes.fromhex(rpc.get_code(address, header.ref)[2:])
        if not code:
            raise ValueError("required fork contract has empty code")
        digest = "0x" + keccak(code).hex()
        contracts[role] = {"address": address, "runtime_keccak": digest, "bytes": len(code)}
        env["OASIS_FORK_" + role] = address
        env["OASIS_FORK_" + role + "_HASH"] = digest
    if rpc.get_block_snapshot(args.block) != header:
        raise ValueError("fork preflight block changed")
    manifest = {
        "schema": "oasis.local-fork.v1",
        "header": asdict(header),
        "source": args.network + "-public",
        "retrieved_at": datetime.now(UTC).isoformat(),
        "contracts": contracts,
        "broadcast": False,
        "deployment_eligible": False,
        "wire": [
            {
                "method": x.method,
                "request_id": x.request_id,
                "attempt": x.attempt,
                "request": x.request_payload.hex(),
                "response": x.response_payload.hex(),
            }
            for x in recorder.exchanges
        ],
    }
    (args.output / "preflight.json").write_bytes(canonical_json(manifest))
    command = ["forge", "test", "--match-contract", "OasisForkLifecycleTest", "-vv"]
    with (args.output / "forge.log").open("wb") as log:
        result = subprocess.run(
            command,
            cwd=Path(__file__).resolve().parents[1] / "contracts",
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            timeout=900,
            check=False,
        )
    manifest.pop("wire")
    manifest["exit_code"] = result.returncode
    manifest["preflight_sha256"] = (
        __import__("hashlib").sha256((args.output / "preflight.json").read_bytes()).hexdigest()
    )
    (args.output / "result.json").write_bytes(canonical_json(manifest))
    print(f"Local fork exit {result.returncode}; evidence: {args.output}")
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
