"""Verify public chain/runtime pins, then execute a strictly local Foundry fork."""

import argparse
import hashlib
import json
import os
import subprocess
from collections.abc import Mapping
from dataclasses import asdict
from datetime import UTC, datetime
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
    def __init__(self, url: str, user_agent: str | None = None):
        self.url = url
        self.user_agent = user_agent

    def post(self, payload: bytes) -> bytes:
        command = [
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
        ]
        if self.user_agent is not None:
            command += ["-A", self.user_agent]
        command += ["--data-binary", "@-"]
        return subprocess.run(
            command,
            input=payload,
            capture_output=True,
            check=True,
            timeout=25,
        ).stdout


def main() -> None:
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
    # Some L2 node implementations (Arbitrum-Nitro/reth) include l1BlockNumber in
    # the block JSON. Foundry 1.8.1 uses that value as the fork-head block number,
    # so record it here and pass it to the test as the expected fork head. The L2
    # head timestamp is the L2-valid height anchor: on this L2 the EVM blockhash
    # opcode is relative to the L1 number, so it cannot reach the L2 parent hash.
    l1_block = 0
    fork_timestamp = None
    raw_block = rpc.transport.request("eth_getBlockByNumber", [hex(args.block), False])
    if isinstance(raw_block, Mapping):
        raw_l1 = raw_block.get("l1BlockNumber")
        if raw_l1 is not None:
            l1_block = int(str(raw_l1), 16)
            raw_ts = raw_block.get("timestamp")
            if raw_ts is not None:
                fork_timestamp = int(str(raw_ts), 16)
    if l1_block and fork_timestamp is None:
        raise ValueError("L2 preflight pinning data unavailable (head timestamp)")
    targets = {"POOL_MANAGER": manager, "POSITION_MANAGER": positions, "PERMIT2": PERMIT2}
    env = os.environ.copy()
    env.update(
        FOUNDRY_PROFILE="fork",
        OASIS_FORK_RPC=url,
        OASIS_FORK_BLOCK=str(args.block),
        OASIS_FORK_CHAIN_ID=str(chain),
        OASIS_FORK_PARENT_HASH=header.parent_hash,
        OASIS_FORK_L1_BLOCK=str(l1_block),
        OASIS_FORK_TIMESTAMP=str(fork_timestamp or 0),
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
    # Mirror the state-access path Foundry uses so a non-archive public endpoint
    # is diagnosed here instead of surfacing as an opaque forge failure. The
    # probe uses the same User-Agent as forge (foundry/<version>) because the
    # endpoint routes backends by UA.
    probe_poster = PublicPoster(url, user_agent="foundry/1.8.1")
    probe_payload = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "eth_getCode", "params": [manager, hex(args.block)]}
    ).encode()
    state_probe: dict[str, object] = {"available": False, "error": None}
    try:
        parsed = json.loads(probe_poster.post(probe_payload))
        code = parsed.get("result") if isinstance(parsed, Mapping) else None
        state_probe["available"] = isinstance(code, str) and code not in {"", "0x"}
        if not state_probe["available"]:
            error = parsed.get("error") if isinstance(parsed, Mapping) else parsed
            state_probe["error"] = (
                str(error.get("message", error)) if isinstance(error, Mapping) else str(error)
            )
    except (ValueError, TypeError, OSError) as exc:
        state_probe["error"] = str(exc)
    manifest = {
        "schema": "oasis.local-fork.v1",
        "header": asdict(header),
        "l1_block_number": l1_block or None,
        "fork_timestamp": fork_timestamp if fork_timestamp is not None else None,
        "forge_state_probe": state_probe,
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
    # Code hashes are shared by many blocks; they do not uniquely identify state.
    # Re-read the exact requested height after execution to catch a changed pin.
    postflight = rpc.get_block_snapshot(args.block)
    stable_pin = postflight == header
    (args.output / "postflight.json").write_bytes(
        canonical_json(
            {
                "header": asdict(postflight),
                "matches_preflight": stable_pin,
                "wire": [
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
    manifest.pop("wire")
    manifest["postflight_matches"] = stable_pin
    manifest["exit_code"] = result.returncode if stable_pin else 1
    manifest["preflight_sha256"] = hashlib.sha256(
        (args.output / "preflight.json").read_bytes()
    ).hexdigest()
    (args.output / "result.json").write_bytes(canonical_json(manifest))
    print(f"Local fork exit {result.returncode}; evidence: {args.output}")
    raise SystemExit(manifest["exit_code"])


if __name__ == "__main__":
    main()
