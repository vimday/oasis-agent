"""Offline paired operator rearm from original retained evidence; STOP stays set.

Review is the default. --apply additionally requires the exact prior review hash
and a new output directory. No keys, network, quota replenishment or start path.
"""

import argparse
import json
import os
import stat
import time
from pathlib import Path

from oasis_agent.evidence.rpc_validation import strict_json_object
from oasis_agent.live.config import LiveConfig
from oasis_agent.live.rearm import apply_pair, review_pair
from oasis_agent.live.rpc_profile import LiveRpcProfile
from oasis_agent.models.common import canonical_json, evidence_ref_from_json, sha256_hex

ROOT = Path(__file__).resolve().parents[1]


def _write(path: Path, value: object) -> None:
    with path.open("xb") as handle:
        handle.write(canonical_json(value))
        handle.flush()
        os.fsync(handle.fileno())


def _sync(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strategy-dir", type=Path, required=True)
    parser.add_argument("--authority-dir", type=Path, required=True)
    parser.add_argument("--references", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=ROOT / "configs/live.base-eth-usdc.json")
    parser.add_argument("--rpc-profile", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--approved-review-hash")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    if args.apply != bool(args.approved_review_hash) or (args.apply and args.out is None):
        parser.error("--apply requires --approved-review-hash and a new --out directory")
    result = {"execution_eligible": False, "rearm_applied": False, "status": "refused"}
    attempted = False
    output_created = False
    try:
        fd = os.open(args.references, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise ValueError("regular reference file required")
            raw = handle.read(131_073)
        if len(raw) > 131_072:
            raise ValueError("reference file exceeds bound")
        parsed = strict_json_object(raw)
        if set(parsed) != {"strategy", "authority"} or any(
            not isinstance(items, dict)
            or set(items) != {"nav", "settlements"}
            or any(not isinstance(refs, list) or len(refs) != 2 for refs in items.values())
            for items in parsed.values()
        ):
            raise ValueError("exact service NAV and settlement reference pairs required")
        refs = {
            role: {
                kind: tuple(evidence_ref_from_json(ref) for ref in pair)
                for kind, pair in items.items()
            }
            for role, items in parsed.items()
        }
        config, profile = LiveConfig.load(args.config), LiveRpcProfile.load(args.rpc_profile)
        review = review_pair(args.strategy_dir, args.authority_dir, config, profile, refs)
        if args.out:
            args.out.mkdir(mode=0o700, parents=False)
            output_created = True
        if args.apply:
            if review["review_hash"] != args.approved_review_hash:
                raise ValueError("explicit approval must name this exact review")
            _write(
                args.out / "attempt.json",
                {
                    "status": "unresolved",
                    "rearm_applied": None,
                    "execution_eligible": False,
                    "approved_review_hash": args.approved_review_hash,
                    "reference_file_hash": sha256_hex(raw),
                    "attempted_at": int(time.time()),
                },
            )
            _sync(args.out)
            _sync(args.out.parent)
            attempted = True
            result = apply_pair(
                args.strategy_dir,
                args.authority_dir,
                config,
                profile,
                refs,
                approved_review_hash=args.approved_review_hash,
            )
            result["status"] = (
                "rearmed_STOP_preserved"
                if result["projection_complete"]
                else "rearmed_projection_incomplete_STOP_preserved"
            )
        else:
            result = {**review, "rearm_applied": False, "status": "reviewed_only"}
    except Exception:  # noqa: BLE001 - fixed refusal; never echo provider/raw artifact errors
        result = {
            "execution_eligible": False,
            "rearm_applied": None if attempted else False,
            "status": "unresolved_inspect_both_original_journals" if attempted else "refused",
        }
    if output_created:
        _write(args.out / "report.json", result)
        _sync(args.out)
    print(json.dumps(result, indent=2))
    raise SystemExit(
        0 if result.get("status") in {"reviewed_only", "rearmed_STOP_preserved"} else 3
    )


if __name__ == "__main__":
    main()
