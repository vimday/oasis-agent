"""Offline current-clock check of two retained NAV candidates; never live release."""

import argparse
import json
import os
import stat
import time
from contextlib import closing
from pathlib import Path

from oasis_agent.evidence.store import ReadOnlyEvidenceStore
from oasis_agent.live.config import LiveConfig
from oasis_agent.live.signed_nav import verify_signed_nav_quorum
from oasis_agent.live.state import Position
from oasis_agent.models.common import canonical_json, evidence_ref_from_json, sha256_hex


def read_input(path: Path) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 64_000:
            raise ValueError("bounded regular input required")
        raw = handle.read(64_001)
    if len(raw) > 64_000:
        raise ValueError("input exceeds byte bound")
    return raw


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate request field")
        result[key] = value
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    # No historical override: sample the host clock before and after replay.
    if args.out.exists() or args.out.is_symlink():
        parser.error("output already exists; preserve previous evidence")
    report = {
        "schema": "oasis.signed-nav-check.v1",
        "passed": False,
        "execution_eligible": False,
        "network_calls": 0,
        "clock_source": "host",
    }
    try:
        raw = read_input(args.input)
        data = json.loads(raw, object_pairs_hook=unique)
        if (
            set(data) != {"schema", "evidence", "sources", "position"}
            or data["schema"] != "oasis.signed-nav-check-input.v1"
            or not isinstance(data["evidence"], list)
            or len(data["evidence"]) != 2
            or not isinstance(data["sources"], list)
            or len(data["sources"]) != 2
        ):
            raise ValueError("invalid NAV check input")
        position = None if data["position"] is None else Position(**data["position"])
        c = LiveConfig.from_json(read_input(args.config))
        refs = tuple(evidence_ref_from_json(r) for r in data["evidence"])
        if args.evidence.is_symlink() or not args.evidence.is_file():
            raise ValueError("existing regular evidence database required")
        with closing(ReadOnlyEvidenceStore(args.evidence)) as store:
            result = verify_signed_nav_quorum(
                store,
                c,
                (refs[0], refs[1]),
                sources=tuple(data["sources"]),
                position=position,
                now_ms=time.time_ns() // 1_000_000,
            )
            # A slow replay must not publish a now-expired success.
            finished = time.time_ns() // 1_000_000
            if (
                finished < result["checked_at_ms"]
                or finished - result["block"]["timestamp"] * 1000 > 120_000
                or any(
                    finished > deadline["valid_until_ms"]
                    for deadline in result["feed_deadlines"].values()
                )
            ):
                raise ValueError("candidate expired or host clock regressed during replay")
        report.update(
            passed=True,
            input_hash=sha256_hex(raw),
            result=result,
            completed_at_ms=finished,
        )
    except Exception:  # noqa: BLE001 - never echo hostile input, file or database contents
        report["refusal"] = "signed NAV quorum verification refused"
    report["evidence_hash"] = sha256_hex(canonical_json(report))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("x") as handle:
        json.dump(report, handle, indent=2)
    print(json.dumps({k: report[k] for k in ("passed", "execution_eligible", "evidence_hash")}))
    raise SystemExit(0 if report["passed"] else 3)


if __name__ == "__main__":
    main()
