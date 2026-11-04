"""Explicit offline two-service strict-feed migration; keeps STOP and HALT in place.

Default is read-only evidence review. --apply requires a new attempt directory,
both original service locks/STOP files and fresh retained raw NAV in both stores.
No RPC, budget creation, key, signature, broadcast or service start is supported.
"""

import argparse
import json
import os
import stat
import time
from contextlib import ExitStack, closing
from pathlib import Path

from oasis_agent.evidence.rpc_validation import strict_json_object
from oasis_agent.evidence.store import ReadOnlyEvidenceStore
from oasis_agent.live.config import LiveConfig
from oasis_agent.live.feed_transition import apply_pair
from oasis_agent.live.state import StateStore
from oasis_agent.models.common import canonical_json, evidence_ref_from_json, sha256_hex
from scripts.inspect_live_state import inspect
from scripts.review_live_feed_transition import review

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("strategy", "authority"):
        parser.add_argument(f"--{name}-state", type=Path, required=True)
        parser.add_argument(f"--{name}-evidence", type=Path, required=True)
    parser.add_argument("--fresh-nav", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=ROOT / "configs/live.base-eth-usdc.json")
    parser.add_argument("--sources", nargs=2, default=["live_primary", "live_secondary"])
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    if args.apply and args.out is None:
        parser.error("--apply requires a new --out directory")
    result = {"execution_eligible": False, "migration_applied": False, "status": "refused"}
    attempted = False
    try:
        fd = os.open(args.fresh_nav, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise ValueError("regular reference file required")
            raw = handle.read(65_537)
        if len(raw) > 65_536:
            raise ValueError("reference file exceeds bound")
        fresh = strict_json_object(raw)
        if set(fresh) != {"strategy", "authority"} or any(
            not isinstance(refs, list) or len(refs) != 2 for refs in fresh.values()
        ):
            raise ValueError("two exact per-service reference pairs required")
        config = LiveConfig.load(args.config)
        evidence_paths = {"strategy": args.strategy_evidence, "authority": args.authority_evidence}
        sources = tuple(args.sources)
        if not args.apply:
            result = review(
                args.strategy_state,
                args.authority_state,
                evidence_paths,
                config,
                fresh,
                sources,
                int(time.time()),
            )
        else:
            scope = inspect(args.strategy_state, args.authority_state, config)
            args.out.mkdir(mode=0o700, parents=False)
            with (args.out / "attempt.json").open("xb") as handle:
                handle.write(
                    canonical_json(
                        {
                            **result,
                            "status": "unresolved",
                            "migration_applied": None,
                            "attempted_at": int(time.time()),
                            "target_config_hash": scope["expected_config_hash"],
                            "services": scope["services"],
                            "reference_file_hash": sha256_hex(raw),
                        }
                    )
                )
                handle.flush()
                os.fsync(handle.fileno())
            attempted = True
            # Persist both the marker and its newly created directory entry before
            # either original journal can change, including across a power loss.
            for directory in (args.out, args.out.parent):
                directory_fd = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            with ExitStack() as resources:
                stores = {
                    name: resources.enter_context(closing(ReadOnlyEvidenceStore(path)))
                    for name, path in evidence_paths.items()
                }
                result = apply_pair(
                    StateStore(args.strategy_state),
                    StateStore(args.authority_state),
                    config,
                    stores,
                    {
                        name: tuple(evidence_ref_from_json(r) for r in refs)
                        for name, refs in fresh.items()
                    },
                    sources=sources,
                    expected_tips={
                        name: state["journal_tip"] for name, state in scope["services"].items()
                    },
                )
                result["status"] = "both_migrated_halted"
    except Exception:  # noqa: BLE001 - preserve original artifacts; fixed redacted outcome
        result = {
            "execution_eligible": False,
            "migration_applied": None if attempted else False,
            "status": "unresolved_inspect_both_journals" if attempted else "refused",
        }
    if attempted:
        result["evidence_hash"] = sha256_hex(canonical_json(result))
        with (args.out / "report.json").open("x") as handle:
            json.dump(result, handle, indent=2)
    print(json.dumps(result, indent=2))
    raise SystemExit(
        0
        if result.get("evidence_review_passed") or result.get("status") == "both_migrated_halted"
        else 3
    )


if __name__ == "__main__":
    main()
