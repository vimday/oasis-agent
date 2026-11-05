"""Review missing maintenance HALTs; application requires an exact approved plan.

Only original journal snapshots may be appended. STOP, projections, evidence,
budgets, configuration and financial fields stay unchanged. No keys or network.
"""

import argparse
import json
import os
import stat
from pathlib import Path

from oasis_agent.evidence.rpc_validation import strict_json_object
from oasis_agent.live.config import LiveConfig
from oasis_agent.live.quiesce import apply_pair, review_pair
from oasis_agent.models.common import canonical_json, sha256_hex

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strategy-dir", type=Path, required=True)
    parser.add_argument("--authority-dir", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=ROOT / "configs/live.base-eth-usdc.json")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--plan-file", type=Path)
    parser.add_argument("--approved-plan-hash")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    if args.apply != all(
        value is not None for value in (args.plan_file, args.approved_plan_hash, args.out)
    ) or (
        not args.apply
        and any(value is not None for value in (args.plan_file, args.approved_plan_hash, args.out))
    ):
        parser.error("--apply requires --plan-file, --approved-plan-hash and a new --out")
    attempted = False
    output_created = False
    try:
        config = LiveConfig.load(args.config)
        if args.apply:
            fd = os.open(args.plan_file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as handle:
                if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                    raise ValueError("regular retained approval plan required")
                raw = handle.read(32_769)
            if len(raw) > 32_768:
                raise ValueError("bounded retained plan required")
            approved = strict_json_object(raw)
            current = review_pair(args.strategy_dir, args.authority_dir, config)
            if current["plan_hash"] != args.approved_plan_hash or canonical_json(
                current
            ) != canonical_json(approved):
                raise ValueError("exact reviewed quiesce approval required")
            for root in (args.strategy_dir, args.authority_dir):
                if args.out.resolve().is_relative_to(root.resolve()):
                    raise ValueError("attempt output must be outside original state directories")
            args.out.mkdir(mode=0o700, parents=False)
            output_created = True
            with (args.out / "attempt.json").open("xb") as handle:
                handle.write(
                    canonical_json(
                        {
                            "status": "unresolved",
                            "quiesce_applied": None,
                            "approved_plan_hash": args.approved_plan_hash,
                            "execution_eligible": False,
                        }
                    )
                )
                handle.flush()
                os.fsync(handle.fileno())
            for root in (args.out, args.out.parent):
                fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            attempted = True
            result = apply_pair(
                args.strategy_dir, args.authority_dir, config, approved, args.approved_plan_hash
            )
        else:
            result = review_pair(args.strategy_dir, args.authority_dir, config)
        code = 0
    except Exception:  # noqa: BLE001 - no raw state, halt reason or local diagnostics
        result = {
            "schema": "oasis.live-quiesce-result.v1",
            "execution_eligible": False,
            "rpc_requests": 0,
            "quiesce_applied": None if attempted else False,
            "status": "unresolved_inspect_originals_no_retry" if attempted else "refused",
        }
        code = 3
    if output_created:
        result["evidence_hash"] = sha256_hex(canonical_json(result))
        with (args.out / "report.json").open("x") as handle:
            json.dump(result, handle, indent=2)
    print(json.dumps(result, indent=2))
    raise SystemExit(code)


if __name__ == "__main__":
    main()
