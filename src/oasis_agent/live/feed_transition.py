"""Evidence review and explicit halted 90000s -> 1800s configuration transition.

Review is read-only. Application requires a stopped service and valid new raw
proofs; it always preserves HALT. Neither path acquires RPC data or can trade.
"""

import json
import os
import sqlite3
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import ExitStack, closing, contextmanager
from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Literal, NoReturn

from ..evidence.store import EvidenceStore
from ..models.common import EvidenceRef, canonical_json, sha256_hex
from .config import LiveConfig
from .journal import require_paired_rollback_transaction
from .receipts import RetainedReceiptCostReader
from .runner import LiveLoop
from .startup_state import acquire_existing_lock, require_file
from .state import LoopState, Position, StateStore, day_of, validate_state
from .valuation import nav_fresh_until, replay_nav, replay_nav_quorum
from .wallet import require_wallet_continuity


def historical_config(target: LiveConfig, binding: str) -> LiveConfig:
    """Recognize exactly the legacy USDC age difference, for historical replay only."""
    target.validate()
    if target.oracle_max_age_seconds != 1800 or target.usdc_oracle_max_age_seconds != 1800:
        raise ValueError("transition requires the owner's two 1800-second ceilings")
    previous = replace(target, usdc_oracle_max_age_seconds=90_000)
    if sha256_hex(canonical_json(asdict(previous))) != binding:
        raise ValueError("unrecognized prior configuration; only USDC age may differ")
    return previous


def _no_acquisition(*_: object) -> NoReturn:
    raise ValueError("offline transition review cannot acquire data")


def _identity(p: Position | None) -> tuple[int, int, int, int] | None:
    # opened_at is service-local, not part of the on-chain position identity.
    return (p.token_id, p.lower, p.upper, p.liquidity) if p else None


