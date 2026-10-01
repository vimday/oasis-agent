"""Retain bounded OKX pool discovery from a replay-verified token universe.

Provider TVL ranks discovery requests only; it never proves executable depth,
pool access, token safety, economic edge, or release eligibility.
"""

import argparse
import json
import time
from decimal import Decimal
from pathlib import Path

from oasis_agent.models.common import canonical_json, normalize_address, sha256_hex
from scripts.capture_token_research import credentials, okx_query, replay


def select_tokens(directory: Path, limit: int) -> tuple[tuple[int, str], ...]:
    if type(limit) is not int or not 1 <= limit <= 10:
        raise ValueError("one to ten tokens per chain required")
    replay(directory)
    report = json.loads((directory / "result.json").read_bytes())
    # Query established currencies before speculative candidates so a later
    # provider outage cannot starve the benchmark comparison.
    selected = [
        (8453, "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"),
        (8453, "0x4200000000000000000000000000000000000006"),
        (4663, "0x5fc5360d0400a0fd4f2af552add042d716f1d168"),
    ]
    for capture in report["captures"]:
        if (capture["provider"], capture["kind"], capture["status"]) != (
            "okx",
            "trending",
            "captured",
        ):
            continue
        rows = json.loads((directory / (capture["raw_sha256"] + ".raw.json")).read_bytes())["data"]
        ranked = []
        for row in rows:
            liquidity = Decimal(row["liquidity"])
            if not liquidity.is_finite() or liquidity < 0:
                raise ValueError("invalid provider liquidity")
            chain = capture["chain_id"]
            if str(chain) != row["chainIndex"]:
                raise ValueError("ranking chain mismatch")
            ranked.append((liquidity, normalize_address(row["tokenContractAddress"])))
        selected.extend((chain, token) for _, token in sorted(ranked, reverse=True)[:limit])
    return tuple(dict.fromkeys(selected))


def run(source: Path, output: Path, env_file: Path, limit: int) -> None:
    selected = select_tokens(source, limit)
    creds = credentials(env_file, ("OKX_API_KEY", "OKX_SECRET_KEY", "OKX_PASSPHRASE"))
    output.mkdir(parents=True, exist_ok=False)
    records = []
    for chain, token in selected:
        record = {"chain_id": chain, "token": token, "started_at": int(time.time())}
        try:
            raw = okx_query("pools", chain, token, 5, creds)
            if any(value.encode() in raw for value in creds.values()):
                raise ValueError("sensitive response")
            body = json.loads(raw)
            if body.get("code") != "0" or not isinstance(body.get("data"), list):
                raise ValueError("provider response refused")
            digest = sha256_hex(raw)
            (output / (digest + ".raw.json")).write_bytes(raw)
            record.update(status="captured", raw_sha256=digest, pools=body["data"])
        except Exception as error:  # noqa: BLE001 - retain no credential-bearing diagnostics
            record.update(status="failed", error_type=type(error).__name__)
        record["retrieved_at"] = int(time.time())
        records.append(record)
        report = {
            "schema": "oasis.lp-pool-discovery.v1",
            "provider": "okx",
            "route": "/api/v6/dex/market/token/top-liquidity",
            "source_result_sha256": sha256_hex((source / "result.json").read_bytes()),
            "selection": "top_reported_token_liquidity_per_chain_plus_benchmark_currencies",
            "requested": selected,
            "captures": records,
            "execution_eligible": False,
            "trust": "unverified_provider_claims",
        }
        (output / "report.json").write_bytes(canonical_json(report))
        if record["status"] == "failed":
            break  # Stop on a provider failure; never hammer a rate limit.
        time.sleep(1.1)
    print(f"Retained {len(records)} pool queries; execution disabled.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--per-chain", type=int, default=5)
    args = parser.parse_args()
    try:
        run(args.source, args.output, args.env_file, args.per_chain)
    except Exception:  # noqa: BLE001 - never print provider or credential diagnostics
        parser.exit(1, "Pool discovery refused; inspect retained sanitized records.\n")


if __name__ == "__main__":
    main()
