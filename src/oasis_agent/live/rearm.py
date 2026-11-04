"""Explicit evidence-constrained paired rearm, entirely offline and keyless.

STOP files stay in place. This does not start services, approve a transaction,
clear arbitrary safety halts, acquire evidence or replenish an RPC budget.
"""

import os
import sqlite3
import stat
import time
from collections.abc import Iterator
from contextlib import ExitStack, closing, contextmanager
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ..evidence.rpc_validation import strict_json_object
from ..evidence.store import ReadOnlyEvidenceStore
from ..models.common import EvidenceRef, canonical_json, sha256_hex
from ..rpc.budget import RpcBudget
from .config import LiveConfig
from .feed_epoch import HALT
from .journal import MAX_SNAPSHOT, JournalConflict, LiveJournal
from .rearm_history import replay_history
from .rearm_settlements import require_current_settlements
from .receipts import RetainedReceiptCostReader
from .rpc_profile import LiveRpcProfile
from .runner import MAX_CONSECUTIVE_REVERTS, LiveLoop
from .startup_state import (
    SOURCES,
    Role,
    _no_network,
    _position_identity,
    acquire_existing_lock,
    paths,
    require_file,
)
from .state import LoopState, StateStore, day_of, validate_state
from .valuation import nav_fresh_until, replay_nav, replay_nav_quorum

ALLOWED_HALTS = frozenset(
    {HALT, "live observation refused; operator review required before polling"}
)
ROLES: tuple[Role, Role] = ("strategy", "authority")
SCHEMAS = ("main", "peer")
type References = dict[Role, dict[str, tuple[EvidenceRef, ...]]]


def _clock() -> int:
    return int(time.time())


def _config(config: LiveConfig) -> str:
    config.validate()
    if (
        config.oracle_max_age_seconds,
        config.usdc_oracle_max_age_seconds,
        config.max_capital_usd,
        config.max_daily_loss_usd,
        config.max_cumulative_loss_usd,
    ) != (1800, 1800, 500, 200, 500):
        raise ValueError("rearm requires exact owner feed and risk limits")
    return sha256_hex(canonical_json(asdict(config)))


def _identity(path: Path) -> tuple[int, int]:
    info = require_file(path)
    return info.st_dev, info.st_ino


def _stop_fingerprint(path: Path) -> tuple[int, ...]:
    info = require_file(path)
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_uid,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


class _Scope:
    def __init__(self) -> None:
        self.stores: dict[Role, StateStore] = {}
        self.evidence: dict[Role, ReadOnlyEvidenceStore] = {}
        self.journals: dict[Role, LiveJournal] = {}
        self.identities: dict[Path, tuple[int, int]] = {}
        self.budget_counts: dict[Role, dict[str, int]] = {}
        self.stop_fingerprints: dict[Path, tuple[int, ...]] = {}

    def verify_files(self) -> None:
        for path, identity in self.identities.items():
            if _identity(path) != identity:
                raise ValueError("original service artifact or STOP replaced")
        for path, fingerprint in self.stop_fingerprints.items():
            if _stop_fingerprint(path) != fingerprint:
                raise ValueError("original STOP changed")


@contextmanager
def _scope(
    strategy: Path,
    authority: Path,
    config: LiveConfig,
    profile: LiveRpcProfile,
    *,
    exclusive: bool,
) -> Iterator[_Scope]:
    binding = _config(config)
    scope = _Scope()
    with ExitStack() as resources:
        for role, root in zip(ROLES, (strategy, authority), strict=True):
            directory = root.absolute()
            info = directory.lstat()
            if (
                directory.resolve() != directory
                or not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
            ):
                raise ValueError("original private service directory required")
            state, evidence, lock = paths(directory, role)
            store = StateStore(state, require_existing=True)
            budget = directory / "live.receipt-budget.db"
            for path in (state, evidence, lock, store.journal_path, budget, directory / "STOP"):
                identity = _identity(path)
                if identity in scope.identities.values():
                    raise ValueError("distinct original service artifacts required")
                scope.identities[path] = identity
            scope.stop_fingerprints[directory / "STOP"] = _stop_fingerprint(directory / "STOP")
            for path in (evidence, store.journal_path, budget):
                for suffix in ("-wal", "-shm", "-journal"):
                    if os.path.lexists(str(path) + suffix):
                        raise ValueError("SQLite recovery artifacts require separate inspection")
            resources.enter_context(acquire_existing_lock(lock, exclusive=exclusive))
            scope.stores[role] = store
            scope.journals[role] = LiveJournal(store.journal_path, binding, require_existing=True)
            retained = resources.enter_context(closing(ReadOnlyEvidenceStore(evidence)))
            retained.validate_existing_schema()
            scope.evidence[role] = retained
            policies = profile.policies(SOURCES)
            quota = resources.enter_context(RpcBudget(budget, policies, read_only=True))
            counts = quota.counts()
            if any(not 0 < counts[p.source] < p.max_requests for p in policies):
                raise ValueError("original nonempty unexhausted RPC budget required")
            scope.budget_counts[role] = counts
        scope.verify_files()
        yield scope


