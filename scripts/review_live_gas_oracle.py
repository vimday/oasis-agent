"""Offline review of two retained gas-oracle wire captures; never acquires RPC.

Writes only a new review directory. Its candidate is a proposal, not an approved
configuration, journal migration, service start or execution permit.
"""

import argparse
import json
import os
import stat
import time
from contextlib import closing
from pathlib import Path

from oasis_agent.evidence.rpc_validation import restore_exchanges, strict_json_object
from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.live.config import LiveConfig
from oasis_agent.live.gas_oracle_evidence import (
    candidate_configuration,
    retain_discovery_quorum,
)
from oasis_agent.models.common import BlockRef, canonical_json, sha256_hex

ROOT = Path(__file__).resolve().parents[1]


def _read(path: Path, maximum: int) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError("bounded regular input file required")
        raw = handle.read(maximum + 1)
    if len(raw) > maximum:
        raise ValueError("input exceeds byte bound")
    return raw


def review(wire: Path, config: Path, out: Path, *, sources: tuple[str, str], now: int) -> dict:
    # Exclusive creation preserves previous failures and prevents replacing an
    # original evidence store. No path naming an existing directory is accepted.
    out.mkdir(mode=0o700, parents=False)
    report = {
        "schema": "oasis.live-gas-oracle-review.v1",
        "status": "refused",
        "execution_eligible": False,
        "runtime_approved": False,
        "configuration_applied": False,
        "rpc_requests": 0,
        "wire_file_hash": None,
        "config_file_hash": None,
    }
    try:
        raw = _read(wire, 8_000_000)
        config_raw = _read(config, 65_536)
        report["wire_file_hash"] = sha256_hex(raw)
        report["config_file_hash"] = sha256_hex(config_raw)
        scope = strict_json_object(raw)
        if set(scope) != {"block", "providers"} or not isinstance(scope["block"], dict):
            raise ValueError("exact block and provider scope required")
        block = BlockRef(**scope["block"])
        providers = scope["providers"]
        if (
            not isinstance(providers, list)
            or len(providers) != 2
            or any(
                not isinstance(p, dict) or set(p) != {"source", "request", "response"}
                for p in providers
            )
            or tuple(p["source"] for p in providers) != sources
        ):
            raise ValueError("two declared exact provider captures required")
        captures = [
            restore_exchanges(canonical_json(p["request"]), canonical_json(p["response"]))
            for p in providers
        ]
        policy = LiveConfig.from_json(config_raw)
        with closing(EvidenceStore(out / "evidence.db")) as store:
            refs = retain_discovery_quorum(
                store, block, (captures[0], captures[1]), sources=sources
            )
            candidate = candidate_configuration(
                store, refs, block, policy, sources=sources, now=now
            )
        candidate_hash = sha256_hex(canonical_json(candidate))
        with (out / "candidate.json").open("xb") as handle:
            handle.write(canonical_json(candidate))
        report.update(status="candidate_requires_review", candidate_hash=candidate_hash)
    except Exception:  # noqa: BLE001 - fixed refusal, never echo provider/file content
        report["reason"] = "retained_gas_oracle_review_unresolved"
    report["evidence_hash"] = sha256_hex(canonical_json(report))
    with (out / "report.json").open("x") as handle:
        json.dump(report, handle, indent=2)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wire", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--config", type=Path, default=ROOT / "configs/live.base-eth-usdc.json")
    parser.add_argument("--sources", nargs=2, default=["live_primary", "live_secondary"])
    args = parser.parse_args()
    try:
        report = review(
            args.wire, args.config, args.out, sources=tuple(args.sources), now=int(time.time())
        )
    except OSError, ValueError:
        parser.exit(3, "Review setup refused; previous evidence preserved.\n")
    print(json.dumps(report, indent=2))
    raise SystemExit(0 if report["status"] == "candidate_requires_review" else 3)


if __name__ == "__main__":
    main()