def review_service(
    target: LiveConfig,
    state: LoopState,
    binding: str,
    evidence: EvidenceStore,
    *,
    source_payload: bytes,
    sources: tuple[str, str],
    fresh: tuple[EvidenceRef, ...] = (),
    now: int,
) -> dict[str, Any]:
    """The caller must obtain raw bytes and binding from one verified journal snapshot.

    Old risk is replayed under its recorded policy. Only new proofs are evaluated
    under the current policy. Missing fresh proofs never produce a risk preview.
    """
    if type(now) is not int or now < 0:
        raise ValueError("explicit integer review clock required")
    previous = historical_config(target, binding)
    validate_state(state)
    if state.mode != "live":
        raise ValueError("live state required")
    if (
        not isinstance(source_payload, bytes)
        or len(source_payload) > 2_000_000
        or canonical_json(asdict(StateStore._decode(source_payload, "live")))
        != canonical_json(asdict(state))
        or canonical_json(json.loads(source_payload)) != source_payload
    ):
        raise ValueError("canonical retained bytes must describe the reviewed state")
    blockers: list[str] = []
    report: dict[str, Any] = {
        "schema": "oasis.feed-transition-service-review.v1",
        "execution_eligible": False,
        "state_hash": sha256_hex(source_payload),
        "source_config_hash": binding,
        "target_config_hash": sha256_hex(canonical_json(asdict(target))),
        "historical_checkpoint_verified": False,
        "fresh_nav_verified": False,
        "fresh_nav": None,
        "risk_preview": None,
        "halt_preserved": bool(state.halted),
        "blockers": blockers,
    }
    for field in ("pending", "signing_attempt"):
        if getattr(state, field) is not None:
            report["blockers"].append(field + "_unresolved")
    if state.legacy_gas_cost_incomplete:
        report["blockers"].append("historical_native_cost_incomplete")
    if state.acknowledge_flow:
        report["blockers"].append("external_flow_acknowledgement_pending")
    risk = state.exact_risk
    if risk is None or risk.basis is None or risk.last_nav is None or state.last_units is None:
        report["blockers"].append("complete_integer_risk_baseline_required")
    if report["blockers"]:
        return report
    assert risk is not None
    # Reuse the production historical-proof verifier, with no acquisition capability.
    loop = object.__new__(LiveLoop)
    loop.mode, loop.c, loop.state = "live", previous, deepcopy(state)
    loop.receipt_costs = RetainedReceiptCostReader(
        evidence,
        chain_id=previous.chain_id,
        account=previous.account,
        native_decimals=previous.decimals0,
        sources=sources,
        sessions=_no_acquisition,
    )
    try:
        anchor = loop._check_nav_checkpoint()
        if anchor is None:
            raise ValueError("verified historical wallet anchor required")
        if state.nav_evidence:
            prior = replay_nav_quorum(evidence, state.nav_evidence, previous, sources=sources)
            prior_position = replay_nav(evidence, state.nav_evidence[0], previous).position
            if (
                _identity(prior_position) != _identity(state.position)
                or [
                    prior.wallet.native,
                    prior.wallet.token,
                    prior_position.liquidity if prior_position else 0,
                ]
                != state.last_units
            ):
                raise ValueError("historical holdings differ from checkpoint")
    except Exception:  # noqa: BLE001 - fixed redacted refusal, retain source evidence
        report["blockers"].append("historical_checkpoint_unresolved")
        return report
    report["historical_checkpoint_verified"] = True
    if not fresh:
        report["blockers"].append("fresh_strict_nav_required")
        return report
    try:
        value = replay_nav_quorum(evidence, fresh, target, sources=sources)
        deadline = nav_fresh_until(evidence, fresh, target, sources=sources)
        block = value.wallet.header.ref
        if block.timestamp is None or not risk.last_at < block.timestamp <= now <= deadline:
            raise ValueError("new current NAV required")
        require_wallet_continuity(anchor, value.wallet)
        position = replay_nav(evidence, fresh[0], target).position

        if _identity(position) != _identity(state.position):
            raise ValueError("position changed")
        if [
            value.wallet.native,
            value.wallet.token,
            position.liquidity if position else 0,
        ] != state.last_units:
            raise ValueError("unexplained account movement")
        # Future application must recheck all settlement anchors against both providers.
        if any(
            a["number"] > block.number
            or (a["number"] == block.number and a["hash"] != block.block_hash)
            for a in state.settled_blocks
        ):
            raise ValueError("NAV predates or contradicts settlement")
        preview = deepcopy(risk)
        daily, cumulative = preview.observe(
            day=day_of(block.timestamp), at=block.timestamp, nav=value.nav_micros, checkpoint=True
        )
        report["fresh_nav_verified"] = True
        report["fresh_nav"] = {
            "block": asdict(block),
            "nav_usd_micros": value.nav_micros,
            "valid_until": deadline,
            "evidence": [asdict(ref) for ref in fresh],
        }
        report["risk_preview"] = asdict(preview)
        if (
            daily >= target.max_daily_loss_usd * 1_000_000
            or cumulative >= target.max_cumulative_loss_usd * 1_000_000
        ):
            report["blockers"].append("loss_budget_exhausted")
    except Exception:  # noqa: BLE001 - no raw provider or evidence diagnostics
        report["blockers"].append("fresh_nav_or_continuity_unresolved")
    return report


def apply_service(
    store: StateStore,
    target: LiveConfig,
    evidence: EvidenceStore,
    *,
    sources: tuple[str, str],
    fresh: tuple[EvidenceRef, ...],
    expected_tip: str,
    stop_file: Path,
    role: Literal["strategy", "authority"],
) -> dict[str, Any]:
    """Explicit stopped-service migration; always remains halted after application.

    This is not service startup or a release approval. The operator must stop both
    services and retain their STOP files. A partial two-service migration stays
    closed and must be inspected; no automatic retry or reversal is performed.
    """
    with _service_lock(store, stop_file, role) as verify_stopped:
        return _apply_service(
            store,
            target,
            evidence,
            sources=sources,
            fresh=fresh,
            expected_tip=expected_tip,
            stop_file=stop_file,
            verify_stopped=verify_stopped,
        )


@contextmanager
def _service_lock(
    store: StateStore, stop_file: Path, role: Literal["strategy", "authority"]
) -> Iterator[Callable[[], None]]:
    if (
        role not in {"strategy", "authority"}
        or stop_file.absolute() != (store.path.parent / "STOP").absolute()
    ):
        raise ValueError("service role and its own STOP path required")
    lock_path = store.path.parent / ("live.lock" if role == "strategy" else "authority.lock")

    def stop_info() -> os.stat_result:
        try:
            return require_file(stop_file)
        except OSError as exc:
            raise ValueError("an existing owned regular STOP file is required") from exc

    with acquire_existing_lock(lock_path, exclusive=True) as lock:
        expected_stop = stop_info()
        expected_lock = os.fstat(lock.fileno())

        def verify_stopped() -> None:
            current_lock = require_file(lock_path)
            current_stop = stop_info()
            if (current_lock.st_dev, current_lock.st_ino) != (
                expected_lock.st_dev,
                expected_lock.st_ino,
            ) or any(
                getattr(current_stop, field) != getattr(expected_stop, field)
                for field in (
                    "st_dev",
                    "st_ino",
                    "st_mode",
                    "st_uid",
                    "st_size",
                    "st_mtime_ns",
                    "st_ctime_ns",
                )
            ):
                raise ValueError("service lock or STOP changed during evidence review")

        verify_stopped()
        yield verify_stopped