def _snapshot(
    scope: _Scope, role: Role, db: sqlite3.Connection, schema: str
) -> tuple[str, LoopState, list[tuple[str, bytes]]]:
    tip, payload = scope.journals[role]._read(db, schema=schema)
    if payload is None:
        raise ValueError("nonempty original execution journal required")
    rows = db.execute(
        "SELECT config, payload FROM main.events ORDER BY seq"
        if schema == "main"
        else "SELECT config, payload FROM peer.events ORDER BY seq"
    ).fetchall()
    path = scope.stores[role].path
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        if (os.fstat(handle.fileno()).st_dev, os.fstat(handle.fileno()).st_ino) != scope.identities[
            path
        ]:
            raise ValueError("original projection replaced")
        projection = handle.read(MAX_SNAPSHOT + 1)
    if len(projection) > MAX_SNAPSHOT or canonical_json(strict_json_object(projection)) not in {
        raw for _, raw in rows
    }:
        raise ValueError("state projection is not in original journal")
    return tip, scope.stores[role]._decode(payload, "live"), rows


def _service(
    scope: _Scope,
    role: Role,
    db: sqlite3.Connection,
    schema: str,
    config: LiveConfig,
    refs: dict[str, tuple[EvidenceRef, ...]],
    now: int,
) -> tuple[dict[str, Any], LoopState]:
    tip, state, rows = _snapshot(scope, role, db, schema)
    validate_state(state)
    if (
        state.halted not in ALLOWED_HALTS
        or state.pending is not None
        or state.signing_attempt is not None
        or state.acknowledge_flow
        or state.legacy_gas_cost_incomplete
        or state.consecutive_reverts >= MAX_CONSECUTIVE_REVERTS
        or not state.journal_required
    ):
        raise ValueError("halt or unresolved execution requires a different recovery procedure")
    risk = state.exact_risk
    if (
        risk is None
        or risk.basis is None
        or risk.last_nav is None
        or state.last_units is None
        or risk.migrated_legacy
        or risk.day != day_of(risk.last_at)
        or not 0 < risk.checkpoint_at <= risk.last_at
    ):
        raise ValueError("complete original integer risk baseline required")
    limits = (config.max_daily_loss_usd * 1_000_000, config.max_cumulative_loss_usd * 1_000_000)
    if any(value >= limit for value, limit in zip(risk.losses(risk.last_nav), limits, strict=True)):
        raise ValueError("existing loss latch cannot be rearmed")
    evidence = scope.evidence[role]
    loop = object.__new__(LiveLoop)
    loop.c, loop.mode, loop.state = config, "live", deepcopy(state)
    loop.receipt_costs = RetainedReceiptCostReader(
        evidence,
        chain_id=config.chain_id,
        account=config.account,
        native_decimals=config.decimals0,
        sources=SOURCES,
        sessions=_no_network,
    )
    loop._check_nav_checkpoint()
    prior_nonce = None
    if state.nav_evidence:
        prior_nonce = replay_nav_quorum(
            evidence, state.nav_evidence, config, sources=SOURCES
        ).wallet.nonce
    if set(refs) != {"nav", "settlements"}:
        raise ValueError("exact NAV and settlement reference pairs required")
    value = replay_nav_quorum(evidence, refs["nav"], config, sources=SOURCES)
    position = replay_nav(evidence, refs["nav"][0], config).position
    head = value.wallet.header.ref
    deadline = nav_fresh_until(evidence, refs["nav"], config, sources=SOURCES)
    if (
        head.timestamp is None
        or not risk.last_at <= head.timestamp <= now <= deadline
        or (prior_nonce is not None and value.wallet.nonce != prior_nonce)
        or _position_identity(position) != _position_identity(state.position)
        or state.last_units
        != [value.wallet.native, value.wallet.token, position.liquidity if position else 0]
    ):
        raise ValueError("fresh NAV does not reconcile original inventory")
    historical = replay_history(
        rows, state, config, evidence, SOURCES, current_nonce=value.wallet.nonce
    )
    require_current_settlements(
        evidence,
        refs["settlements"],
        sources=SOURCES,
        head=head,
        anchors=tuple(state.settled_blocks),
        historical=historical,
    )
    updated = deepcopy(state)
    updated.nav_evidence = refs["nav"]
    updated.halted = None
    updated.last_observation = updated.last_light_check = None
    preview = updated.integer_risk()
    losses = preview.observe(
        day=day_of(head.timestamp), at=head.timestamp, nav=value.nav_micros, checkpoint=True
    )
    if any(loss >= limit for loss, limit in zip(losses, limits, strict=True)):
        raise ValueError("fresh loss budget exhausted")
    assert preview.basis is not None
    updated.initial_nav_usd = preview.basis / 1_000_000
    updated.external_flow_usd = preview.external / 1_000_000
    updated.daily_loss_usd = preview.daily_locked / 1_000_000
    updated.checkpoint_nav_usd = value.nav_micros / 1_000_000
    updated.checkpoint_at = head.timestamp
    updated.last_nav_usd = value.nav_micros / 1_000_000
    updated.day = preview.day
    if risk.day != preview.day:
        updated.actions_today = 0
    report = {
        "source_tip": tip,
        "source_state_hash": sha256_hex(canonical_json(asdict(state))),
        "source_halt": state.halted,
        "risk_before": asdict(risk),
        "risk_after": asdict(preview),
        "position": _position_identity(position),
        "last_units": state.last_units,
        "head": asdict(head),
        "nav_usd_micros": value.nav_micros,
        "valid_until": deadline,
        "settled_receipts_hash": sha256_hex(canonical_json(historical)),
        "settlement_count": len(historical),
        "native_gas_units": state.gas_spent_wei,
        "references": {name: [asdict(ref) for ref in values] for name, values in refs.items()},
        "rpc_counts": scope.budget_counts[role],
    }
    validate_state(updated)
    return report, updated


