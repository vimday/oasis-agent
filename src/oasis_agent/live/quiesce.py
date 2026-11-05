"""Review and explicitly latch missing maintenance halts; never unhalt or trade.

Each needed journal append is independently atomic. Partial completion only
removes authority; STOP files and JSON projections remain untouched. A failed
attempt must be inspected, never automatically retried.
"""

import json
import os
import sqlite3
import stat
from collections.abc import Callable, Iterator
from contextlib import ExitStack, closing, contextmanager
from pathlib import Path
from typing import Any

from ..models.common import canonical_json, sha256_hex
from .config import LiveConfig
from .config_epochs import retained_config
from .feed_transition import _service_lock
from .journal import MAX_SNAPSHOT, LiveJournal
from .startup_state import Role, paths
from .state import StateStore

MAINTENANCE_HALT = "operator maintenance stop; runtime transition review required"
ROLES: tuple[Role, Role] = ("strategy", "authority")
Scope = tuple[dict[Role, StateStore], dict[Path, tuple[int, ...]], set[Path], Callable[[], None]]


def _signature(path: Path) -> tuple[int, ...]:
    info = path.lstat()
    # Existing lock files carry no state and may be readable inside their private
    # directory. Their identity/ownership and lack of other-writer permission matter.
    forbidden = 0o022 if path.name in {"live.lock", "authority.lock"} else 0o077
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & forbidden
        or info.st_nlink != 1
        or info.st_size > 64_000_000
    ):
        raise ValueError("private owned original file required")
    return tuple(
        getattr(info, key)
        for key in (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_uid",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
    )


@contextmanager
def _scope(strategy: Path, authority: Path, config: LiveConfig) -> Iterator[Scope]:
    config.validate()
    directories: dict[Role, Path] = dict(zip(ROLES, (strategy, authority), strict=True))
    frozen_dirs: dict[Path, tuple[int, ...]] = {}
    files: list[Path] = []
    stores: dict[Role, StateStore] = {}
    checks: list[Callable[[], None]] = []
    with ExitStack() as stack:
        for role, root in directories.items():
            info = root.lstat()
            if (
                root.resolve() != root.absolute()
                or not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
            ):
                raise ValueError("private original service directory required")
            frozen_dirs[root] = (info.st_dev, info.st_ino, info.st_mode, info.st_uid)
            state, _, lock = paths(root, role)
            store = StateStore(state, require_existing=True)
            stores[role] = store
            files.extend((state, store.journal_path, lock, root / "STOP"))
            checks.append(stack.enter_context(_service_lock(store, root / "STOP", role)))
        frozen = {path: _signature(path) for path in files}
        if len(frozen) != 8 or len({value[:2] for value in frozen.values()}) != 8:
            raise ValueError("distinct original files required")
        changed: set[Path] = set()

        def verify() -> None:
            for check in checks:
                check()
            for root, identity in frozen_dirs.items():
                info = root.lstat()
                if (info.st_dev, info.st_ino, info.st_mode, info.st_uid) != identity:
                    raise ValueError("original directory changed")
            for path, expected in frozen.items():
                actual = _signature(path)
                if actual[:4] != expected[:4] or (path not in changed and actual != expected):
                    raise ValueError("original files changed")
            for store in stores.values():
                if any(
                    os.path.lexists(str(store.journal_path) + suffix)
                    for suffix in ("-wal", "-shm", "-journal")
                ):
                    raise ValueError("journal recovery artifacts require inspection")

        verify()
        for store in stores.values():
            fd = os.open(store.journal_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as handle:
                header = handle.read(20)
            if header[:16] != b"SQLite format 3\x00" or header[18:20] != b"\x01\x01":
                raise ValueError("original DELETE journal required; no automatic conversion")
        yield stores, frozen, changed, verify


def _snapshot(store: StateStore, db: sqlite3.Connection) -> tuple[str, bytes, str]:
    journal = LiveJournal(store.journal_path, require_existing=True)
    tip, payload = journal._read(db)
    if payload is None or journal.config_hash is None:
        raise ValueError("nonempty original journal required")
    store._decode(payload, "live")
    fd = os.open(store.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        projection = handle.read(MAX_SNAPSHOT + 1)
    if len(projection) > MAX_SNAPSHOT:
        raise ValueError("bounded original projection required")
    store._decode(projection, "live")
    if (
        db.execute(
            "SELECT 1 FROM events WHERE payload=? LIMIT 1",
            (canonical_json(json.loads(projection)),),
        ).fetchone()
        is None
    ):
        raise ValueError("projection must belong to its original journal")
    return tip, payload, journal.config_hash


def _target(payload: bytes) -> tuple[bool, bytes]:
    raw = json.loads(payload)
    if raw.get("halted") is not None and not isinstance(raw["halted"], str):
        raise ValueError("typed original halt required")
    needs_halt = not raw.get("halted")
    if needs_halt:
        raw["halted"] = MAINTENANCE_HALT
    target = canonical_json(raw)
    if len(target) > MAX_SNAPSHOT:
        raise ValueError("halt snapshot exceeds byte bound")
    StateStore._decode(target, "live")
    return needs_halt, target


def _review(
    stores: dict[Role, StateStore], frozen: dict[Path, tuple[int, ...]], config: LiveConfig
) -> dict[str, Any]:
    services = {}
    for role, store in stores.items():
        with closing(
            sqlite3.connect(
                store.journal_path.resolve().as_uri() + "?mode=ro&immutable=1",
                uri=True,
                isolation_level=None,
            )
        ) as db:
            db.execute("BEGIN")
            tip, payload, binding = _snapshot(store, db)
        retained_config(config, binding)
        needs, target = _target(payload)
        services[role] = {
            "directory": str(store.path.parent),
            "source_tip": tip,
            "config_hash": binding,
            "source_state_hash": sha256_hex(payload),
            "target_state_hash": sha256_hex(target),
            "needs_halt": needs,
            "existing_halt_preserved": not needs,
        }
    report = {
        "schema": "oasis.live-quiesce-plan.v1",
        "execution_eligible": False,
        "rpc_requests": 0,
        "status": "proposal_not_authorization",
        "atomic_pair": False,
        "mutation": "append_missing_maintenance_halt_only",
        "maintenance_halt": MAINTENANCE_HALT,
        "json_projections_written": False,
        "stop_preserved": True,
        "services": services,
        "original_files": {str(path): list(value) for path, value in frozen.items()},
        "limitations": [
            "individually_atomic_risk_reduction",
            "partial_completion_requires_inspection",
            "not_migration_rearm_start_or_rpc_authorization",
        ],
    }
    report["plan_hash"] = sha256_hex(canonical_json(report))
    return report


def review_pair(strategy: Path, authority: Path, config: LiveConfig) -> dict[str, Any]:
    with _scope(strategy, authority, config) as (stores, frozen, _, verify):
        report = _review(stores, frozen, config)
        verify()
        return report


def apply_pair(
    strategy: Path, authority: Path, config: LiveConfig, approved: dict[str, Any], digest: str
) -> dict[str, Any]:
    with _scope(strategy, authority, config) as (stores, frozen, changed, verify):
        report = _review(stores, frozen, config)
        if report["plan_hash"] != digest or canonical_json(report) != canonical_json(approved):
            raise ValueError("exact reviewed original quiesce plan required")
        outcomes = {}
        for role, store in stores.items():
            source = report["services"][role]
            verify()
            if not source["needs_halt"]:
                outcomes[role] = {"appended": False, "journal_tip": source["source_tip"]}
                continue
            with closing(
                sqlite3.connect(
                    store.journal_path.resolve().as_uri() + "?mode=rw",
                    uri=True,
                    isolation_level=None,
                    timeout=5,
                )
            ) as db:
                db.execute("PRAGMA synchronous=FULL")
                db.execute("BEGIN IMMEDIATE")
                try:
                    if db.execute("PRAGMA journal_mode").fetchone() != ("delete",):
                        raise ValueError("locked DELETE mode required; no automatic conversion")
                    if db.execute("PRAGMA synchronous").fetchone() != (2,):
                        raise ValueError("locked FULL durability required")
                    tip, payload, binding = _snapshot(store, db)
                    needs, target = _target(payload)
                    if (
                        not needs
                        or tip != source["source_tip"]
                        or binding != source["config_hash"]
                        or sha256_hex(payload) != source["source_state_hash"]
                        or sha256_hex(target) != source["target_state_hash"]
                    ):
                        raise ValueError("original quiesce checkpoint changed")
                    seq = db.execute("SELECT COUNT(*) FROM events").fetchone()[0] + 1
                    parent = sha256_hex(
                        canonical_json(
                            {
                                "seq": seq,
                                "config": binding,
                                "parent": tip,
                                "state_hash": sha256_hex(target),
                            }
                        )
                    )
                    db.execute(
                        "INSERT INTO events VALUES (?, ?, ?, ?, ?)",
                        (seq, binding, tip, parent, target),
                    )
                    # The current transaction may have created its own rollback journal.
                    for check_path, expected in frozen.items():
                        actual = _signature(check_path)
                        if actual[:4] != expected[:4] or (
                            check_path not in changed
                            and check_path != store.journal_path
                            and actual != expected
                        ):
                            raise ValueError("original files changed before halt commit")
                    db.execute("COMMIT")
                except BaseException:
                    if db.in_transaction:
                        db.execute("ROLLBACK")
                    raise
            changed.add(store.journal_path)
            outcomes[role] = {"appended": True, "journal_tip": parent}
        verify()
        return {
            "schema": "oasis.live-quiesce-result.v1",
            "quiesce_applied": True,
            "execution_eligible": False,
            "rpc_requests": 0,
            "atomic_pair": False,
            "json_projections_written": False,
            "stop_preserved": True,
            "approved_plan_hash": digest,
            "services": outcomes,
        }
