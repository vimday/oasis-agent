"""Explicit offline paired runtime epoch; default is read-only preflight.

--apply requires the exact approved retained review, a new attempt directory and
both original stopped services. It preserves HALT, history and budgets. It never
starts a service, reads a key, acquires RPC, signs or broadcasts.
"""

import argparse
import json
import os
import sqlite3
import time
from contextlib import ExitStack, closing
from dataclasses import asdict
from pathlib import Path

from oasis_agent.evidence.rpc_validation import strict_digest, strict_json_object
from oasis_agent.live.config import LiveConfig
from oasis_agent.live.feed_transition import _service_lock
from oasis_agent.live.journal import epoch_deadline, require_paired_rollback_transaction
from oasis_agent.live.runtime_epoch import SCHEMA
from oasis_agent.live.startup_state import paths
from oasis_agent.live.state import StateStore
from oasis_agent.models.common import canonical_json, sha256_hex
from scripts.review_live_gas_oracle import _read
from scripts.review_live_runtime_transition import _signature, review

ROLES = ("strategy", "authority")


def apply_pair(strategy, authority, target, references, gas_evidence, retained, approved_hash):
    """Re-derive the exact approved preflight, lock originals, then commit once.

    The retained reviewed_at is replayed for digest equality. Current time is
    independently checked before locking, staging and commit. Approval of a
    preflight authorizes this narrow migration, never trading or a source audit.
    """
    strict_digest(approved_hash, "approved runtime review")
    if retained["evidence_hash"] != approved_hash:
        raise ValueError("exact retained review approval required")
    directories = dict(zip(ROLES, (strategy, authority), strict=True))
    stores = {
        name: StateStore(paths(root, name)[0], require_existing=True)
        for name, root in directories.items()
    }
    files = [gas_evidence]
    journals = set()
    for name, root in directories.items():
        state, evidence, lock = paths(root, name)
        journal = stores[name].journal_path
        files.extend(
            [state, evidence, lock, journal, root / "STOP", root / "live.receipt-budget.db"]
        )
        journals.add(journal)
    frozen = {path: _signature(path) for path in files}
    directory_ids = {
        root: (root.stat().st_dev, root.stat().st_ino, root.stat().st_mode, root.stat().st_uid)
        for root in directories.values()
    }
    if len(frozen) != len(files) or len({v[:2] for v in frozen.values()}) != len(files):
        raise ValueError("distinct original runtime artifacts required")
    if len({frozen[path][0] for path in journals}) != 1:
        raise ValueError("paired runtime migration requires one journal filesystem")
    # Refuse WAL before any SQLite reader can create recovery sidecars. The
    # persistent SQLite read/write versions are both 1 for rollback journals.
    for path in journals:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            identity = tuple(
                getattr(info, field)
                for field in (
                    "st_dev",
                    "st_ino",
                    "st_mode",
                    "st_uid",
                    "st_size",
                    "st_mtime_ns",
                    "st_ctime_ns",
                )
            )
            if identity != frozen[path]:
                raise ValueError("original journal changed before format inspection")
            header = handle.read(20)
        if header[:16] != b"SQLite format 3\x00" or header[18:20] != b"\x01\x01":
            raise ValueError("original DELETE rollback journal format required")
    guards = []

    def unchanged(*, staging=False):
        for check in guards:
            check()
        for root, identity in directory_ids.items():
            info = root.stat()
            if (info.st_dev, info.st_ino, info.st_mode, info.st_uid) != identity:
                raise ValueError("original runtime directory changed")
        for path, identity in frozen.items():
            current = _signature(path)
            if current[:4] != identity[:4] or (
                (path not in journals or not staging) and current != identity
            ):
                raise ValueError("original runtime proof or state changed")
        if not staging and any(
            os.path.lexists(str(path) + suffix)
            for path in journals
            for suffix in ("-wal", "-shm", "-journal")
        ):
            raise ValueError("journal recovery sidecars require inspection")
        if not retained["block"]["timestamp"] <= int(time.time()) <= retained["valid_until"]:
            raise ValueError("runtime migration review expired")

    unchanged()
    actual = review(
        strategy, authority, target, references, gas_evidence, now=retained["reviewed_at"]
    )
    if (
        canonical_json(actual) != canonical_json(retained)
        or actual["evidence_hash"] != approved_hash
    ):
        raise ValueError("approved preflight differs from raw proof replay")
    target_hash = sha256_hex(canonical_json(asdict(target)))
    reference_hash = sha256_hex(canonical_json(references))
    with ExitStack() as locks:
        guards.extend(
            locks.enter_context(_service_lock(stores[name], directories[name] / "STOP", name))
            for name in ROLES
        )
        unchanged()
        records, tips = {}, {}
        for name, store in stores.items():
            state = store.load("live", read_only=True)
            journal = store.journal
            service = actual["services"][name]
            if journal is None or (
                journal.config_hash,
                journal.revision,
                sha256_hex(canonical_json(asdict(state))),
            ) != (service["source_config_hash"], service["journal_tip"], service["state_hash"]):
                raise ValueError("original runtime journal differs from approved preflight")
            tips[name] = journal.revision
            records[name] = {
                "schema": SCHEMA,
                "source_tip": journal.revision,
                "source_state_hash": service["state_hash"],
                "target_config": asdict(target),
                "service": name,
                "references": references,
                "review": actual,
                "approved_review_hash": approved_hash,
            }
        record_hashes = {
            name: sha256_hex(canonical_json(record)) for name, record in records.items()
        }
        staged = {}
        main = stores["strategy"].journal_path.resolve().as_uri() + "?mode=rw"
        peer = stores["authority"].journal_path.resolve().as_uri() + "?mode=rw"
        unchanged()
        with closing(sqlite3.connect(main, uri=True, timeout=5, isolation_level=None)) as db:
            db.execute("ATTACH DATABASE ? AS peer", (peer,))
            for schema in ("main", "peer"):
                if db.execute(f"PRAGMA {schema}.journal_mode").fetchone() != ("delete",):
                    raise ValueError("original DELETE journal mode required")
                db.execute(f"PRAGMA {schema}.synchronous=FULL")
                if db.execute(f"PRAGMA {schema}.synchronous").fetchone() != (2,):
                    raise ValueError("FULL paired durability required")
            db.execute("BEGIN IMMEDIATE")
            try:
                require_paired_rollback_transaction(db)
                unchanged()
                for name, schema in zip(ROLES, ("main", "peer"), strict=True):
                    unchanged(staging=True)
                    staged[name] = stores[name].journal._transition_in_transaction(
                        db, records[name], schema=schema
                    )
                require_paired_rollback_transaction(db)
                unchanged(staging=True)
                if target_hash != sha256_hex(
                    canonical_json(asdict(target))
                ) or reference_hash != sha256_hex(canonical_json(references)):
                    raise ValueError("runtime migration inputs changed")
                if (
                    canonical_json(actual) != canonical_json(retained)
                    or actual["evidence_hash"] != approved_hash
                ):
                    raise ValueError("runtime migration approval changed")
                if any(
                    sha256_hex(canonical_json(record)) != record_hashes[name]
                    for name, record in records.items()
                ):
                    raise ValueError("staged runtime epoch record changed")
                for record in records.values():
                    start, end = epoch_deadline(record)
                    if not start <= int(time.time()) <= end:
                        raise ValueError("paired runtime migration expired before commit")
                db.execute("COMMIT")
            except BaseException:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise
        services = {}
        for name, store in stores.items():
            binding, tip, payload = staged[name]
            store.journal.config_hash, store.journal.revision = binding, tip
            services[name] = {
                "source_tip": tips[name],
                "journal_tip": tip,
                "transition_hash": sha256_hex(canonical_json(records[name])),
                "state_hash": sha256_hex(payload),
                "halt_preserved": True,
            }
        complete = True
        for name, store in stores.items():
            try:
                state = store.load("live", read_only=True)
                if store.journal.revision != staged[name][1] or not state.halted:
                    raise ValueError("committed runtime epoch changed before projection")
                store.save(state)
            except (OSError, ValueError, sqlite3.Error):
                complete = False
        return {
            "execution_eligible": False,
            "runtime_approved": False,
            "migration_applied": True,
            "atomic_pair": True,
            "projection_complete": complete,
            "approved_review_hash": approved_hash,
            "services": services,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ROLES:
        parser.add_argument(f"--{name}-dir", type=Path, required=True)
    for name in ("config", "references", "gas-evidence"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--review", type=Path)
    parser.add_argument("--approved-review-hash")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    approval = (args.review, args.approved_review_hash, args.out)
    if args.apply != all(value is not None for value in approval) or (
        not args.apply and any(value is not None for value in approval)
    ):
        parser.error(
            "--apply requires --review, --approved-review-hash and fresh --out; review accepts none"
        )
    attempted = False
    try:
        target = LiveConfig.from_json(_read(args.config, 65_536))
        references = strict_json_object(_read(args.references, 32_768))
        if not args.apply:
            result = review(
                args.strategy_dir,
                args.authority_dir,
                target,
                references,
                args.gas_evidence,
                now=int(time.time()),
            )
        else:
            retained = strict_json_object(_read(args.review, 65_536))
            if args.out.resolve() != args.out.absolute() or any(
                args.out.resolve().is_relative_to(root.resolve())
                for root in (args.strategy_dir, args.authority_dir)
            ):
                raise ValueError("fresh canonical attempt outside original volumes required")
            args.out.mkdir(mode=0o700, parents=False)
            with (args.out / "attempt.json").open("xb") as handle:
                handle.write(
                    canonical_json(
                        {
                            "execution_eligible": False,
                            "migration_applied": None,
                            "status": "unresolved",
                            "approved_review_hash": args.approved_review_hash,
                            "target_config_hash": sha256_hex(canonical_json(asdict(target))),
                            "references_hash": sha256_hex(canonical_json(references)),
                            "attempted_at": int(time.time()),
                        }
                    )
                )
                handle.flush()
                os.fsync(handle.fileno())
            for directory in (args.out, args.out.parent):
                fd = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            attempted = True
            result = apply_pair(
                args.strategy_dir,
                args.authority_dir,
                target,
                references,
                args.gas_evidence,
                retained,
                args.approved_review_hash,
            )
            result["status"] = (
                "both_migrated_halted"
                if result["projection_complete"]
                else "both_migrated_halted_projection_incomplete"
            )
    except Exception:  # noqa: BLE001 - preserve originals; no provider/private-state diagnostics
        result = {
            "execution_eligible": False,
            "runtime_approved": False,
            "migration_applied": None if attempted else False,
            "status": "unresolved_inspect_both_journals" if attempted else "refused",
        }
    if attempted:
        result["evidence_hash"] = sha256_hex(canonical_json(result))
        with (args.out / "report.json").open("x") as handle:
            json.dump(result, handle, indent=2)
    print(json.dumps(result, indent=2))
    raise SystemExit(
        0 if result.get("review_passed") or result.get("status") == "both_migrated_halted" else 3
    )


if __name__ == "__main__":
    main()