def _evaluate(
    scope: _Scope,
    dbs: dict[Role, tuple[sqlite3.Connection, str]],
    config: LiveConfig,
    references: References,
) -> tuple[dict[str, Any], dict[Role, LoopState]]:
    if set(references) != set(ROLES):
        raise ValueError("both service references required")
    now = _clock()
    services, states = {}, {}
    for role in ROLES:
        services[role], states[role] = _service(
            scope, role, *dbs[role], config, references[role], now
        )
    for name in (
        "head",
        "nav_usd_micros",
        "position",
        "last_units",
        "settled_receipts_hash",
        "native_gas_units",
    ):
        if services["strategy"][name] != services["authority"][name]:
            raise ValueError("services disagree on current account or settled history")
    report = {
        "schema": "oasis.live-paired-rearm.v1",
        "config_hash": _config(config),
        "account": config.account,
        "chain_id": config.chain_id,
        "services": services,
        "rearm_prerequisites_passed": True,
        "execution_eligible": False,
        "stop_preserved": True,
        "limitations": [
            "no_start_signature_broadcast_or_new_rpc_authorization",
            "retained_wire_provenance_provider_honesty_and_host_clock_are_trusted",
            "bounded_current_canonical_check_not_permanent_finality",
            "runtime_must_recheck_settlements_and_fresh_NAV",
        ],
    }
    report["review_hash"] = sha256_hex(canonical_json(report))
    scope.verify_files()
    _fresh(report)
    return report, states


def _fresh(report: dict[str, Any]) -> None:
    now = _clock()
    for service in report["services"].values():
        if not service["head"]["timestamp"] <= now <= service["valid_until"]:
            raise ValueError("rearm evidence expired")


