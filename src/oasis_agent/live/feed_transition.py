"""Evidence review and explicit halted 90000s -> 1800s configuration transition.

Review is read-only. Application requires a stopped service and valid new raw
proofs; it always preserves HALT. Neither path acquires RPC data or can trade.
"""

import fcntl
import os
import stat
import time
from collections.abc import Iterator, Mapping
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Literal, NoReturn

from ..evidence.store import EvidenceStore
from ..models.common import EvidenceRef, canonical_json, sha256_hex
from .config import LiveConfig
from .receipts import RetainedReceiptCostReader
from .runner import LiveLoop
from .state import LoopState, Position, StateStore, day_of, validate_state
from .valuation import nav_fresh_until, replay_nav, replay_nav_quorum


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
    sources: tuple[str, str],
    fresh: tuple[EvidenceRef, ...] = (),
    now: int,
) -> dict[str, Any]:
    """The caller must obtain state and binding from one verified journal snapshot.

    Old risk is replayed under its recorded policy. Only new proofs are evaluated
    under the current policy. Missing fresh proofs never produce a risk preview.
    """
    if type(now) is not int or now < 0:
        raise ValueError("explicit integer review clock required")
    previous = historical_config(target, binding)
    validate_state(state)
    if state.mode != "live":
        raise ValueError("live state required")
    blockers: list[str] = []
    report: dict[str, Any] = {
        "schema": "oasis.feed-transition-service-review.v1",
        "execution_eligible": False,
        "state_hash": sha256_hex(canonical_json(asdict(state))),
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
        loop._check_nav_checkpoint()
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
    with _service_lock(store, stop_file, role):
        return _apply_service(
            store,
            target,
            evidence,
            sources=sources,
            fresh=fresh,
            expected_tip=expected_tip,
            stop_file=stop_file,
        )


@contextmanager
def _service_lock(
    store: StateStore, stop_file: Path, role: Literal["strategy", "authority"]
) -> Iterator[None]:
    if (
        role not in {"strategy", "authority"}
        or stop_file.absolute() != (store.path.parent / "STOP").absolute()
    ):
        raise ValueError("service role and its own STOP path required")
    lock_path = store.path.parent / ("live.lock" if role == "strategy" else "authority.lock")
    fd = os.open(lock_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as lock:
        info = os.fstat(lock.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError("existing owned service lock required")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if not stop_file.is_file() or stop_file.is_symlink():
            raise ValueError("an existing regular STOP file is required")
        yield


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
    """Lock both stopped services, review a common NAV, then append halted epochs.

    The two SQLite commits are not atomic together. Any interruption requires
    inspection; committed state remains HALTed and no automatic retry is made.
    """
    if strategy.journal_path.samefile(authority.journal_path):
        raise ValueError("distinct service journals required")
    stores = {"strategy": strategy, "authority": authority}
    with ExitStack() as locks:
        for name, store in stores.items():
            locks.enter_context(_service_lock(store, store.path.parent / "STOP", name))  # type: ignore[arg-type]
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
        result = {}
        for name, store in stores.items():
            tip = tips[name]
            assert tip is not None
            result[name] = _apply_service(
                store,
                target,
                evidence[name],
                sources=sources,
                fresh=fresh[name],
                expected_tip=tip,
                stop_file=store.path.parent / "STOP",
            )
        return {"execution_eligible": False, "migration_applied": True, "services": result}


def _apply_service(
    store: StateStore,
    target: LiveConfig,
    evidence: EvidenceStore,
    *,
    sources: tuple[str, str],
    fresh: tuple[EvidenceRef, ...],
    expected_tip: str,
    stop_file: Path,
) -> dict[str, Any]:
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
        sources=sources,
        fresh=fresh,
        now=int(time.time()),
    )
    if review["blockers"]:
        raise ValueError("transition evidence remains unresolved")
    raw = journal.load(read_only=True)
    if raw is None or journal.revision != expected_tip:
        raise ValueError("journal changed during evidence review")
    record = {
        "schema": "oasis.strict-feed-epoch.v1",
        "source_tip": expected_tip,
        "source_state_hash": sha256_hex(raw),
        "target_config": asdict(target),
        "review": review,
    }
    if not stop_file.is_file() or stop_file.is_symlink():
        raise ValueError("STOP file changed during evidence review")
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
