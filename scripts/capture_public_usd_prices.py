"""One credential-free Kraken/Bitstamp price observation; no RPC or live activation."""

import argparse
import json
import time
from pathlib import Path

from oasis_agent.live.config import LiveConfig
from oasis_agent.live.public_usd_prices import (
    POLICY,
    URLS,
    fetch_public_prices,
    verify_public_price_bundle,
)
from oasis_agent.models.common import canonical_json, sha256_hex

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "configs/live.base-eth-usdc.json")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(mode=0o700)
    report = {
        "schema": "oasis.public-usd-acquisition.v1",
        "price_policy": POLICY,
        "passed": False,
        "attempts": [],
        "request_limit": 4,
        "rpc_requests": 0,
        "execution_eligible": False,
        "clock_source": "host_price_asof_not_a_chain_block",
    }
    try:
        config = LiveConfig.load(args.config)
        asof = time.time_ns() // 1_000_000
        raw = fetch_public_prices(on_request=report["attempts"].append)
        observed = time.time_ns() // 1_000_000
        (args.out / "prices.json").write_bytes(raw)
        report.update(raw_sha256=sha256_hex(raw), as_of_ms=asof, observed_at_ms=observed)
        result = verify_public_price_bundle(raw, config, as_of_ms=asof, now_ms=observed)
        report.update(passed=True, result=result, endpoints=URLS)
    except Exception:  # noqa: BLE001 - retain fixed categories, no arbitrary diagnostics
        report["refusal"] = "public_usd_price_acquisition_or_verification_refused"
    report["evidence_hash"] = sha256_hex(canonical_json(report))
    with (args.out / "report.json").open("xb") as handle:
        handle.write(canonical_json(report))
    print(json.dumps(report))
    return 0 if report["passed"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