def review_pair(
    strategy: Path,
    authority: Path,
    config: LiveConfig,
    profile: LiveRpcProfile,
    references: References,
) -> dict[str, Any]:
    with (
        _scope(strategy, authority, config, profile, exclusive=False) as scope,
        ExitStack() as stack,
    ):
        dbs = {}
        for role in ROLES:
            db = stack.enter_context(closing(scope.journals[role]._connect(read_only=True)))
            if db.execute("PRAGMA journal_mode").fetchone() != ("delete",):
                raise ValueError("DELETE journal mode required")
            db.execute("BEGIN")
            dbs[role] = (db, "main")
        report, _ = _evaluate(scope, dbs, config, references)
        return report


def _append(
    db: sqlite3.Connection, schema: str, binding: str, parent: str, state: LoopState
) -> str:
    if schema not in SCHEMAS:
        raise ValueError("unsupported journal schema")
    payload = canonical_json(asdict(state))
    if len(payload) > MAX_SNAPSHOT:
        raise ValueError("rearm snapshot exceeds byte budget")
    seq = (
        db.execute(
            "SELECT COUNT(*) FROM main.events"
            if schema == "main"
            else "SELECT COUNT(*) FROM peer.events"
        ).fetchone()[0]
        + 1
    )
    digest = sha256_hex(
        canonical_json(
            {"seq": seq, "config": binding, "parent": parent, "state_hash": sha256_hex(payload)}
        )
    )
    db.execute(
        "INSERT INTO main.events VALUES (?, ?, ?, ?, ?)"
        if schema == "main"
        else "INSERT INTO peer.events VALUES (?, ?, ?, ?, ?)",
        (seq, binding, parent, digest, payload),
    )
    return digest


def apply_pair(
    strategy: Path,
    authority: Path,
    config: LiveConfig,
    profile: LiveRpcProfile,
    references: References,
    *,
    approved_review_hash: str,
) -> dict[str, Any]:
    """Append both rearm snapshots in one SQLite rollback-journal transaction.

    The original databases must use DELETE; synchronous=FULL is verified on both.
    ATTACH uses mode=rw and never creates a missing peer. SQLite's super-journal
    supplies paired commit atomicity, subject to correct host locking/fsync/VFS.
    An interrupted commit must be inspected with both original files available;
    recovery sidecars are never removed or repaired here. STOP is never removed.
    """
    with _scope(strategy, authority, config, profile, exclusive=True) as scope:
        main = scope.stores["strategy"].journal_path.resolve().as_uri() + "?mode=rw"
        peer = scope.stores["authority"].journal_path.resolve().as_uri() + "?mode=rw"
        with closing(sqlite3.connect(main, uri=True, timeout=5, isolation_level=None)) as db:
            db.execute("ATTACH DATABASE ? AS peer", (peer,))
            for schema in SCHEMAS:
                if db.execute(f"PRAGMA {schema}.journal_mode").fetchone() != ("delete",):
                    raise ValueError("DELETE rollback mode required; no automatic conversion")
                db.execute(f"PRAGMA {schema}.synchronous=FULL")
                if db.execute(f"PRAGMA {schema}.synchronous").fetchone() != (2,):
                    raise ValueError("FULL durability required")
            db.execute("BEGIN IMMEDIATE")
            try:
                report, states = _evaluate(
                    scope,
                    {role: (db, schema) for role, schema in zip(ROLES, SCHEMAS, strict=True)},
                    config,
                    references,
                )
                if report["review_hash"] != approved_review_hash:
                    raise JournalConflict("review or original journal tips changed")
                tips = {}
                for role, schema in zip(ROLES, SCHEMAS, strict=True):
                    states[role].record("operator_rearmed", review=report, role=role)
                    tips[role] = _append(
                        db,
                        schema,
                        report["config_hash"],
                        report["services"][role]["source_tip"],
                        states[role],
                    )
                scope.verify_files()
                _fresh(report)
                db.execute("COMMIT")
            except BaseException:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise
        # The journal is authoritative. Projection failure cannot undo or repeat
        # the paired commit; old projections remain valid historical snapshots.
        projection_complete = True
        for role in ROLES:
            try:
                store = scope.stores[role]
                current = store.load("live", read_only=True)
                if store.journal is None or store.journal.revision != tips[role] or current.halted:
                    raise JournalConflict("committed rearm changed before projection")
                store.save(current)
            except (OSError, ValueError, sqlite3.Error):
                projection_complete = False
        return {
            **report,
            "rearm_applied": True,
            "journal_tips": tips,
            "projection_complete": projection_complete,
        }
