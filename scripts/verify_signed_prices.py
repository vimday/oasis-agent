"""Verify retained RedStone price candidates offline; never authorize live NAV."""

import argparse
import json
import os
import stat
import time
from pathlib import Path

from oasis_agent.live.signed_prices import MAX_RAW_BYTES, verify_prices
from oasis_agent.models.common import canonical_json, sha256_hex


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--at-ms", type=int, help="Historical replay clock; defaults to current clock"
    )
    args = parser.parse_args()
    if args.out.exists():
        parser.error("output already exists; preserve previous evidence")
    clock = time.time_ns() // 1_000_000 if args.at_ms is None else args.at_ms
    report: dict[str, object] = {
        "schema": "oasis.signed-price-verification.v1",
        "passed": False,
        "execution_eligible": False,
        "network_calls": 0,
        "clock_source": "host" if args.at_ms is None else "operator_historical",
        "observed_at_ms": clock,
    }
    try:
        fd = os.open(args.input, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_RAW_BYTES:
                raise ValueError("bounded regular price input required")
            raw = handle.read(MAX_RAW_BYTES + 1)
        if len(raw) > MAX_RAW_BYTES:
            raise ValueError("price candidate exceeds byte bound")
        report["input_hash"] = sha256_hex(raw)
        report["result"] = verify_prices(raw, observed_at_ms=clock)
        report["passed"] = True
    except Exception:  # noqa: BLE001 - no arbitrary file/package data in diagnostics
        report["refusal"] = "signed price candidate verification refused"
    report["evidence_hash"] = sha256_hex(canonical_json(report))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("x") as handle:
        json.dump(report, handle, indent=2)
    print(json.dumps({k: report[k] for k in ("passed", "execution_eligible", "evidence_hash")}))
    raise SystemExit(0 if report["passed"] else 3)


if __name__ == "__main__":
    main()
