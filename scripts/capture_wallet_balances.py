"""Bounded two-source native balance capture. Public account only; no signing material."""

import argparse
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from oasis_agent.evidence.rpc_capture import BoundedExchangeRecorder
from oasis_agent.evidence.rpc_validation import rpc_quantity
from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.evidence.wallet_balance import reconstruct_native_balance
from oasis_agent.models.common import BlockRef, canonical_json, normalize_address
from oasis_agent.rpc.budget import BudgetedPoster, BudgetPolicy
from oasis_agent.rpc.transport import JsonRpcTransport, _UrlLibPoster


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", required=True)
    parser.add_argument("--chain-id", required=True, type=int)
    parser.add_argument("--primary-env", required=True)
    parser.add_argument("--secondary-env", required=True)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--budget", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--interval-ms", type=int, default=3000)
    args = parser.parse_args()
    try:
        account = normalize_address(args.account)
        if int(account, 16) == 0 or args.chain_id <= 0:
            raise ValueError("invalid identity")
        names = (args.primary_env, args.secondary_env)
        if names[0] == names[1] or any("RPC" not in name or "KEY" in name for name in names):
            raise ValueError("two explicit RPC environment names required")
        if not 3000 <= args.interval_ms <= 60000:
            raise ValueError("minimum request interval is three seconds")
        urls = {}
        with args.env_file.open() as stream:
            for line in stream:
                name, sep, value = line.strip().removeprefix("export ").partition("=")
                if sep and name.strip() in names:
                    if name.strip() in urls:
                        raise ValueError("duplicate RPC configuration")
                    urls[name.strip()] = value.strip().strip("\"'")
        if set(urls) != set(names) or urls[names[0]] == urls[names[1]]:
            raise ValueError("two configured RPC endpoints required")
        # Bind quotas to chain/environment aliases so changing a source cannot reset its budget.
        aliases = tuple(f"wallet_{args.chain_id}_{name}" for name in names)
        policies = tuple(BudgetPolicy(alias, 5, args.interval_ms) for alias in aliases)
        args.output.mkdir(parents=True, exist_ok=False)
        store = EvidenceStore(args.output / "evidence.db")
    except Exception:  # noqa: BLE001 - never print env values
        parser.exit(1, "Invalid wallet capture configuration; no signing is available.\n")
    snapshots = []
    results = []
    chosen: int | None = None
    try:
        for name, alias in zip(names, aliases, strict=True):
            recorder = BoundedExchangeRecorder()
            try:
                rpc = JsonRpcTransport(
                    "https://unused.invalid",
                    http=BudgetedPoster(
                        _UrlLibPoster(urls[name], 15), args.budget, policies, alias
                    ),
                    retries=0,
                    recorder=recorder,
                )
                if rpc_quantity(rpc.request("eth_chainId", [])) != args.chain_id:
                    raise ValueError("chain mismatch")
                if chosen is None:
                    head = rpc.request("eth_getBlockByNumber", ["latest", False])
                    if not isinstance(head, dict):
                        raise ValueError("head unavailable")
                    chosen = rpc_quantity(head.get("number")) - 3
                header = rpc.request("eth_getBlockByNumber", [hex(chosen), False])
                if not isinstance(header, dict):
                    raise ValueError("header unavailable")
                block = BlockRef(
                    args.chain_id,
                    rpc_quantity(header.get("number")),
                    header["hash"],
                    rpc_quantity(header.get("timestamp")),
                )
                assert block.timestamp is not None
                age = int(datetime.now(UTC).timestamp()) - block.timestamp
                if not -30 <= age <= 180:
                    raise ValueError("stale or future block")
                rpc.request("eth_getBalance", [account, hex(chosen)])
                rpc.request("eth_getBlockByNumber", [hex(chosen), False])
                snapshot = reconstruct_native_balance(
                    recorder.exchanges, account=account, block=block
                )
                envelopes = [
                    {
                        "encoding": "hex_exact_bytes",
                        "exchanges": [
                            {
                                "method": x.method,
                                "request_id": x.request_id,
                                "attempt": x.attempt,
                                "payload": (
                                    x.response_payload if response else x.request_payload
                                ).hex(),
                            }
                            for x in recorder.exchanges
                        ],
                    }
                    for response in (False, True)
                ]
                reference = store.put(
                    "wallet_native_snapshot",
                    snapshot.payload,
                    block,
                    alias,
                    request=envelopes[0],
                    raw_response=envelopes[1],
                    interpretation={
                        "schema": "oasis.wallet-native-snapshot.v1",
                        "execution_eligible": False,
                    },
                )
                snapshots.append(snapshot)
                results.append(
                    {"source": alias, "snapshot": asdict(snapshot), "evidence": asdict(reference)}
                )
            except Exception:  # noqa: BLE001 - RPC errors may echo URLs
                results.append({"source": alias, "status": "failed_redacted"})
    finally:
        store.close()
    agreed = len(snapshots) == 2 and snapshots[0] == snapshots[1]
    report = {
        "schema": "oasis.native-wallet-capture.v1",
        "sources": results,
        "provider_agreement": agreed,
        "execution_eligible": False,
        "unresolved": ["USD_valuation", "finality", "token_and_LP_inventory"],
    }
    (args.output / "report.json").write_bytes(canonical_json(report) + b"\n")
    print(canonical_json({"provider_agreement": agreed, "execution_eligible": False}).decode())
    if not agreed:
        parser.exit(2)


if __name__ == "__main__":
    main()