def apply_pair(
    strategy: StateStore,
    authority: StateStore,
    target: LiveConfig,
    evidence: Mapping[str, EvidenceStore],
    fresh: Mapping[str, tuple[EvidenceRef, ...]],
    *,
    sources: tuple[str, str],
    expected_tips: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Commit both halted epochs in one ATTACH rollback-journal transaction.

    Both originals must already use DELETE, on the same filesystem, without
    recovery sidecars. FULL synchronization is checked on both. This relies on
    correct SQLite/VFS locking and fsync, as does paired rearm. A failed or
    interrupted attempt requires inspection; there is no automatic retry.
    """
    if strategy.journal_path.samefile(authority.journal_path):
        raise ValueError("distinct service journals required")
    stores = {"strategy": strategy, "authority": authority}
    identities: dict[Path, tuple[int, int]] = {}
    for store in stores.values():
        for path in (store.path, store.journal_path):
            info = require_file(path)
            identity = info.st_dev, info.st_ino
            if identity in identities.values():
                raise ValueError("distinct original state and journal files required")
            identities[path] = identity
        if any(
            os.path.lexists(str(store.journal_path) + suffix)
            for suffix in ("-wal", "-shm", "-journal")
        ):
            raise ValueError("journal recovery sidecars require separate inspection")
    if len({identity[0] for identity in identities.values()}) != 1:
        raise ValueError("paired migration requires one filesystem")
    with ExitStack() as locks:
        stopped = {}
        for name, store in stores.items():
            stopped[name] = locks.enter_context(
                _service_lock(store, store.path.parent / "STOP", name)  # type: ignore[arg-type]
            )

        def verify_stopped() -> None:
            for check in stopped.values():
                check()
            for path, identity in identities.items():
                info = require_file(path)
                if (info.st_dev, info.st_ino) != identity:
                    raise ValueError("original migration artifact changed")

        reviews, tips, states = {}, {}, {}
        for name, store in stores.items():
            state = store.load("live", read_only=True)
            journal = store.journal
            if journal is None or journal.config_hash is None:
                raise ValueError("retained service journal required")
            if expected_tips is not None and journal.revision != expected_tips[name]:
                raise ValueError("service journal differs from retained attempt scope")
            reviews[name] = review_service(
                target,
                state,
                journal.config_hash,
                evidence[name],
                source_payload=store.retained_payload(state),
                sources=sources,
                fresh=fresh[name],
                now=int(time.time()),
            )
            tips[name], states[name] = journal.revision, state
        if any(r["blockers"] for r in reviews.values()):
            raise ValueError("both services need a passing fresh evidence review")
        a, b = reviews["strategy"]["fresh_nav"], reviews["authority"]["fresh_nav"]
        if (a["block"], a["nav_usd_micros"]) != (b["block"], b["nav_usd_micros"]) or _identity(
            states["strategy"].position
        ) != _identity(states["authority"].position):
            raise ValueError("services disagree on position or current NAV")
        records = {}
        for name, store in stores.items():
            tip = tips[name]
            assert tip is not None
            journal = store.journal
            assert journal is not None
            raw = store.retained_payload(states[name])
            if journal.revision != tip:
                raise ValueError("journal changed during paired proof review")
            records[name] = {
                "schema": "oasis.strict-feed-epoch.v1",
                "source_tip": tip,
                "source_state_hash": sha256_hex(raw),
                "target_config": asdict(target),
                "review": reviews[name],
            }
        verify_stopped()
        main = strategy.journal_path.resolve().as_uri() + "?mode=rw"
        peer = authority.journal_path.resolve().as_uri() + "?mode=rw"
        staged = {}
        with closing(sqlite3.connect(main, uri=True, timeout=5, isolation_level=None)) as db:
            db.execute("ATTACH DATABASE ? AS peer", (peer,))
            for schema in ("main", "peer"):
                if db.execute(f"PRAGMA {schema}.journal_mode").fetchone() != ("delete",):
                    raise ValueError("DELETE rollback mode required; no automatic conversion")
                db.execute(f"PRAGMA {schema}.synchronous=FULL")
                if db.execute(f"PRAGMA {schema}.synchronous").fetchone() != (2,):
                    raise ValueError("FULL durability required")
            db.execute("BEGIN IMMEDIATE")
            try:
                require_paired_rollback_transaction(db)
                for name, schema in zip(stores, ("main", "peer"), strict=True):
                    verify_stopped()
                    journal = stores[name].journal
                    assert journal is not None
                    staged[name] = journal._transition_in_transaction(
                        db, records[name], schema=schema
                    )
                require_paired_rollback_transaction(db)
                verify_stopped()
                now = int(time.time())
                if any(
                    not review["fresh_nav"]["block"]["timestamp"]
                    <= now
                    <= review["fresh_nav"]["valid_until"]
                    for review in reviews.values()
                ):
                    raise ValueError("paired transition proof expired before commit")
                db.execute("COMMIT")
            except BaseException:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise
        # Both authoritative chains are committed before either projection can
        # change. A stale projection is a retained historical snapshot, not a
        # reason to repeat a migration or replace a journal.
        result = {}
        for name, store in stores.items():
            binding, tip, payload = staged[name]
            assert store.journal is not None
            store.journal.config_hash, store.journal.revision = binding, tip
            result[name] = {
                "schema": "oasis.strict-feed-migration.v1",
                "execution_eligible": False,
                "migration_applied": True,
                "halted": True,
                "source_tip": tips[name],
                "journal_tip": tip,
                "transition_hash": sha256_hex(canonical_json(records[name])),
                "state_hash": sha256_hex(payload),
            }
        projection_complete = True
        for name, store in stores.items():
            try:
                migrated = store.load("live", read_only=True)
                if (
                    store.journal is None
                    or store.journal.revision != staged[name][1]
                    or not migrated.halted
                ):
                    raise ValueError("committed halted migration changed before projection")
                store.save(migrated)
            except (OSError, ValueError, sqlite3.Error):
                projection_complete = False
        return {
            "execution_eligible": False,
            "migration_applied": True,
            "atomic_pair": True,
            "projection_complete": projection_complete,
            "services": result,
        }


def _apply_service(
    store: StateStore,
    target: LiveConfig,
    evidence: EvidenceStore,
    *,
    sources: tuple[str, str],
    fresh: tuple[EvidenceRef, ...],
    expected_tip: str,
    stop_file: Path,
    verify_stopped: Callable[[], None],
) -> dict[str, Any]:
    verify_stopped()
    if not stop_file.is_file() or stop_file.is_symlink():
        raise ValueError("an existing regular STOP file is required")
    state = store.load("live", read_only=True)
    journal = store.journal
    if journal is None or journal.revision != expected_tip or journal.config_hash is None:
        raise ValueError("existing journal and exact reviewed tip required")
    review = review_service(
        target,
        state,
        journal.config_hash,
        evidence,
        source_payload=store.retained_payload(state),
        sources=sources,
        fresh=fresh,
        now=int(time.time()),
    )
    if review["blockers"]:
        raise ValueError("transition evidence remains unresolved")
    raw = store.retained_payload(state)
    if journal.revision != expected_tip:
        raise ValueError("journal changed during evidence review")
    record = {
        "schema": "oasis.strict-feed-epoch.v1",
        "source_tip": expected_tip,
        "source_state_hash": sha256_hex(raw),
        "target_config": asdict(target),
        "review": review,
    }
    verify_stopped()
    payload = journal.transition(record)
    migrated = StateStore._decode(payload, "live")
    store.save(migrated)  # Journal already committed; a projection failure is recoverable.
    return {
        "schema": "oasis.strict-feed-migration.v1",
        "execution_eligible": False,
        "migration_applied": True,
        "halted": True,
        "source_tip": expected_tip,
        "journal_tip": journal.revision,
        "transition_hash": sha256_hex(canonical_json(record)),
        "state_hash": sha256_hex(canonical_json(asdict(migrated))),
    }
